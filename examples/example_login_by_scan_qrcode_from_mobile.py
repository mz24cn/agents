"""
示例：通过手机扫码自动登录视频号创作者平台

【目标】
在电脑浏览器上自动化登录微信视频号创作者平台（https://channels.weixin.qq.com/login.html）。
该平台需要使用微信扫码登录，本脚本通过两个手机配合完成整个流程：
- 手机A（显示二维码）：接收电脑截取的登录二维码并全屏显示
- 手机B（扫码手机）：打开微信扫一扫，扫描手机A上的二维码完成登录

【前提条件】
1. 电脑已连接两台Android手机（通过USB并开启ADB调试）
2. 电脑浏览器已安装并运行 chrome-devtools-mcp 服务（提供 /v1/tools/call 接口）
3. 手机B（扫码手机）已安装微信并登录
4. 两台手机的屏幕已解锁
5. Agent Service服务已启动

【流程概览】
1. 电脑浏览器打开视频号登录页面
2. 从页面A11Y树中定位二维码iframe并截图
3. 将二维码图片发送到手机A全屏显示（最大亮度）
4. 在手机B上打开微信 → 点击"+" → 点击"扫一扫"
5. 等待扫码完成，在浏览器中确认登录成功（检测到"登录"字样）

【API说明】
本脚本通过 /v1/tools/call 接口调用 chrome-devtools-mcp 和 android_use_mcp 工具，全程无大模型参与。
- tool_id 格式：mcp-{service}-{tool_name}
- 浏览器工具：mcp-chrome-devtools-take_snapshot, mcp-chrome-devtools-take_screenshot, mcp-chrome-devtools-navigate_page
- Android工具：mcp-android-show_image, mcp-android-run_adb_command, mcp-android-find_and_click
"""

import re
import json
import urllib.request
import urllib.error
import time
from typing import Optional, Dict, Any, Tuple

# ============================================================
# 配置区域
# ============================================================

# Agent Service API 地址
MCP_API_URL = "http://localhost:7988/v1/tools/call"

# 视频号登录页面
LOGIN_URL = "https://channels.weixin.qq.com/login.html"

# 手机配置（通过 adb devices 获取）
# 型号标识 -> (device_id, 用途说明, PIN码)
PHONES = {
    "MI5X": {
        "device_id": "0043d38b0504",
        "role": "display",  # 用于显示二维码
        "pin": None,  # 锁屏PIN码，None表示无PIN码或仅滑动解锁
    },
    "Redmi": {
        "device_id": "476cf991",
        "role": "scan",  # 用于微信扫码
        "pin": None,  # 锁屏PIN码，None表示无PIN码或仅滑动解锁
    },
}

# 显示二维码的手机型号（必须在 PHONES 中配置）
DISPLAY_PHONE_MODEL = "MI5X"

# 扫码的手机型号（必须在 PHONES 中配置）
SCAN_PHONE_MODEL = "Redmi"

# ============================================================
# 工具函数
# ============================================================


def call_tool(tool_id: str, arguments: dict, format: str = "json") -> Any:
    """
    调用 Agent Service 的 /v1/tools/call 接口。

    Args:
        tool_id: 工具ID，如 "mcp-chrome-devtools-take_snapshot", "mcp-android-show_image"
        arguments: 工具参数，与大模型调用时的 arguments 格式完全一致
        format: 返回格式，"json" 返回 JSON 对象，否则返回原始文本

    Returns:
        工具执行结果（dict 或 str，取决于 format 参数）
    """
    payload = {
        "tool_id": tool_id,
        "arguments": arguments,
        "format": format,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        # 即使状态码是 4xx/5xx，body 中也可能包含有用的错误信息
        error_body = e.read().decode("utf-8") if e.fp else ""
        try:
            return json.loads(error_body)
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(f"HTTP 请求失败: {e.code} {e.reason} {error_body}")

    response_text = resp.read().decode("utf-8")
    
    # 根据 format 参数决定如何解析响应
    if format == "text":
        # text 格式：尝试解析 JSON，失败则返回原始文本
        try:
            result = json.loads(response_text)
            # 检查是否执行成功
            if isinstance(result, dict) and not result.get("success", True):
                raise RuntimeError(f"工具调用失败: {result.get('message', '未知错误')}")
            return result
        except json.JSONDecodeError:
            return response_text
    else:
        # json 格式：必须解析 JSON
        result = json.loads(response_text)
        # 检查是否执行成功
        if not result.get("success", False):
            raise RuntimeError(f"工具调用失败: {result.get('message', '未知错误')}")
        return result


def save_and_replace_base64(text: str, output_dir: str = "/tmp") -> str:
    """
    后处理：将返回结果中的 base64 图片保存为文件，并替换为 filePath。
    与 runtime.py 中的逻辑一致。
    """
    # 确保输出目录存在
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 正则表达式：匹配 "key": "base64内容"
    pattern = r'"(screenshot|data|image|base64|base64_content|base64_data|image_base64)":\s*"([A-Za-z0-9+/]{100,}={0,2})"'

    def replace_logic(match):
        b64_content = match.group(2)
        file_name = f"snap_{int(time.time()*1000)}.png"
        file_path = os.path.abspath(os.path.join(output_dir, file_name))
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(b64_content))
        safe_path = file_path.replace("\\", "/")
        return f'"filePath": "{safe_path}"'

    return re.sub(pattern, replace_logic, text)


# ============================================================
# 浏览器操作
# ============================================================


def browser_navigate(url: str) -> dict:
    """导航到指定URL"""
    return call_tool(
        "mcp-chrome-devtools-navigate_page",
        {"type": "url", "url": url},
        format="text",
    )


def browser_take_snapshot() -> dict:
    """获取页面 A11Y 快照（文本树）"""
    return call_tool(
        "mcp-chrome-devtools-take_snapshot",
        {},
        format="text",
    )


def browser_take_screenshot(uid: str = None) -> dict:
    """截取页面或指定元素的截图"""
    args = {}
    if uid:
        args["uid"] = uid
    result = call_tool(
        "mcp-chrome-devtools-take_screenshot",
        args,
        format="text",
    )
    return result


def browser_find_and_click(keyword: str) -> dict:
    """在页面上查找并点击包含指定文字的元素"""
    return call_tool(
        "mcp-chrome-devtools-find_and_click",
        {"keyword": keyword},
        format="text",
    )


# ============================================================
# Android 操作
# ============================================================


def android_show_image(base64_content: str, device_id: str) -> dict:
    """
    在手机上显示图片（自动唤醒屏幕、保持亮屏、最大亮度、打开图片查看器）

    Args:
        base64_content: 图片文件base64
        device_id: 手机设备ID
    """
    return call_tool(
        "mcp-android-show_image",
        {"base64_content": base64_content, "device_id": device_id},
    )


def android_run_adb(command: str, device_id: str) -> dict:
    """执行 ADB 命令"""
    return call_tool(
        "mcp-android-run_adb_command",
        {"command": command, "device_id": device_id},
    )


def android_wakeup_and_unlock(device_id: str, pin: str = None) -> bool:
    """
    唤醒屏幕并解锁设备，适用于各种状态（息屏、亮屏锁屏、亮屏已解锁）

    Args:
        device_id: 手机设备ID
        pin: 锁屏PIN码（如果有），None表示没有PIN码或仅滑动解锁

    Returns:
        是否成功解锁
    """
    print("   检测屏幕状态...")

    # 1. 检查屏幕是否亮着
    power_result = android_run_adb("shell dumpsys power | grep 'Display Power'", device_id)
    power_text = str(power_result)
    is_screen_on = "state=ON" in power_text

    if not is_screen_on:
        print("   屏幕已熄灭，正在唤醒...")
        android_run_adb("shell input keyevent KEYCODE_WAKEUP", device_id)
        time.sleep(0.5)
    else:
        print("   屏幕已点亮")

    # 2. 检查是否需要解锁（检查锁屏状态）
    window_result = android_run_adb("shell dumpsys window policy | grep -E 'showing|dreaming'", device_id)
    window_text = str(window_result)

    # 检查是否需要解锁
    if "showing=true" in window_text or "isStatusBarKeyguard=true" in window_text:
        print("   屏幕已锁定，正在解锁...")

        # 先滑动解锁（覆盖大多数情况）
        android_run_adb("shell input swipe 540 1800 540 800", device_id)
        time.sleep(0.5)

        # 如果有PIN码，输入PIN码
        if pin:
            print(f"   输入PIN码...")
            # 输入PIN码数字
            for digit in pin:
                android_run_adb(f"shell input keyevent KEYCODE_{digit}", device_id)
                time.sleep(0.1)
            # 确认PIN码（按回车）
            android_run_adb("shell input keyevent KEYCODE_ENTER", device_id)
            time.sleep(0.5)

        # 再次检查是否解锁成功
        window_result2 = android_run_adb("shell dumpsys window policy | grep -E 'showing|dreaming'", device_id)
        window_text2 = str(window_result2)
        if "showing=true" in window_text2 or "isStatusBarKeyguard=true" in window_text2:
            print("   ❌ 解锁失败，可能需要PIN码")
            return False
        print("   ✅ 已解锁")
    else:
        print("   屏幕已解锁")

    # 3. 确保保持亮屏和最大亮度
    android_run_adb("shell svc power stayon true", device_id)
    android_run_adb("shell settings put system screen_brightness 255", device_id)
    print("   ✅ 屏幕已点亮，保持亮屏，最大亮度")

    return True


def android_find_and_click(keyword: str, device_id: str) -> dict:
    """在手机屏幕上查找并点击包含指定文字的元素"""
    return call_tool(
        "mcp-android-find_and_click",
        {"keyword_or_image_file": keyword, "device_id": device_id},
    )


# ============================================================
# 核心业务逻辑
# ============================================================


def find_qrcode_iframe_uid(snapshot_data) -> Optional[str]:
    """
    从页面快照中查找二维码 iframe 的 uid。

    策略：在 A11Y 树中查找 Iframe 类型的节点，
    其子节点通常包含"扫码"、"二维码"等关键词。

    Args:
        snapshot_data: take_snapshot 返回的数据

    Returns:
        iframe 的 uid，未找到返回 None
    """
    snapshot_text = snapshot_data.get("snapshot", "") if isinstance(snapshot_data, dict) else str(snapshot_data)

    # 解析快照文本，查找 Iframe 节点
    # 格式示例：uid=2_27 Iframe
    lines = snapshot_text.split("\n")
    for i, line in enumerate(lines):
        if "Iframe" in line:
            # 提取 uid
            parts = line.strip().split()
            for part in parts:
                if part.startswith("uid="):
                    uid = part.split("=")[1]
                    # 检查后续几行是否包含二维码相关内容
                    context = "\n".join(lines[i : min(i + 10, len(lines))])
                    if any(
                        kw in context
                        for kw in ["扫码", "二维码", "QR", "scan", "登录"]
                    ):
                        return uid
                    # 如果没有明确关键词，也返回第一个 Iframe（通常只有一个登录二维码）
                    return uid
    return None


def wait_for_element_in_snapshot(
    keyword: str, timeout: int = 30, interval: int = 2
) -> bool:
    """
    等待页面快照中出现包含指定关键词的元素。

    Args:
        keyword: 要查找的关键词
        timeout: 超时时间（秒）
        interval: 轮询间隔（秒）

    Returns:
        是否找到
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = browser_take_snapshot()
            snapshot_text = result.get("snapshot", "") if isinstance(result, dict) else str(result)
            if keyword in snapshot_text:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def get_device_id_by_model(model: str) -> str:
    """根据手机型号获取 device_id"""
    if model not in PHONES:
        raise ValueError(
            f"未找到型号为 {model} 的手机配置。可用型号: {list(PHONES.keys())}"
        )
    return PHONES[model]["device_id"]


def get_device_pin_by_model(model: str) -> str:
    """根据手机型号获取锁屏PIN码"""
    if model not in PHONES:
        raise ValueError(
            f"未找到型号为 {model} 的手机配置。可用型号: {list(PHONES.keys())}"
        )
    return PHONES[model].get("pin")


# ============================================================
# 主流程
# ============================================================


def login_video_account_by_scan(
    display_phone: str = DISPLAY_PHONE_MODEL,
    scan_phone: str = SCAN_PHONE_MODEL,
    login_url: str = LOGIN_URL,
) -> bool:
    """
    通过手机扫码自动登录视频号创作者平台。

    完整流程：
    1. 电脑浏览器打开登录页面
    2. 从页面中截取二维码图片
    3. 将二维码发送到手机A全屏显示
    4. 在手机B上打开微信扫一扫
    5. 扫描手机A上的二维码
    6. 等待登录成功

    Args:
        display_phone: 显示二维码的手机型号（默认 MI5X）
        scan_phone: 扫码的手机型号（默认 Redmi）
        login_url: 登录页面地址

    Returns:
        是否登录成功
    """
    print("=" * 60)
    print("视频号创作者平台 - 扫码登录自动化")
    print("=" * 60)

    # 获取设备ID和PIN码
    display_device_id = get_device_id_by_model(display_phone)
    scan_device_id = get_device_id_by_model(scan_phone)
    display_pin = get_device_pin_by_model(display_phone)
    scan_pin = get_device_pin_by_model(scan_phone)

    print(f"\n📱 显示二维码手机: {display_phone} ({display_device_id})")
    print(f"📱 扫码手机: {scan_phone} ({scan_device_id})")

    # --------------------------------------------------------
    # 步骤1：打开登录页面
    # --------------------------------------------------------
    print("\n[步骤1/7] 打开登录页面...")
    browser_navigate(login_url)
    time.sleep(3)  # 等待页面加载

    # --------------------------------------------------------
    # 步骤2：获取页面快照，定位二维码 iframe
    # --------------------------------------------------------
    print("[步骤2/7] 定位二维码 iframe...")
    snapshot_result = browser_take_snapshot()
    iframe_uid = find_qrcode_iframe_uid(snapshot_result)

    if not iframe_uid:
        print("❌ 未找到二维码 iframe，登录流程终止")
        return False

    print(f"   ✅ 找到二维码 iframe: uid={iframe_uid}")

    # --------------------------------------------------------
    # 步骤3：截取二维码图片
    # --------------------------------------------------------
    print("[步骤3/7] 截取二维码图片...")
    screenshot_result = browser_take_screenshot(uid=iframe_uid)
    _m = re.search(r'"data":\s*"([A-Za-z0-9+/=]+)"', str(screenshot_result))
    qrcode_base64 = _m.group(1) if _m else ""

    if not qrcode_base64:
        print("❌ 二维码截图失败")
        return False

    print(f"   ✅ 二维码已获取。")

    # --------------------------------------------------------
    # 步骤4：唤醒屏幕并显示二维码到手机A
    # --------------------------------------------------------
    print(f"[步骤4/7] 唤醒 {display_phone} 屏幕...")
    if not android_wakeup_and_unlock(display_device_id, display_pin):
        print(f"❌ {display_phone} 屏幕解锁失败，请检查PIN码配置")
        return False

    print(f"   将二维码发送到 {display_phone} 显示...")
    android_show_image(qrcode_base64, display_device_id)
    time.sleep(2)

    print(f"   ✅ 二维码已在 {display_phone} 上全屏显示（最大亮度）")

    # --------------------------------------------------------
    # 步骤5：唤醒手机B并打开微信
    # --------------------------------------------------------
    print(f"[步骤5/7] 唤醒 {scan_phone} 屏幕...")
    if not android_wakeup_and_unlock(scan_device_id, scan_pin):
        print(f"❌ {scan_phone} 屏幕解锁失败，请检查PIN码配置")
        return False

    print(f"   打开微信...")
    android_run_adb(
        "shell am start -n com.tencent.mm/.ui.LauncherUI",
        scan_device_id,
    )
    time.sleep(5)  # 等待微信启动

    # --------------------------------------------------------
    # 步骤6：点击"+"按钮，然后点击"扫一扫"
    # --------------------------------------------------------
    print("[步骤6/7] 点击微信右上角 '+' 按钮...")
    android_find_and_click("\\+", scan_device_id)
    time.sleep(1)

    print("   点击 '扫一扫'...")
    android_find_and_click("扫一扫", scan_device_id)
    time.sleep(2)

    # 此时手机B的相机已经打开，对准手机A的屏幕
    # 微信扫一扫会自动识别二维码，无需手动拍照
    print("   ✅ 扫一扫已启动，等待自动识别二维码...")

    # --------------------------------------------------------
    # 步骤7：等待登录成功
    # --------------------------------------------------------
    print("[步骤7/7] 等待登录成功...")

    # 在手机B上确认扫码（如果有确认按钮的话）
    time.sleep(3)
    try:
        android_find_and_click("^登录$", scan_device_id)
        print("   ✅ 已在手机上确认登录")
    except Exception:
        print("   ℹ️  无需手机确认（可能已自动确认）")

    # 等待浏览器页面跳转（登录成功后会跳转到后台页面）
    print("   等待页面跳转...")
    login_success = wait_for_element_in_snapshot(
        keyword="发表", timeout=30, interval=3
    )

    # --------------------------------------------------------
    # 步骤8：清理 - 将两台手机恢复到初始状态
    # --------------------------------------------------------
    print("\n[清理] 将手机恢复到初始状态...")

    # 1. MI5X：关闭图片预览 APP，回到桌面
    print(f"   {display_phone}：关闭图片预览 APP...")
    # 关闭 MIUI 相册应用
    android_run_adb("shell am force-stop com.miui.gallery", display_device_id)
    time.sleep(0.5)
    # 按 HOME 键回到桌面
    android_run_adb("shell input keyevent KEYCODE_HOME", display_device_id)
    time.sleep(0.5)
    # 恢复屏幕设置（取消保持亮屏，恢复正常亮度）
    android_run_adb("shell svc power stayon false", display_device_id)
    android_run_adb("shell settings put system screen_brightness 128", display_device_id)
    # 息屏
    android_run_adb("shell input keyevent KEYCODE_SLEEP", display_device_id)
    print(f"   ✅ {display_phone} 已恢复到初始状态（息屏）")

    # 2. Redmi：关闭微信，回到桌面
    print(f"   {scan_phone}：关闭微信...")
    # 关闭微信应用
    android_run_adb("shell am force-stop com.tencent.mm", scan_device_id)
    time.sleep(0.5)
    # 按 HOME 键回到桌面
    android_run_adb("shell input keyevent KEYCODE_HOME", scan_device_id)
    time.sleep(0.5)
    # 恢复屏幕设置
    android_run_adb("shell svc power stayon false", scan_device_id)
    android_run_adb("shell settings put system screen_brightness 128", scan_device_id)
    # 息屏
    android_run_adb("shell input keyevent KEYCODE_SLEEP", scan_device_id)
    print(f"   ✅ {scan_phone} 已恢复到初始状态（息屏）")

    print("   🔄 所有手机已恢复到初始状态，等待下一次执行")

    if login_success:
        print("\n" + "=" * 60)
        print("🎉 登录成功！已进入视频号创作者平台后台")
        print("=" * 60)
        return True
    else:
        print("\n" + "=" * 60)
        print("⚠️  登录超时，请检查扫码是否完成")
        print("=" * 60)
        return False


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    success = login_video_account_by_scan()
    exit(0 if success else 1)

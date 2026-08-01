"""
示例：通过手机扫码自动登录视频号创作者平台（需要 Android 设备）

【目标】
在电脑浏览器上自动化登录微信视频号创作者平台（https://channels.weixin.qq.com/login.html）。
该平台需要使用微信扫码登录，本脚本通过两个手机（微信登录扫码强制要求摄像头物理拍照）配合完成整个流程：
- 手机A（显示二维码）：接收电脑截取的登录二维码并全屏显示
- 手机B（扫码手机）：打开微信扫一扫，扫描手机A上的二维码完成登录

【前提条件】
1. Agent Service 服务已启动（localhost:7988），android-use MCP server 已注册并连接两台 Android 手机
2. chrome-devtools-mcp 和 android-use-mcp 工具已注册
3. 手机B（扫码手机）已安装微信并登录
4. 两台手机的屏幕已解锁
5. 已安装 Python Pillow 库（用于图片旋转处理）
6. 运行前请修改下方 PHONES 配置为你的实际设备信息（device_id 可通过本脚本启动时的设备检测自动列出）

【流程概览】
1. 电脑浏览器打开视频号登录页面
2. （可选）如果页面有多种登录方式，点击切换到二维码扫码模式
3. 对整页截图，微信扫一扫会自动放大识别二维码
4. 将二维码图片发送到手机A全屏显示（最大亮度）
5. 在手机B上打开微信 → 点击"+" → 点击"扫一扫"
6. 等待扫码完成，点击"登录"。在浏览器中确认登录成功

【API说明】
本脚本通过 /v1/tools/call 接口调用 chrome-devtools-mcp 和 android-use-mcp 工具，全程无大模型参与。
- tool_id 格式：mcp-{service}-{tool_name}
- 浏览器工具：mcp-chrome-devtools-take_snapshot, mcp-chrome-devtools-take_screenshot, mcp-chrome-devtools-navigate_page
- Android工具：mcp-android-use-show_image, mcp-android-use-run_adb_command, mcp-android-use-find_and_click
- 设备检测通过 mcp-android-use-run_adb_command（command="devices"）完成，不再依赖本机 adb 命令行

【注意】
如果 Agent Service 不可达、android-use MCP 未注册或没有已连接设备，脚本会在启动时检测并报错退出。

"""

import re
import sys
import json
import urllib.request
import urllib.error
import time
import base64
import io
from typing import Optional, Dict, Any, Tuple
from PIL import Image

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
        tool_id: 工具ID，如 "mcp-chrome-devtools-take_snapshot", "mcp-android-use-show_image"
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


def browser_take_screenshot() -> dict:
    """截取整页截图（无需指定元素，微信扫一扫会自动识别二维码）"""
    return call_tool(
        "mcp-chrome-devtools-take_screenshot",
        {},
        format="text",
    )


def browser_find_and_click(keyword: str) -> dict:
    """
    在页面上查找并点击包含指定文字的元素。
    
    实现逻辑：
    1. 截取页面截图
    2. 调用 MCP-OCR 服务的 find_location 工具定位目标文字
    3. 使用 mcp-chrome-devtools-click_at 点击对应坐标
    """
    import base64
    
    # 步骤 1: 截取页面截图
    screenshot_result = browser_take_screenshot()
    
    # 提取 base64 图片数据
    _m = re.search(r'"(?:data|screenshot|image)":\s*"([A-Za-z0-9+/=]+)"', str(screenshot_result))
    if not _m:
        return {"success": False, "message": "无法获取页面截图"}
    
    screenshot_base64 = _m.group(1)
    
    # 步骤 2: 调用 MCP-OCR find_location 定位目标
    location_result = call_tool(
        "mcp-OCR-find_location",
        {
            "base64_image_big": screenshot_base64,
            "pattern": keyword
        },
        format="json",
    )
    
    if not location_result.get("success"):
        return {"success": False, "message": f"定位失败: {location_result.get('message', '未找到目标')}"}
    
    center_x = location_result.get("center_x")
    center_y = location_result.get("center_y")
    
    if center_x is None or center_y is None:
        return {"success": False, "message": "无法获取目标坐标"}
    
    # 步骤 3: 点击对应坐标
    return call_tool(
        "mcp-chrome-devtools-click_at",
        {"x": center_x, "y": center_y},
        format="text",
    )


# ============================================================
# 图片处理工具
# ============================================================


def rotate_image_for_mobile_display(base64_image: str, angle: int = 90) -> str:
    """
    旋转图片以便在竖屏手机上全屏显示。
    
    电脑截屏通常是横屏图片，在竖屏手机上显示时会被缩小，导致二维码太小难以扫描。
    旋转90度后变为竖屏图片，在竖屏手机上显示时会填满屏幕，显示更大更清晰。
    
    Args:
        base64_image: base64编码的图片数据
        angle: 旋转角度，90表示顺时针90度，-90表示逆时针90度
        
    Returns:
        旋转后的base64编码图片数据
    """
    try:
        # 解码base64图片
        image_data = base64.b64decode(base64_image)
        img = Image.open(io.BytesIO(image_data))
        
        original_size = img.size
        print(f"   原始图片尺寸: {original_size[0]}x{original_size[1]}")
        
        # 旋转图片
        if angle == 90:
            rotated_img = img.transpose(Image.ROTATE_270)  # 顺时针90度
        elif angle == -90 or angle == 270:
            rotated_img = img.transpose(Image.ROTATE_90)   # 逆时针90度
        elif angle == 180:
            rotated_img = img.transpose(Image.ROTATE_180)
        else:
            rotated_img = img.rotate(angle, expand=True)
        
        new_size = rotated_img.size
        print(f"   旋转后尺寸: {new_size[0]}x{new_size[1]}")
        
        # 转换回base64
        buffer = io.BytesIO()
        rotated_img.save(buffer, format='PNG')
        rotated_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        return rotated_base64
        
    except Exception as e:
        print(f"   ⚠️ 图片旋转失败: {e}，使用原图")
        return base64_image


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
        "mcp-android-use-show_image",
        {"base64_content": base64_content, "device_id": device_id},
        format="json",
    )


def android_run_adb(command: str, device_id: str) -> dict:
    """执行 ADB 命令（通过 android-use MCP server，非本机命令行）"""
    return call_tool(
        "mcp-android-use-run_adb_command",
        {"command": command, "device_id": device_id},
        format="json",
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
        "mcp-android-use-find_and_click",
        {"keyword_or_image_file": keyword, "device_id": device_id},
        format="json",
    )


# ============================================================
# 核心业务逻辑
# ============================================================


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
    2. （可选）切换到二维码扫码模式
    3. 对整页截图，微信扫一扫会自动放大识别二维码
    4. 将二维码发送到手机A全屏显示
    5. 在手机B上打开微信扫一扫
    6. 扫描手机A上的二维码
    7. 等待登录成功

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
    # 步骤2：（可选）切换到二维码扫码模式
    # --------------------------------------------------------
    # 【说明】很多网页登录页面有多种登录方式（账号密码、手机验证码、扫码等），
    # 需要点击切换到"扫码登录"模式，让二维码显示出来。
    # 
    # 示例：通过 chrome-devtools-mcp 执行点击操作，切换到扫码登录模式
    # ```python
    # # 获取页面快照，查看有哪些登录方式
    # snapshot = browser_take_snapshot()
    # # 点击"扫码登录"或"二维码登录"按钮
    # browser_find_and_click("扫码登录")
    # ```
    # 
    # 【注意】视频号登录页面默认就是扫码模式，无需此步骤。
    # 但其他网站（如淘宝、京东等）可能需要先切换登录方式。
    
    print("[步骤2/7] 检查登录模式...")
    print("   ℹ️  视频号登录页面默认为扫码模式，无需切换")

    # --------------------------------------------------------
    # 步骤3：截取整页截图（包含二维码）
    # --------------------------------------------------------
    print("[步骤3/7] 截取登录页面截图...")
    screenshot_result = browser_take_screenshot()
    
    # 从返回结果中提取 base64 图片数据
    _m = re.search(r'"(?:data|screenshot|image)":\s*"([A-Za-z0-9+/=]+)"', str(screenshot_result))
    qrcode_base64 = _m.group(1) if _m else ""

    if not qrcode_base64:
        print("❌ 截图失败，未获取到图片数据")
        return False

    print(f"   ✅ 页面截图已获取（微信扫一扫会自动放大识别二维码）")
    
    # 旋转图片以便在竖屏手机上全屏显示
    # 电脑截屏是横屏图片，在竖屏手机上显示时会缩小，二维码太小难以扫描
    # 旋转90度后变为竖屏图片，在竖屏手机上会填满屏幕，显示更大更清晰
    print("   旋转图片以便在竖屏手机上全屏显示...")
    qrcode_base64 = rotate_image_for_mobile_display(qrcode_base64, angle=90)

    # --------------------------------------------------------
    # 步骤4：唤醒屏幕并显示二维码到手机A
    # --------------------------------------------------------
    print(f"[步骤4/7] 唤醒 {display_phone} 屏幕...")
    if not android_wakeup_and_unlock(display_device_id, display_pin):
        print(f"❌ {display_phone} 屏幕解锁失败，请检查PIN码配置")
        return False

    print(f"   将截图发送到 {display_phone} 显示...")
    show_image_result = android_show_image(qrcode_base64, display_device_id)
    # 保存返回的文件路径，用于后续清理
    qrcode_phone_path = show_image_result.get("phone_path", "") if show_image_result else ""
    time.sleep(2)

    print(f"   ✅ 登录页面截图已在 {display_phone} 上全屏显示（最大亮度）")

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

    try:
        android_find_and_click("轻触照亮", scan_device_id)
        print(f"   ✅ 点击 '轻触照亮' 成功")
        time.sleep(2)
    except Exception as e:
        pass

    # 此时手机B的相机已经打开，对准手机A的屏幕
    # 微信扫一扫会自动放大图片并识别二维码，无需手动拍照
    print("   ✅ 扫一扫已启动，对准手机A屏幕，等待自动识别二维码...")

    # --------------------------------------------------------
    # 步骤7：等待登录成功
    # --------------------------------------------------------
    print("[步骤7/7] 等待登录成功...")

    # 在手机B上确认扫码（如果有确认按钮的话）
    time.sleep(5)
    for attempt in range(3):
        try:
            android_find_and_click("^登录$", scan_device_id)
            print("   ✅ 已在手机上确认登录")
            break
        except Exception as e:
            time.sleep(5)
            try:
                android_find_and_click("允许", scan_device_id)
                print("   ✅ 已在手机上确认登录")
                break
            except Exception as e:
                time.sleep(3)
            print(f"   ⏳ 重试第 {attempt + 1} 次: {e}")

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
    print(f"   {display_phone}：删除之前传输到相册的图片...")
    # 删除传输到相册的图片（使用 show_image 返回的具体路径）
    if qrcode_phone_path:
        android_run_adb(f"shell rm -f {qrcode_phone_path}", display_device_id)
        print(f"   ✅ 已删除图片: {qrcode_phone_path}")
    else:
        print(f"   ⚠️  未找到图片路径，跳过删除")
    time.sleep(0.5)
    
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

# 启动时必需的 android-use MCP 工具
REQUIRED_ANDROID_TOOLS = {
    "mcp-android-use-run_adb_command",
    "mcp-android-use-show_image",
    "mcp-android-use-find_and_click",
}


def _check_android_mcp():
    """
    通过 Agent Service 检查 android-use MCP server 及已连接设备。

    不再依赖本机 adb 命令行：设备检测走 localhost:7988 上已注册的
    mcp-android-use-run_adb_command 工具（command="devices"）。
    """
    # 1. 检查 Agent Service 可达 + android-use 工具已注册
    try:
        with urllib.request.urlopen("http://localhost:7988/v1/tools", timeout=10) as resp:
            tools_data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"错误: 无法连接 Agent Service (http://localhost:7988): {e}")
        print("      请先启动 Agent Service，并确保 android-use MCP server 已注册。")
        sys.exit(1)

    tool_ids = {t["tool_id"] for t in tools_data.get("tools", [])}
    android_tool_ids = sorted(t for t in tool_ids if t.startswith("mcp-android-use-"))
    if not android_tool_ids:
        print("错误: Agent Service 上未注册 android-use MCP server 的任何工具。")
        print("      请检查 android-use MCP server 配置并重新加载。")
        sys.exit(1)

    missing = REQUIRED_ANDROID_TOOLS - tool_ids
    if missing:
        print(f"错误: android-use MCP 缺少必需工具: {sorted(missing)}")
        print(f"      当前已注册的 android-use 工具: {android_tool_ids}")
        sys.exit(1)

    print(f"✅ android-use MCP server 已注册（{len(android_tool_ids)} 个工具）")

    # 2. 通过 MCP server 的 run_adb_command 检测已连接设备（走 server 侧 adb）
    try:
        result = call_tool(
            "mcp-android-use-run_adb_command",
            {"command": "devices"},
            format="json",
        )
    except Exception as e:
        print(f"错误: 通过 android-use MCP 检测设备失败: {e}")
        sys.exit(1)

    stdout_text = str(result.get("stdout", ""))
    devices = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line or "List of devices" in line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    if not devices:
        print("错误: android-use MCP 未检测到已连接的 Android 设备。")
        print("      请确认手机已连接并通过 MCP server 的 ADB 可见。")
        sys.exit(1)

    print(f"✅ 检测到 {len(devices)} 台设备: {devices}")

    # 3. 对比 PHONES 配置的设备是否全部在线
    configured = {p["device_id"] for p in PHONES.values()}
    missing_cfg = configured - set(devices)
    if missing_cfg:
        print(f"⚠️  以下 PHONES 中配置的设备未在线: {missing_cfg}")
        print(f"    当前在线设备: {devices}")
        print("    请更新 PHONES 配置或连接对应设备后重试。")
        sys.exit(1)
    print("✅ PHONES 配置的全部设备均在线")

if __name__ == "__main__":
    _check_android_mcp()
    success = login_video_account_by_scan()
    exit(0 if success else 1)

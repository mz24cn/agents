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
- 浏览器工具：mcp-chrome-devtools-take_snapshot, mcp-chrome-devtools-take_screenshot, mcp-chrome-devtools-navigate_page,
  mcp-chrome-devtools-list_network_requests（取二维码直链 uuid，缺了会退回截图兜底）
- Android工具：mcp-android-use-show_image, mcp-android-use-run_adb_command, mcp-android-use-find_and_click
- 设备检测通过 mcp-android-use-run_adb_command（command="devices"）完成，不再依赖本机 adb 命令

【注意】
脚本入口和 login_video_account_by_scan() 内部都会先做一次预检（check_android_mcp），并把失败原因
分清楚，不再把三种不同的问题都说成"没检测到设备"：
- Agent Service 不可达（服务没启动 / 端口不通）         -> AgentServiceError
- android-use MCP server 不可调用（server 没启动 / MCP 端口不通 /
  连接已失效；Agent Service 会先重连、30s 超时后以 "Error: ..." 返回工具失败）-> ToolCallError
- adb 在 MCP host 上没有列出设备（unauthorized / offline / 空列表）    -> 这才是"没有已连接设备"
- 浏览器侧不可用（chrome-devtools MCP 没注册 / stdio 进程被空闲回收后拉不起来 / npx 冷启动
  卡住 / root 下 Chrome 缺 --no-sandbox / WSL 里没有浏览器）      -> check_browser_mcp 拦住
预检分手机侧（check_android_mcp）和浏览器侧（check_browser_mcp）两段，两段都必须真的调用一次
工具才算通过 —— 只看注册表快照会把"服务在跑但 MCP server 已经断了"看成一切正常。
另外注意：Agent Service 的 /v1/tools 里的 MCP 工具列表是上次运行落盘的注册表快照，
"工具在列表里" ≠ "MCP server 现在还活着"，所以预检一定会真的调用一次 adb 才算通过。

流程里任何一次 Android/浏览器工具硬失败都会立刻中断并返回 False，不会继续把 7 个步骤
的"成功"日志刷完（以前 android_run_adb 的返回值被完全忽略，MCP 已经断了还在一路打 ✅）。

【命令行】
  python example_login_by_scan_qrcode_from_mobile.py            # 预检 + 扫码登录
  python example_login_by_scan_qrcode_from_mobile.py --check    # 只做预检（MCP 连通性 + 设备）
  python example_login_by_scan_qrcode_from_mobile.py --cleanup  # 只把手机恢复到初始状态（流程中断后用）

"""

import os
import re
import sys
import json
import time
import base64
import socket
import io
import urllib.request
import urllib.error
from typing import Any
from PIL import Image

# ============================================================
# 配置区域
# ============================================================

# Agent Service API 地址（可用环境变量 AGENT_SERVICE_BASE 覆盖，便于换端口/换机器调试）
AGENT_SERVICE_BASE = os.environ.get("AGENT_SERVICE_BASE", "http://localhost:7988").rstrip("/")
MCP_API_URL = AGENT_SERVICE_BASE + "/v1/tools/call"
TOOLS_API_URL = AGENT_SERVICE_BASE + "/v1/tools"
MCP_SERVERS_API_URL = AGENT_SERVICE_BASE + "/v1/mcp-servers"

# android-use MCP server 的名字（tool_id 前缀是 mcp-{server_name}-）
MCP_SERVER_NAME = "android-use"
ANDROID_TOOL_PREFIX = f"mcp-{MCP_SERVER_NAME}-"

# chrome-devtools MCP server 的名字（步骤1 打开页面、步骤3 截图、步骤6 看页面都走它）
BROWSER_SERVER_NAME = "chrome-devtools"
BROWSER_TOOL_PREFIX = f"mcp-{BROWSER_SERVER_NAME}-"

# 超时（秒）。
# 关键：MCP 连接失效时，Agent Service 会先尝试重连 MCP server，并在 30s 后才报错
# （见 runtime/mcp_client.py 的 _DEFAULT_CONNECT_TIMEOUT=30）。客户端超时必须明显
# 大于这个值，否则会把"服务端 MCP 连不上"误判成"本地 HTTP 请求超时"，看不到真实原因。
# 另外 stdio 型 MCP server 空闲 300s 会被回收（_DEFAULT_IDLE_TIMEOUT）：隔了 5 分钟再跑，
# 第一次调用要重新拉起 server + Chrome，慢是正常现象，所以浏览器侧探测给得更宽。
# 三个值都能用环境变量覆盖（冷启动特别慢的机器上调大，调试时调小）。
TOOL_CALL_TIMEOUT = int(os.environ.get("TOOL_CALL_TIMEOUT", "90"))
PREFLIGHT_TIMEOUT = int(os.environ.get("PREFLIGHT_TIMEOUT", "60"))
BROWSER_PROBE_TIMEOUT = int(os.environ.get("BROWSER_PROBE_TIMEOUT", "75"))

# 视频号登录页面
LOGIN_URL = "https://channels.weixin.qq.com/login.html"

# 二维码取图路径（2026-11-25 起优先直链，不再依赖截图；机理注释见下面
# 「取二维码：优先二维码直链」那一节的现场记录）
QRCODE_IMAGE_BASE = "https://open.weixin.qq.com/connect/qrcode/"
# 一个正则同时认两种来源：二维码图本体、以及页面查"扫了没有"的长轮询
QR_UUID_PATTERNS = (
    r"connect/qrcode/([0-9A-Za-z]{6,})",
    r"l/qrconnect\?uuid=([0-9A-Za-z]{6,})",
)
QR_UUID_RE = re.compile("|".join(QR_UUID_PATTERNS))
# 导航后等二维码请求出现的时间上限（页面自己那 12 个 checkLogin 探测要几秒钟才跑完）
QR_WAIT_TIMEOUT = int(os.environ.get("QR_WAIT_TIMEOUT", "25"))
# 下载二维码原图的超时（这是脚本所在机器直接出网，不走 MCP）
QRCODE_FETCH_TIMEOUT = int(os.environ.get("QRCODE_FETCH_TIMEOUT", "20"))
# 取不到码 / 码失效时，重新打开登录页的次数（注意不是点「点击重试」）
QR_PAGE_RETRIES = int(os.environ.get("QR_PAGE_RETRIES", "3"))
# 推到手机后等登录跳转的总时长；以前硬编码 30s，手机侧根本来不及扫完
LOGIN_WAIT_TIMEOUT = int(os.environ.get("LOGIN_WAIT_TIMEOUT", "90"))
LOGIN_POLL_INTERVAL = int(os.environ.get("LOGIN_POLL_INTERVAL", "3"))

# 手机配置（通过 adb devices 获取）
# 型号标识 -> (device_id, 用途说明, PIN码, 流程结束后要关掉的 APP 包名)
PHONES = {
    "MI5X": {
        "device_id": "0043d38b0504",
        "role": "display",  # 用于显示二维码
        "pin": None,  # 锁屏PIN码，None表示无PIN码或仅滑动解锁
        "close_app": "com.miui.gallery",  # show_image 会打开的 MIUI 相册
    },
    "Redmi": {
        "device_id": "476cf991",
        "role": "scan",  # 用于微信扫码
        "pin": None,  # 锁屏PIN码，None表示无PIN码或仅滑动解锁
        "close_app": "com.tencent.mm",  # 微信
    },
}

# 显示二维码的手机型号（必须在 PHONES 中配置）
DISPLAY_PHONE_MODEL = "MI5X"

# 扫码的手机型号（必须在 PHONES 中配置）
SCAN_PHONE_MODEL = "Redmi"

# show_image 推到手机相册的二维码文件命名（见 android_use_mcp.show_image），--cleanup 会清掉
PHONE_QRCODE_GLOB = "/sdcard/DCIM/Camera/show_image_*.png"

# ============================================================
# 错误分类
# ============================================================


class AgentServiceError(RuntimeError):
    """连 Agent Service 本身都访问不到：服务没启动 / 端口不通 / 鉴权失败 / 响应无法解析。"""

    def __init__(self, message: str, elapsed: float = None):
        super().__init__(message)
        self.message = message
        self.elapsed = elapsed


class ToolCallError(RuntimeError):
    """工具调用发出去了，但返回失败：MCP server 连不上、ADB 报错、执行超时等。"""

    def __init__(self, tool_id: str, message: str, raw: Any = None, elapsed: float = None):
        super().__init__(f"[{tool_id}] {message}")
        self.tool_id = tool_id
        self.message = message
        self.raw = raw
        self.elapsed = elapsed


class ToolCallTimeout(ToolCallError):
    """
    HTTP 请求发出去了，但在超时里没拿到任何响应。

    单独一个类型，是为了不再把这种情况报成"Agent Service 没启动"：预检阶段已经调通过
    服务，说明服务在跑，真正没返回的是它背后那个 MCP server（进程卡住 / 正在被重新拉起）。
    """

    def __init__(self, tool_id: str, timeout: float, elapsed: float = None):
        super().__init__(
            tool_id,
            f"请求 {MCP_API_URL} 调用 {tool_id} 后 {timeout}s 没有拿到响应（HTTP 读超时）",
            elapsed=elapsed,
        )
        self.timeout = timeout


class FlowAborted(RuntimeError):
    """业务性失败（解锁失败、找不到按钮等）：MCP 是通的，只是这一步做不下去。

    单独一个异常类型，是为了让"中途放弃"也能走手机清理，而不是 return 直接跳过复位。
    """


# 命中这些字样基本可以断定是"通道/网络"层面的问题，而不是"没有设备"。
# 中英文都列上：Agent Service 在 Windows 上会把 WinError 的中文消息原样带出来。
_NETWORK_MARKERS = (
    "connection refused", "actively refused", "refused", "connection aborted",
    "connection reset", "remotedisconnected", "unable to connect", "cannot connect",
    "can't connect", "failed to establish", "unreachable", "not connected",
    "no route to host", "getaddrinfo", "timed out", "timeout", "eof occurred",
    "winerror 10061", "winerror 10060", "winerror 10042", "winerror 1225",
    "拒绝网络连接", "积极拒绝", "无法连接", "连接超时", "没有反应", "找不到主机",
    "mcp tool call failed", "mcpservers", "mcp server", "connectionerror",
)


def looks_like_transport_failure(text: Any) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _NETWORK_MARKERS)


def _short(text: Any, limit: int = 400) -> str:
    """把可能很长（含 base64）的返回压成一行短文本：报错看得清，也不刷屏。"""
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[:limit] + f"...(共 {len(s)} 字符)"
    return s


def _error_detail(body_text: str) -> str:
    """/v1/tools/call 的失败响应体形如 {"error": "Error: ..."}，取出人类可读部分。"""
    if not body_text:
        return ""
    try:
        obj = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return _short(body_text)
    if isinstance(obj, dict):
        for key in ("error", "message", "detail"):
            if obj.get(key):
                return _short(obj[key])
    return _short(body_text)


def _server_of_tool(tool_id: Any) -> str:
    """tool_id 形如 mcp-{server_name}-{tool_name}，取回它属于哪个 MCP server。"""
    tid = str(tool_id or "")
    if not tid.startswith("mcp-"):
        return ""
    rest = tid[4:]
    for name in (MCP_SERVER_NAME, BROWSER_SERVER_NAME, "OCR"):
        if rest.startswith(name + "-"):
            return name
    return rest.split("-")[-1] if "-" in rest else rest


def print_android_checklist() -> None:
    print("      排查建议（android-use 侧）:")
    print(f"        1) 确认 {MCP_SERVER_NAME} MCP server 进程在跑：python accessories/android_use_mcp.py --port <端口>")
    print("        2) 确认 Agent Service 里保存的 MCP url 端口能通（见上面打印的 url，curl 该地址试一下）")
    print("        3) 在 MCP server 所在机器上（不是本机）执行 adb devices，确认设备确实在这台机器上")
    print("        4) MCP server 重启过就要重新注册：POST /v1/tools/mcp，或重启 Agent Service")


def print_browser_checklist() -> None:
    print("      排查建议（chrome-devtools 侧）:")
    print("        1) 先在前台手工跑一次，看它到底卡在哪：npx -y chrome-devtools-mcp@latest")
    print("           （npx 冷启动要下载包，机器离线/代理不通时这一步会一直卡住 —— 正是“请求发出去但没返回”）")
    print("        2) 确认 MCP host 上有浏览器：google-chrome --version / chromium --version；")
    print("           没有就先装一个，或给 chrome-devtools-mcp 加 --executablePath 指到已有浏览器")
    print("        3) root 用户直接跑 Chrome 会被拒（Running as root without --no-sandbox）。")
    print("           这种情况先自己起一个：chromium --headless=new --no-sandbox --remote-debugging-port=9222，")
    print("           再让 MCP 用 --browser-url http://127.0.0.1:9222 连上去，别让它自己启动 Chrome")
    print("        4) WSL/无显示器环境必须 headless；页面能不能打开还取决于 MCP host 的网络和 DNS，")
    print("           在 MCP host 上试：curl -sI https://channels.weixin.qq.com/login.html")
    print("        5) 改了 MCP 的 command/args 要重新注册（POST /v1/tools/mcp）或重启 Agent Service，")
    print("           否则 Agent Service 还按老配置连，而 /v1/tools 里的工具只是上次落盘的快照")


def print_diagnosis(err: Exception) -> None:
    """打印可操作的诊断信息：把"MCP 没启动/端口不通"和"没有连接设备"区分开。"""
    elapsed = getattr(err, "elapsed", None)
    text = str(getattr(err, "message", err))
    server = _server_of_tool(getattr(err, "tool_id", ""))

    if isinstance(err, AgentServiceError) and not _service_alive:
        print(f"      判断: Agent Service（{AGENT_SERVICE_BASE}）本身不可达 —— 请先启动 Agent Service。")
        return

    if isinstance(err, ToolCallTimeout):
        print(f"      判断: 请求发出去了但 {err.timeout}s 都没返回 —— 不是 Agent Service 没启动（它早前明明回过话），")
        print(f"            是它转过去的那个 MCP server（{server or '未知'}）没回话：进程卡住，或正被重新拉起（stdio 冷启动）。")
    elif isinstance(err, AgentServiceError):
        print(f"      判断: Agent Service（{AGENT_SERVICE_BASE}）这次运行早前是调得通的，所以不是没启动；")
        print(            "            这次连上了却不返回，通常是它把请求转给了某个 MCP server，而那边卡住了。")
    elif looks_like_transport_failure(text):
        print("      判断: 失败发生在 MCP 通道上 —— 更像是 MCP server 未启动 / MCP 端口不通 / 连接已失效，")
        print("            不是“没有已连接的 Android 设备”。本机命令行 adb devices 能看到设备并不能说明")
        print(f"            {MCP_SERVER_NAME} MCP server 那侧的 adb 也能看到。")
    else:
        print("      判断: MCP 工具返回了失败结果，不一定是“没有连接设备”，请按上面的原始错误排查。")

    if elapsed is not None and elapsed >= 25:
        print(f"      耗时 {elapsed:.1f}s：Agent Service 发现 MCP 连接失效后会先重连、30s 超时后报错，")
        print("      这个耗时正好对应“MCP 端口连不上 / server 进程没在跑”。")

    _print_mcp_server_config()
    if server == BROWSER_SERVER_NAME:
        print_browser_checklist()
    elif server and server != MCP_SERVER_NAME:
        print(f"      这次失败的工具属于 {server} MCP server，请按它自己的启动方式排查；下面这几条只适用于 android-use：")
        print_android_checklist()
    else:
        print_android_checklist()


def _print_mcp_server_config() -> None:
    """打印 Agent Service 里保存的 MCP server 配置（url / command），便于定位端口问题。"""
    try:
        with urllib.request.urlopen(MCP_SERVERS_API_URL, timeout=10) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # 只是辅助信息，读不到不影响主诊断
        print(f"      （读 {MCP_SERVERS_API_URL} 失败，无法显示 MCP 配置: {e}）")
        return
    servers = cfg.get("mcpServers", {}) if isinstance(cfg, dict) else {}
    if not servers:
        print("      Agent Service 没有保存任何 MCP server 配置（/v1/mcp-servers 为空）。")
        return
    for name, sc in servers.items():
        if not isinstance(sc, dict):
            continue
        target = sc.get("url") or " ".join([str(sc.get("command") or "")] + list(sc.get("args") or []))
        mark = "   <== 本脚本用的 server" if name == MCP_SERVER_NAME else ""
        print(f"      MCP server [{name}]: {target or '(无 url/command)'}{mark}")


# ============================================================
# 工具函数
# ============================================================


def call_tool(
    tool_id: str,
    arguments: dict,
    format: str = "json",
    timeout: int = TOOL_CALL_TIMEOUT,
    allow_business_failure: bool = False,
) -> Any:
    """
    调用 Agent Service 的 /v1/tools/call 接口。

    Args:
        tool_id: 工具ID，如 "mcp-chrome-devtools-take_snapshot", "mcp-android-use-show_image"
        arguments: 工具参数，与大模型调用时的 arguments 格式完全一致
        format: 返回格式，"json" 返回 JSON 对象，否则返回原始文本
        timeout: 本次 HTTP 请求超时（秒），务必大于服务端重连 MCP 的 30s
        allow_business_failure: True 时，工具返回 {"success": false} 不当异常抛出，
            而是原样返回让调用方自己判断（例如 find_and_click 的"没找到目标"）

    Returns:
        工具执行结果（dict 或 str，取决于 format 参数）

    Raises:
        AgentServiceError: Agent Service 连不上 / 鉴权失败（连 TCP 都没成功）
        ToolCallTimeout:   连上了、请求发出去了，但 timeout 秒内没返回（MCP server 卡住）
        ToolCallError:     工具执行失败（MCP server 连不上、ADB 报错、success=false 等）
    """
    global _service_alive

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
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_text = resp.read().decode("utf-8", errors="replace")
        _service_alive = True      # 服务确实在应答，后面的诊断可以用这个事实
    except urllib.error.HTTPError as e:
        # /v1/tools/call 对工具级失败返回 {"error": "Error: ..."}（状态码 4xx），
        # 以前这里把 error 体当成"结果"返回，导致后面只能报"未知错误"。
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
        detail = _error_detail(body_text) or f"HTTP {e.code} {e.reason}"
        _service_alive = True      # 能回 4xx 也说明服务活着，只是工具失败
        if e.code in (401, 403):
            raise AgentServiceError(
                f"Agent Service 拒绝访问（HTTP {e.code} {e.reason}），可能开启了登录鉴权: {detail}",
                elapsed=time.time() - t0,
            ) from None
        raise ToolCallError(tool_id, detail, raw=body_text, elapsed=time.time() - t0) from None
    except urllib.error.URLError as e:
        raise AgentServiceError(
            f"无法访问 {MCP_API_URL}: {e.reason}", elapsed=time.time() - t0
        ) from None
    except (TimeoutError, socket.timeout):
        # 这不是"连不上"，而是"连上了但没回"。以前报成 AgentServiceError，会把人引到
        # "请先启动 Agent Service"，而真正没返回的是这个工具背后的那个 MCP server。
        raise ToolCallTimeout(tool_id, timeout, elapsed=time.time() - t0) from None
    except OSError as e:
        raise AgentServiceError(
            f"访问 {MCP_API_URL} 失败: {e}", elapsed=time.time() - t0
        ) from None

    # 根据 format 参数决定如何解析响应
    try:
        result = json.loads(response_text)
    except (json.JSONDecodeError, ValueError):
        if format == "text":
            # text 格式：解析失败就返回原始文本（浏览器工具返回 text/plain）
            return response_text
        raise ToolCallError(
            tool_id, f"响应不是 JSON: {_short(response_text)}",
            raw=response_text, elapsed=time.time() - t0,
        ) from None

    if isinstance(result, dict):
        if result.get("error"):
            # 200 里带 error 字段也按失败处理，不能当成正常结果往下走
            raise ToolCallError(
                tool_id, _short(result["error"]), raw=result, elapsed=time.time() - t0
            )
        if result.get("success") is False and format != "text" and not allow_business_failure:
            msg = result.get("message") or result.get("stderr") or json.dumps(result, ensure_ascii=False)
            raise ToolCallError(
                tool_id, _short(msg), raw=result, elapsed=time.time() - t0
            )
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


# ============================================================
# 取二维码：优先二维码直链，截图只作兜底
# ============================================================
#
# 2026-11-25 现场（WSL + chrome-devtools-mcp@1.6.0 --browser-url=http://localhost:9222）。
# 下面是当时用 mcp-chrome-devtools-list_network_requests 打出来的真实网络记录，按 reqid 排序：
#
#   reqid=51 GET  https://open.weixin.qq.com/connect/qrcode/<uuid>              [200]  ← 码已经拿到
#   reqid=54..65 POST/OPTIONS https://localhost.weixin.qq.com:14013/14014/14015/api/check-login
#                        https://localhost.weixin.qq.com:13013/13014/13015/api/check-login
#                                                                               [net::ERR_CONNECTION_REFUSED] ×12
#   reqid=66 GET  https://support.weixin.qq.com/...&action=connect_qrconnect_checkLogin_fail
#   reqid=67 GET  https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid=<uuid>   [200]
#   reqid=69 GET  同上                                                            [pending] ← 还在查手机扫没扫
#
# localhost.weixin.qq.com:130xx/140xx 是 **PC 微信客户端**在本机监听的端口，登录页用它做
# fastLogin/checkLogin。WSL、服务器、容器里都没有 PC 微信，12 个探测请求全部 connection
# refused，页面于是走 checkLogin 失败分支，把**已经显示出来的**二维码换成「加载失败，点击重试」
# —— 这就是「本来有码、后来没了」的全部机理。跟 take_screenshot / 「检测拍照」无关：
# 只用 evaluate 读 innerText、一次截图都不做，也复现过；同样零截图的另一轮里码 14s 一直在。
# 顺带解释了为什么连点三次「点击重试」必然修不好：它重跑的是同一条 checkLogin。
#
# 更值钱的一条：iframe 里那张码就是 https://open.weixin.qq.com/connect/qrcode/<uuid> 的图片本体。
# 实测不带 cookie 直接 GET → `200 image/jpeg 46961B`，OpenCV 解出来是
#   https://open.weixin.qq.com/connect/confirm?uuid=<uuid>
# 与页面长轮询 lp.open.weixin.qq.com/connect/l/qrconnect?uuid=<uuid> 的 uuid 完全一致。
# 所以**不必截图**：从网络记录里抓 uuid → 下载原图 → 推给手机。470x470 原图比整页截图清楚得多，
# 而且不受 iframe 失败态影响（截图模式下 iframe 一坏，推给手机的就是那个错误框）。


def browser_list_network_requests(page_size: int = 120) -> str:
    """浏览器侧当前页面的网络请求转储（文本）。"""
    return call_tool(
        BROWSER_TOOL_PREFIX + "list_network_requests",
        {"pageSize": page_size},
        format="text",
    )


def _snapshot_text() -> str:
    """页面 A11Y 快照的纯文本（浏览器工具经 Agent Service 返回的是 text/plain）。"""
    result = browser_take_snapshot()
    return result.get("snapshot", "") if isinstance(result, dict) else str(result)


def extract_qrcode_uuids(net_text: str) -> list[str]:
    """按出现顺序取二维码 uuid（去重，最后一个最新）。

    两个来源都认：``connect/qrcode/<uuid>`` 是二维码图本体，
    ``l/qrconnect?uuid=<uuid>`` 是页面查「手机扫了没有」的长轮询，两者 uuid 相同。
    """
    out: list[str] = []
    for m in QR_UUID_RE.finditer(net_text or ""):
        uuid = m.group(1) or m.group(2)
        if uuid and uuid not in out:
            out.append(uuid)
    return out


def diagnose_qrcode_network(net_text: str) -> str:
    """二维码请求没出现时，按网络记录给结论（别再让人猜「被拍照检测了」）。"""
    if not net_text:
        return "网络记录为空：chrome-devtools 可能没连上页面，先跑 --check 看浏览器侧"
    if "api/check-login" in net_text and "ERR_CONNECTION_REFUSED" in net_text:
        return ("页面在探测本机 PC 微信客户端（localhost.weixin.qq.com:130xx/140xx /api/check-login），"
                "全部 ERR_CONNECTION_REFUSED —— 这台机器上没有 PC 微信，页面因此把二维码换成"
                "「加载失败，点击重试」。这不是截图/拍照检测，点「点击重试」也修不好"
                "（它重跑同一条 checkLogin）；靠二维码直链取码才绕得开，本脚本走的就是这条路")
    if "ERR_BLOCKED" in net_text:
        return ("有请求被 chrome-devtools 的 --blocked-url-patterns/--blocked-origins 屏蔽了，"
                "去 Agent Service 的 data/tools.json 里把那条 chrome-devtools 启动参数去掉")
    for tag, why in (
        ("ERR_NAME_NOT_RESOLVED", "DNS 解析失败：跑 chrome-devtools 的那台机器网络有问题"),
        ("ERR_INTERNET_DISCONNECTED", "跑 chrome-devtools 的那台机器没网"),
        ("ERR_TIMED_OUT", "连 open.weixin.qq.com 超时：检查那台机器的代理/防火墙"),
        ("ERR_CERT", "证书校验失败：代理中间人或系统时间不对"),
    ):
        if tag in net_text:
            return why
    return ("网络记录里没有 open.weixin.qq.com/connect/qrconnect：登录 iframe 可能根本没建起来，"
            "看看是不是被登录态/风控跳走了")


def download_qrcode_image(uuid: str, timeout: int = QRCODE_FETCH_TIMEOUT) -> bytes:
    """下载二维码图本体（不经浏览器）。返回 JPEG/PNG 原始字节。

    Raises:
        RuntimeError: 下载失败或拿到的不是图片 —— 调用方据此退回截图路径
    """
    url = QRCODE_IMAGE_BASE + uuid
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://open.weixin.qq.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    last_err = ""
    # 先按环境里的 http_proxy 走（Chrome 也是走它），代理不通再试一次直连
    for label, opener in (("代理", urllib.request.build_opener()),
                          ("直连", urllib.request.build_opener(urllib.request.ProxyHandler({})))):
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                data = resp.read()
        except Exception as e:   # DNS/SSL/代理拒绝/超时，形态太多，统一交给调用方兜底
            last_err = f"{label}失败: {type(e).__name__}: {e}"
            continue
        if len(data) < 1024:
            last_err = f"{label}拿到的内容只有 {len(data)} 字节，不像二维码图"
            continue
        if not (data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG"):
            last_err = f"{label}拿到的不是 JPEG/PNG（前 4 字节 {data[:4]!r}），多半被网关/代理拦了"
            continue
        print(f"   ✅ 二维码原图已下载（{label}）: {url} → {len(data)} 字节")
        return data
    raise RuntimeError(f"下载二维码图 {url} 失败: {last_err}")


def to_png_base64(image_bytes: bytes) -> str:
    """转成 PNG 的 base64。

    关键：android_use_mcp.show_image 把收到的字节原样写成 ``show_image_<ts>.png``，
    直接喂 JPEG 会让相册按 PNG 解析（打不开/黑屏），所以这里必须真转成 PNG。
    """
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    if img.format == "PNG":
        return base64.b64encode(image_bytes).decode("ascii")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def wait_for_qrcode_uuid(
    timeout: int = QR_WAIT_TIMEOUT, interval: float = 2.0, known: tuple = ()
) -> tuple[str, str]:
    """轮询网络记录，直到出现（新的）二维码 uuid。返回 (uuid, 最后一次网络转储)。

    老版本 chrome-devtools-mcp 没有 list_network_requests 时返回 ("", "")，
    让调用方退回截图兜底，而不是把整个流程判死。
    """
    start = time.time()
    last_net = ""
    while time.time() - start < timeout:
        try:
            last_net = browser_list_network_requests()
        except ToolCallTimeout:
            raise                       # 浏览器 MCP 卡住了：这是通信故障，不能当成"没这个工具"
        except ToolCallError as e:
            print(f"   ℹ️  {BROWSER_TOOL_PREFIX}list_network_requests 不可用，只能走截图兜底: {e}")
            return "", ""
        fresh = [u for u in extract_qrcode_uuids(last_net) if u not in known]
        if fresh:
            return fresh[-1], last_net
        time.sleep(interval)
    return "", last_net


def acquire_qrcode_image(login_url: str, known_uuids: tuple = ()) -> tuple[str, str, str]:
    """取到要推给手机的二维码。返回 (png_base64, 来源说明, uuid)。

    主路径：网络记录里的 uuid → 下载二维码原图；兜底：整页截图（老环境没有网络工具时用）。
    """
    for attempt in range(1, QR_PAGE_RETRIES + 1):
        if attempt > 1:
            print(f"   ↻ 第 {attempt}/{QR_PAGE_RETRIES} 次重新打开登录页取码"
                  "（不点「点击重试」：它重跑同一条 checkLogin，必然再失败）")
            browser_navigate(login_url)
            time.sleep(3)
        uuid, last_net = wait_for_qrcode_uuid(known=known_uuids)
        if not uuid:
            if last_net == "":          # 这个浏览器 MCP 没有网络工具，直接走截图兜底
                break
            print(f"   ⚠️  第 {attempt}/{QR_PAGE_RETRIES} 次没等到二维码请求: "
                  f"{diagnose_qrcode_network(last_net)}")
            continue
        try:
            image_bytes = download_qrcode_image(uuid)
        except RuntimeError as e:
            print(f"   ⚠️  第 {attempt}/{QR_PAGE_RETRIES} 次下载二维码原图失败: {e}")
            continue
        return to_png_base64(image_bytes), f"二维码原图 uuid={uuid}", uuid

    print("   ↩ 退回整页截图取码（清晰度会降，iframe 坏的时候截到的就是那个错误框）")
    snap = _snapshot_text()
    if "加载失败" in snap or "点击重试" in snap:
        raise FlowAborted(
            "页面二维码已是「加载失败，点击重试」，而 uuid/直链两条路都没拿到码："
            "这时截图只会把错误框推到手机上去扫。失败原因见上面的网络诊断"
            "（多半是这台机器上没有 PC 微信客户端，页面 checkLogin 失败）"
        )
    screenshot_result = browser_take_screenshot()
    m = re.search(r'"(?:data|screenshot|image)":\s*"([A-Za-z0-9+/=]+)"', str(screenshot_result))
    if not m:
        raise ToolCallError(
            BROWSER_TOOL_PREFIX + "take_screenshot",
            f"截图结果里没有 base64 图片数据（浏览器工具可能没正常工作）: {_short(screenshot_result)}",
            raw=screenshot_result,
        )
    return rotate_image_for_mobile_display(m.group(1), angle=90), "整页截图兜底", ""


def push_qrcode_to_phone(png_base64: str, device_id: str, old_phone_path: str) -> str:
    """把二维码推给显示手机（show_image 自己会亮屏解锁+拉满亮度），顺手删掉上一张。"""
    result = android_show_image(png_base64, device_id)
    new_path = str(result.get("phone_path") or "")
    if old_phone_path and old_phone_path != new_path:
        try:
            android_run_adb(f"shell rm -f {old_phone_path}", device_id, timeout=30)
        except (ToolCallError, AgentServiceError) as e:
            print(f"   ⚠️  删除旧二维码 {old_phone_path} 失败（不影响登录）: {e}")
    return new_path


def wait_for_login_and_keep_qr_fresh(
    login_url: str, display_device_id: str, uuid: str, qrcode_phone_path: str
) -> tuple[bool, str]:
    """等登录成功（页面出现「发表」）；期间二维码换了就重新取码、重新推给手机。

    以前这里是对着快照死等 30s：微信二维码本身会过期刷新，页面也可能中途坏成「加载失败」，
    手机屏幕上仍是那张旧码，扫了服务端也不认 —— 每轮就只能报「扫码流程未完成」。
    """
    deadline = time.time() + LOGIN_WAIT_TIMEOUT
    repaired = 0
    while time.time() < deadline:
        try:
            snap = _snapshot_text()
        except (ToolCallError, AgentServiceError) as e:
            print(f"   ⚠️  读取页面快照失败（继续等）: {e}")
            snap = ""
        if "发表" in snap:
            return True, qrcode_phone_path

        broken = ("加载失败" in snap) or ("点击重试" in snap)
        need_refresh = broken
        reason = "页面二维码已坏成「加载失败」" if broken else ""
        if not need_refresh and uuid:
            try:
                live = extract_qrcode_uuids(browser_list_network_requests())
                if live and live[-1] != uuid:
                    need_refresh = True
                    reason = f"页面换了二维码（{uuid} → {live[-1]}，多半是过期后自动刷新）"
            except ToolCallTimeout:
                raise
            except (ToolCallError, AgentServiceError) as e:
                print(f"   ⚠️  读取网络记录失败（继续等）: {e}")

        if need_refresh:
            if not uuid:
                print(f"   ⚠️  {reason}；但这次是截图取码，换不了码，继续等手机侧")
            elif repaired >= QR_PAGE_RETRIES:
                print(f"   ⚠️  已经换过 {repaired} 次码仍没登录成功，停止换码")
            else:
                repaired += 1
                print(f"   ↻ {reason}，第 {repaired}/{QR_PAGE_RETRIES} 次重新取码并推给手机")
                browser_navigate(login_url)
                time.sleep(3)
                fresh_uuid, last_net = wait_for_qrcode_uuid(known=(uuid,))
                if not fresh_uuid:
                    print(f"   ⚠️  没等到新的二维码: {diagnose_qrcode_network(last_net)}")
                    break
                try:
                    png = to_png_base64(download_qrcode_image(fresh_uuid))
                except RuntimeError as e:
                    print(f"   ⚠️  下载新二维码失败: {e}")
                    break
                qrcode_phone_path = push_qrcode_to_phone(png, display_device_id, qrcode_phone_path)
                uuid = fresh_uuid
                print(f"   ✅ 新二维码已推到手机（uuid={fresh_uuid}）")
        time.sleep(LOGIN_POLL_INTERVAL)
    return False, qrcode_phone_path


def browser_find_and_click(keyword: str) -> dict:
    """
    在页面上查找并点击包含指定文字的元素。
    
    实现逻辑：
    1. 截取页面截图
    2. 调用 MCP-OCR 服务的 find_location 工具定位目标文字
    3. 使用 mcp-chrome-devtools-click_at 点击对应坐标
    """
    
    # 步骤 1: 截取页面截图
    screenshot_result = browser_take_screenshot()
    
    # 提取 base64 图片数据
    _m = re.search(r'"(?:data|screenshot|image)":\s*"([A-Za-z0-9+/=]+)"', str(screenshot_result))
    if not _m:
        raise ToolCallError(
            "mcp-chrome-devtools-take_screenshot",
            f"截图结果里找不到 base64 图片数据: {_short(screenshot_result)}",
            raw=screenshot_result,
        )
    
    screenshot_base64 = _m.group(1)
    
    # 步骤 2: 调用 MCP-OCR find_location 定位目标
    location_result = call_tool(
        "mcp-OCR-find_location",
        {
            "base64_image_big": screenshot_base64,
            "pattern": keyword
        },
        format="json",
        allow_business_failure=True,
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
# 这里的每一个函数都遵循同一条约定：
#   - 通信 / 通道失败（MCP 没启动、端口不通、ADB 命令报错、执行超时）-> 抛异常，流程中断
#   - "目标没找到"这类业务性失败 -> 返回 False，并把 MCP 给出的原因打印出来
# 绝不在没验证返回值的情况下打 ✅。
# ============================================================


def android_run_adb(command: str, device_id: str = None, timeout: int = TOOL_CALL_TIMEOUT) -> dict:
    """执行 ADB 命令（通过 android-use MCP server，非本机命令行）。

    返回 {"success", "stdout", "stderr", "returncode", ...}。
    抛 ToolCallError / AgentServiceError 的情况（= 链路有问题，调用方应中断流程）：
      - call_tool 本身失败：MCP server 没启动、MCP 端口不通、连接已失效、ADB 执行超时等
      - 返回体里没有 stdout 字段：拿到的不是 run_adb_command 的结果
      - returncode 非 0：命令在 MCP host 上真的失败了

    注意：MCP 那边是 `command.split()` 拼参数（accessories/android_use_mcp.py 的
    run_adb_command），引号会被丢掉，所以 `grep 'Display Power'` 这种带空格的模式是坏的。
    本脚本因此避免在 adb 命令里用带引号的 grep，改为取回完整输出后在 Python 侧匹配。
    """
    result = call_tool(
        ANDROID_TOOL_PREFIX + "run_adb_command",
        {"command": command, "device_id": device_id},
        format="json",
        timeout=timeout,
    )
    if not isinstance(result, dict) or "stdout" not in result:
        raise ToolCallError(
            ANDROID_TOOL_PREFIX + "run_adb_command",
            f"返回的不是 ADB 结果结构（缺少 stdout 字段）: {_short(result)}",
            raw=result,
        )
    rc = result.get("returncode")
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if rc not in (0, None):
        raise ToolCallError(
            ANDROID_TOOL_PREFIX + "run_adb_command",
            f"ADB 命令失败（returncode={rc}）| device={device_id} | command={command} | "
            f"stderr={_short(stderr) or '(空)'} | stdout={_short(stdout) or '(空)'}",
            raw=result,
        )
    return result


def adb_stdout(command: str, device_id: str = None, timeout: int = TOOL_CALL_TIMEOUT) -> str:
    """跑一条 adb 命令并返回 stdout（失败会抛异常）。"""
    return str(android_run_adb(command, device_id, timeout=timeout).get("stdout") or "")


def parse_adb_devices(stdout: str) -> tuple[list[str], dict]:
    """解析 adb devices 输出 -> (状态为 device 的序列号列表, {序列号: 状态})。"""
    online, states = [], {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or "List of devices" in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
            if parts[1] == "device":
                online.append(parts[0])
    return online, states


def adb_devices_raw(timeout: int = PREFLIGHT_TIMEOUT, retries: int = 2, delay: float = 2.0) -> dict:
    """跑 `adb devices`；stdout 里没有 "List of devices attached" 时重试几次。

    Windows 上 adb server 未启动时，第一次 `adb devices` 可能只把 daemon 拉起来、
    stdout 是空的 —— 这不是"没有设备"，直接下结论会误导人。
    """
    last = None
    for attempt in range(1, max(1, retries) + 1):
        result = android_run_adb("devices", None, timeout=timeout)
        stdout = str(result.get("stdout") or "")
        if "List of devices attached" in stdout:
            return result
        last = result
        if attempt < retries:
            print(f"   ⚠️  adb devices 输出里没有设备列表标题（第 {attempt} 次），{delay}s 后重试...")
            time.sleep(delay)
    return last


def probe_device(device_id: str, timeout: int = 30) -> tuple[bool, str]:
    """最小往返（adb shell echo ok）：慢或失败说明 MCP→adb→设备 这条链路有问题。"""
    t0 = time.time()
    try:
        out = adb_stdout("shell echo ok", device_id, timeout=timeout)
    except (ToolCallError, AgentServiceError) as e:
        return False, f"{e}（耗时 {time.time() - t0:.1f}s）"
    cost = time.time() - t0
    if "ok" not in out:
        return False, f"shell echo 没回显预期内容: {_short(out) or '(空)'}（耗时 {cost:.1f}s）"
    return True, f"往返 {cost:.1f}s"


def android_show_image(base64_content: str, device_id: str) -> dict:
    """
    在手机上显示图片（自动唤醒屏幕、保持亮屏、最大亮度、打开图片查看器）

    Args:
        base64_content: 图片文件base64
        device_id: 手机设备ID

    Returns:
        show_image 的返回体（含 phone_path / viewer_component）
    """
    result = call_tool(
        ANDROID_TOOL_PREFIX + "show_image",
        {"base64_content": base64_content, "device_id": device_id},
        format="json",
    )
    if not isinstance(result, dict):
        raise ToolCallError(
            ANDROID_TOOL_PREFIX + "show_image",
            f"返回结构异常: {_short(result)}",
            raw=result,
        )
    return result


def android_find_and_click(
    keyword: str = None,
    device_id: str = None,
    image_file_base64: str = None,
    timeout: int = TOOL_CALL_TIMEOUT,
) -> bool:
    """
    在手机屏幕上查找并点击指定文字 / 图片。

    Returns:
        True  = 已点击到目标
        False = 没找到目标（MCP 返回的具体原因会打印出来，例如截图失败、OCR 未匹配）

    Raises:
        ToolCallError / AgentServiceError: MCP 通道失败（未启动、端口不通、超时等）
    """
    arguments: dict = {"device_id": device_id}
    if image_file_base64:
        arguments["image_file_base64"] = image_file_base64
    if keyword:
        arguments["keyword"] = keyword
    target = f"图片({len(image_file_base64)} 字符 base64)" if image_file_base64 else f"'{keyword}'"

    result = call_tool(
        ANDROID_TOOL_PREFIX + "find_and_click",
        arguments,
        format="json",
        timeout=timeout,
        allow_business_failure=True,  # "没找到目标" 是业务性失败，不是链路失败
    )
    if isinstance(result, dict) and result.get("success") is False:
        print(f"   ⚠️  没找到/没点到目标 {target}: {result.get('message') or _short(result)}")
        return False
    coord = (result or {}).get("coordinates") or {}
    print(f"   ℹ️  已点击 {target} @ ({coord.get('x')}, {coord.get('y')})")
    return True


def android_find_image_and_click(image_file: str, device_id: str) -> bool:
    """在手机屏幕上查找并点击包含指定图片的元素"""
    if not os.path.isfile(image_file):
        print(f"   ⚠️  模板图片不存在: {image_file}")
        return False
    with open(image_file, "rb") as f:
        image_base64 = base64.b64encode(f.read()).decode("ascii")
    return android_find_and_click(device_id=device_id, image_file_base64=image_base64)


def _screen_power_state(device_id: str) -> str:
    """返回 'ON' / 'OFF' / 'UNKNOWN'（不用 grep，见 android_run_adb 的说明）。"""
    out = adb_stdout("shell dumpsys power", device_id)
    if not out.strip():
        raise ToolCallError(
            ANDROID_TOOL_PREFIX + "run_adb_command",
            f"dumpsys power 没有任何输出（设备 {device_id} 可能不在线 / MCP host 的 adb 状态异常）",
        )
    m = re.search(r"Display Power:\s*state=(\w+)", out)
    if m:
        return m.group(1).upper()
    m = re.search(r"mWakefulness=(\w+)", out)
    if m:
        return "ON" if m.group(1).lower() == "awake" else "OFF"
    return "UNKNOWN"


def _keyguard_showing(device_id: str) -> bool:
    """锁屏界面是否显示中（同样在 Python 侧匹配，避免 adb 命令里的 grep 引号问题）。"""
    out = adb_stdout("shell dumpsys window policy", device_id)
    if not out.strip():
        raise ToolCallError(
            ANDROID_TOOL_PREFIX + "run_adb_command",
            f"dumpsys window policy 没有任何输出（设备 {device_id} 可能不在线）",
        )
    return (
        "showing=true" in out
        or "isStatusBarKeyguard=true" in out
        or "mShowingLockscreen=true" in out
    )


def android_wakeup_and_unlock(device_id: str, pin: str = None) -> bool:
    """
    唤醒屏幕并解锁设备，适用于各种状态（息屏、亮屏锁屏、亮屏已解锁）

    Args:
        device_id: 手机设备ID
        pin: 锁屏PIN码（如果有），None表示没有PIN码或仅滑动解锁

    Returns:
        是否成功解锁（只在"确实解不开锁"时返回 False；链路问题一律抛异常）
    """
    print("   检测屏幕状态...")

    # 1. 检查屏幕是否亮着
    state = _screen_power_state(device_id)
    if state == "OFF":
        print("   屏幕已熄灭，正在唤醒...")
        android_run_adb("shell input keyevent KEYCODE_WAKEUP", device_id)
        time.sleep(0.5)
        state = _screen_power_state(device_id)
        if state == "OFF":
            print(f"   ❌ 发送 KEYCODE_WAKEUP 后屏幕仍是 OFF（设备 {device_id}）")
            return False
    elif state == "UNKNOWN":
        print("   ⚠️  dumpsys power 里没找到屏幕状态字段，按已点亮处理并继续检测锁屏")
    else:
        print("   屏幕已点亮")

    # 2. 检查是否需要解锁（检查锁屏状态）
    if _keyguard_showing(device_id):
        print("   屏幕已锁定，正在解锁...")

        # 先滑动解锁（覆盖大多数情况）
        android_run_adb("shell input swipe 540 1800 540 800", device_id)
        time.sleep(0.5)

        # 如果有PIN码，输入PIN码
        if pin:
            print("   输入PIN码...")
            # 输入PIN码数字
            for digit in pin:
                android_run_adb(f"shell input keyevent KEYCODE_{digit}", device_id)
                time.sleep(0.1)
            # 确认PIN码（按回车）
            android_run_adb("shell input keyevent KEYCODE_ENTER", device_id)
            time.sleep(0.5)

        # 再次检查是否解锁成功
        if _keyguard_showing(device_id):
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


def restore_phone(label: str, device_id: str, close_app: str, delete_path: str = "") -> bool:
    """把手机恢复到初始状态（关 APP、回桌面、恢复亮度、息屏）。

    清理阶段不再假装成功：哪一步失败就打 ⚠️ 并带上 MCP 的真实原因。
    """
    ok = True
    steps = []
    if delete_path:
        steps.append((f"删除传输到相册的图片 {delete_path}", f"shell rm -f {delete_path}"))
    steps += [
        (f"关闭 {close_app}", f"shell am force-stop {close_app}"),
        ("回到桌面", "shell input keyevent KEYCODE_HOME"),
        ("取消屏幕常亮", "shell svc power stayon false"),
        ("恢复亮度", "shell settings put system screen_brightness 128"),
        ("息屏", "shell input keyevent KEYCODE_SLEEP"),
    ]
    for desc, cmd in steps:
        try:
            android_run_adb(cmd, device_id)
        except (ToolCallError, AgentServiceError) as e:
            ok = False
            print(f"   ⚠️  {label}: {desc} 失败: {e}")
    return ok


def restore_phone_best_effort(label: str, device_id: str, close_app: str, delete_path: str = "") -> None:
    """流程中途失败时的补救清理：先探活，避免 MCP 已断时十几条命令各卡 30s。"""
    print(f"\n[清理] 尝试把 {label} 恢复到初始状态...")
    reachable, info = probe_device(device_id, timeout=30)
    if not reachable:
        print(f"   ⚠️  {label}: MCP/ADB 仍不可达，跳过清理（手机可能停在二维码/微信界面）。")
        print(f"       链路恢复后运行: python {os.path.basename(__file__)} --cleanup")
        print(f"       探活失败原因: {info}")
        return
    if restore_phone(label, device_id, close_app, delete_path=delete_path):
        print(f"   ✅ {label} 已恢复到初始状态（息屏）")


def cleanup_phones(
    display_phone: str, display_device_id: str, display_close_app: str,
    scan_phone: str, scan_device_id: str, scan_close_app: str,
    qrcode_path: str = "",
) -> None:
    """步骤8：把两台手机恢复到初始状态（正常结束和业务性中途放弃都要走到这里）。"""
    print("\n[清理] 将手机恢复到初始状态...")

    print(f"   {display_phone}：删除之前传输到相册的图片 + 关闭图片预览 APP...")
    if restore_phone(display_phone, display_device_id, display_close_app, qrcode_path):
        print(f"   ✅ {display_phone} 已恢复到初始状态（息屏）")
    else:
        print(f"   ⚠️  {display_phone} 清理未全部成功（见上面的失败原因）")

    print(f"   {scan_phone}：关闭微信...")
    if restore_phone(scan_phone, scan_device_id, scan_close_app):
        print(f"   ✅ {scan_phone} 已恢复到初始状态（息屏）")
    else:
        print(f"   ⚠️  {scan_phone} 清理未全部成功（见上面的失败原因）")

    print("   🔄 所有手机已恢复到初始状态，等待下一次执行")


# ============================================================
# 启动预检
# ============================================================

# 启动时必需的 android-use MCP 工具
REQUIRED_ANDROID_TOOLS = {
    ANDROID_TOOL_PREFIX + name
    for name in ("run_adb_command", "show_image", "find_and_click")
}

# 启动时必须的浏览器 MCP 工具：步骤1 导航、步骤3 截图、步骤6 读页面都要用。
# 缺任何一个都在预检阶段就报出来，而不是等到步骤1 卡 90s 再看运气。
REQUIRED_BROWSER_TOOLS = {
    BROWSER_TOOL_PREFIX + name
    for name in ("navigate_page", "take_snapshot", "take_screenshot")
}

# 预检时按顺序挑第一个已注册的、代价最小的浏览器工具做往返探测
BROWSER_PROBE_TOOLS = (
    BROWSER_TOOL_PREFIX + "list_pages",
    BROWSER_TOOL_PREFIX + "new_page",
    BROWSER_TOOL_PREFIX + "take_snapshot",
)

# 本次运行里是否真的收到过 Agent Service 的响应。诊断时靠这个事实区分
# "Agent Service 没启动" 和 "服务在跑、但它背后的 MCP server 卡住"。
_service_alive = False

_preflight_passed = False
_browser_preflight_passed = False


def _preflight_fail(strict: bool) -> bool:
    if strict:
        sys.exit(1)
    return False


def check_android_mcp(strict: bool = True, reuse: bool = False) -> bool:
    """
    预检，按"哪一段链路断了"分层定位，不再一律报"没检测到设备"：

      1) Agent Service 可达（GET /v1/tools）
      2) android-use 工具在注册表里（注意这只是上次运行落盘的快照）
      3) 真的调用一次 adb devices —— 只有这一步成功才说明 MCP server 活着
      4) adb 确实列出了设备，且配置里的 device_id 都在，且每台手机能回显 echo

    strict=True：失败直接 sys.exit(1)（命令行入口）
    strict=False：失败返回 False（被 login_video_account_by_scan 复用）
    """
    global _preflight_passed
    if reuse and _preflight_passed:
        return True

    # ---- 1) Agent Service 可达 + 2) 工具已注册 ----
    try:
        with urllib.request.urlopen(TOOLS_API_URL, timeout=10) as resp:
            tools_data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"错误: 无法连接 Agent Service（{AGENT_SERVICE_BASE}）: {e}")
        print("      请先启动 Agent Service，并确保 android-use MCP server 已注册。")
        return _preflight_fail(strict)

    tool_ids = {t.get("tool_id") for t in tools_data.get("tools", []) if isinstance(t, dict)}
    android_tool_ids = sorted(t for t in tool_ids if t and t.startswith(ANDROID_TOOL_PREFIX))
    if not android_tool_ids:
        print(f"错误: Agent Service 上未注册 {MCP_SERVER_NAME} MCP server 的任何工具。")
        print("      请检查 MCP server 配置并重新加载（POST /v1/tools/mcp）。")
        _print_mcp_server_config()
        return _preflight_fail(strict)

    missing = REQUIRED_ANDROID_TOOLS - tool_ids
    if missing:
        print(f"错误: {MCP_SERVER_NAME} MCP 缺少必需工具: {sorted(missing)}")
        print(f"      当前已注册的工具: {android_tool_ids}")
        return _preflight_fail(strict)

    print(f"✅ Agent Service 可达，{MCP_SERVER_NAME} 工具已注册（{len(android_tool_ids)} 个）")
    print("   ℹ️  工具列表是上次运行落盘的注册表快照，"
          "所以接下来必须真的调用一次 adb 才能确认 MCP server 现在可用")

    # ---- 3) 真实调用 adb devices ----
    print(f"   检测 MCP → ADB 链路（{ANDROID_TOOL_PREFIX}run_adb_command devices）...")
    t0 = time.time()
    try:
        devices_result = adb_devices_raw(timeout=PREFLIGHT_TIMEOUT)
    except ToolCallError as e:
        print("")
        print(f"错误: 调用 {MCP_SERVER_NAME} 的 run_adb_command 失败（耗时 {time.time() - t0:.1f}s）")
        print(f"      {e}")
        print_diagnosis(e)
        return _preflight_fail(strict)
    except AgentServiceError as e:
        print("")
        print(f"错误: 调用 Agent Service 工具失败（耗时 {time.time() - t0:.1f}s）: {e}")
        print_diagnosis(e)
        return _preflight_fail(strict)

    stdout = str(devices_result.get("stdout") or "")
    stderr = str(devices_result.get("stderr") or "")
    print(f"   adb devices 往返 {time.time() - t0:.1f}s，returncode={devices_result.get('returncode')}")

    # stdout 里没有设备列表标题：这是最容易误报的分支，明确区别于"没有设备"
    if "List of devices attached" not in stdout:
        print("")
        print("错误: adb devices 没有返回设备列表（缺少 'List of devices attached'）。")
        print(f"      stdout: {_short(stdout) or '(空)'}")
        print(f"      stderr: {_short(stderr) or '(空)'}")
        print("      这不是“没有已连接设备”，而是 MCP host 上 adb server 自身状态异常，例如首次调用时")
        print("      只拉起了 daemon（Windows 常见，stdout 为空），或 MCP server 把失败写到了别的字段。")
        print("      建议先在 MCP host 上手工跑一次 adb devices 确认，再重跑本脚本。")
        return _preflight_fail(strict)

    online, states = parse_adb_devices(stdout)
    if not online:
        if states:
            print(f"错误: adb 列出了设备，但状态都不是 device: {states}")
            print("      unauthorized = 手机上没点“允许 USB 调试”；offline = adb 掉线（可 adb kill-server 后重试）。")
        else:
            print("错误: adb 返回了空的设备列表 —— MCP host 上确实没有已连接的 Android 设备。")
            print("      注意：这里的 adb 跑在 MCP server 那台机器上，本机命令行 adb devices 的结果不代表它。")
        print(f"      原始输出: {_short(stdout)}")
        return _preflight_fail(strict)

    print(f"✅ 检测到 {len(online)} 台设备: {online}")

    # ---- 4) 对比 PHONES 配置 + 每台手机做一次 echo 探活 ----
    configured = {name: p["device_id"] for name, p in PHONES.items()}   # 型号 -> serial
    missing_cfg = {n: d for n, d in configured.items() if d not in online}
    if missing_cfg:
        print(f"⚠️  以下 PHONES 中配置的设备未在 MCP host 的 adb 列表里: {missing_cfg}")
        print(f"    当前在线设备: {online}（状态: {states}）")
        print("    请更新 PHONES 配置或连接对应设备后重试。")
        return _preflight_fail(strict)
    print("✅ PHONES 配置的全部设备均在 MCP host 的 adb 列表里")

    dead = []
    for name, device_id in configured.items():
        ok, info = probe_device(device_id)
        if ok:
            print(f"✅ {name} ({device_id}) 可用，{info}")
        else:
            print(f"⚠️  {name} ({device_id}) 探活失败: {info}")
            dead.append(name)
    if dead:
        detail = {n: configured.get(n) for n in dead}
        print("")
        print(f"错误: 设备 {detail} 在 adb 列表里，但按 serial 执行命令跑不通。")
        print("      按上面的原始报错对号入座：")
        print("        · adb.exe: device 'X' not found  -> 传给 adb -s 的不是设备真实 serial（PHONES 配置写错）")
        print("        · unauthorized / offline         -> 手机上点“允许 USB 调试”，或 adb kill-server 后重连")
        print("        · 超时 / daemon 无响应            -> MCP host 到设备的通道卡住；这种情况后面每一步都会卡 30s")
        return _preflight_fail(strict)

    _preflight_passed = True
    return True


def check_browser_mcp(strict: bool = True, reuse: bool = False) -> bool:
    """
    预检浏览器那一侧（步骤1 打开页面、步骤3 截图、步骤6 读页面都走 chrome-devtools MCP）。

    android-use 的预检全绿并不代表 Chrome 那侧能用 —— 上一次就是 6 项 android 检测都过，
    然后卡死在步骤1。这里同样分三层，失败时能说清断在哪一段：

      1) chrome-devtools 工具在注册表里（注意这也只是上次落盘的快照，不代表进程活着）
      2) 真的调一次最便宜的浏览器工具：只有这一步才能暴露"stdio MCP server 空闲 300s 被
         回收后重新拉起失败 / Chrome 起不来 / npx 冷启动卡住"这类只有真调用才暴露的问题
      3) navigate_page 打开 about:blank：确认 Chrome 不只是进程在，而是真能驱动一个页面
         （这一步不访问外网，所以它失败与登录网站通不通无关）
    """
    global _browser_preflight_passed
    if reuse and _browser_preflight_passed:
        return True

    try:
        with urllib.request.urlopen(TOOLS_API_URL, timeout=10) as resp:
            tools_data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"错误: 读取工具列表失败（{TOOLS_API_URL}）: {e}")
        return _preflight_fail(strict)

    tool_ids = {t.get("tool_id") for t in tools_data.get("tools", []) if isinstance(t, dict)}
    browser_tool_ids = sorted(t for t in tool_ids if t and t.startswith(BROWSER_TOOL_PREFIX))
    if not browser_tool_ids:
        print(f"错误: Agent Service 上未注册 {BROWSER_SERVER_NAME} MCP server 的任何工具，"
              "步骤1（打开登录页面）必然失败。")
        _print_mcp_server_config()
        return _preflight_fail(strict)

    missing = REQUIRED_BROWSER_TOOLS - tool_ids
    if missing:
        print(f"错误: {BROWSER_SERVER_NAME} MCP 缺少必需工具: {sorted(missing)}")
        print(f"      当前已注册的浏览器工具: {browser_tool_ids}")
        return _preflight_fail(strict)

    # list_network_requests 不是硬依赖（老版本 chrome-devtools-mcp 没有它），但缺了就只能
    # 走整页截图取码：码小且糊，iframe 一坏还会把「加载失败」截给手机，所以先说在前面
    if BROWSER_TOOL_PREFIX + "list_network_requests" not in tool_ids:
        print(f"ℹ️  未注册 {BROWSER_TOOL_PREFIX}list_network_requests：取码只能走整页截图兜底"
              "（二维码直链取码依赖它，新版 chrome-devtools-mcp 才有）")

    print(f"✅ {BROWSER_SERVER_NAME} 工具已注册（{len(browser_tool_ids)} 个）")
    _print_mcp_server_config()

    # ---- 2) 便宜的往返探测。第一次调用可能要把被回收的 stdio MCP server 重新拉起，慢是正常现象 ----
    probe_id = next((t for t in BROWSER_PROBE_TOOLS if t in tool_ids), None)
    if probe_id is None:
        print(f"   ⚠️  没有可用于探测的浏览器工具（试过 {list(BROWSER_PROBE_TOOLS)}），跳过探测")
        _browser_preflight_passed = True
        return True

    print(f"   检测 MCP → Chrome 链路（{probe_id}，最多等 {BROWSER_PROBE_TIMEOUT}s，冷启动会更久）...")
    t0 = time.time()
    probe_args = {} if probe_id.endswith("list_pages") else {"url": "about:blank"}
    try:
        probe = call_tool(probe_id, probe_args, format="text", timeout=BROWSER_PROBE_TIMEOUT)
    except (ToolCallError, AgentServiceError) as e:
        print("")
        print(f"错误: 调用浏览器工具 {probe_id} 失败（耗时 {time.time() - t0:.1f}s）")
        print(f"      {e}")
        print(f"      判断: android-use 那边是通的（前面 adb 往返都正常），所以问题只出在 {BROWSER_SERVER_NAME}")
        print(            "            MCP server / Chrome 这一侧 —— 与手机、USB 调试、Agent Service 是否启动都无关。")
        print_browser_checklist()
        return _preflight_fail(strict)
    print(f"   {probe_id} 往返 {time.time() - t0:.1f}s：{_short(probe, 100) or '(空)'}")

    # ---- 3) 真开一个页面（about:blank，不碰外网）：确认 Chrome 能驱动页面，而不只是进程在 ----
    nav_id = BROWSER_TOOL_PREFIX + "navigate_page"
    t0 = time.time()
    try:
        call_tool(nav_id, {"type": "url", "url": "about:blank"},
                  format="text", timeout=BROWSER_PROBE_TIMEOUT)
    except (ToolCallError, AgentServiceError) as e:
        print("")
        print(f"错误: {BROWSER_SERVER_NAME} 连上了，但打不开页面（{nav_id}，耗时 {time.time() - t0:.1f}s）")
        print(f"      {e}")
        print(            "      多半是 Chrome 进程起不来：root 下缺 --no-sandbox、WSL 里没装 Chrome、或缺共享库/显示环境。")
        print_browser_checklist()
        return _preflight_fail(strict)
    print(f"   navigate_page(about:blank) 往返 {time.time() - t0:.1f}s")

    if (BROWSER_TOOL_PREFIX + "click_at") not in tool_ids or "mcp-OCR-find_location" not in tool_ids:
        print("   ℹ️  未注册 chrome-devtools-click_at 或 mcp-OCR-find_location：默认流程用不到；")
        print(           "      但若需要“在网页上按文字点按钮”（browser_find_and_click，步骤2 那种）会失败。")

    _browser_preflight_passed = True
    return True


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

    Raises:
        ToolCallError / AgentServiceError: 连续 3 次读不到快照（浏览器工具/服务有问题）
    """
    start_time = time.time()
    consecutive_errors = 0
    while time.time() - start_time < timeout:
        try:
            result = browser_take_snapshot()
            snapshot_text = result.get("snapshot", "") if isinstance(result, dict) else str(result)
            if keyword in snapshot_text:
                return True
            consecutive_errors = 0
        except (ToolCallError, AgentServiceError) as e:
            consecutive_errors += 1
            print(f"   ⚠️  读取页面快照失败（连续第 {consecutive_errors} 次）: {e}")
            if consecutive_errors >= 3:
                raise
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
    3. 取二维码：优先用网络记录里的 uuid 直接下载二维码原图，整页截图只作兜底
    4. 将二维码发送到手机A全屏显示
    5. 在手机B上打开微信扫一扫
    6. 扫描手机A上的二维码
    7. 等待登录成功

    整个流程的任何一次 Android/浏览器工具失败都会立即中断：
    以前这些返回值全被忽略，MCP 已经断了还会把 7 步的 ✅ 打完，最后一句"登录超时"收场。

    Args:
        display_phone: 显示二维码的手机型号（默认 MI5X）
        scan_phone: 扫码的手机型号（默认 Redmi）
        login_url: 登录页面地址

    Returns:
        是否登录成功
    """
    # 入口自检：手机侧（android-use）和浏览器侧（chrome-devtools）都是硬依赖，任何一侧不通
    # 就别往下走 —— 否则会在步骤1 卡 90s，看起来像"网页打不开"，实际是浏览器 MCP 不可用
    if not (check_android_mcp(strict=False, reuse=True)
            and check_browser_mcp(strict=False, reuse=True)):
        print("\n❌ 预检未通过，登录流程未执行（原因见上面的分层判断）。")
        return False

    try:
        return _login_flow(display_phone, scan_phone, login_url)
    except (ToolCallError, AgentServiceError) as e:
        print("\n" + "=" * 60)
        print("❌ 流程中断：工具调用失败，已停止后续步骤")
        print(f"   {e}")
        print_diagnosis(e)
        print("=" * 60)
        return False


def _login_flow(
    display_phone: str,
    scan_phone: str,
    login_url: str,
) -> bool:
    """扫码登录的 7 个步骤；失败一律以异常抛出，由 login_video_account_by_scan 统一收口。"""
    print("=" * 60)
    print("视频号创作者平台 - 扫码登录自动化")
    print("=" * 60)

    # 获取设备ID和PIN码
    display_device_id = get_device_id_by_model(display_phone)
    scan_device_id = get_device_id_by_model(scan_phone)
    display_pin = get_device_pin_by_model(display_phone)
    scan_pin = get_device_pin_by_model(scan_phone)
    display_close_app = PHONES[display_phone].get("close_app", "com.miui.gallery")
    scan_close_app = PHONES[scan_phone].get("close_app", "com.tencent.mm")

    print(f"\n📱 显示二维码手机: {display_phone} ({display_device_id})")
    print(f"📱 扫码手机: {scan_phone} ({scan_device_id})")

    qrcode_phone_path = ""

    try:
        # --------------------------------------------------------
        # 步骤1：打开登录页面
        # --------------------------------------------------------
        print("\n[步骤1/7] 打开登录页面...")
        # 先记下导航**之前**网络记录里已有的二维码 uuid：chrome-devtools 的请求日志不一定
        # 每次导航都清空，不清掉这批就会把上一轮的旧码当这一轮的（旧码扫了服务端必然不认）
        try:
            known_uuids = tuple(extract_qrcode_uuids(browser_list_network_requests()))
        except (ToolCallError, AgentServiceError):
            known_uuids = ()
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
        # # 获取页面快照，看有哪些登录方式
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
        # 步骤3：取二维码（优先直链下载原图，整页截图只作兜底）
        # --------------------------------------------------------
        print("[步骤3/7] 获取登录二维码...")
        qrcode_base64, qr_source, qr_uuid = acquire_qrcode_image(login_url, known_uuids)
        print(f"   ✅ 二维码已获取（来源: {qr_source}）")

        # --------------------------------------------------------
        # 步骤4：唤醒屏幕并显示二维码到手机A
        # --------------------------------------------------------
        print(f"[步骤4/7] 唤醒 {display_phone} 屏幕...")
        if not android_wakeup_and_unlock(display_device_id, display_pin):
            raise FlowAborted(f"{display_phone} 屏幕解锁失败，请检查PIN码配置或手动解锁后重试")

        print(f"   把二维码发送到 {display_phone} 显示（来源: {qr_source}）...")
        show_image_result = android_show_image(qrcode_base64, display_device_id)
        # 保存返回的文件路径，用于后续清理
        qrcode_phone_path = str(show_image_result.get("phone_path") or "")
        time.sleep(2)
        if qrcode_phone_path:
            print(f"   ✅ 二维码已在 {display_phone} 上全屏显示（最大亮度）: {qrcode_phone_path}")
        else:
            print(f"   ⚠️  show_image 没返回手机上的图片路径，清理阶段会跳过删除: {_short(show_image_result)}")

        # --------------------------------------------------------
        # 步骤5：唤醒手机B并打开微信
        # --------------------------------------------------------
        print(f"[步骤5/7] 唤醒 {scan_phone} 屏幕...")
        if not android_wakeup_and_unlock(scan_device_id, scan_pin):
            raise FlowAborted(f"{scan_phone} 屏幕解锁失败，请检查PIN码配置或手动解锁后重试")

        print("   打开微信...")
        android_run_adb(
            "shell am start -n com.tencent.mm/.ui.LauncherUI",
            scan_device_id,
        )
        time.sleep(5)  # 等待微信启动

        # --------------------------------------------------------
        # 步骤6：点击"+"按钮，然后点击"扫一扫"
        # --------------------------------------------------------
        print("[步骤6/7] 点击微信右上角 '+' 按钮...")
        wechat_button_png = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "wechat_button.png")
        )
        if not android_find_image_and_click(wechat_button_png, scan_device_id):
            raise FlowAborted(f"没找到微信右上角 '+' 按钮（模板图: {wechat_button_png}），微信界面可能不是预期状态")
        time.sleep(1)

        print("   点击 '扫一扫'...")
        if not android_find_and_click("扫一扫", scan_device_id):
            raise FlowAborted("没找到 '扫一扫'，微信“+”菜单可能没展开（上面已打印 MCP 返回的失败原因）")
        time.sleep(2)

        # "轻触照亮"是可选步骤：没有这个按钮很正常。但通信失败必须中止 ——
        # 以前这里是 except Exception: pass，MCP 断了也会照样往下打 ✅。
        try:
            lit = android_find_and_click("轻触照亮", scan_device_id)
        except (ToolCallError, AgentServiceError) as e:
            print(f"   ❌ 与 {MCP_SERVER_NAME} MCP 的通信失败，无法继续: {e}")
            raise
        if lit:
            print("   ✅ 点击 '轻触照亮' 成功")
            time.sleep(2)
        else:
            print("   ℹ️  没有 '轻触照亮' 按钮（多数机型没有），继续")

        # 此时手机B的相机已经打开，对准手机A的屏幕
        # 微信扫一扫会自动放大图片并识别二维码，无需手动拍照
        print("   ✅ 扫一扫已启动，对准手机A屏幕，等待自动识别二维码...")

        # --------------------------------------------------------
        # 步骤7：等待登录成功
        # --------------------------------------------------------
        print("[步骤7/7] 等待登录成功...")

        # 在手机B上确认扫码（如果有确认按钮的话）
        time.sleep(5)
        confirmed = False
        for attempt in range(3):
            for target in ("^登录$", "允许"):
                if android_find_and_click(target, scan_device_id):
                    print(f"   ✅ 已在手机上确认登录（点击了 '{target}'）")
                    confirmed = True
                    break
            if confirmed:
                break
            print(f"   ⏳ 第 {attempt + 1}/3 次没找到 '登录' / '允许' 按钮，稍后重试...")
            time.sleep(5)
        if not confirmed:
            print("   ⚠️  始终没找到确认按钮：可能扫码还没完成、二维码过期，或界面文案变了")

        # 等待浏览器页面跳转（登录成功后会跳转到后台页面）
        print("   等待页面跳转（期间二维码若刷新会自动重推）...")
        login_success, qrcode_phone_path = wait_for_login_and_keep_qr_fresh(
            login_url, display_device_id, qr_uuid, qrcode_phone_path
        )
    except FlowAborted as e:
        # 业务性放弃：MCP 链路是通的，正常复位两台手机
        print(f"\n❌ 流程未完成: {e}")
        cleanup_phones(
            display_phone, display_device_id, display_close_app,
            scan_phone, scan_device_id, scan_close_app, qrcode_phone_path,
        )
        return False
    except (ToolCallError, AgentServiceError):
        # 中途失败：手机大概率还停在二维码/微信界面。尽力复位，但绝不因此再卡十几轮 30s。
        print("\n❌ 工具调用失败，停止后续步骤并尝试复位手机（原始错误见下面的诊断）")
        restore_phone_best_effort(display_phone, display_device_id, display_close_app, qrcode_phone_path)
        restore_phone_best_effort(scan_phone, scan_device_id, scan_close_app)
        raise

    # --------------------------------------------------------
    # 步骤8：清理 - 将两台手机恢复到初始状态
    # --------------------------------------------------------
    cleanup_phones(
        display_phone, display_device_id, display_close_app,
        scan_phone, scan_device_id, scan_close_app, qrcode_phone_path,
    )

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


def cleanup_all_phones() -> bool:
    """--cleanup：只把手机恢复到初始状态（流程中途失败、手机停在二维码/微信界面时用）。"""
    ok = True
    for name, cfg in PHONES.items():
        device_id = cfg["device_id"]
        close_app = cfg.get("close_app", "com.miui.gallery" if cfg.get("role") == "display" else "com.tencent.mm")
        reachable, info = probe_device(device_id, timeout=PREFLIGHT_TIMEOUT)
        if not reachable:
            print(f"⚠️  {name} ({device_id}) 不可达，跳过清理: {info}")
            ok = False
            continue
        delete_path = PHONE_QRCODE_GLOB if cfg.get("role") == "display" else ""
        if not restore_phone(name, device_id, close_app, delete_path=delete_path):
            ok = False
    return ok


if __name__ == "__main__":
    flags = set(sys.argv[1:])
    unknown = flags - {"--check", "--cleanup"}
    if unknown:
        print(f"用法: python {os.path.basename(__file__)} [--check|--cleanup]")
        sys.exit(2)

    if "--cleanup" in flags:
        sys.exit(0 if cleanup_all_phones() else 1)

    check_android_mcp(strict=True)
    check_browser_mcp(strict=True)

    if "--check" in flags:
        print("✅ 预检通过：Agent Service 可达、android-use→ADB 链路可用、配置的手机都能响应命令，")
        print(          "   chrome-devtools→Chrome 也真的打开了页面")
        sys.exit(0)

    success = login_video_account_by_scan()
    sys.exit(0 if success else 1)

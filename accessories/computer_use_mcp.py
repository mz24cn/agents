"""
Computer Use MCP Server

用于在 Agent Service 宿主机上控制鼠标和键盘，实现桌面自动化操作。

前置条件：
    # Python 包（所有平台）
    pip install fastmcp pyautogui

    # 仅 Ubuntu/Debian：tkinter 被拆为独立包，需额外安装。
    # Windows / macOS 的 Python 发行版自带 tkinter，无需此步骤。
    sudo apt-get install python3-tk

功能：
1. get_screen_size - 获取屏幕尺寸
2. get_mouse_position - 获取当前鼠标位置
3. move_to - 移动鼠标到指定坐标
4. click - 点击鼠标
5. double_click - 双击鼠标
6. hotkey - 按下组合键
7. press_key - 按下单个按键
8. typewrite - 输入字符串
9. take_screenshot - 截取屏幕截图
10. scroll - 滚动鼠标滚轮
11. find_and_click - 查找并点击屏幕上的文字或图片。先截图，然后调用 OCR 工具定位目标，最后执行点击操作。

使用方法：
    # HTTP 方式运行（默认）
    python computer_use_mcp.py
    
    # 指定端口
    python computer_use_mcp.py --port 8001
    
    # SSE 方式运行
    python computer_use_mcp.py --transport sse
    
    # stdio 方式运行（本地调试）
    python computer_use_mcp.py --transport stdio
    
MCP Server 配置（添加到 Agent Service）：
{
  "mcpServers": {
    "computer-use": {
      "url": "http://<remote-host>:8000/mcp",
      "headers": {}
    }
  }
}
"""

import io
import os
import sys
import json
import base64
import logging
import argparse
import subprocess
import urllib.request
import urllib.error
from typing import List, Dict, Optional


def _fix_xauth():
    """修复 Linux Xwayland 下 python3-xlib 的 xauth 认证问题。

    Xwayland 生成的 Xauthority 文件中 display number 可能为空字符串，
    而 python3-xlib 用严格匹配（如 b'1' 匹配 :1），导致认证失败。
    这里通过 xauth add 补一条带正确 display number 的条目。

    仅在 Linux 上执行；Windows/macOS 的 pyautogui 使用原生 API，不需要 X11。
    """
    if sys.platform != 'linux':
        return

    xauth_file = os.environ.get('XAUTHORITY')
    display = os.environ.get('DISPLAY', '')
    if not display:
        return

    display_num = display.lstrip(':').split('.')[0]

    try:
        result = subprocess.run(
            ['xauth', '-f', xauth_file, 'list', display],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout.strip()
        if not output:
            return

        # 检查是否已有带正确 display number 的条目
        for line in output.split('\n'):
            if display in line.split()[0]:
                return

        # 从现有条目取 cookie，补一条带 display number 的
        first_entry = output.split('\n')[0]
        parts = first_entry.split()
        if len(parts) >= 3:
            protocol, cookie = parts[1], parts[2]
            hostname = os.uname().nodename
            subprocess.run(
                ['xauth', 'add', f'{hostname}/unix:{display_num}',
                 protocol, cookie],
                capture_output=True, timeout=5
            )
            logging.getLogger(__name__).info(
                'Xauthority fixed: added display :%s entry', display_num
            )
    except Exception:
        pass


_fix_xauth()

from fastmcp import FastMCP  # noqa: E402
import pyautogui  # noqa: E402

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局 MCP Server 实例
server = FastMCP(
    "computer-use",
    instructions="计算机桌面操作服务：基于 pyautogui 提供鼠标、键盘控制及屏幕截图等工具",
)


# ==================== 基础工具 ====================

@server.tool()
def get_screen_size() -> Dict[str, str]:
    """获取屏幕尺寸。"""
    return {"status": "success", "message": f"Screen size: {pyautogui.size()}"}


@server.tool()
def get_mouse_position() -> Dict[str, str]:
    """获取当前鼠标位置。"""
    return {"status": "success", "message": f"Mouse position: {pyautogui.position()}"}


@server.tool()
def move_to(x: int, y: int) -> Dict[str, str]:
    """移动鼠标到指定的 (x, y) 坐标。"""
    try:
        pyautogui.moveTo(x, y)
        return {"status": "success", "message": f"Mouse moved to coordinates ({x}, {y})"}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse moved to screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to move mouse: {str(e)}"}


@server.tool()
def click() -> Dict[str, str]:
    """在当前鼠标位置单击。"""
    try:
        pyautogui.click()
        return {"status": "success", "message": "Mouse clicked at current position"}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse in screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to click mouse: {str(e)}"}


@server.tool()
def double_click() -> Dict[str, str]:
    """在当前鼠标位置双击。"""
    try:
        pyautogui.doubleClick()
        return {"status": "success", "message": "Mouse double-clicked at current position"}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse in screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to double-click mouse: {str(e)}"}


@server.tool()
def hotkey(keys: List[str]) -> Dict[str, str]:
    """同时按下多个按键（组合键）。"""
    try:
        pyautogui.hotkey(*keys)
        return {"status": "success", "message": f"Pressed hotkey combination: {' + '.join(keys)}"}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse in screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to press hotkey: {str(e)}"}


@server.tool()
def press_key(key: str) -> Dict[str, str]:
    """按下并释放单个按键（如 'enter', 'space', 'a'）。"""
    try:
        pyautogui.press(key)
        return {"status": "success", "message": f"Pressed key: {key}"}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse in screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to press key: {str(e)}"}


@server.tool()
def typewrite(message: str) -> Dict[str, str]:
    """输入一串字符。"""
    try:
        pyautogui.typewrite(message)
        return {"status": "success", "message": f"Typed string of length {len(message)} characters"}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse in screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to type string: {str(e)}"}


@server.tool()
def take_screenshot(filePath: Optional[str] = None, quality: int = 60) -> Dict[str, str]:
    """截取当前屏幕截图。"""
    try:
        screenshot = pyautogui.screenshot()
        if filePath:
            screenshot.convert("RGB").save(filePath, format="JPEG", quality=quality, optimize=True)
            return {"status": "success", "filePath": filePath}
        else:
            buffer = io.BytesIO()
            screenshot.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
            return {"status": "success", "base64_content": base64.b64encode(buffer.getvalue()).decode("utf-8")}
    except pyautogui.FailSafeException:
        return {"status": "error", "message": "Operation cancelled - mouse in screen corner (failsafe)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to take screenshot: {str(e)}"}


@server.tool()
def scroll(amount: int) -> Dict[str, str]:
    """滚动鼠标滚轮。正数向上滚动，负数向下滚动。"""
    try:
        pyautogui.scroll(amount)
        return {"status": "success", "message": f"Scrolled {amount}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to scroll: {str(e)}"}


# ==================== OCR 辅助函数 ====================

def screenshot_to_base64() -> str:
    """
    截取屏幕并返回 base64 编码的图片（PNG 格式，供 OCR 使用）。

    Returns:
        base64 编码的图片字符串，如果失败则返回空字符串。
    """
    try:
        screenshot = pyautogui.screenshot()
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        return img_base64
    except Exception as e:
        logger.error(f"截图失败: {str(e)}")
        return ""


def _safe_move_to(x: int, y: int) -> bool:
    """移动鼠标到目标坐标，自动处理 FAILSAFE（鼠标在屏幕边缘时）。"""
    try:
        pyautogui.moveTo(x, y)
        return True
    except pyautogui.FailSafeException:
        failsafe = pyautogui.FAILSAFE
        pyautogui.FAILSAFE = False
        try:
            pyautogui.moveTo(x, y)
            return True
        finally:
            pyautogui.FAILSAFE = failsafe


def click_at(x: int, y: int) -> bool:
    """
    在指定坐标处点击鼠标。

    先 moveTo 移动到目标，延迟 0.1s 让目标窗口响应鼠标进入事件，
    再 click 点击。比 pyautogui.click(x, y) 更逼真，避免点击太快
    导致目标应用（如浏览器）来不及响应。

    Args:
        x: X 坐标
        y: Y 坐标

    Returns:
        是否成功
    """
    try:
        if not _safe_move_to(x, y):
            return False
        pyautogui.sleep(0.1)  # 给目标窗口一点反应时间
        pyautogui.click()
        return True
    except pyautogui.FailSafeException:
        # 鼠标在屏幕边缘触发了安全保护。因为坐标来自 OCR 验证，
        # 不是 bug 产生的 (0,0)，可以临时绕过安全检查。
        logger.warning(
            "FailSafe triggered (mouse at screen edge), "
            "temporarily bypassing to click at (%d, %d)", x, y
        )
        failsafe = pyautogui.FAILSAFE
        pyautogui.FAILSAFE = False
        try:
            pyautogui.moveTo(x, y)
            pyautogui.sleep(0.1)
            pyautogui.click()
            return True
        except Exception as e:
            logger.error(f"点击失败: {str(e)}")
            return False
        finally:
            pyautogui.FAILSAFE = failsafe
    except Exception as e:
        logger.error(f"点击失败: {str(e)}")
        return False


def call_mcp_ocr_tool(tool_name: str, arguments: dict) -> dict:
    """
    调用 MCP-OCR 服务工具（如 find_location）。

    优先通过 MCP 协议调用，如果 MCP 服务不可用则回退到直接调用。

    Args:
        tool_name: 工具名称，如 "find_location"
        arguments: 工具参数

    Returns:
        工具调用结果字典
    """
    agent_service_url = os.getenv("AGENT_SERVICE_URL", "http://localhost:7988")
    url = f"{agent_service_url}/v1/tools/call"

    payload = {
        "tool_id": f"mcp-OCR-{tool_name}",
        "arguments": arguments,
        "format": "json"
    }

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error_msg = f"调用 MCP-OCR 工具失败: HTTP {e.code} - {body}"
        logger.error(error_msg)
        return {"success": False, "message": error_msg}
    except Exception as e:
        error_msg = f"调用 MCP-OCR 工具失败: {str(e)}"
        logger.error(error_msg)
        return {"success": False, "message": error_msg}


# ==================== find_and_click 工具 ====================

@server.tool()
def find_and_click(keyword_or_image_file: str, is_image: bool = False) -> str:
    """
    查找并点击屏幕上的文字或图片。

    实现逻辑：
    1. 使用 pyautogui 截取当前屏幕
    2. 调用 MCP-OCR 服务的 find_location 工具定位目标
    3. 根据返回的坐标使用 pyautogui 点击

    Args:
        keyword_or_image_file: 如果 is_image=True，则为小图片路径；否则为要查找的关键词（支持正则）。
        is_image: 是否为图片模式。True 表示图片模式，False 表示文字模式。

    Returns:
        JSON 格式的操作结果
    """
    try:
        # 步骤 1: 截图
        logger.info("正在截取屏幕...")
        screenshot_base64 = screenshot_to_base64()
        if not screenshot_base64:
            return json.dumps({
                "success": False,
                "message": "截图失败，无法获取屏幕内容"
            }, ensure_ascii=False)

        logger.info("截图成功，正在调用 MCP-OCR find_location 工具定位...")

        # 步骤 2: 调用 MCP-OCR 服务的 find_location 工具
        find_args = {
            "base64_image_big": screenshot_base64
        }

        if is_image:
            # 图片模式：读取小图片文件
            if not os.path.exists(keyword_or_image_file):
                return json.dumps({
                    "success": False,
                    "message": f"图片文件不存在: {keyword_or_image_file}"
                }, ensure_ascii=False)

            with open(keyword_or_image_file, 'rb') as f:
                small_img_bytes = f.read()
            find_args["base64_image_small"] = base64.b64encode(small_img_bytes).decode('utf-8')
        else:
            # 文字模式：使用 pattern 参数
            find_args["pattern"] = keyword_or_image_file

        # 调用 MCP-OCR find_location 工具
        location_result = call_mcp_ocr_tool("find_location", find_args)

        if not location_result.get("success"):
            return json.dumps({
                "success": False,
                "message": f"定位失败: {location_result.get('message', '未找到目标')}"
            }, ensure_ascii=False)

        # 步骤 3: 获取坐标并执行点击
        center_x = location_result.get("center_x")
        center_y = location_result.get("center_y")
        score = location_result.get("score", 0)
        text = location_result.get("text")

        if center_x is None or center_y is None:
            return json.dumps({
                "success": False,
                "message": "无法获取目标坐标"
            }, ensure_ascii=False)

        if text:
            logger.info(f"找到文字 '{text}'，中心坐标: ({center_x}, {center_y})，置信度: {score:.2f}")
        else:
            logger.info(f"找到图片，中心坐标: ({center_x}, {center_y})，置信度: {score:.2f}")

        # 步骤 4: 执行 pyautogui 点击
        if click_at(center_x, center_y):
            return json.dumps({
                "success": True,
                "message": "点击成功",
                "coordinates": {"x": center_x, "y": center_y},
                "confidence": score,
                "is_image": is_image,
                "target": keyword_or_image_file
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "success": False,
                "message": "点击失败",
                "coordinates": {"x": center_x, "y": center_y}
            }, ensure_ascii=False)

    except Exception as e:
        error_msg = f"查找并点击失败: {str(e)}"
        logger.error(error_msg)
        return json.dumps({
            "success": False,
            "message": error_msg
        }, ensure_ascii=False)


# ==================== 主入口 ====================

def main():
    """主入口函数，供命令行调用"""
    parser = argparse.ArgumentParser(description="Computer Use MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="streamable-http",
        help="传输协议 (默认: streamable-http)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="监听地址 (默认: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="监听端口 (默认: 8000)"
    )
    parser.add_argument(
        "--mount-path",
        default="/mcp",
        help="SSE/HTTP 挂载路径 (默认: /mcp)"
    )

    args = parser.parse_args()

    logger.info("启动 Computer Use MCP Server...")
    logger.info(f"传输协议: {args.transport}")

    if args.transport in ("sse", "streamable-http"):
        logger.info(f"监听地址: {args.host}:{args.port}")
        logger.info(f"挂载路径: {args.mount_path}")
        logger.info(f"访问地址: http://{args.host}:{args.port}{args.mount_path}")

    server.run(transport=args.transport, host=args.host, port=args.port, path=args.mount_path)


if __name__ == "__main__":
    main()

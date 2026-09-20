"""
Android Use MCP Server

用于在 Agent Service 宿主机和远程机器及远程机器上的安卓设备之间进行交互。
运行在远程机器上，提供基于 ADB 的各种安卓设备操作工具。

前置条件：
pip install fastmcp

功能：
1. save_file_to_phone_gallery - 将 base64 图片保存到手机相册
2. read_file_from_phone_gallery - 读取手机相册最近的一张图片并以 base64 返回
3. run_adb_command - 执行任意 ADB 命令
4. find_and_click - 查找并点击屏幕上的文字或图片。先截图，然后调用 OCR 工具定位目标，最后执行点击操作。
5. show_image - 在手机上显示图片（打开图片查看器）。自动唤醒屏幕、解锁、查找默认图片查看器并打开图片，同时设置屏幕常亮和最大亮度。
6. read_verification_code - 读取最近5分钟内的短信（SMS/MMS/RCS，含5G富媒体消息）并提取4-6位验证码
   （优先匹配带“验证码”、“code”等前缀的数字，支持重试机制；除 content://sms 外还会查询 MMS 库，
   并枚举设备上的消息类 ContentProvider 覆盖 RCS/IMS 私有消息库；
   若厂商私有库不向 shell 开放（如小米 RCS 聊天机器人库），则兜底打开短信 App 截图 OCR 提取，
   结束后自动还原到调用前用户所在的界面，不打断原有流程）

使用方法：
    # HTTP 方式运行（默认）
    python android_use_mcp.py
    
    # 指定端口
    python android_use_mcp.py --port 8001
    
    # SSE 方式运行
    python android_use_mcp.py --transport sse
    
    # stdio 方式运行（本地调试）
    python android_use_mcp.py --transport stdio
    
MCP Server 配置（添加到 Agent Service）：
{
  "mcpServers": {
    "android-use": {
      "url": "http://<remote-host>:8000/mcp",
      "headers": {}
    }
  }
}
"""

import base64
import os
import re
import sys
import json
import logging
import argparse
import subprocess
import time
import tempfile
import urllib.request
import urllib.error

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局 MCP Server 实例
server = FastMCP(
    "android-use",
    instructions="安卓设备操作服务：基于 ADB 提供文件传输、设备控制等工具",
)


# ==================== 辅助函数 ====================


def _normalize_image_base64(image_file_base64):
    """
    校验并规范化 base64 编码的图片字符串。

    Args:
        image_file_base64: base64 编码的图片内容字符串。

    Returns:
        解析成功时返回规范化（去除空白字符）后的 base64 字符串；
        为空或 base64 解析失败时返回 None。
    """
    if not image_file_base64 or not isinstance(image_file_base64, str):
        return None
    s = ''.join(image_file_base64.split())
    if not s:
        return None
    try:
        base64.b64decode(s, validate=True)
        return s
    except (base64.binascii.Error, ValueError):
        pass
    # 兼容去掉 padding 的 base64
    padded = s + '=' * (-len(s) % 4)
    try:
        base64.b64decode(padded, validate=True)
        return padded
    except (base64.binascii.Error, ValueError):
        return None


def adb_screenshot_to_base64(device_id: str = None) -> str:
    """
    截取手机屏幕并返回 base64 编码的图片。
    
    Args:
        device_id: 可选，指定设备 ID。
        
    Returns:
        base64 编码的图片字符串，如果失败则返回空字符串。
    """
    try:
        cmd = ["adb"]
        if device_id:
            cmd.extend(["-s", device_id])
        cmd.extend(["exec-out", "screencap", "-p"])
        
        img_bytes = subprocess.check_output(cmd)
        img_base64 = base64.b64encode(img_bytes).decode('utf-8')
        return img_base64
    except Exception as e:
        logger.error(f"截屏失败: {str(e)}")
        return ""


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
    # 方式1: 通过 MCP 协议调用
    agent_service_url = os.getenv("AGENTS_URL", "http://localhost:7988")
    
    # 尝试通过 Agent Service 的 MCP 代理调用
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


# ==================== MCP 工具 ====================

@server.tool()
def run_adb_command(command: str = "devices", device_id: str = None) -> str:
    """
    执行任意 ADB 命令。

    Args:
        command: ADB 命令（不含 adb 前缀），如 "devices", "shell pm list packages", "pull /sdcard/file local_path" 等。
        device_id: 可选，指定设备 ID。仅有一个设备时无需指定。

    Returns:
        JSON 格式的执行结果
    """
    try:
        cmd_parts = ["adb"]
        if device_id:
            cmd_parts.extend(["-s", device_id])
        cmd_parts.extend(command.split())

        logger.info(f"执行命令: {' '.join(cmd_parts)}")
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            encoding='utf-8',    # 解决中文乱码问题
            timeout=60
        )

        return json.dumps({
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "command": command,
            "device_id": device_id
        }, ensure_ascii=False)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "success": False,
            "message": "ADB 命令执行超时"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"ADB 命令执行失败: {str(e)}"
        }, ensure_ascii=False)


@server.tool()
def save_file_to_phone_gallery(base64_content: str, filename: str, device_id: str = None) -> str:
    """
    将 base64 编码的图片保存到手机相册。
    
    Args:
        base64_content: base64 编码的图片内容。提供本地文件路径即可，底层会自动读取并编码。
        filename: 保存到手机相册的文件名（如 screenshot.png）
        device_id: 可选，指定设备 ID。仅有一个设备时无需指定。
    
    Returns:
        JSON 格式的操作结果
    """
    try:
        # 步骤 1: 保存到临时目录
        temp_dir = r"C:\temp"
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
        
        temp_path = os.path.join(temp_dir, filename)
        file_data = base64.b64decode(base64_content)
        
        with open(temp_path, 'wb') as f:
            f.write(file_data)
        
        logger.info(f"临时文件已保存: {temp_path}")
        
        # 步骤 2: 通过 ADB 推送到手机
        phone_path = f"/sdcard/DCIM/Camera/{filename}"
        
        # 推送文件
        push_result = run_adb_command(f"push {temp_path} {phone_path}", device_id)
        push_data = json.loads(push_result)
        
        if not push_data.get("success"):
            return json.dumps({
                "success": False,
                "message": f"ADB 推送失败: {push_data.get('stderr', push_data.get('message', ''))}"
            }, ensure_ascii=False)
        
        logger.info(f"文件已推送到手机: {phone_path}")
        
        # 步骤 3: 触发媒体扫描
        run_adb_command(
            f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{phone_path}",
            device_id
        )
        logger.info("媒体扫描已触发")
        
        # 步骤 4: 清理临时文件
        try:
            os.remove(temp_path)
            logger.info(f"临时文件已清理: {temp_path}")
        except Exception:
            pass
        
        return json.dumps({
            "success": True,
            "message": f"图片已保存到手机相册",
            "phone_path": phone_path,
            "local_temp_path": temp_path
        }, ensure_ascii=False)
        
    except base64.binascii.Error as e:
        return json.dumps({
            "success": False,
            "message": f"Base64 解码失败: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"保存到手机失败: {str(e)}"
        }, ensure_ascii=False)


@server.tool()
def read_file_from_phone_gallery(device_id: str = None) -> str:
    """
    读取手机相册最近的一张图片并以 base64 编码返回。
    
    Args:
        device_id: 可选，指定设备 ID。仅有一个设备时无需指定。
    
    Returns:
        JSON 格式的结果，包含 base64 编码的图片内容
    """
    try:
        # 步骤 1: 获取最新的图片文件名
        ls_result = run_adb_command("shell ls -t /sdcard/DCIM/Camera/ | head -1", device_id)
        ls_data = json.loads(ls_result)
        
        if not ls_data.get("success"):
            return json.dumps({
                "success": False,
                "message": f"获取相册文件列表失败: {ls_data.get('stderr', ls_data.get('message', ''))}"
            }, ensure_ascii=False)
        
        filename = ls_data.get("stdout", "").strip()
        if not filename:
            return json.dumps({
                "success": False,
                "message": "相册中没有找到图片"
            }, ensure_ascii=False)
        
        logger.info(f"找到最新图片: {filename}")
        
        # 步骤 2: 拉取图片到本地临时目录
        temp_dir = r"C:\temp"
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir, exist_ok=True)
        
        temp_path = os.path.join(temp_dir, filename)
        phone_path = f"/sdcard/DCIM/Camera/{filename}"
        
        pull_result = run_adb_command(f"pull {phone_path} {temp_path}", device_id)
        pull_data = json.loads(pull_result)
        
        if not pull_data.get("success"):
            return json.dumps({
                "success": False,
                "message": f"拉取图片失败: {pull_data.get('stderr', pull_data.get('message', ''))}"
            }, ensure_ascii=False)
        
        logger.info(f"图片已拉取到本地: {temp_path}")
        
        # 步骤 3: 读取文件并编码为 base64
        with open(temp_path, 'rb') as f:
            file_data = f.read()
        
        base64_content = base64.b64encode(file_data).decode('utf-8')
        
        # 步骤 4: 清理临时文件
        try:
            os.remove(temp_path)
            logger.info(f"临时文件已清理: {temp_path}")
        except Exception:
            pass
        
        return json.dumps({
            "success": True,
            "message": f"成功读取手机相册最新图片: {filename}",
            "filename": filename,
            "phone_path": phone_path,
            "file_size": len(file_data),
            "base64_content": base64_content
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"读取手机相册失败: {str(e)}"
        }, ensure_ascii=False)


@server.tool()
def find_and_click(keyword: str = None, image_file_base64: str = None, device_id: str = None) -> str:
    """
    查找并点击屏幕上的文字或图片。
    
    实现逻辑：
    1. 使用 ADB 截取手机屏幕
    2. 调用 MCP-OCR 服务的 find_location 工具定位目标
    3. 根据返回的坐标使用 ADB 点击
    
    Args:
        keyword: 要查找的关键词（支持正则），文字模式使用。
        image_file_base64: 小图片的 base64 编码内容，图片模式使用。
            为空或 None 时按文字模式处理；base64 解析失败时同样按文字模式处理。
        device_id: 可选，指定设备 ID。
        
    Returns:
        JSON 格式的操作结果
    """
    try:
        # 步骤 1: 截屏
        logger.info("正在截取手机屏幕...")
        screenshot_base64 = adb_screenshot_to_base64(device_id)
        if not screenshot_base64:
            return json.dumps({
                "success": False,
                "message": "截屏失败，无法获取屏幕内容"
            }, ensure_ascii=False)
        
        logger.info("截屏成功，正在调用 MCP-OCR find_location 工具定位...")
        
        # 步骤 2: 调用 MCP-OCR 服务的 find_location 工具
        find_args = {
            "base64_image_big": screenshot_base64
        }
        
        # 判断查找模式：image_file_base64 非空且 base64 解析成功 → 图片模式，否则 → 文字模式
        small_img_base64 = _normalize_image_base64(image_file_base64)
        if image_file_base64 and small_img_base64 is None:
            logger.warning("image_file_base64 不是有效的 base64，回退为文字模式")
        is_image = small_img_base64 is not None

        if is_image:
            # 图片模式：直接使用调用方传入的 base64 小图片（无需本地文件）
            find_args["base64_image_small"] = small_img_base64
        else:
            # 文字模式：使用 pattern 参数
            if not keyword:
                return json.dumps({
                    "success": False,
                    "message": "必须提供 keyword 或有效的 image_file_base64"
                }, ensure_ascii=False)
            find_args["pattern"] = keyword
        
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
        
        # 步骤 4: 执行 ADB 点击
        click_command = f"shell input tap {center_x} {center_y}"
        click_result = run_adb_command(click_command, device_id)
        click_data = json.loads(click_result)
        
        if click_data.get("success"):
            return json.dumps({
                "success": True,
                "message": f"点击成功",
                "coordinates": {"x": center_x, "y": center_y},
                "confidence": score,
                "is_image": is_image,
                "target": f"image (base64, {len(small_img_base64)} chars)" if is_image else keyword
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "success": False,
                "message": f"点击失败: {click_data.get('stderr', click_data.get('message', ''))}",
                "coordinates": {"x": center_x, "y": center_y}
            }, ensure_ascii=False)
            
    except Exception as e:
        error_msg = f"查找并点击失败: {str(e)}"
        logger.error(error_msg)
        return json.dumps({
            "success": False,
            "message": error_msg
        }, ensure_ascii=False)


@server.tool()
def show_image(base64_content: str, device_id: str = None) -> str:
    """
    在手机上显示图片（打开图片查看器）。
    
    实现逻辑：
    1. 将 base64 图片保存到临时文件
    2. 通过 ADB push 到手机
    3. 触发媒体扫描
    4. 动态查找默认图片查看器 Activity（不写死，适用于各品牌手机）
    5. 唤醒屏幕并解锁
    6. 打开图片查看器显示图片
    7. 保持亮屏 + 最大亮度
    
    Args:
        base64_content: base64 编码的图片内容。提供本地文件路径即可，底层会自动读取并编码。
        device_id: 可选，指定设备 ID。仅有一个设备时无需指定。
    
    Returns:
        JSON 格式的操作结果
    """
    try:
        # ========== 步骤 1: 保存到临时文件 ==========
        # 使用操作系统临时目录
        temp_dir = tempfile.gettempdir()
        
        # 生成唯一文件名
        timestamp = int(time.time())
        filename = f"show_image_{timestamp}.png"
        temp_path = os.path.join(temp_dir, filename)
        
        # 解码并保存
        file_data = base64.b64decode(base64_content)
        with open(temp_path, 'wb') as f:
            f.write(file_data)
        
        logger.info(f"临时文件已保存: {temp_path}")
        
        # ========== 步骤 2: ADB push 到手机 ==========
        phone_path = f"/sdcard/DCIM/Camera/{filename}"
        
        push_result = run_adb_command(f"push {temp_path} {phone_path}", device_id)
        push_data = json.loads(push_result)
        
        if not push_data.get("success"):
            return json.dumps({
                "success": False,
                "message": f"ADB 推送失败: {push_data.get('stderr', push_data.get('message', ''))}"
            }, ensure_ascii=False)
        
        logger.info(f"文件已推送到手机: {phone_path}")
        
        # ========== 步骤 3: 触发媒体扫描 ==========
        run_adb_command(
            f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{phone_path}",
            device_id
        )
        logger.info("媒体扫描已触发")
        
        # ========== 步骤 4: 动态查找默认图片查看器 Activity ==========
        viewer_component = None
        
        # 方法 1: 使用 cmd package resolve-activity 查找默认处理 image/* 的 Activity
        logger.info("正在查找默认图片查看器...")
        resolve_result = run_adb_command(
            "shell cmd package resolve-activity --brief -a android.intent.action.VIEW -t image/*",
            device_id
        )
        resolve_data = json.loads(resolve_result)
        
        if resolve_data.get("success"):
            stdout = resolve_data.get("stdout", "")
            # 输出格式通常是：
            # priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
            #   com.miui.gallery/.activity.ExternalPhotoPageActivity
            lines = stdout.strip().split('\n')
            for line in lines:
                line = line.strip()
                if '/' in line and not line.startswith('priority='):
                    # 排除 ResolverActivity（系统选择器）
                    if 'ResolverActivity' in line:
                        logger.info(f"跳过系统选择器: {line}")
                        continue
                    # 找到 Activity 组件
                    viewer_component = line
                    logger.info(f"找到默认图片查看器: {viewer_component}")
                    break
        
        # 如果方法 1 返回 ResolverActivity 或未找到，继续尝试方法 2 和 3
        # （代码会继续往下执行）
        
        # 方法 2: 如果方法 1 失败，使用 pm query-activities 查询
        if not viewer_component:
            logger.info("尝试使用 pm query-activities 查找...")
            query_result = run_adb_command(
                "shell pm query-activities -a android.intent.action.VIEW -t image/*",
                device_id
            )
            query_data = json.loads(query_result)
            
            if query_data.get("success"):
                try:
                    # 输出是 JSON 数组
                    activities = json.loads(query_data.get("stdout", "[]"))
                    if activities:
                        # 取第一个 Activity
                        activity = activities[0]
                        pkg = activity.get("packageName", "")
                        name = activity.get("name", "")
                        if pkg and name:
                            viewer_component = f"{pkg}/{name}"
                            logger.info(f"通过 pm query-activities 找到: {viewer_component}")
                except json.JSONDecodeError:
                    pass
        
        # 方法 3: 如果方法 1 和 2 都失败，动态查找已安装的图片相关应用
        if not viewer_component:
            logger.info("尝试动态查找已安装的图片相关应用...")
            
            # 查找包含 gallery/photo/image/viewer 的包
            pkg_result = run_adb_command(
                'shell pm list packages | grep -iE "gallery|photo|image|viewer"',
                device_id
            )
            pkg_data = json.loads(pkg_result)
            
            if pkg_data.get("success"):
                stdout = pkg_data.get("stdout", "")
                packages = []
                for line in stdout.strip().split('\n'):
                    # 格式: package:com.miui.gallery
                    if line.startswith("package:"):
                        pkg_name = line.replace("package:", "").strip()
                        packages.append(pkg_name)
                
                logger.info(f"找到图片相关应用: {packages}")
                
                # 逐个检查哪个包支持 VIEW intent
                for pkg_name in packages:
                    if viewer_component:
                        break
                    
                    # 检查该包是否有处理 VIEW intent 的 Activity
                    dump_result = run_adb_command(
                        f'shell dumpsys package {pkg_name} | grep -A 5 "android.intent.action.VIEW"',
                        device_id
                    )
                    dump_data = json.loads(dump_result)
                    
                    if dump_data.get("success"):
                        dump_stdout = dump_data.get("stdout", "")
                        
                        # 查找处理 image/png 的 Activity
                        # 格式: com.miui.gallery/.activity.ExternalPhotoPageActivity
                        lines = dump_stdout.strip().split('\n')
                        candidates = []  # 收集所有候选 Activity
                        
                        for i, line in enumerate(lines):
                            # 查找包含包名和 Activity 的行
                            if f'{pkg_name}/' in line and 'filter' in line:
                                # 提取组件名
                                parts = line.split()
                                for part in parts:
                                    if '/' in part and pkg_name in part:
                                        candidate = part.strip()
                                        # 检查后续几行是否有 image/* 或 image/png
                                        context = '\n'.join(lines[max(0,i-2):min(len(lines),i+5)])
                                        if 'image/' in context or 'vnd.android.cursor.dir/image' in context:
                                            if candidate not in candidates:
                                                candidates.append(candidate)
                                            logger.info(f"找到候选 Activity: {candidate}")
                                            break
                        
                        # 优先选择包含 "External" 或 "PhotoPage" 的 Activity（用于查看图片）
                        # 排除包含 "Picker" 或 "Pick" 的 Activity（用于选择图片）
                        for candidate in candidates:
                            if ('External' in candidate or 'PhotoPage' in candidate) and 'Pick' not in candidate:
                                viewer_component = candidate
                                logger.info(f"优先选择查看器: {viewer_component}")
                                break
                        
                        # 如果没有找到优先的，使用第一个候选
                        if not viewer_component and candidates:
                            viewer_component = candidates[0]
                            logger.info(f"使用第一个候选: {viewer_component}")
        
        # ========== 步骤 5: 唤醒屏幕并解锁 ==========
        logger.info("正在唤醒屏幕...")
        
        # 唤醒屏幕
        run_adb_command("shell input keyevent KEYCODE_WAKEUP", device_id)
        time.sleep(1)
        
        # 解锁屏幕（使用 MENU 键，适用于无密码锁屏）
        run_adb_command("shell input keyevent 82", device_id)  # 82 = KEYCODE_MENU
        time.sleep(1)
        
        logger.info("屏幕已唤醒并解锁")
        
        # ========== 步骤 6: 打开图片查看器 ==========
        used_chooser = False
        if viewer_component:
            # 使用找到的 Activity 打开图片
            logger.info(f"使用 {viewer_component} 打开图片...")
            open_result = run_adb_command(
                f"shell am start -n {viewer_component} -a android.intent.action.VIEW -d file://{phone_path} -t image/png",
                device_id
            )
        else:
            # 方法 3: 回退到通用 intent（会弹出选择器）
            logger.info("未找到默认查看器，使用通用 intent...")
            open_result = run_adb_command(
                f"shell am start -a android.intent.action.VIEW -d file://{phone_path} -t image/png",
                device_id
            )
            used_chooser = True
        
        open_data = json.loads(open_result)
        if not open_data.get("success"):
            logger.warning(f"打开图片可能失败: {open_data.get('stderr', '')}")
        
        # ========== 步骤 6.5: 如果弹出 chooser，点击"图片"选项 ==========
        if used_chooser:
            logger.info("检测到 chooser，等待弹出并点击图片选项...")
            time.sleep(2)  # 等待 chooser 弹出
            
            # 使用 find_and_click 点击"图片"关键词
            click_result = find_and_click("图片", device_id=device_id)
            click_data = json.loads(click_result)
            
            if click_data.get("success"):
                logger.info("成功点击图片选项")
            else:
                logger.warning(f"点击图片选项失败: {click_data.get('message', '')}")
        
        # ========== 步骤 7: 保持亮屏 + 最大亮度 ==========
        logger.info("设置屏幕常亮和最大亮度...")
        run_adb_command("shell svc power stayon true", device_id)
        run_adb_command("shell settings put system screen_brightness 255", device_id)
        
        # ========== 清理临时文件 ==========
        try:
            os.remove(temp_path)
            logger.info(f"临时文件已清理: {temp_path}")
        except Exception:
            pass
        
        return json.dumps({
            "success": True,
            "message": "图片已在手机上打开显示",
            "phone_path": phone_path,
            "viewer_component": viewer_component or "通用 intent (可能弹出选择器)"
        }, ensure_ascii=False)
        
    except base64.binascii.Error as e:
        return json.dumps({
            "success": False,
            "message": f"Base64 解码失败: {str(e)}"
        }, ensure_ascii=False)
    except Exception as e:
        error_msg = f"显示图片失败: {str(e)}"
        logger.error(error_msg)
        return json.dumps({
            "success": False,
            "message": error_msg
        }, ensure_ascii=False)


# ==================== 短信/MMS/RCS 读取辅助 ====================

# 消息类 ContentProvider authority 关键字（用于发现 RCS/5G 富媒体消息的私有消息库）
_MESSAGE_AUTH_KEYWORDS_RE = re.compile(
    r'(sms|mms|msg|message|ims|rcs|rich|5g|chat|conv)', re.IGNORECASE
)

# 已由专门数据源处理、或明显不含消息内容的 provider authority，跳过以避免无效查询
_SKIP_AUTHORITIES = {
    "sms", "mms", "mms-sms", "mmssms", "calllog",
    "com.android.providers.telephony",   # 系统别名 authority，实际数据在 sms/mms
    "settings", "com.android.providers.settings", "com.android.providers.settings.module",
    "media", "media/external", "media/internal", "media/none",
    "com.android.providers.media", "com.android.providers.media.module",
    "contacts", "com.android.contacts", "com.android.providers.contacts",
    "accounts", "downloads", "calendar", "appwidget", "wallpaper",
    "com.android.providers.calendar", "com.android.providers.downloads",
}

# 行内时间字段列名关键字
_DATE_FIELD_RE = re.compile(r'(date|time|_ts|timestamp|created|sent|recv)', re.IGNORECASE)

# 标识/地址类字段列名（拼接兜底文本时排除，避免号码被误识别为验证码）
_ID_FIELD_RE = re.compile(r'(_id|^id$|addr|from|to|number|phone|num|ref|type|status|size|read|box)', re.IGNORECASE)

# 设备端 sh 需要加引号才能安全传递的字符（空格、重定向、管道等）
_SHELL_SPECIAL_RE = re.compile(r'[\s><|&;()$`\\]')


def _shell_arg(value):
    """
    把参数包装成设备端 sh 能安全解析的形式。

    run_adb_command 会对整条命令做 .split()，参数最终由**设备端 sh** 重新解析，
    因此含空格或 > < | 等符号的参数必须整体带引号：
      `--sort "date DESC"` 正确；
      `--sort date DESC` 会被 sh 拆成两个参数，content 工具只打印 usage +
      "Unsupported argument: DESC"，而**返回码依然是 0** —— 调用方会误以为
      查询成功，把 usage 文本当成结果解析（短信一直读不出来就是这个原因）。
    """
    value = str(value if value is not None else "")
    if not value or '"' in value:
        return value
    if _SHELL_SPECIAL_RE.search(value):
        return f'"{value}"'
    return value


def _content_query_raw(device_id, uri, projection=None, sort=None, where=None, limit=None):
    """
    执行 `adb shell content query` 并返回 stdout（失败时返回空字符串）。

    注意：run_adb_command 会对命令做 .split() 后再交给设备端 sh 解析，
    含空格/重定向符的参数必须经 _shell_arg() 加引号，否则：
      `--sort date DESC` 会被拆散成两个参数；
      `--where date > 123` 里的 > 会被当成写文件重定向，查询根本不执行。

    部分 ROM（如 HyperOS）的 content 工具不支持 --limit / 参数写错时，
    只在 stdout 打 usage + [ERROR]（返回码仍为 0），此时自动去掉 --limit
    重试一次；仍不支持则视为空结果，避免把 usage 文本当数据解析。
    """
    attempts = [True, False] if limit is not None else [True]
    for use_limit in attempts:
        parts = ["shell", "content", "query", "--uri", uri]
        if projection:
            parts += ["--projection", _shell_arg(projection)]
        if sort:
            parts += ["--sort", _shell_arg(sort)]
        if where:
            parts += ["--where", _shell_arg(where)]
        if use_limit and limit is not None:
            parts += ["--limit", str(limit)]
        result = run_adb_command(" ".join(parts), device_id)
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            logger.error(f"content query 返回值解析失败: {str(result)[:200]}")
            return ""
        if not data.get("success"):
            logger.warning(f"content query {uri} 失败: {data.get('stderr') or data.get('message', '')}")
            return ""
        stdout = data.get("stdout", "")
        # 参数不被支持时 content 只打印 usage/[ERROR]（返回码仍为 0），
        # 当成数据解析会得到"没有消息"的假象，必须识别出来并降级重试
        if "usage: adb shell content" in stdout or "[ERROR]" in stdout:
            last = (stdout.strip().splitlines() or [""])[-1].strip()
            if use_limit and limit is not None:
                logger.info(f"content query {uri}: 参数不被支持({last})，去掉 --limit 重试")
                continue
            logger.warning(f"content query {uri}: 参数不被支持({last})")
            return ""
        return stdout
    return ""


def _parse_content_rows(output):
    """
    解析 `content query` 输出为行列表（list[dict]，保持输出顺序）。

    兼容两种输出格式：
      单行: Row: 0 address=10086, date=1780639504356, body=...
      多行: Row: 0
             address=10086
             date=1780639504356
             body=...
    """
    rows = []
    if not output:
        return rows
    current = None
    for line in output.splitlines():
        s = line.strip()
        if not s or s.startswith("No result found."):
            continue
        m = re.match(r'^Row:\s*\d+\s*$', s)
        if m:
            current = {}
            rows.append(current)
            continue
        m = re.match(r'^Row:\s*\d+\s+(.+)$', s)
        if m:
            current = {}
            rows.append(current)
            s = m.group(1)
        if current is None:
            continue
        # 按 ", key=" 边界拆分字段（字段值本身可能包含逗号）
        for part in re.split(r',\s*(?=[A-Za-z_][A-Za-z0-9_]*=)', s):
            if '=' in part:
                k, v = part.split('=', 1)
                current[k.strip()] = v.strip()
    return rows


def _row_date_ms(row):
    """从行中提取时间字段并归一化为毫秒时间戳，找不到返回 None。"""
    for k, v in row.items():
        if not v:
            continue
        if _DATE_FIELD_RE.search(k) and re.fullmatch(r'\d{9,14}', v):
            n = int(v)
            if n < 100_000_000_000:  # 小于 1e11 视为秒级时间戳
                n *= 1000
            return n
    return None


def _row_text(row):
    """
    提取行的正文文本：优先取 body/m_text/text 等正文字段；
    没有正文字段时拼接非标识类字段值（排除地址、ID 等，避免号码被误识别为验证码）。
    """
    low = {k.lower(): k for k in row.keys()}
    # 小米等 ROM 的 MMS 库正文存在 snippet 列，AOSP 在 body/m_text
    for name in ("body", "m_text", "text", "content", "message", "msg", "snippet"):
        key = low.get(name)
        if key is not None and row.get(key):
            return str(row[key]).strip()
    parts = []
    for k, v in row.items():
        if not v:
            continue
        if _ID_FIELD_RE.search(k):
            continue
        # 纯数字的时间字段没有正文价值，跳过
        if _DATE_FIELD_RE.search(k) and re.fullmatch(r'\d+', str(v)):
            continue
        parts.append(str(v))
    return " ".join(parts)


def _query_recent_mms(device_id):
    """
    查询 MMS 数据库最近的消息。

    部分机型（尤其国内 ROM）的 RCS/5G 富媒体消息正文也会落入 MMS 数据库，
    是 content://sms 之外的重要数据来源。
    不同 ROM 的列名有差异（AOSP: m_from/m_text；小米等: snippet、无 m_from 列），
    因此按投影链逐个尝试，第一个能返回行的组合生效。
    """
    results = []
    rows = []
    for projection, sort in (
        ("_id:date:m_from:m_text", "date DESC"),
        ("_id:date:snippet", "date DESC"),
        ("_id:date:m_from:m_text", None),
        ("_id:date:snippet", None),
        (None, "date DESC"),
        (None, None),
    ):
        out = _content_query_raw(device_id, "content://mms",
                                 projection=projection, sort=sort, limit=10)
        rows = _parse_content_rows(out)
        if rows:
            break
        # 查询被接受但没有数据（输出 "No result found."），换投影也不会有效果
        if out and "No result found." in out:
            break
    body_fetched = 0
    for row in rows:
        date_ms = _row_date_ms(row)
        text = _row_text(row)
        # MMS 正文存储在 part 表，行内完全没有任何正文字段时按消息 id 再查一次 body
        rid = row.get("_id") or row.get("message_id")
        has_body = any(row.get(k) for k in
                       ("body", "m_text", "text", "content", "message", "msg", "snippet"))
        if rid and not has_body and body_fetched < 3:
            body_fetched += 1
            body_out = _content_query_raw(device_id, f"content://mms/{rid}/body")
            body_rows = _parse_content_rows(body_out)
            if body_rows:
                body_text = _row_text(body_rows[0])
                if len(body_text) > len(text):
                    text = body_text
        results.append({"date_ms": date_ms, "text": text, "source": "content://mms"})
    return results


# 常见厂商消息类 provider authority 的已知列表。
# 部分 ROM 的 dumpsys 输出里没有完整 provider 列表，直接尝试这些 authority；
# shell 无权限时查询会优雅失败，不影响其他数据源。
_KNOWN_MESSAGE_AUTHORITIES = (
    "org.rcs.service.provider.rcs_chatbot",  # 小米/HyperOS RCS 聊天机器人库
    "com.android.mms.smscodeprovider",       # 小米 SMS 验证码提取库
    "com.carrier.rcs.msg",                   # 运营商 RCS 消息库
    "com.miui.mmsa",                         # 小米短信助手
    "com.android.mms.service",
)

# authority 枚举结果按设备缓存（进程内），避免每次调用都重复 dumpsys
_discover_cache = {}


def _discover_message_authorities(device_id):
    """
    枚举设备上注册的消息类 ContentProvider authority（RCS/5G 消息等
    厂商私有消息库通常在这里）。

    依次尝试两个来源：
      1. `dumpsys content` 中的 mAuthority 行（AOSP 格式）
      2. `dumpsys package` 的 "ContentProvider Authorities:" 段
         （部分 ROM 如 HyperOS 的 dumpsys content 只输出同步表，没有 mAuthority）
    最后合并一份已知的厂商消息库 authority 列表。结果按设备缓存。
    """
    cache_key = device_id or ""
    if cache_key in _discover_cache:
        return _discover_cache[cache_key]

    authorities = []
    # 来源1: dumpsys content
    try:
        data = json.loads(run_adb_command("shell dumpsys content", device_id))
        stdout = data.get("stdout", "") if data.get("success") else ""
    except (ValueError, TypeError):
        stdout = ""
    authorities.extend(re.findall(r'mAuthority:content://([^,\s]+)', stdout))

    # 来源2: dumpsys package（输出较大，仅当来源1没有结果时执行）
    if not authorities:
        try:
            data = json.loads(run_adb_command("shell dumpsys package", device_id))
            stdout = data.get("stdout", "") if data.get("success") else ""
        except (ValueError, TypeError):
            stdout = ""
        section = re.search(
            r'ContentProvider Authorities:$(.*?)(?:\n\S|\Z)', stdout, re.M | re.S)
        if section:
            authorities.extend(re.findall(r'\[([A-Za-z0-9_.]+)\]:', section.group(1)))

    # 合并已知厂商列表
    authorities.extend(_KNOWN_MESSAGE_AUTHORITIES)

    seen = set()
    candidates = []
    for auth in authorities:
        auth = auth.strip()
        if not auth or auth in seen or auth in _SKIP_AUTHORITIES:
            continue
        seen.add(auth)
        if _MESSAGE_AUTH_KEYWORDS_RE.search(auth):
            candidates.append(auth)
    if len(candidates) > 8:
        logger.info(f"发现 {len(candidates)} 个消息类 provider，仅查询前 8 个: {candidates[:8]}")
        candidates = candidates[:8]
    else:
        logger.info(f"发现候选消息类 provider: {candidates}")

    _discover_cache[cache_key] = candidates
    return candidates


def _adb_json(device_id, command):
    """执行 adb 命令并解析返回的 JSON，失败返回 None。"""
    try:
        data = json.loads(run_adb_command(command, device_id))
        return data if data.get("success") else None
    except (ValueError, TypeError):
        return None


def _adb_result(device_id, command):
    """执行 adb 命令并解析 JSON，返回完整 dict（含失败时的 stdout/stderr），
    解析不出时返回 None。与 _adb_json 不同：命令失败(rc!=0)时不丢弃返回体。"""
    try:
        data = json.loads(run_adb_command(command, device_id))
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _adb_failed(data):
    """_adb_result 的返回值是否表示命令失败。"""
    return data is None or not data.get("success")


def _adb_error_text(data):
    """从 _adb_result 的返回值里提取错误文本（stdout+stderr 合并，截断）。"""
    if data is None:
        return "无返回"
    parts = [data.get("stdout", ""), data.get("stderr", "")]
    return " ".join(p.strip() for p in parts if p and p.strip())[:200]


def _screen_size(device_id):
    """解析 `wm size` 返回 (宽, 高)，失败时返回常见分辨率兜底值。"""
    data = _adb_json(device_id, "shell wm size")
    if data:
        m = re.search(r'(\d+)x(\d+)', data.get("stdout", ""))
        if m:
            return int(m.group(1)), int(m.group(2))
    return 1080, 2400


def _display_state_off(device_id):
    """判断主屏 display 状态是否为 OFF（无法判断时返回 False，避免误关屏）。"""
    data = _adb_json(device_id, "shell dumpsys display")
    if not data:
        return False
    m = re.search(r'mState=(\w+)', data.get("stdout", ""))
    return bool(m) and m.group(1) == "OFF"


def _wake_and_unlock(device_id):
    """
    唤醒并点亮屏幕、关闭通知栏、尝试解锁。

    关键点：scrcpy 投屏 / 息屏状态下仅发 WAKEUP 可能只让电源状态变 Awake
    而物理屏仍 OFF（截屏全黑），需要 stayon + 亮度 + 必要时电源键点亮。
    """
    w, h = _screen_size(device_id)
    run_adb_command("shell input keyevent 224", device_id)  # KEYCODE_WAKEUP
    run_adb_command("shell svc power stayon true", device_id)
    run_adb_command("shell settings put system screen_brightness 255", device_id)
    time.sleep(0.5)
    if _display_state_off(device_id):
        # 物理屏仍 OFF：电源键点亮（只在确认 OFF 时才发，避免误关屏）
        run_adb_command("shell input keyevent 26", device_id)  # KEYCODE_POWER
        time.sleep(0.5)
    # 关闭通知栏 / 系统对话框
    run_adb_command(
        "shell am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS", device_id)
    # 解锁：MENU（无密码锁屏）+ 上滑（已解锁时均无副作用）
    run_adb_command("shell input keyevent 82", device_id)  # KEYCODE_MENU
    cx = w // 2
    run_adb_command(
        f"shell input swipe {cx} {int(h * 0.75)} {cx} {int(h * 0.25)} 300", device_id)
    time.sleep(0.5)


def _ocr_current_screen(device_id):
    """
    截屏当前屏幕并 OCR，返回 (full_text, lines)。
    full_text 为按阅读顺序合并的完整文本；lines 为 OCR 返回的行信息
    （每项含 texts / x_ranges / y_ranges，自上而下排列）。失败时返回 ("", [])。
    """
    shot = adb_screenshot_to_base64(device_id)
    if not shot:
        return "", []
    try:
        res = call_mcp_ocr_tool("ocr", {"base64_content": shot})
    except Exception as e:
        logger.warning(f"调用 MCP-OCR 失败: {e}")
        return "", []
    if isinstance(res, dict) and res.get("success"):
        return res.get("full_text", "") or "", res.get("lines", []) or []
    return "", []


def _top_region_text(lines, h, ratio=0.45):
    """
    取屏幕上部区域（约前 ratio 屏高）的文字并按阅读顺序合并。
    会话列表里最新的会话在最上方，只看顶部区域可避免把下方历史会话
    预览里的旧验证码误读出来。
    """
    parts = []
    for line in lines:
        try:
            ys = [r for r in line.get("y_ranges", []) if r]
            if not ys:
                continue
            top_y = min(r[0] for r in ys)
        except (TypeError, ValueError):
            continue
        if top_y <= h * ratio:
            parts.append(" ".join(line.get("texts", [])))
    return "\n".join(parts)


def _top_conversation_center(lines, w, h):
    """
    在会话列表区域（约 10%~45% 屏高）找最靠上的文字行，返回其中心坐标，
    用于点开最新（顶部）会话。找不到时返回 None，调用方据此跳过点击，
    避免 OCR 没拿到文本时按固定坐标盲点、把用户丢进读不到码的会话。
    """
    best = None
    for line in lines:
        try:
            xs = [r for r in line.get("x_ranges", []) if r]
            ys = [r for r in line.get("y_ranges", []) if r]
            if not xs or not ys:
                continue
            top_y = min(r[0] for r in ys)
            bot_y = max(r[1] for r in ys)
            yc = (top_y + bot_y) / 2
            xc = (min(r[0] for r in xs) + max(r[1] for r in xs)) / 2
        except (TypeError, ValueError):
            continue
        if h * 0.10 <= yc <= h * 0.45:
            if best is None or top_y < best[2]:
                best = (xc, yc, top_y)
    if best:
        return int(best[0]), int(best[1])
    return None


def _launch_default_sms_app(device_id):
    """启动系统默认短信 App，返回其包名（找不到或启动失败返回空字符串）。"""
    holder = ""
    data = _adb_json(device_id, "shell cmd role get-holder android.role.SMS")
    if data:
        holder = (data.get("stdout") or "").strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]*', holder or ""):
        for pkg in ("com.android.mms", "com.google.android.apps.messaging",
                    "com.android.messaging", "com.miui.mmsa"):
            data = _adb_json(device_id, f"shell pm list packages {pkg}")
            if data and f"package:{pkg}" in data.get("stdout", ""):
                holder = pkg
                break
    if not holder:
        return ""
    # 先 force-stop 保证从会话列表（而非上次停留的会话）开始，OCR 才确定
    run_adb_command(f"shell am force-stop {holder}", device_id)
    time.sleep(0.5)
    data = _adb_json(device_id, f"shell monkey -p {holder} -c android.intent.category.LAUNCHER 1")
    if not data:
        return ""
    return holder


def _get_focused_component(device_id):
    """
    获取当前焦点窗口的组件 "pkg/Activity"（如 com.tencent.mm/.ui.LauncherUI）。
    用于 UI 兜底前记住用户所在界面，读完验证码后原路返回，
    避免把用户丢回桌面。取不到时返回空串。
    """
    data = _adb_json(
        device_id,
        "shell dumpsys window 2>/dev/null | grep 'mCurrentFocus='",
    )
    out = data.get("stdout", "") if data else ""
    # 多屏设备会输出多行 mCurrentFocus（每 display 一行），
    # 且部分行是 null，逐行找第一个带组件名的
    for line in out.splitlines():
        m = re.search(r"mCurrentFocus=.*?([A-Za-z][\w.]*/[\w.]+)", line)
        if m:
            return m.group(1)
    return ""


def _short_component(component):
    """pkg/full.Activity → pkg/.Activity（dumpsys 输出里用的缩写形式）。"""
    pkg, _, act = (component or "").partition("/")
    if act.startswith(pkg + "."):
        act = "." + act[len(pkg) + 1:]
    return f"{pkg}/{act}"


def _get_focused_task_info(device_id, component):
    """
    从 dumpsys activity recents 的任务列表里，找包含焦点组件的那个任务的
    taskId/rootTaskId。用于 `cmd activity stack move-task` 按任务 ID 还原界面：
    该方式不受 Activity 是否导出(exported)限制，且能精确还原原任务的原页面
    （例如微信小程序是独立任务，am start -n / monkey 都只能打开微信主任务，
    到不了小程序页）。
    返回 (task_id, root_task_id) 字符串元组，找不到返回 None。
    """
    if not component:
        return None
    data = _adb_json(device_id, "shell dumpsys activity recents")
    out = data.get("stdout", "") if data else ""
    if not out:
        return None
    # 只解析 "Recent tasks:" 段落（"Visible recent tasks" 段落格式不同）
    start = out.find("Recent tasks:")
    if start < 0:
        return None
    end = out.find("Visible recent tasks")
    if end > start:
        out = out[start:end]
    else:
        out = out[start:]
    short = _short_component(component)
    for block in out.split("* Recent #"):
        if short not in block:
            continue
        m = re.search(r"taskId=(\d+)\s+rootTaskId=(\d+)", block)
        if m:
            return m.group(1), m.group(2)
    return None


def _focus_is_pkg(device_id, pkg, tries=1, delay=0.7):
    """
    轮询当前焦点组件是否属于 pkg，用来判断"还原"到底有没有真的生效。

    必须以真实焦点为准、而不是命令返回码：
    `cmd activity stack move-task X X true` 在部分 ROM（实测 HyperOS）上
    对根任务是**静默空操作**——返回码 0、无任何报错，界面却纹丝不动。
    这正是「打开短信点进一条会话后就出不来」的根因。
    """
    for i in range(max(1, tries)):
        try:
            cur = _get_focused_component(device_id)
        except Exception:
            cur = ""
        if cur and cur.split("/", 1)[0] == pkg:
            return True
        if i + 1 < tries:
            time.sleep(delay)
    return False


def _restore_screen_to(device_id, prev_component, sms_package, prev_task=None):
    """
    UI 兜底结束后，把界面还原到进入短信 App 之前的状态：
      - 无记录 / 之前是桌面(启动器) → 回桌面；
      - 之前就在短信 App 内 → 保持当前界面（是否从详情页退回由调用方负责）；
      - 其他 App → 依次尝试下面几步，**每一步都用真实焦点校验**，不生效就继续
        下一步（部分 ROM 的命令会"假成功"，只看返回码就会卡在短信界面）：
          1. am force-stop <短信App>：关掉我们刚拉起的 App，系统自然露出它
             下面的原任务。实测最可靠：不受 Activity 是否导出(exported)限制，
             小程序等非导出页面也能精确还原；
          2. cmd activity stack move-task <taskId> <rootTaskId> true；
          3. am start -n <组件>（仅对导出的 Activity 有效）；
          4. 该应用默认入口（monkey）；
          5. 都失败 → 回桌面。
    """
    comp = (prev_component or "").strip()
    if not comp or comp == "null":
        logger.info("UI兜底: 未记录到之前的界面，回到桌面")
        run_adb_command("shell input keyevent 3", device_id)
        return

    pkg = comp.split("/", 1)[0]
    if sms_package and pkg == sms_package:
        logger.info("UI兜底: 之前就在短信 App 内，保持当前界面")
        return

    low = pkg.lower()
    if "launcher" in low or low.rsplit(".", 1)[-1] == "home":
        logger.info(f"UI兜底: 之前是桌面/启动器({pkg})，回到桌面")
        run_adb_command("shell input keyevent 3", device_id)
        return

    # 1) 关掉临时拉起的短信 App，露出它下面的原任务（原页面）。
    #    这是实测最可靠的一条：不受 Activity 导出属性限制，小程序页也能精确还原。
    if sms_package:
        logger.info(f"UI兜底: 关闭临时拉起的短信 App({sms_package})，露出原界面")
        run_adb_command(f"shell am force-stop {sms_package}", device_id)
        if _focus_is_pkg(device_id, pkg, tries=4, delay=0.7):
            logger.info(f"UI兜底: 已还原到原界面 {comp}")
            return

    # 2) 按任务 ID 还原原任务（对未导出的 Activity 也有效）
    if prev_task:
        task_id, root_task_id = prev_task
        logger.info(
            f"UI兜底: 按原任务还原之前的界面 "
            f"(move-task {task_id} rootTask={root_task_id})")
        data = _adb_result(
            device_id,
            f"shell cmd activity stack move-task {task_id} {root_task_id} true",
        )
        if _focus_is_pkg(device_id, pkg, tries=3, delay=0.7):
            logger.info(f"UI兜底: 已还原到原界面 {comp}")
            return
        logger.warning(
            f"UI兜底: move-task 未生效({_adb_error_text(data)})，"
            f"尝试 am start -n {comp}")

    # 3) am start -n（仅对导出的 Activity 有效）
    logger.info(f"UI兜底: 还原之前的界面 am start -n {comp}")
    data = _adb_result(device_id, f"shell am start -n {comp} 2>&1")
    out2 = data.get("stdout", "") if data else ""
    if _focus_is_pkg(device_id, pkg, tries=2, delay=0.7):
        logger.info(f"UI兜底: 已还原到原界面 {comp}")
        return
    logger.warning(
        f"UI兜底: am start 未生效({out2.strip()[:120] or _adb_error_text(data)})，"
        f"尝试该应用默认入口")

    # 4) 该应用默认入口
    data = _adb_result(
        device_id,
        f"shell monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
    )
    out3 = data.get("stdout", "") if data else ""
    if _focus_is_pkg(device_id, pkg, tries=2, delay=0.7):
        logger.info(f"UI兜底: 已回到 {pkg} 默认入口")
        return
    # 5) 最后兜底：回桌面
    logger.warning(
        f"UI兜底: 默认入口也不可用({out3.strip()[:120]})，回到桌面")
    run_adb_command("shell input keyevent 3", device_id)


# UI 兜底最近一次的失败原因，供 read_verification_code 汇总到返回里（便于定位问题）
_ui_last_note = ""


def _leave_conversation_detail(device_id, sms_package, prev_component):
    """
    兜底读码时点进了会话详情页，读完按一次返回键退回收件箱列表，
    避免把用户留在「被我们点开的会话」里（用户原本可能根本不在短信 App）。
    只在当前焦点仍在本机短信 App、且不是用户原本所在页面时才按返回。
    """
    if not sms_package:
        return
    try:
        cur = _get_focused_component(device_id)
    except Exception:
        cur = ""
    if not cur or cur.split("/", 1)[0] != sms_package:
        return
    if prev_component and cur == prev_component:
        return
    logger.info("UI兜底: 从会话详情页返回列表")
    run_adb_command("shell input keyevent 4", device_id)  # KEYCODE_BACK
    time.sleep(0.5)


def _read_verification_code_via_ui(device_id, prev_component="",
                                   prev_task=None):
    """
    UI 兜底：打开默认短信 App，截图 OCR 会话列表，从最新（顶部）会话的
    预览里提取验证码；列表里读不到时，点击顶部会话进入详情页再 OCR 一次。

    适用于厂商 RCS/5G 聊天机器人消息库（如小米
    org.rcs.service.provider.rcs_chatbot）不向 shell 用户开放、
    content:// 途径读不到的场景。

    结束后通过 prev_component/prev_task 把界面还原到用户之前所在的 App
    （优先按任务 ID 精确还原原任务，而不是回到桌面），避免打断用户正在
    进行的流程。

    返回 (验证码, 识别到的文本) 或 None。
    """
    global _ui_last_note
    _ui_last_note = ""
    try:
        _wake_and_unlock(device_id)
        holder = _launch_default_sms_app(device_id)
        if not holder:
            logger.warning("UI 兜底: 未找到可用的短信 App")
            _ui_last_note = "未找到可用的短信 App"
            _restore_screen_to(device_id, prev_component, "", prev_task)
            return None
        time.sleep(3)  # 等待 App 启动完成

        w, h = _screen_size(device_id)

        # 1) 会话列表页：最新会话在最上方，预览通常带验证码；只取顶部区域，
        #    避免把下方历史会话里的旧验证码误读出来
        text, lines = _ocr_current_screen(device_id)
        top_text = _top_region_text(lines, h) if lines else text
        code = _extract_verification_code(top_text) if top_text else None

        # 2) 列表没读到：点开顶部（最新）会话进详情页再看。
        #    只有确实定位到会话行时才点：OCR 没返回任何文本时按固定坐标盲点，
        #    只会把用户丢进一个同样读不出码的会话里（还会连带还原失败卡在那里）
        tapped = False
        if not code:
            if not lines:
                logger.warning("UI兜底: 截图 OCR 未返回任何文本，跳过点击会话详情")
            else:
                center = _top_conversation_center(lines, w, h)
                if center is None:
                    logger.info("UI兜底: 未定位到会话行，跳过点击会话详情")
                else:
                    tap_x, tap_y = center
                    logger.info(f"UI兜底: 点开顶部会话 ({tap_x},{tap_y}) 再 OCR 一次")
                    run_adb_command(f"shell input tap {tap_x} {tap_y}", device_id)
                    tapped = True
                    time.sleep(2)
                    detail, _ = _ocr_current_screen(device_id)
                    if detail:
                        code = _extract_verification_code(detail)
                        if code:
                            text = detail

        # 点开过会话就先退回列表，别把用户留在详情页
        if tapped:
            _leave_conversation_detail(device_id, holder, prev_component)

        # 结束：无论读码成功与否，都还原到用户之前的界面
        _restore_screen_to(device_id, prev_component, holder, prev_task)
        if code and text:
            return code, text
        if not lines:
            _ui_last_note = "短信界面截图未识别出任何文本（截图或 OCR 服务可能异常）"
        elif tapped:
            _ui_last_note = "已点开顶部会话，仍未读到验证码"
        else:
            _ui_last_note = "会话列表顶部未见验证码，且未定位到可点击的会话行"
        return None
    except Exception as e:
        _ui_last_note = f"UI 兜底异常: {e}"
        logger.warning(f"UI 兜底读取验证码失败: {e}")
        return None


# 带“验证码/code/动态码”等前缀的 4-6 位数字
# 前缀与数字之间允许少量间隔（冒号、空格、"为/是/is" 等连接词），
# 例如 "验证码为 134595"、"code is 1234"、"验证码:652897"
_CODE_PREFIX_RE = re.compile(
    r'(?:验证码|code(?:\s+is)?|验证|verification|sms\s*code|动态码|校验码)'
    r'[：:\s为是]{0,4}(\d{4,6})',
    re.IGNORECASE)


def _extract_code_with_prefix(text):
    """只匹配带验证码类前缀的数字。用于回退到历史消息的场景，
    避免把 URL、订单号等噪声里的数字误认为验证码。"""
    if not text:
        return None
    m = _CODE_PREFIX_RE.search(text)
    return m.group(1) if m else None


def _extract_verification_code(text):
    """
    从文本中提取 4-6 位验证码：优先匹配带“验证码/code/动态码”等前缀的数字，
    否则匹配独立的 4-6 位数字（前后不是数字）。
    """
    if not text:
        return None
    m = _CODE_PREFIX_RE.search(text)
    if m:
        return m.group(1)
    m = re.search(r'(?<!\d)(\d{4,6})(?!\d)', text)
    return m.group(1) if m else None


@server.tool()
def read_verification_code(device_id: str = None) -> str:
    """
    读取最近5分钟内的短信（SMS/MMS/RCS）并提取验证码。

    实现逻辑：
    1. 查询 content://sms（传统短信）最近5分钟内的消息（按时间倒序）
    2. 再查询 MMS 数据库（很多机型上 RCS/5G 富媒体消息的正文存放在 MMS 库）
    3. 若前两个数据源没有近期消息，则枚举设备上的 ContentProvider
       （dumpsys content / dumpsys package），逐个查询 authority 名称包含
       sms/mms/ims/rcs/5g/chat 等关键字的消息库，覆盖厂商私有库里下发的
       RCS/5G 富媒体消息（如"5G 消息小助手"转发的验证码）
    4. 若上述 content:// 途径全部读不到（部分厂商如小米的 RCS 聊天机器人
       消息库不向 shell 用户开放），则兜底走 UI：唤醒屏幕、打开默认短信
       App、截图 OCR 会话列表提取验证码（首次和末次重试各做一次）；
       列表预览读不到时才会点开顶部会话再 OCR 一次——OCR 一张文本都没识别
       出来时**不会**按固定坐标盲点，避免把用户丢进读不到码的会话里；
       结束时把界面还原到调用前用户所在的 App/页面（而非桌面）：先关掉我们
       刚拉起的短信 App（系统会自然露出原任务，可还原小程序等非导出页面），
       再依次尝试按任务 ID move-task、am start -n、默认入口，每一步都用真实
       焦点校验（命令返回码 0 不代表真的生效），确保不会把用户留在短信页。
    5. 如果未收到消息，会重试最多6次，每次间隔10秒
    6. 提取最新的消息，使用正则表达式提取4-6位验证码
       （优先匹配带有"验证码"、"code"等前缀的数字）

    Args:
        device_id: 可选，指定设备 ID。仅有一个设备时无需指定。

    Returns:
        JSON 格式的结果，包含验证码和原始短信内容
        如果多次尝试后仍未收到短信，verification_code 返回空字符串
    """
    try:
        # 重试机制：最多尝试6次，每次间隔10秒
        max_retries = 6
        retry_interval = 10  # 秒

        # 当前时间戳（秒）与5分钟前的毫秒时间戳
        current_timestamp = int(time.time())
        since_ms = (current_timestamp - 300) * 1000

        # 消息类 provider 枚举结果在重试窗口内不会变化，且仅在需要查私有库
        # 时才执行（惰性 + 缓存，避免短信直接命中时白白 dumpsys）
        discovered_authorities = None

        candidates = []
        for attempt in range(max_retries):
            candidates = []

            # 数据源1：传统短信 content://sms
            sms_out = _content_query_raw(
                device_id, "content://sms",
                projection="address:date:body",
                sort="date DESC",
                where=f"date > {since_ms}",
            )
            for row in _parse_content_rows(sms_out):
                candidates.append({
                    "date_ms": _row_date_ms(row),
                    "text": _row_text(row),
                    "source": "content://sms",
                })

            # 数据源2：MMS 数据库（部分机型 RCS 消息存于此）
            candidates.extend(_query_recent_mms(device_id))

            # 数据源3：厂商私有消息 provider（RCS/5G 富媒体消息通常在这里）
            # 仅在前两个专用数据源没有近期消息时才启用，避免无谓查询
            has_recent = any(c.get("date_ms") and c["date_ms"] >= since_ms for c in candidates)
            if not has_recent:
                if discovered_authorities is None:
                    discovered_authorities = _discover_message_authorities(device_id)
                for auth in discovered_authorities:
                    try:
                        out = _content_query_raw(device_id, f"content://{auth}", limit=10)
                    except Exception as e:
                        logger.warning(f"查询 provider content://{auth} 失败: {e}")
                        continue
                    for row in _parse_content_rows(out):
                        candidates.append({
                            "date_ms": _row_date_ms(row),
                            "text": _row_text(row),
                            "source": f"content://{auth}",
                        })
                has_recent = any(c.get("date_ms") and c["date_ms"] >= since_ms for c in candidates)

            if has_recent:
                # 已获取到近期消息，跳出循环
                break

            # 数据源4（兜底）：截图+OCR 读短信 App 界面。
            # 部分厂商（如小米）的 RCS 聊天机器人消息库不向 shell 用户开放，
            # content:// 途径读不到，只能走 UI。首尾各做一次，避免每次重试都打开 App。
            if attempt == 0 or attempt == max_retries - 1:
                # 进入短信 App 前记录当前界面（组件 + 所属任务 ID），读完后原路返回，不打断用户流程
                prev_comp = _get_focused_component(device_id)
                ui_result = _read_verification_code_via_ui(
                    device_id,
                    prev_component=prev_comp,
                    prev_task=_get_focused_task_info(device_id, prev_comp),
                )
                if ui_result:
                    code, text = ui_result
                    return json.dumps({
                        "success": True,
                        "message": "成功提取验证码（来源: 短信App界面截图OCR）",
                        "verification_code": code,
                        "sms_body": text,
                        "source": "UI-OCR",
                        "date_ms": current_timestamp * 1000,
                        "raw_output": text,
                    }, ensure_ascii=False)

            logger.info(f"尝试 {attempt + 1}/{max_retries}: 最近5分钟内没有收到短信（SMS/MMS/RCS）")
            if attempt < max_retries - 1:
                time.sleep(retry_interval)

        # 如果所有尝试都没有获取到任何候选消息
        if not candidates:
            msg = f"在{max_retries}次尝试后仍未收到短信（含RCS/5G富媒体消息）"
            if _ui_last_note:
                msg += f"；UI 兜底读码结果：{_ui_last_note}"
            return json.dumps({
                "success": False,
                "message": msg,
                "verification_code": "",
                "sms_body": "",
                "raw_output": "",
                "ui_note": _ui_last_note
            }, ensure_ascii=False)

        # 按时间倒序（无时间字段的消息排最后），优先处理最新的
        candidates.sort(key=lambda c: c.get("date_ms") or 0, reverse=True)

        # 若存在5分钟内的消息，只在这些消息里提取（避免误用历史消息里的旧验证码）
        recent = [c for c in candidates if c.get("date_ms") and c["date_ms"] >= since_ms]
        scope = recent if recent else candidates
        # 回退到历史消息时收紧匹配（只认带前缀的数字），避免 URL/订单号等误报
        extract = _extract_verification_code if scope is recent else _extract_code_with_prefix

        # 提取验证码
        for c in scope:
            verification_code = extract(c.get("text", ""))
            if verification_code:
                return json.dumps({
                    "success": True,
                    "message": f"成功提取验证码（来源: {c.get('source')}）",
                    "verification_code": verification_code,
                    "sms_body": c.get("text", ""),
                    "source": c.get("source"),
                    "date_ms": c.get("date_ms"),
                    "raw_output": c.get("text", "")
                }, ensure_ascii=False)

        newest = scope[0]
        return json.dumps({
            "success": False,
            "message": "未能从短信中提取验证码",
            "verification_code": "",
            "sms_body": newest.get("text", ""),
            "source": newest.get("source"),
            "date_ms": newest.get("date_ms"),
            "raw_output": newest.get("text", ""),
            "ui_note": _ui_last_note
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"读取验证码失败: {str(e)}"
        }, ensure_ascii=False)


# ==================== 主入口 ====================

def main():
    """主入口函数，供 uvx 和命令行调用"""
    parser = argparse.ArgumentParser(description="Android Use MCP Server")
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
    
    logger.info(f"启动 Android Use MCP Server...")
    logger.info(f"传输协议: {args.transport}")
    
    if args.transport in ("sse", "streamable-http"):
        logger.info(f"监听地址: {args.host}:{args.port}")
        logger.info(f"挂载路径: {args.mount_path}")
        logger.info(f"访问地址: http://{args.host}:{args.port}{args.mount_path}")
    
    server.run(transport=args.transport, host=args.host, port=args.port, path=args.mount_path)


if __name__ == "__main__":
    main()

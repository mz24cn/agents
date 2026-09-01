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
6. read_verification_code - 读取最近5分钟内的短信并提取4-6位验证码（优先匹配带"验证码"、"code"等前缀的数字，支持重试机制）

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


@server.tool()
def read_verification_code(device_id: str = None) -> str:
    """
    读取最近5分钟内的短信并提取验证码。
    
    实现逻辑：
    1. 使用ADB命令获取最近5分钟内的短信内容（按时间倒序）
    2. 如果未收到短信，会重试最多6次，每次间隔10秒
    3. 提取第一条短信（最新的）
    4. 使用正则表达式提取4-6位验证码
    5. 优先提取带有"验证码"、"code"等前缀的数字
    
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
        
        sms_output = None
        for attempt in range(max_retries):
            # 步骤1：获取最近5分钟内的短信
            # 获取当前时间戳（秒）
            current_timestamp = int(time.time())
            # 5分钟前的时间戳（秒）
            five_minutes_ago = current_timestamp - 300
            # 转换为毫秒
            five_minutes_ago_ms = five_minutes_ago * 1000
            
            command = f'shell content query --uri content://sms --projection address:date:body --sort "date DESC" --where "date > {five_minutes_ago_ms}"'
            
            result = run_adb_command(command, device_id)
            result_data = json.loads(result)
            
            if not result_data.get("success"):
                error_msg = result_data.get('stderr', result_data.get('message', '获取短信失败'))
                logger.error(f"获取短信失败: {error_msg}")
                return json.dumps({
                    "success": False,
                    "message": error_msg,
                    "verification_code": "",
                    "sms_body": "",
                    "raw_output": ""
                }, ensure_ascii=False)
            
            sms_output = result_data.get("stdout", "")
            # 检查 stderr 是否包含权限错误或其它致命错误（部分设备 returncode=0 但 stderr 有异常）
            stderr_output = result_data.get("stderr", "")
            if stderr_output and ("Error" in stderr_output or "Exception" in stderr_output or "Permission" in stderr_output):
                logger.error(f"ADB 命令执行出错: {stderr_output}")
                return json.dumps({
                    "success": False,
                    "message": f"ADB 命令执行出错: {stderr_output}",
                    "verification_code": "",
                    "sms_body": "",
                    "raw_output": stderr_output
                }, ensure_ascii=False)

            if not sms_output or sms_output.strip() == "No result found.":
                logger.info(f"尝试 {attempt + 1}/{max_retries}: 最近5分钟内没有收到短信")
                if attempt < max_retries - 1:
                    time.sleep(retry_interval)
                continue
            
            # 成功获取到短信，跳出循环
            break
        
        # 如果所有尝试都失败，返回空验证码
        if not sms_output or sms_output.strip() == "No result found.":
            return json.dumps({
                "success": False,
                "message": f"在{max_retries}次尝试后仍未收到短信",
                "verification_code": "",
                "sms_body": "",
                "raw_output": ""
            }, ensure_ascii=False)
        
        # 步骤1.5：提取第一条短信（Row: 0）
        # 输出可能包含多条短信，每条以"Row: X"开头
        # 我们需要提取从"Row: 0"开始到"Row: 1"之前的内容
        lines = sms_output.split('\n')
        first_sms_lines = []
        in_first_sms = False
        for line in lines:
            if line.startswith('Row: 0'):
                in_first_sms = True
                first_sms_lines.append(line)
            elif in_first_sms and line.startswith('Row: 1'):
                # 遇到第二条短信，停止
                break
            elif in_first_sms:
                first_sms_lines.append(line)
        
        if not first_sms_lines:
            return json.dumps({
                "success": False,
                "message": "未找到第一条短信"
            }, ensure_ascii=False)
        
        first_sms_output = '\n'.join(first_sms_lines)
        
        # 步骤2：解析短信内容
        # 输出格式可能为：
        # 单行格式: Row: 0 address=10086, date=1780639504356, body=...
        # 多行格式: Row: 0
        #          address=10086
        #          date=1780639504356
        #          body=...
        
        body = ""
        # 先尝试单行解析（更常见的情况）
        body_start = first_sms_output.find('body=')
        if body_start != -1:
            # 找到 body= 的位置，提取后面的内容
            body_content = first_sms_output[body_start + 5:]  # 去掉 "body="
            # 如果有逗号，说明后面还有其他字段，只取到逗号之前
            comma_pos = body_content.find(',')
            if comma_pos != -1:
                body = body_content[:comma_pos].strip()
            else:
                body = body_content.strip()
        else:
            # 尝试多行解析（备用方案）
            lines = first_sms_output.strip().split('\n')
            body_lines = []
            in_body = False
            for line in lines:
                if line.startswith('body='):
                    body_part = line[5:]
                    body_lines.append(body_part)
                    in_body = True
                elif in_body and not line.startswith('Row:'):
                    body_lines.append(line)
                elif line.startswith('Row:'):
                    break
            body = '\n'.join(body_lines).strip()
        
        # 步骤3：提取验证码
        # 正则表达式：优先匹配带有前缀的4-6位数字
        # 前缀：验证码、code、CODE、验证、verification、sms code、动态码、校验码
        # 如果没找到带前缀的，则匹配独立的4-6位数字（但可能误匹配）
        
        # 首先尝试带前缀的匹配
        pattern_with_prefix = r'(?:验证码|code|CODE|验证|verification|sms\s*code|动态码|校验码)[：\:\s]*(\d{4,6})'
        match = re.search(pattern_with_prefix, body, re.IGNORECASE)
        
        if match:
            verification_code = match.group(1)
        else:
            # 尝试匹配4-6位独立数字（前后不是数字）
            pattern_standalone = r'(?<!\d)(\d{4,6})(?!\d)'
            match = re.search(pattern_standalone, body)
            if match:
                verification_code = match.group(1)
            else:
                verification_code = None
        
        # 步骤4：返回结果
        if verification_code:
            return json.dumps({
                "success": True,
                "message": "成功提取验证码",
                "verification_code": verification_code,
                "sms_body": body,
                "raw_output": first_sms_output
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "success": False,
                "message": "未能从短信中提取验证码",
                "sms_body": body,
                "raw_output": first_sms_output
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

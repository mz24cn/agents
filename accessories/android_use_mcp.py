"""
Android Use MCP Server

用于在 Agent Service 宿主机和远程机器及远程机器上的安卓设备之间进行交互。
运行在远程机器上，提供基于 ADB 的各种安卓设备操作工具。

前置条件：
pip install fastmcp

功能：
1. save_file_from_base64 - 将 base64 内容保存为文件。对大模型来说，提供本地文件路径即可，底层会自动读取并编码。
2. read_file_to_base64 - 读取文件并以 base64 返回。对大模型来说，最终拿到的是本地文件路径（Agent Service 底层会将 base64 替换为本地路径）。
3. save_file_to_phone_gallery - 将 base64 图片保存到手机相册
4. run_adb_command - 执行任意 ADB 命令
5. find_and_click - 查找并点击屏幕上的文字或图片。先截屏，然后调用 OCR 工具定位目标，最后执行点击操作。

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
import sys
import json
import logging
import argparse
import subprocess
import urllib.request
import urllib.error

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_mcp_server(host: str = "0.0.0.0", port: int = 8000) -> FastMCP:
    """创建 MCP server 实例并注册工具"""
    server = FastMCP(
        "android-use",
        instructions="安卓设备操作服务：基于 ADB 提供文件传输、设备控制等工具",
        host=host,
        port=port,
    )
    
    @server.tool()
    def save_file_from_base64(base64_content: str, remote_path: str) -> str:
        """
        将 base64 编码的内容保存到指定的远程文件路径。
        
        Args:
            base64_content: base64 编码的文件内容。提供本地文件路径即可，底层会自动读取并编码。
            remote_path: 目标文件完整路径（Windows 格式，如 C:\\temp\\image.png）
        
        Returns:
            JSON 格式的操作结果
        """
        try:
            # 确保目标目录存在
            target_dir = os.path.dirname(remote_path)
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)
                logger.info(f"创建目录: {target_dir}")
            
            # 解码并保存
            file_data = base64.b64decode(base64_content)
            with open(remote_path, 'wb') as f:
                f.write(file_data)
            
            result = {
                "success": True,
                "message": f"文件已保存到: {remote_path}",
                "remote_path": remote_path,
                "file_size": len(file_data)
            }
            logger.info(f"文件保存成功: {remote_path} ({len(file_data)} bytes)")
            return json.dumps(result, ensure_ascii=False)
            
        except base64.binascii.Error as e:
            error_msg = f"Base64 解码失败: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)
        except Exception as e:
            error_msg = f"保存文件失败: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)

    @server.tool()
    def read_file_to_base64(remote_path: str) -> str:
        """
        读取远程机器的指定文件并以 base64 编码返回。Agent Service 底层会拦截返回值，将 base64 替换为本地文件路径，大模型最终拿到的是本地路径。

        Args:
            remote_path: 要读取的文件完整路径（远程机器上的路径，如 C:\\temp\\image.png）

        Returns:
            JSON 格式的结果，包含 base64 编码的文件内容，或本地文件路径。
        """
        try:
            if not os.path.exists(remote_path):
                return json.dumps({
                    "success": False,
                    "message": f"文件不存在: {remote_path}"
                }, ensure_ascii=False)

            if not os.path.isfile(remote_path):
                return json.dumps({
                    "success": False,
                    "message": f"路径不是文件: {remote_path}"
                }, ensure_ascii=False)

            # 读取文件并编码为 base64
            with open(remote_path, 'rb') as f:
                file_data = f.read()

            base64_content = base64.b64encode(file_data).decode('utf-8')

            result = {
                "success": True,
                "message": f"文件读取成功: {remote_path}",
                "remote_path": remote_path,
                "file_size": len(file_data),
                "base64_content": base64_content
            }
            logger.info(f"文件读取成功: {remote_path} ({len(file_data)} bytes)")
            return json.dumps(result, ensure_ascii=False)

        except PermissionError as e:
            error_msg = f"无权限读取文件: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)
        except Exception as e:
            error_msg = f"读取文件失败: {str(e)}"
            logger.error(error_msg)
            return json.dumps({"success": False, "message": error_msg}, ensure_ascii=False)

    @server.tool()
    def save_file_to_phone_gallery(base64_content: str, filename: str) -> str:
        """
        将 base64 编码的图片保存到手机相册。
        
        Args:
            base64_content: base64 编码的图片内容。提供本地文件路径即可，底层会自动读取并编码。
            filename: 保存到手机相册的文件名（如 screenshot.png）
        
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
            push_result = run_adb_command(f"push {temp_path} {phone_path}")
            push_data = json.loads(push_result)
            
            if not push_data.get("success"):
                return json.dumps({
                    "success": False,
                    "message": f"ADB 推送失败: {push_data.get('stderr', push_data.get('message', ''))}"
                }, ensure_ascii=False)
            
            logger.info(f"文件已推送到手机: {phone_path}")
            
            # 步骤 3: 触发媒体扫描
            scan_result = run_adb_command(
                f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{phone_path}"
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
    
    def call_ocr_tool(tool_name: str, arguments: dict) -> dict:
        """
        通过 /v1/tools/call 接口调用 OCR 工具。
        
        Args:
            tool_name: 工具名称，如 "mcp-OCR-locate" 或 "mcp-OCR-find_image"
            arguments: 工具参数
            
        Returns:
            工具调用结果字典
        """
        agent_service_url = os.getenv("AGENT_SERVICE_URL", "http://localhost:7988")
        url = f"{agent_service_url}/v1/tools/call"
        
        payload = {
            "tool_id": tool_name,
            "arguments": arguments,
            "format": "json"
        }
        
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            error_msg = f"调用 OCR 工具失败: HTTP {e.code} - {body}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"调用 OCR 工具失败: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
    
    @server.tool()
    def find_and_click(keyword_or_image_file: str, is_image: bool = False, device_id: str = None) -> str:
        """
        查找并点击屏幕上的文字或图片。
        
        实现逻辑：
        1. 使用 ADB 截取手机屏幕
        2. 调用 OCR 工具（locate 或 find_image）定位目标
        3. 根据返回的坐标使用 ADB 点击
        
        Args:
            keyword_or_image_file: 如果 is_image=True，则为小图片路径；否则为要查找的关键词。
            is_image: 是否为图片模式。True 表示图片模式，False 表示文字模式。
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
            
            logger.info("截屏成功，正在调用 OCR 工具...")
            
            # 步骤 2: 根据模式调用不同的 OCR 工具
            ocr_tool_prefix = os.getenv("OCR_TOOL", "mcp-OCR")
            
            if is_image:
                # 图片模式：调用 find_image
                tool_name = f"{ocr_tool_prefix}-find_image"
                
                # 读取小图片并转换为 base64
                if not os.path.exists(keyword_or_image_file):
                    return json.dumps({
                        "success": False,
                        "message": f"图片文件不存在: {keyword_or_image_file}"
                    }, ensure_ascii=False)
                
                with open(keyword_or_image_file, 'rb') as f:
                    small_img_bytes = f.read()
                small_img_base64 = base64.b64encode(small_img_bytes).decode('utf-8')
                
                arguments = {
                    "base64_big_img": screenshot_base64,
                    "base64_small_img": small_img_base64
                }
            else:
                # 文字模式：调用 locate
                tool_name = f"{ocr_tool_prefix}-locate"
                arguments = {
                    "base64_content": screenshot_base64
                }
            
            # 调用 OCR 工具
            ocr_result = call_ocr_tool(tool_name, arguments)
            if not ocr_result.get("success"):
                return json.dumps({
                    "success": False,
                    "message": f"OCR 工具调用失败: {ocr_result.get('message', '未知错误')}"
                }, ensure_ascii=False)
            
            # 步骤 3: 解析坐标并执行点击
            if is_image:
                # 图片模式
                if not ocr_result.get("success"):
                    return json.dumps({
                        "success": False,
                        "message": f"图片查找失败: {ocr_result.get('message', '未找到匹配的图片')}"
                    }, ensure_ascii=False)
                
                x_range = ocr_result.get("x_range", [])
                y_range = ocr_result.get("y_range", [])
                
                if not x_range or not y_range:
                    return json.dumps({
                        "success": False,
                        "message": "未找到匹配的图片"
                    }, ensure_ascii=False)
                
                # 计算中心点坐标
                center_x = (x_range[0] + x_range[1]) // 2
                center_y = (y_range[0] + y_range[1]) // 2
                score = ocr_result.get("score", 0)
                
                logger.info(f"找到图片，中心坐标: ({center_x}, {center_y})，置信度: {score:.2f}")
                
            else:
                # 文字模式
                locations = ocr_result.get("locations", [])
                
                # 查找包含关键词的文字
                target_location = None
                for loc in locations:
                    text = loc.get("text", "")
                    if keyword_or_image_file in text:
                        target_location = loc
                        break
                
                if not target_location:
                    return json.dumps({
                        "success": False,
                        "message": f"未找到包含关键词 '{keyword_or_image_file}' 的文字",
                        "found_texts": [loc.get("text", "") for loc in locations]
                    }, ensure_ascii=False)
                
                x_range = target_location.get("x_range", [])
                y_range = target_location.get("y_range", [])
                
                if not x_range or not y_range:
                    return json.dumps({
                        "success": False,
                        "message": "无法获取文字坐标"
                    }, ensure_ascii=False)
                
                # 计算中心点坐标
                center_x = (x_range[0] + x_range[1]) // 2
                center_y = (y_range[0] + y_range[1]) // 2
                score = target_location.get("score", 0)
                
                logger.info(f"找到文字 '{target_location.get('text')}'，中心坐标: ({center_x}, {center_y})，置信度: {score:.2f}")
            
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
                    "target": keyword_or_image_file
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
    
    return server


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
    
    # 创建 MCP server（传递 host 和 port）
    mcp = create_mcp_server(host=args.host, port=args.port)
    
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

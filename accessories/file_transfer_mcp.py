"""
File Transfer MCP Server

用于在 Agent Service 宿主机和远程机器之间传输文件。
运行在远程机器上，接收 base64 编码的文件内容并保存到本地。

前置条件：
pip install fastmcp

功能：
1. save_file_from_base64 - 将 base64 内容保存为文件。对大模型来说，提供本地文件路径即可，底层会自动读取并编码。
2. save_file_to_phone_gallery - 将 base64 图片保存到手机相册

使用方法：
    # HTTP 方式运行（默认）
    python file_transfer_mcp.py
    
    # 指定端口
    python file_transfer_mcp.py --port 8001
    
    # SSE 方式运行
    python file_transfer_mcp.py --transport sse
    
    # stdio 方式运行（本地调试）
    python file_transfer_mcp.py --transport stdio
    
MCP Server 配置（添加到 Agent Service）：
{
  "mcpServers": {
    "file-transfer": {
      "url": "http://<remote-host>:8001/mcp",
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

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ADB 路径配置（根据实际环境调整）
ADB_PATH = os.environ.get(
    "ADB_PATH",
    "C:\\platform-tools\\adb.exe"
)


def create_mcp_server(host: str = "0.0.0.0", port: int = 8000) -> FastMCP:
    """创建 MCP server 实例并注册工具"""
    server = FastMCP(
        "file-transfer",
        instructions="文件传输服务：支持 base64 文件保存和手机相册推送",
        host=host,
        port=port,
    )
    
    @server.tool()
    def save_file_from_base64(base64_content: str, target_path: str) -> str:
        """
        将 base64 编码的内容保存到指定文件路径。
        
        Args:
            base64_content: base64 编码的文件内容。提供本地文件路径即可，底层会自动读取并编码。
            target_path: 目标文件完整路径（Windows 格式，如 C:\\temp\\image.png）
        
        Returns:
            JSON 格式的操作结果
        """
        try:
            # 确保目标目录存在
            target_dir = os.path.dirname(target_path)
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)
                logger.info(f"创建目录: {target_dir}")
            
            # 解码并保存
            file_data = base64.b64decode(base64_content)
            with open(target_path, 'wb') as f:
                f.write(file_data)
            
            result = {
                "success": True,
                "message": f"文件已保存到: {target_path}",
                "file_path": target_path,
                "file_size": len(file_data)
            }
            logger.info(f"文件保存成功: {target_path} ({len(file_data)} bytes)")
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
    def save_file_to_phone_gallery(base64_content: str, filename: str) -> str:
        """
        将 base64 编码的图片保存到手机相册（通过 ADB 推送）。
        
        Args:
            base64_content: base64 编码的图片内容。提供本地文件路径即可，底层会自动读取并编码。
            filename: 文件名（如 screenshot.png）
        
        Returns:
            JSON 格式的操作结果
        """
        import subprocess
        
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
            
            # 步骤 2: 检查 ADB 是否可用
            if not os.path.exists(ADB_PATH):
                return json.dumps({
                    "success": False,
                    "message": f"ADB 不存在: {ADB_PATH}"
                }, ensure_ascii=False)
            
            # 步骤 3: 通过 ADB 推送到手机
            phone_path = f"/sdcard/DCIM/Camera/{filename}"
            
            # 推送文件
            push_cmd = [ADB_PATH, "push", temp_path, phone_path]
            logger.info(f"执行命令: {' '.join(push_cmd)}")
            push_result = subprocess.run(
                push_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if push_result.returncode != 0:
                return json.dumps({
                    "success": False,
                    "message": f"ADB 推送失败: {push_result.stderr}"
                }, ensure_ascii=False)
            
            logger.info(f"文件已推送到手机: {phone_path}")
            
            # 步骤 4: 触发媒体扫描
            scan_cmd = [
                ADB_PATH, "shell", "am", "broadcast",
                "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", f"file://{phone_path}"
            ]
            subprocess.run(scan_cmd, capture_output=True, timeout=10)
            logger.info("媒体扫描已触发")
            
            # 步骤 5: 清理临时文件
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
            
        except subprocess.TimeoutExpired:
            return json.dumps({
                "success": False,
                "message": "ADB 命令执行超时"
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
    def check_adb_connection() -> str:
        """
        检查 ADB 连接状态。
        
        Returns:
            JSON 格式的连接状态信息
        """
        import subprocess
        
        try:
            if not os.path.exists(ADB_PATH):
                return json.dumps({
                    "success": False,
                    "message": f"ADB 不存在: {ADB_PATH}"
                }, ensure_ascii=False)
            
            # 检查设备连接
            result = subprocess.run(
                [ADB_PATH, "devices"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            devices = []
            for line in result.stdout.strip().split('\n')[1:]:
                if line.strip() and '\t' in line:
                    device_id, status = line.strip().split('\t')
                    devices.append({"id": device_id, "status": status})
            
            return json.dumps({
                "success": True,
                "message": "ADB 连接检查完成",
                "devices": devices,
                "device_count": len(devices)
            }, ensure_ascii=False)
            
        except Exception as e:
            return json.dumps({
                "success": False,
                "message": f"ADB 检查失败: {str(e)}"
            }, ensure_ascii=False)
    
    return server


def main():
    """主入口函数，供 uvx 和命令行调用"""
    parser = argparse.ArgumentParser(description="File Transfer MCP Server")
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
    
    logger.info(f"启动 File Transfer MCP Server...")
    logger.info(f"传输协议: {args.transport}")
    logger.info(f"ADB 路径: {ADB_PATH}")
    
    if args.transport in ("sse", "streamable-http"):
        logger.info(f"监听地址: {args.host}:{args.port}")
        logger.info(f"挂载路径: {args.mount_path}")
        logger.info(f"访问地址: http://{args.host}:{args.port}{args.mount_path}")
    
    # 创建 MCP server（传递 host 和 port）
    mcp = create_mcp_server(host=args.host, port=args.port)
    
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

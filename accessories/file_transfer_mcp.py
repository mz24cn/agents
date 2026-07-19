"""
File Transfer MCP Server

用于在 Agent Service 宿主机和远程机器及远程机器上传输文件。
运行在远程机器上，接收 base64 编码的文件内容并保存到本地。

前置条件：
pip install fastmcp

功能：
1. save_file_from_base64 - 将 base64 内容保存为文件。对大模型来说，提供本地文件路径即可，底层会自动读取并编码。
2. read_file_to_base64 - 读取文件并以 base64 返回。对大模型来说，最终拿到的是本地文件路径（Agent Service 底层会将 base64 替换为本地路径）。

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

from fastmcp import FastMCP

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 全局 MCP Server 实例
server = FastMCP(
    "file-transfer",
    instructions="文件传输服务：支持 base64 文件保存/读取",
)


@server.tool()
def save_file_from_base64(base64_content: str, remote_path: str) -> str:
    """
    将 base64 编码的内容保存到指定文件路径。
    
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
    读取指定文件并以 base64 编码返回。与 save_file_from_base64 对称。

    在 MCP Server 侧（远程机器），读取本地文件并返回 base64 编码内容。
    Agent Service 底层会拦截返回值，将 base64 替换为本地文件路径，大模型最终拿到的是本地路径。

    Args:
        remote_path: 要读取的文件完整路径（远程机器上的路径，如 C:\\temp\\image.png）

    Returns:
        JSON 格式的结果，包含 base64 编码的文件内容
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


# ==================== 主入口 ====================

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
    
    if args.transport in ("sse", "streamable-http"):
        logger.info(f"监听地址: {args.host}:{args.port}")
        logger.info(f"挂载路径: {args.mount_path}")
        logger.info(f"访问地址: http://{args.host}:{args.port}{args.mount_path}")
    
    server.run(transport=args.transport, host=args.host, port=args.port, path=args.mount_path)


if __name__ == "__main__":
    main()

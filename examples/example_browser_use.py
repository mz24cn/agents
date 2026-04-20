#!/usr/bin/env python3
"""
使用示例：通过 RuntimeHTTPServer 远程调用大模型 + chrome-devtools MCP 控制浏览器

两种模式：
  --server  启动 HTTP Server（注册模型 + MCP 工具），等待客户端请求
  无参数    作为客户端，调用 Server 的 /v1/infer/stream 和 /v1/tools/call

演示流程：
  1. Server 端连接 chrome-devtools-mcp，注册浏览器控制工具
  2. Client 调用 /v1/tools/call 直接打开百度首页（确定性操作）
  3. Client 调用 /v1/infer/stream，让大模型查看网页内容并关闭页面

用法：
  # 终端 1：启动 Server
  python examples/example_browser_use.py --server

  # 终端 2：运行 Client
  python examples/example_browser_use.py

前置条件：
  - Ollama 服务运行在 localhost:11434，已拉取 qwen3:14b
  - Chrome/Chromium 以 --remote-debugging-port=9222 启动
  - 已安装 npx（用于启动 chrome-devtools-mcp）
"""

import sys
import os
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
BOLD = "\033[1m"

# ── Server Mode ──

def run_server():
    """启动 HTTP Server，注册模型和 chrome-devtools MCP 工具。"""
    from runtime import (
        ModelConfig, ModelRegistry, ToolRegistry, Runtime, RuntimeHTTPServer,
    )
    from runtime.mcp_client import MCPClientManager

    MCP_CONFIG = {
        "mcpServers": {
            "chrome-devtools": {
                "command": "npx",
                "args": ["-y", "chrome-devtools-mcp@latest",
                         "--browser-url=http://localhost:9222"],
            }
        }
    }

    # 1. 注册模型
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="qwen3",
        api_base="http://localhost:11434",
        model_name="qwen3:14b",
        api_protocol="ollama",
        model_type="llm",
        generate_params={"temperature": 0.7},
    ))

    # 2. 连接 MCP
    tool_registry = ToolRegistry()
    mcp = MCPClientManager()

    print(">>> 正在连接 chrome-devtools MCP Server...")
    mcp.load_config(MCP_CONFIG)

    # load_config 只注册配置，需要显式调用 get_tools 来连接并发现工具
    all_tools = []
    for server_name in MCP_CONFIG["mcpServers"]:
        tools = mcp.get_tools(server_name)
        for t in tools:
            tool_registry.register(t)
        all_tools.extend(tools)

    for t in all_tools:
        print(f"    {t.tool_id}  ({t.mcp_server_name})")
    print(f">>> 共 {len(all_tools)} 个工具就绪\n")

    # 3. 创建 Runtime + Server
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
        mcp_manager=mcp,
    )
    server = RuntimeHTTPServer(runtime, host=SERVER_HOST, port=SERVER_PORT)

    print(f">>> Server 启动在 http://{SERVER_HOST}:{SERVER_PORT}")
    print(">>> 按 Ctrl+C 停止\n")
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n>>> 正在关闭...")
        server.stop()
        mcp.disconnect_all()
        print(">>> 已停止")


# ── Client Mode ──

def api_post_json(path, body):
    """向 Server 发送 JSON POST 请求，返回解析后的 dict。"""
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_stream(path, body):
    """向 Server 发送 SSE 流式请求，逐行 yield 解析后的 dict。"""
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:]  # strip "data: "
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue
    finally:
        resp.close()


def run_client():
    """作为客户端调用 Server API。"""

    # 0. 检查 Server 是否可达
    try:
        tools_resp = api_post_json("/v1/tools", {})  # GET via POST won't work
    except Exception:
        pass
    # 用 GET 获取工具列表
    try:
        url = f"http://{SERVER_HOST}:{SERVER_PORT}/v1/tools"
        with urllib.request.urlopen(url) as resp:
            tools_data = json.loads(resp.read().decode("utf-8"))
        tool_ids = [t["tool_id"] for t in tools_data.get("tools", [])]
        print(f"{DIM}Server 工具: {tool_ids}{RESET}\n")
    except Exception as e:
        print(f"{RED}无法连接 Server (http://{SERVER_HOST}:{SERVER_PORT}): {e}{RESET}")
        print(f"请先在另一个终端运行: python examples/example_browser_use.py --server")
        sys.exit(1)

    # 1. 直接调用工具：打开百度首页（确定性操作）
    print(f"{BOLD}>>> 直接调用 /v1/tools/call 打开百度首页{RESET}")

    # 从工具列表中找 new_page 的 tool_id
    new_page_tool_id = None
    for tid in tool_ids:
        if "new" in tid.lower() and "page" in tid.lower():
            new_page_tool_id = tid
            break
    if not new_page_tool_id:
        new_page_tool_id = "mcp-chrome-devtools-new_page"  # fallback
        print(f"{DIM}未找到 new_page 工具，尝试默认 ID: {new_page_tool_id}{RESET}")

    try:
        open_result = api_post_json("/v1/tools/call", {
            "tool_id": new_page_tool_id,
            "arguments": {"url": "https://www.baidu.com"},
        })
        print(f"{CYAN}结果: {json.dumps(open_result, ensure_ascii=False, indent=2)}{RESET}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"{RED}打开页面失败: {e.code} {body}{RESET}")
        print(f"\n{DIM}>>> 完成{RESET}")
        return
    except Exception as e:
        print(f"{RED}打开页面失败: {e}{RESET}")
        print(f"\n{DIM}>>> 完成{RESET}")
        return

    # 2. 流式推理：让大模型查看网页内容并关闭页面
    print(f"\n{BOLD}[用户]{RESET} 报告当前网页内容，然后报告打开的所有页面地址\n")

    in_thinking = False
    in_content = False

    for event in api_stream("/v1/infer/stream", {
        "model_id": "qwen3",
        "tool_ids": tool_ids,
        "messages": [
            {"role": "system", "content": (
                "你是一个浏览器自动化助手，可以通过工具控制 Chrome 浏览器。"
                "请根据用户指令操作浏览器，并用中文回复结果。"
            )},
            {"role": "user", "content": "报告当前网页内容，然后报告打开的所有页面地址"},
        ],
        "max_tool_rounds": 10,
    }):
        role = event.get("role", "")
        if role == "assistant":
            thinking = event.get("thinking", "")
            content = event.get("content", "")
            # Prefer tool_calls list (parallel calls)
            tool_calls = event.get("tool_calls")

            if thinking:
                if not in_thinking:
                    print(f"\n{DIM}[思考] ", end="", flush=True)
                    in_thinking = True
                    in_content = False
                print(thinking, end="", flush=True)
            if content:
                if in_thinking:
                    print(RESET)
                    in_thinking = False
                if not in_content:
                    print(f"\n{GREEN}", end="", flush=True)
                    in_content = True
                print(content, end="", flush=True)
            if tool_calls:
                if in_thinking:
                    print(RESET)
                    in_thinking = False
                if in_content:
                    print(RESET)
                    in_content = False
                for tc in tool_calls:
                    args_str = str(tc.get("arguments", "{}"))
                    if len(args_str) > 150:
                        args_str = args_str[:150] + "..."
                    print(f"\n{YELLOW}[调用] {tc['name']}({args_str}){RESET}", flush=True)

        elif role == "tool":
            name = event.get("name", "?")
            result = event.get("content", "")
            preview = result[:400] + ("..." if len(result) > 400 else "")
            print(f"\n{CYAN}[工具 {name}] {preview}{RESET}", flush=True)

    if in_thinking or in_content:
        print(RESET)

    print(f"\n{DIM}>>> 完成{RESET}")


# ── Entry Point ──

if __name__ == "__main__":
    if "--server" in sys.argv:
        run_server()
    else:
        run_client()

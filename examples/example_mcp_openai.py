#!/usr/bin/env python3
"""
使用示例：通过 runtime 调用 OpenAI 兼容 API + MCP 工具

演示：
  1. 连接 OpenAI 兼容 API（支持 OpenAI 官方、Ollama、vLLM、LiteLLM 等）
  2. 通过 load_config 一行连接两个 MCP Server（time、fetch）并注册工具
  3. 询问当前时间
  4. 下载一个网页

运行方式：
  python examples/example_mcp_openai.py              # 非流式（默认）
  python examples/example_mcp_openai.py --stream      # 流式输出

前置条件：
  - 设置环境变量 OPENAI_API_KEY（如使用 OpenAI 官方）
  - 或者使用 Ollama 等本地服务（无需 API Key）
  - 已安装 uv/uvx（用于启动 MCP Server）

配置说明：
  本示例默认使用 Ollama 的 OpenAI 兼容端点（http://localhost:11434/v1）。
  如需切换到其他服务，修改下方 API_BASE、MODEL_NAME、API_KEY 即可：

  # OpenAI 官方（协议层自动拼接 /v1/chat/completions，所以不要带 /v1）
  API_BASE = "https://api.openai.com"
  MODEL_NAME = "gpt-4o"
  API_KEY = os.environ.get("OPENAI_API_KEY", "")

  # vLLM
  API_BASE = "http://localhost:8000"
  MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
  API_KEY = "token-abc123"
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime import (
    ModelConfig,
    ModelRegistry,
    ToolRegistry,
    Runtime,
    InferenceRequest,
    Message,
)
from runtime.mcp_client import MCPClientManager

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD = "\033[1m"

# ============================================================
# 模型配置 —— 修改这三行即可切换不同的 OpenAI 兼容服务
# ============================================================
API_BASE = "http://localhost:11434"              # Ollama OpenAI 兼容端点（协议层自动拼接 /v1/chat/completions）
MODEL_NAME = "qwen3:14b"                        # Ollama 中的模型名
API_KEY = os.environ.get("OPENAI_API_KEY", "")  # Ollama 不需要 key，留空即可


# MCP 配置（与 Kiro / Claude Desktop 格式一致）
MCP_CONFIG = {
    "mcpServers": {
        "time": {
            "command": "uvx",
            "args": ["mcp-server-time", "--local-timezone=Asia/Shanghai"],
        },
        "fetch": {
            "command": "uvx",
            "args": ["mcp-server-fetch"],
        },
    }
}


def print_result(result):
    """打印推理结果的对话历史（非流式模式）。"""
    print(f"\n成功: {result.success}")
    if result.error:
        print(f"错误: {result.error}")
    print("\n--- 对话历史 ---")
    for msg in result.messages:
        if msg.role == "user":
            print(f"\n[用户] {msg.content}")
        elif msg.role == "assistant":
            if msg.thinking:
                print(f"\n[思考] {msg.thinking}")
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"\n[助手] 调用工具: {tc['name']}({tc.get('arguments', '{}')})")
            if msg.content:
                print(f"\n[助手] {msg.content}")
        elif msg.role == "function":
            preview = msg.content[:300] + ("..." if len(msg.content) > 300 else "")
            print(f"\n[工具 {msg.name}] {preview}")


def run_stream(runtime, request):
    """流式推理，实时输出。"""
    in_thinking = False
    in_content = False

    for msg in runtime.infer_stream(request):
        if msg.role == "assistant":
            if msg.thinking:
                if not in_thinking:
                    print(f"\n{DIM}[思考] ", end="", flush=True)
                    in_thinking = True
                    in_content = False
                print(msg.thinking, end="", flush=True)
            if msg.content:
                if in_thinking:
                    print(RESET)
                    in_thinking = False
                if not in_content:
                    print(f"\n{GREEN}", end="", flush=True)
                    in_content = True
                print(msg.content, end="", flush=True)
            if msg.tool_calls:
                if in_thinking:
                    print(RESET)
                    in_thinking = False
                if in_content:
                    print(RESET)
                    in_content = False
                for tc in msg.tool_calls:
                    args_str = str(tc.get("arguments", "{}"))
                    if len(args_str) > 150:
                        args_str = args_str[:150] + "..."
                    print(f"\n{YELLOW}[调用] {tc['name']}({args_str}){RESET}", flush=True)
        elif msg.role == "function":
            preview = msg.content[:300] + ("..." if len(msg.content) > 300 else "")
            print(f"\n{CYAN}[工具 {msg.name}] {preview}{RESET}", flush=True)

    if in_thinking or in_content:
        print(RESET)


def main():
    stream = "--stream" in sys.argv

    # 1. 注册模型 —— 使用 OpenAI 兼容协议
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="openai-compatible",
        api_base=API_BASE,
        model_name=MODEL_NAME,
        api_key=API_KEY,
        api_protocol="openai",              # 关键区别：使用 openai 协议
        model_type="llm",
        generate_params={"temperature": 0.7},
    ))

    # 2. 一行搞定：连接所有 MCP Server + 发现工具 + 注册到 ToolRegistry
    tool_registry = ToolRegistry()
    mcp = MCPClientManager()

    print(">>> 正在连接 MCP Server 并发现工具...")
    mcp.load_config(MCP_CONFIG)

    # load_config 只注册配置，需要显式调用 get_tools 来连接并发现工具
    all_tools = []
    for server_name in MCP_CONFIG["mcpServers"]:
        tools = mcp.get_tools(server_name)
        for t in tools:
            tool_registry.register(t)
        all_tools.extend(tools)

    all_tool_ids = [t.tool_id for t in all_tools]
    for t in all_tools:
        print(f"    {t.tool_id}  ({t.mcp_server_name})")
    print(f">>> 共 {len(all_tools)} 个工具就绪")
    print(f">>> 模型: {MODEL_NAME} @ {API_BASE} (openai 协议)")
    if stream:
        print(">>> 模式: 流式输出\n")
    else:
        print(">>> 模式: 非流式（添加 --stream 启用流式）\n")

    # 3. 创建 Runtime
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
        mcp_manager=mcp,
    )

    # 4. 对话 1：询问当前时间
    print("=" * 60)
    print("对话 1：询问当前时间")
    print("=" * 60)
    req1 = InferenceRequest(
        model_id="openai-compatible",
        tool_ids=all_tool_ids,
        text="现在几点了？请告诉我当前的日期和时间。",
        max_tool_rounds=5,
    )
    if stream:
        run_stream(runtime, req1)
    else:
        print_result(runtime.infer(req1))

    # 5. 对话 2：下载网页
    print("\n\n" + "=" * 60)
    print("对话 2：下载网页内容")
    print("=" * 60)
    req2 = InferenceRequest(
        model_id="openai-compatible",
        tool_ids=all_tool_ids,
        text="请帮我下载 https://httpbin.org/html 这个网页，并简要描述网页内容。",
        max_tool_rounds=5,
    )
    if stream:
        run_stream(runtime, req2)
    else:
        print_result(runtime.infer(req2))

    # 6. 清理
    print("\n\n>>> 断开所有 MCP 连接...")
    mcp.disconnect_all()
    print(">>> 完成")


if __name__ == "__main__":
    main()

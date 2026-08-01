#!/usr/bin/env python3
"""
使用示例：通过 runtime 调用 Ollama 大模型 + 内置工具

演示：
  1. 连接 Ollama qwen3.5:9b 模型（localhost:11434）
  2. 注册内置工具（exec_shell、fetch 等）
  3. 询问当前时间（模型调用 exec_shell 运行 date 命令）
  4. 下载一个网页（模型调用 fetch 工具）

运行方式：
  python examples/example_mcp_ollama.py              # 非流式（默认）
  python examples/example_mcp_ollama.py --stream      # 流式输出

前置条件：
  - Ollama 服务运行在 localhost:11434，已拉取 qwen3.5:9b
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
from runtime.builtin_tools import register_builtin_tools

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD = "\033[1m"


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
                print(f"\n[思考] {msg.thinking[:200]}...")
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"\n[助手] 调用工具: {tc['name']}({tc.get('arguments', '{}')})")
            if msg.content:
                print(f"\n[助手] {msg.content}")
        elif msg.role == "tool":
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
        elif msg.role == "tool":
            preview = msg.content[:300] + ("..." if len(msg.content) > 300 else "")
            print(f"\n{CYAN}[工具 {msg.name}] {preview}{RESET}", flush=True)

    if in_thinking or in_content:
        print(RESET)


def main():
    stream = "--stream" in sys.argv

    # 1. 注册模型 — 使用 qwen3.5:9b（更低显存要求）
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="qwen3.5-9b",
        api_base="http://localhost:11434",
        model_name="qwen3.5:9b",
        api_protocol="ollama",
        generate_params={"temperature": 0.7},
    ))

    # 2. 注册内置工具：exec_shell（运行 date 获取时间）、fetch（下载网页）
    tool_registry = ToolRegistry()
    builtin_ids = register_builtin_tools(tool_registry)
    print(f">>> 内置工具: {builtin_ids}")

    if stream:
        print(">>> 模式: 流式输出\n")
    else:
        print(">>> 模式: 非流式（添加 --stream 启用流式）\n")

    # 3. 创建 Runtime
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
    )

    # 4. 对话 1：询问当前时间
    print("=" * 60)
    print("对话 1：询问当前时间")
    print("=" * 60)
    req1 = InferenceRequest(
        model_id="qwen3.5-9b",
        tool_ids=builtin_ids,
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
        model_id="qwen3.5-9b",
        tool_ids=builtin_ids,
        text="请帮我下载 https://httpbin.org/html 这个网页，并简要描述网页内容。",
        max_tool_rounds=5,
    )
    if stream:
        run_stream(runtime, req2)
    else:
        print_result(runtime.infer(req2))

    print("\n>>> 完成")


if __name__ == "__main__":
    main()

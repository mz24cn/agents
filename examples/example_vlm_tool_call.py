#!/usr/bin/env python3
"""
使用示例：VLM 读图理解指令 + 内置工具执行

演示：
  1. 使用 qwen3.5:9b VLM 通过 Ollama 原生协议读取图片
  2. 大模型识别图片中的文字，理解其中的指令
  3. 大模型自行决定调用 bash 或 fetch 等内置工具执行指令
  4. 全程流式输出

用法：
  python examples/example_vlm_tool_call.py [图片路径]

示例：
  python examples/example_vlm_tool_call.py resources/example_vlm_tool_call.png

前置条件：
  - Ollama 服务运行在 localhost:11434，已拉取 qwen3.5:9b
"""

import sys
import os
import base64

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


def main():
    image_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "example_vlm_tool_call.png")

    if not os.path.isfile(image_path):
        print(f"错误: 图片不存在: {image_path}")
        sys.exit(1)

    # 读取图片为 base64
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")

    print(f"{DIM}图片: {image_path} ({len(image_b64) // 1024}KB base64){RESET}\n")

    # 1. 注册模型（VLM）
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="qwen3.5-vlm",
        api_base="http://localhost:11434",
        model_name="qwen3.5:9b",
        api_protocol="ollama",
        model_type="vlm",
        generate_params={"temperature": 0.7},
    ))

    # 2. 注册内置工具
    tool_registry = ToolRegistry()
    builtin_ids = register_builtin_tools(tool_registry)
    print(f"{DIM}内置工具: {builtin_ids}{RESET}\n")

    # 3. 创建 Runtime
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
    )

    # 4. 流式推理
    user_msg = "查看图片里的文字，根据文字内容执行"
    print(f"{BOLD}[用户]{RESET} {user_msg}\n")

    in_thinking = False
    in_content = False

    for msg in runtime.infer_stream(InferenceRequest(
        model_id="qwen3.5-vlm",
        tool_ids=builtin_ids,
        messages=[
            Message(role="system", content=(
                "你是一个智能助手，能够识别图片中的文字并理解其含义。"
                "如果图片中包含可执行的指令（如命令、URL等），请使用 bash 或 fetch 工具执行。"
                "执行后请用中文总结结果。"
            )),
            Message(role="user", content=user_msg, images=[image_b64]),
        ],
        max_tool_rounds=10,
    )):
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
                    if len(args_str) > 200:
                        args_str = args_str[:200] + "..."
                    print(f"\n{YELLOW}[调用] {tc['name']}({args_str}){RESET}", flush=True)
        elif msg.role == "tool":
            preview = msg.content[:500] + ("..." if len(msg.content) > 500 else "")
            print(f"\n{CYAN}[工具 {msg.name}] {preview}{RESET}", flush=True)

    if in_thinking or in_content:
        print(RESET)
    print(f"\n{DIM}>>> 完成{RESET}")


if __name__ == "__main__":
    main()

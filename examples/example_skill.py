#!/usr/bin/env python3
"""
使用示例：通过 runtime 流式调用 Ollama 大模型 + Skill（渐进披露）

演示 Skill 渐进披露 + 流式输出：
  - 思考过程实时输出
  - 正文内容逐 token 输出
  - 工具调用和结果即时显示
  - 多轮推理全程流式

用法：
  python examples/example_skill.py <skill_dir> <user_message>

示例：
  python examples/example_skill.py /path/to/my_skill "帮我查一下最近的数据"
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
    SkillManager,
)

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD = "\033[1m"


def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <skill_dir> <user_message>")
        sys.exit(1)

    skill_dir = sys.argv[1]
    user_message = sys.argv[2]

    if not os.path.isdir(skill_dir):
        print(f"错误: 目录不存在: {skill_dir}")
        sys.exit(1)

    # 1. 注册模型
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="qwen3-14b",
        api_base="http://localhost:11434",
        model_name="qwen3:14b",
        api_protocol="ollama",
        model_type="llm",
        generate_params={"temperature": 0.7},
    ))

    # 2. 加载 Skill
    tool_registry = ToolRegistry()
    skill_manager = SkillManager(tool_registry)
    skill_config = skill_manager.load_skill(skill_dir)
    print(f"{DIM}Skill: {skill_config.name} — {skill_config.description[:60]}...{RESET}\n")

    # 3. 创建 Runtime
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
        skill_manager=skill_manager,
    )

    # 4. 流式推理
    print(f"{BOLD}[用户]{RESET} {user_message}\n")

    in_thinking = False
    in_content = False

    for msg in runtime.infer_stream(InferenceRequest(
        model_id="qwen3-14b",
        tool_ids=[skill_config.tool_id],
        messages=[
            Message(role="system", content=(
                "你是一个智能助手。当用户的请求与某个可用技能相关时，请选择该技能。"
                "回复时请用中文，并以友好易读的方式展示结果。"
            )),
            Message(role="user", content=user_message),
        ],
        max_tool_rounds=10,
    )):
        if msg.role == "assistant":
            # Thinking chunk
            if msg.thinking:
                if not in_thinking:
                    print(f"\n{DIM}[思考] ", end="", flush=True)
                    in_thinking = True
                    in_content = False
                print(msg.thinking, end="", flush=True)

            # Content chunk
            if msg.content:
                if in_thinking:
                    print(f"{RESET}")
                    in_thinking = False
                if not in_content:
                    print(f"\n{GREEN}", end="", flush=True)
                    in_content = True
                print(msg.content, end="", flush=True)

            # Tool call
            if msg.tool_calls:
                if in_thinking:
                    print(f"{RESET}")
                    in_thinking = False
                if in_content:
                    print(f"{RESET}")
                    in_content = False
                for tc in msg.tool_calls:
                    args_str = str(tc.get("arguments", "{}"))
                    if len(args_str) > 150:
                        args_str = args_str[:150] + "..."
                    print(f"\n{YELLOW}[调用] {tc['name']}({args_str}){RESET}", flush=True)

        elif msg.role == "tool":
            preview = msg.content[:400] + ("..." if len(msg.content) > 400 else "")
            print(f"\n{CYAN}[工具 {msg.name}] {preview}{RESET}", flush=True)

        elif msg.role == "system":
            print(f"\n{DIM}[系统] Skill 文档已注入上下文{RESET}", flush=True)

    # Final newline cleanup
    if in_thinking or in_content:
        print(f"{RESET}")
    print(f"\n{DIM}>>> 完成{RESET}")


if __name__ == "__main__":
    main()

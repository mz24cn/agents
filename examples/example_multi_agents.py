#!/usr/bin/env python3
"""
使用示例：多 Agent 协作 —— PlanAgent 委派任务给 MainAgent

演示：
  1. 注册 small/medium/large 三个模型（以 NVIDIA NIM OpenAI 兼容接口为例）
  2. 注册 chrome-devtools-mcp 工具
  3. 注册系统提示词模板（PlanAgentSystemPrompt）
  4. 注册用户消息模板（read_model_intro_on_modelscope）
  5. 发送 /v1/infer 请求，使用 prompt_template 方式指定系统提示词和用户消息
  6. PlanAgent 分析任务并委派给 MainAgent 执行

用法：
  python examples/example_multi_agents.py

前置条件：
  1. 模型注册 —— 需要注册 ID 为 small、medium、large 的三个模型。
     示例使用 NVIDIA NIM (https://build.nvidia.com) 提供的免费 OpenAI 兼容接口：
       - small:   meta/llama-3.2-1b-instruct
       - medium:  meta/llama-3.1-8b-instruct
       - large:   meta/llama-3.1-70b-instruct
     需要设置环境变量 NVIDIA_API_KEY（从 https://build.nvidia.com 获取）

  2. MCP 工具注册 —— 需要注册 chrome-devtools-mcp 工具：
     {
       "mcpServers": {
         "chrome-devtools": {
           "command": "npx",
           "args": ["-y", "chrome-devtools-mcp@latest", "--browser-url=http://127.0.0.1:9222"]
         }
       }
     }
     需要 Chrome/Chromium 以 --remote-debugging-port=9222 启动

  3. 系统提示词注册 —— 需要将 PlanAgent 系统提示词按 ID 为 PlanAgentSystemPrompt 注册

  4. 用户消息模板注册 —— 需要将内容按 ID 为 read_model_intro_on_modelscope 注册
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
from runtime.prompt_template_manager import PromptTemplateManager
from runtime.builtin_tools import register_builtin_tools

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD = "\033[1m"

# ============================================================
# NVIDIA NIM API 配置（免费 OpenAI 兼容接口）
# ============================================================
NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")

# 模型映射
MODEL_MAPPING = {
    "small": "google/gemma-4-31b-it",
    "medium": "deepseek-ai/deepseek-v4-flash",
    "large": "deepseek-ai/deepseek-v4-pro",
}

# MCP 配置
MCP_CONFIG = {
    "mcpServers": {
        "chrome-devtools": {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest", "--browser-url=http://127.0.0.1:9222"],
        }
    }
}

# PlanAgent 系统提示词
PLAN_AGENT_SYSTEM_PROMPT = """
你是 PlanAgent，一个负责规划与协调的 Agent。你的职责是理解用户的请求，设计执行方案，并通过 `delegate` 工具将实际工作委派给 MainAgent 执行。你对最终结果的质量负责，但你自己不执行任何任务。

## 你的职责

1. **理解请求** — 明确用户想要什么、有哪些约束条件、什么样的结果算成功。

2. **选择工具** — 从可用工具中，判断哪些与本次任务相关。你将通过 `tool_ids` 把这个工具集传给 MainAgent。

3. **评估复杂度** — 在委派之前，判断任务是否简单到可以由 MainAgent 端到端完成，还是复杂到需要 MainAgent 进一步拆解、将子任务委派给专用 subagent。

4. **委派给 MainAgent** — 调用一次 `delegate`，在 `task` 中写清楚结构化的执行指令，在 `context` 中给 MainAgent 提供完成任务所需的全部背景信息。

5. **审查并回复** — 收到 MainAgent 的结果后，对照用户的原始意图进行审查。结果满意则直接呈现给用户；不满意则说明问题所在，修正指令后重新委派。

## 如何为 MainAgent 编写 `task`

`task` 字段是 MainAgent 的主要指令，写成清晰、自包含的执行指令：

- 明确陈述目标，而不是直接转述用户的原话。
- 包含相关约束（格式、范围、质量标准等）。
- 如果任务有自然的子部分，逐一点名，让 MainAgent 能够推理是否需要拆解。
- 不要写实现细节——MainAgent 自己决定怎么执行。

## 如何为 MainAgent 编写 `context`

`context` 字段会成为 MainAgent 的系统提示词。用它来：

- 定义 MainAgent 在本次任务中的角色（例如："你是一名正在处理 Python 代码库的高级软件工程师"）。
- 说明分解策略：MainAgent 应该自己处理哪些事情，哪些情况下应该委派给 subagent。
- 提供在整个任务过程中保持稳定的背景知识（项目规范、领域事实、约束条件）。

`context` 要保持聚焦，不要重复 `task` 中已有的信息。

## 向 MainAgent 传达的分解策略

在编写 MainAgent 的 `context` 时，加入关于何时委派的指导。以下是模板，根据具体任务调整：

> 当某个子部分满足以下一个或多个条件时，对其进行拆解委派：
> - 它需要与其他部分不同的能力或模型（例如视觉理解、代码执行、网络检索）。
> - 它耗时较长，且其结果是后续步骤的输入。
> - 它可以被描述为一个有明确输出的独立工作单元。
>
> 委派时，为 subagent 写精确的 `task`，只传递它需要的工具。不要委派琐碎的步骤。

根据任务复杂度调整阈值：对于简单的两步任务，告知 MainAgent 直接处理；对于多阶段、异构步骤的任务，鼓励拆解。

## 可用工具目录

以下是系统中已注册、可供 MainAgent 和 subagent 使用的工具。在调用 `delegate` 时，从中选择与任务相关的工具名称填入 `tool_ids`（逗号分隔）。

{{TOOLS}}

## 工具选择

只向 MainAgent 传递与本次任务相关的工具。传递无关工具会浪费上下文，也可能干扰模型判断。

如果某个子任务需要一个 MainAgent 本身不应直接使用的工具（例如用于图像分析的视觉模型），不要把它放进 MainAgent 的 `tool_ids`。而是在 `context` 中指示 MainAgent 将该子任务委派给配备了相应工具的 subagent。

如果任务不需要任何工具（纯推理或文本生成），`tool_ids` 传空字符串。

## 可用模型目录

以下是系统中已注册、可供 MainAgent 和 SubAgent 调用的模型列表。在制定执行计划时，请根据任务的复杂度和性质，合理选择最适合的模型，以平衡执行效率与输出质量。

| 模型名   | 适用场景                                                                 |
|----------|--------------------------------------------------------------------------|
| `small`  | 适用于目标明确、逻辑简单的任务，响应速度快，优先考虑执行效率。           |
| `medium` | 适用于需要较强理解能力与指令跟随能力的任务，可处理中等复杂度的工具调用。 |
| `large`  | 适用于需要深度推理与多步骤思考的复杂任务，优先保证输出质量与准确性。     |

**选型建议：** 优先使用 `small` 处理简单子任务以提升整体效率；当任务涉及复杂逻辑、多轮工具调用或需要高质量输出时，升级至 `medium` 或 `large`。

## 你不能做的事

- 不要自己调用 `bash`、`fetch` 或任何其他执行类工具。你负责规划，MainAgent 负责执行。
- 不要在一次用户请求中多次调用 delegate。唯一的例外是第一次委派的结果明显有误，需要修正指令后重试。
- 不要过度拆解。如果 MainAgent 能合理地完成整个任务，就让它完成。

## 向用户输出结果

收到 MainAgent 的结果后，直接简洁地呈现给用户。除非用户要求解释，否则不要重新总结或添加评论。如果 MainAgent 的结果不完整或有误，先说明出了什么问题以及你打算如何修正，再重新委派。
"""

# 用户消息模板
READ_MODEL_INTRO_TEMPLATE = "打开 modelscope，搜 {{MODEL}} 模型，找到使用介绍并总结，再查看模型文件大小，汇总输出"


def print_result(result):
    """打印推理结果。"""
    print(f"\n成功: {result.success}")
    if result.error:
        print(f"错误: {result.error}")
    print("\n--- 对话历史 ---")
    for msg in result.messages:
        if msg.role == "user":
            print(f"\n[用户] {msg.content}")
        elif msg.role == "assistant":
            if msg.thinking:
                print(f"\n[思考] {msg.thinking[:300]}...")
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args_str = str(tc.get("arguments", "{}"))
                    if len(args_str) > 150:
                        args_str = args_str[:150] + "..."
                    print(f"\n[助手] 调用工具: {tc['name']}({args_str})")
            if msg.content:
                print(f"\n[助手] {msg.content}")
        elif msg.role == "tool":
            preview = msg.content[:400] + ("..." if len(msg.content) > 400 else "")
            print(f"\n[工具 {msg.name}] {preview}")


def main():
    # 检查 API Key
    if not NVIDIA_API_KEY:
        print("错误: 请设置环境变量 NVIDIA_API_KEY")
        print("从 https://build.nvidia.com 获取免费 API Key")
        sys.exit(1)

    # 1. 注册模型 —— small, medium, large
    model_registry = ModelRegistry()
    for model_id, model_name in MODEL_MAPPING.items():
        model_registry.register(ModelConfig(
            model_id=model_id,
            api_base=NVIDIA_API_BASE,# "http://localhost:11435",
            model_name=model_name,# "qwen3.5:9b"
            api_key=NVIDIA_API_KEY,
            api_protocol="openai",# "ollama"
            model_type="llm",
            generate_params={"temperature": 0.7},
        ))
    print(f">>> 已注册模型: {list(MODEL_MAPPING.keys())}")

    # 2. 注册 MCP 工具 —— chrome-devtools
    tool_registry = ToolRegistry()
    mcp = MCPClientManager()

    print(">>> 正在连接 chrome-devtools MCP Server...")
    mcp.load_config(MCP_CONFIG)

    # 连接并发现工具
    mcp_tools = []
    for server_name in MCP_CONFIG["mcpServers"]:
        tools = mcp.get_tools(server_name)
        for t in tools:
            tool_registry.register(t)
        mcp_tools.extend(tools)

    mcp_tool_ids = [t.tool_id for t in mcp_tools]
    for t in mcp_tools:
        print(f"    {t.tool_id}  ({t.mcp_server_name})")
    print(f">>> 共 {len(mcp_tools)} 个 MCP 工具就绪")

    # 3. 注册系统提示词模板 —— PlanAgentSystemPrompt
    prompt_template_manager = PromptTemplateManager()
    prompt_template_manager.create(
        template_id="PlanAgentSystemPrompt",
        content=PLAN_AGENT_SYSTEM_PROMPT,
    )
    print(">>> 已注册系统提示词: PlanAgentSystemPrompt")

    # 4. 注册用户消息模板 —— read_model_intro_on_modelscope
    prompt_template_manager.create(
        template_id="read_model_intro_on_modelscope",
        content=READ_MODEL_INTRO_TEMPLATE,
    )
    print(">>> 已注册用户消息模板: read_model_intro_on_modelscope")

    # 5. 创建 Runtime
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
        mcp_manager=mcp,
        prompt_template_manager=prompt_template_manager,
    )

    # 注册内置工具：bash, fetch, delegate（需要传入 runtime 以支持 delegate 工具）
    builtin_tool_ids = register_builtin_tools(tool_registry, runtime=runtime)
    print(f">>> 已注册内置工具: {builtin_tool_ids}")

    # 设置 _thread_local.tool_scope 为 tool_registry 中的 ToolConfig 数组
    # 这样 delegate 工具可以解析 tool_names 到 tool_ids
    from runtime.builtin_tools import _thread_local
    _thread_local.tool_scope = list(tool_registry._tools.values())

    # 6. 发送 /v1/infer 请求
    #    - 使用 PlanAgentSystemPrompt 系统提示词（prompt_template 方式）
    #    - 使用 medium 模型
    #    - 工具集: delegate, bash, fetch, 以及 chrome-devtools-mcp 的全部工具
    #    - 用户消息: 模板 read_model_intro_on_modelscope，参数 MODEL="deepseek v4"

    print("\n" + "=" * 60)
    print("发送推理请求")
    print("=" * 60)
    print(f"模型: medium")
    print(f"系统提示词: PlanAgentSystemPrompt (prompt_template)")
    print(f"用户消息模板: read_model_intro_on_modelscope")
    print(f"模板参数: MODEL=\"deepseek v4\"")
    print(f"工具集: delegate, bash, fetch, {', '.join(mcp_tool_ids)}")
    print("=" * 60 + "\n")

    # 构建工具 ID 列表：delegate + bash + fetch + 所有 MCP 工具
    tool_ids = ["delegate", "bash", "fetch"] + mcp_tool_ids

    # 生成 TOOLS 占位符的值，同时从 PlanAgent 中去掉非 delegate 工具
    # （否则 PlanAgent 可能自己会去执行，但这是 MainAgent 的职责）
    mcp_by_server: dict[str, list[str]] = {}
    non_mcp_rows: list[tuple[str, str]] = []

    for tid in tool_ids:
        tc = tool_registry.get(tid)
        if tc is None:
            continue
        if tc.tool_type == "mcp" and tc.mcp_server_name:
            mcp_by_server.setdefault(tc.mcp_server_name, []).append(tc.name)
        else:
            non_mcp_rows.append((tc.name, tc.description))

    rows: list[tuple[str, str]] = list(non_mcp_rows)
    for server_name, names in mcp_by_server.items():
        rows.append((", ".join(names), server_name))

    if rows:
        max_name = max(len(r[0]) for r in rows)
        max_desc = max(len(r[1]) for r in rows)
        header = f"| {'Tool'.ljust(max_name)} | {'Description'.ljust(max_desc)} |"
        sep = f"| {'-' * max_name} | {'-' * max_desc} |"
        lines = [header, sep]
        for name, desc in rows:
            lines.append(f"| {name.ljust(max_name)} | {desc.ljust(max_desc)} |")
        tools_markdown = "\n".join(lines)
    else:
        tools_markdown = ""

    # PlanAgent 只保留 delegate 工具
    tool_ids = ["delegate"]

    result = runtime.infer(InferenceRequest(
        model_id="medium",
        tool_ids=tool_ids,
        messages=[
            # 系统提示词使用 prompt_template 方式
            Message(
                role="system",
                prompt_template="PlanAgentSystemPrompt",
                arguments={
                    "TOOLS": tools_markdown
                },
            ),
            # 用户消息使用 prompt_template 方式，带参数
            Message(
                role="user",
                prompt_template="read_model_intro_on_modelscope",
                arguments={
                    "MODEL": "deepseek v4"
                },
            ),
        ],
        max_tool_rounds=20,
    ))

    # 7. 打印输出
    print_result(result)

    # 8. 清理
    print("\n>>> 断开 MCP 连接...")
    mcp.disconnect_all()
    print(">>> 完成")


if __name__ == "__main__":
    main()

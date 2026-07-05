#!/usr/bin/env python3
"""
使用示例：通过 runtime 调用 Ollama 大模型 + SearXNG 搜索（Function Tool）

演示：
  1. 将 SearXNG 搜索封装为 Function Tool 注册到 ToolRegistry
  2. 大模型根据用户问题自动调用搜索工具
  3. 支持按 SearXNG 分类搜索，如 general、images、videos、news、map、music、it、science、files、social media
  4. 搜索结果回传给大模型，大模型整理后回复

用法：
  python accessories/web_search_function.py [搜索问题]

示例：
  python accessories/web_search_function.py "Python 3.13 有什么新特性"

前置条件：
  - Ollama 服务运行在 localhost:11434，已拉取 qwen3:14b
  - SearXNG 服务运行在 http://pi:8080
"""

import sys
import os
import json
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime import (
    ModelConfig,
    ModelRegistry,
    ToolConfig,
    ToolRegistry,
    Runtime,
    InferenceRequest,
    Message,
)

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://pi:8080")
SEARCH_TYPES = {
    "general",
    "images",
    "videos",
    "news",
    "map",
    "music",
    "it",
    "science",
    "files",
    "social media",
}

# Function Tool JSON Schema：
WEB_SEARCH_TOOL_CONFIG = {
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "搜索关键词"
    },
    "num_results": {
      "type": "integer",
      "description": "返回结果数量，默认10"
    },
    "search_type": {
      "type": "string",
      "description": "搜索类型/SearXNG 分类，默认general。可选：general、images、videos、news、map、music、it、science、files、social media"
    }
  },
  "required": [
    "query"
  ]
}

def searxng_search(query: str, num_results: int = 10, search_type: str = "general") -> str:
    """通过 SearXNG 执行互联网搜索。

    Args:
        query: 搜索关键词。
        num_results: 返回结果数量，默认 10。
        search_type: 搜索类型/SearXNG 分类，默认 general。
    """
    search_type = (search_type or "general").strip().lower()
    if search_type not in SEARCH_TYPES:
        return f"搜索失败: 不支持的搜索类型 {search_type!r}。可选：{', '.join(sorted(SEARCH_TYPES))}"

    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "language": "zh-CN",
        "categories": search_type,
    })
    url = f"{SEARXNG_URL}/search?{params}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return f"搜索失败: HTTP {e.code}"
    except urllib.error.URLError as e:
        return f"搜索失败: {e.reason}"
    except Exception as e:
        return f"搜索失败: {e}"

    results = data.get("results", [])[:num_results]
    if not results:
        return "未找到相关结果。"

    number_of_results = data.get("number_of_results")
    if isinstance(number_of_results, int) and number_of_results >= len(results):
        result_summary = f"共找到约 {number_of_results} 条结果，展示前 {len(results)} 条：\n"
    else:
        result_summary = f"本次返回 {len(data.get('results', []))} 条结果，展示前 {len(results)} 条：\n"

    lines = [f"搜索类型：{search_type}", result_summary]
    for i, r in enumerate(results, 1):
        title = r.get("title", "(无标题)")
        url = r.get("img_src") or r.get("thumbnail_src") or r.get("url", "")
        content = r.get("content", "").strip()
        engine = r.get("engine", "")
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   链接: {url}")
        if content:
            lines.append(f"   摘要: {content[:200]}")
        if engine:
            lines.append(f"   来源: {engine}")
        lines.append("")

    return "\n".join(lines)


def print_result(result):
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
            preview = msg.content[:600] + ("..." if len(msg.content) > 600 else "")
            print(f"\n[工具 {msg.name}] {preview}")


def main():
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "2026年有什么重大科技新闻"

    # 1. 注册模型
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="qwen3-14b",
        api_base="http://localhost:11434",
        model_name="qwen3:14b",
        api_protocol="ollama",
        generate_params={"temperature": 0.7},
    ))

    # 2. 注册 SearXNG 搜索为 Function Tool
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolConfig(
            tool_id="web_search",
            tool_type="function",
            name="web_search",
            description="通过互联网搜索引擎搜索信息。需要查询实时信息、最新新闻、图片、视频、技术文档等时使用。",
            parameters=WEB_SEARCH_TOOL_CONFIG,
        ),
        callable_fn=searxng_search,
    )

    # 3. 创建 Runtime
    runtime = Runtime(
        model_registry=model_registry,
        tool_registry=tool_registry,
    )

    # 4. 对话
    print("=" * 60)
    print(f"搜索：{query}")
    print("=" * 60)

    result = runtime.infer(InferenceRequest(
        model_id="qwen3-14b",
        tool_ids=["web_search"],
        messages=[
            Message(role="system", content=(
                "你是一个智能助手，可以通过搜索引擎获取最新信息。"
                "当用户提问需要实时数据、图片、视频、新闻或你不确定的内容时，请使用 web_search 工具搜索。"
                "可根据问题选择 search_type，例如图片用 images，新闻用 news，视频用 videos。"
                "搜索后请用中文整理结果，给出清晰的回答。"
            )),
            Message(role="user", content=query),
        ],
        max_tool_rounds=5,
    ))

    print_result(result)
    print("\n>>> 完成")


if __name__ == "__main__":
    main()

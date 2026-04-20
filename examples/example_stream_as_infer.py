#!/usr/bin/env python3
"""
示例：客户端通过 /v1/infer/stream 拿到与 /v1/infer 完全一致的 JSON 结果

问题背景：
  直接调用 /v1/infer 时，推理可能需要几十秒甚至几分钟，期间连接没有任何数据
  传输，容易被网关/代理/防火墙断开（idle timeout）。

解决方案：
  改用 /v1/infer/stream（SSE 流），实时接收每一条消息增量，全部收完后
  在本地拼装成与 /v1/infer 完全相同的 JSON 结构。

SSE 协议说明：
  - 每个事件格式：  data: <json>\n\n
  - 最后一条事件：  data: [DONE]\n\n
  - 每条 data 是一个 Message.to_dict() 序列化的 JSON 对象
  - 错误事件：      data: {"error": "..."}\n\n

运行方式：
  # 先启动 server（另开终端）：
  #   python -m runtime.server
  #
  python examples/example_stream_as_infer.py
  python examples/example_stream_as_infer.py --compare   # 同时调用两个接口对比结果
"""

import json
import sys
import urllib.request
import urllib.error
from typing import Optional

# ── 配置 ──────────────────────────────────────────────────────────────────────
SERVER_URL = "http://localhost:8080"
MODEL_ID   = "qwen3"          # 替换为你在 server 中注册的 model_id
TOOL_IDS   = []                  # 需要的工具 ID 列表，不需要则留空
USER_TEXT  = "你好，请简单介绍一下你自己。"
# ─────────────────────────────────────────────────────────────────────────────


def infer_via_stream(
    server_url: str,
    model_id: str,
    text: Optional[str] = None,
    messages: Optional[list] = None,
    tool_ids: Optional[list] = None,
    max_tool_rounds: int = 10,
    verbose: bool = False,
) -> dict:
    """
    调用 /v1/infer/stream，收集所有 SSE 事件后拼装成与 /v1/infer 相同的结果。

    返回值格式（与 /v1/infer 完全一致）：
        {
            "success": bool,
            "messages": [ {role, content, ...}, ... ],
            "error": str | None,       # 仅失败时存在
            "error_code": str | None,  # 仅失败时存在
        }
    """
    payload = {
        "model_id": model_id,
        "tool_ids": tool_ids or [],
        "max_tool_rounds": max_tool_rounds,
    }
    if text is not None:
        payload["text"] = text
    if messages is not None:
        payload["messages"] = messages

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=f"{server_url}/v1/infer/stream",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # 最终拼装好的消息列表（与 /v1/infer 结构一致）
    merged_messages: list[dict] = []
    # 当前正在累积的 assistant chunk（None 表示没有未完成的 assistant 消息）
    pending_assistant: Optional[dict] = None
    stream_error: Optional[str] = None
    # Token stat 累计
    total_stat: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def flush_assistant() -> None:
        """将累积完毕的 assistant 消息写入 merged_messages。"""
        if pending_assistant is not None:
            merged_messages.append(pending_assistant)

    with urllib.request.urlopen(req) as resp:
        buffer = b""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            buffer += chunk

            # SSE 事件以 \n\n 结尾
            if not buffer.endswith(b"\n\n"):
                continue

            raw_event = buffer.decode("utf-8").strip()
            buffer = b""

            if not raw_event.startswith("data:"):
                continue

            data_str = raw_event[len("data:"):].strip()

            if data_str == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # 服务端推送了错误事件
            if "error" in event and len(event) == 1:
                stream_error = event["error"]
                break

            role = event.get("role", "")

            if verbose:
                content_preview = str(event.get("content", "") or event.get("thinking", ""))[:60]
                print(f"  [stream] role={role}  {content_preview!r}")

            if role == "usage":
                # Per-round stat event — keep the last one (it has overall_ms if last round)
                try:
                    s = json.loads(event.get("content", "{}"))
                    total_stat = s  # last round's stat has cumulative totals + overall_ms
                except (json.JSONDecodeError, ValueError):
                    pass
                continue

            if role == "assistant":
                # assistant 消息是逐 token 的增量，需要合并
                if pending_assistant is None:
                    # 新的 assistant 消息开始
                    pending_assistant = {"role": "assistant", "content": "", "tool_calls": None, "thinking": None}

                pending_assistant["content"] += event.get("content") or ""
                pending_assistant["thinking"] = (pending_assistant["thinking"] or "") + (event.get("thinking") or "") or None

                # tool_calls 增量：每个 chunk 里的 tool_calls 是 delta，需要按 index 合并
                for tc_delta in (event.get("tool_calls") or []):
                    idx = tc_delta.pop("_index", None)
                    if pending_assistant["tool_calls"] is None:
                        pending_assistant["tool_calls"] = []
                    # 找到对应 index 的槽位（或新建）
                    while len(pending_assistant["tool_calls"]) <= (idx if idx is not None else 0):
                        pending_assistant["tool_calls"].append({"name": "", "arguments": ""})
                    slot = pending_assistant["tool_calls"][idx if idx is not None else 0]
                    slot["name"] += tc_delta.get("name") or ""
                    slot["arguments"] += tc_delta.get("arguments") or ""

            else:
                # 非 assistant 消息（function / system / user）是完整的一条
                # 先把之前累积的 assistant 消息落盘
                flush_assistant()
                pending_assistant = None
                merged_messages.append(event)

    # 流结束后，把最后一条 assistant 消息落盘
    flush_assistant()

    # 清理空字段，保持与 /v1/infer 一致
    for msg in merged_messages:
        if msg.get("thinking") == "":
            msg["thinking"] = None
        if msg.get("tool_calls") == []:
            msg["tool_calls"] = None
        # 去掉值为 None 的 key（与 Message.to_dict() 行为一致）
        for k in ["thinking", "tool_calls", "name"]:
            if msg.get(k) is None:
                msg.pop(k, None)

    if stream_error:
        return {"success": False, "messages": merged_messages, "error": stream_error}

    result: dict = {"success": True, "messages": merged_messages}
    if total_stat.get("total_tokens", 0) > 0 or total_stat.get("prompt_tokens", 0) > 0:
        result["stat"] = total_stat
    return result


def infer_direct(
    server_url: str,
    model_id: str,
    text: Optional[str] = None,
    messages: Optional[list] = None,
    tool_ids: Optional[list] = None,
    max_tool_rounds: int = 10,
) -> dict:
    """调用 /v1/infer（非流式），直接返回完整 JSON。"""
    payload = {
        "model_id": model_id,
        "tool_ids": tool_ids or [],
        "max_tool_rounds": max_tool_rounds,
    }
    if text is not None:
        payload["text"] = text
    if messages is not None:
        payload["messages"] = messages

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=f"{server_url}/v1/infer",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_result(result: dict, label: str = "") -> None:
    """格式化打印推理结果。"""
    if label:
        print(f"\n{'─' * 20} {label} {'─' * 20}")
    print(f"success : {result['success']}")
    if result.get("error"):
        print(f"error   : {result['error']}")
    if result.get("stat"):
        s = result["stat"]
        print(f"tokens  : 输入={s.get('prompt_tokens',0)}  输出={s.get('completion_tokens',0)}  合计={s.get('total_tokens',0)}")
        if s.get("overall_ms") is not None:
            print(f"timing  : 首token={s.get('ttft_ms','N/A')}ms  净推理={s.get('net_ms','N/A')}ms  全程={s.get('overall_ms')}ms")
    print(f"messages: {len(result['messages'])} 条")
    for msg in result["messages"]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        thinking = msg.get("thinking", "")
        tool_calls = msg.get("tool_calls")

        if role == "user":
            print(f"\n  [用户] {content}")
        elif role == "assistant":
            if thinking:
                print(f"\n  [思考] {thinking[:120]}{'...' if len(thinking) > 120 else ''}")
            if tool_calls:
                for tc in tool_calls:
                    print(f"\n  [助手→工具] {tc.get('name')}({tc.get('arguments', {})})")
            if content:
                print(f"\n  [助手] {content}")
        elif role == "tool":
            name = msg.get("name", "")
            preview = content[:200] + ("..." if len(content) > 200 else "")
            print(f"\n  [工具 {name}] {preview}")


def main():
    compare_mode = "--compare" in sys.argv

    print(f"Server : {SERVER_URL}")
    print(f"Model  : {MODEL_ID}")
    print(f"Input  : {USER_TEXT!r}")

    # ── 方式一：通过流式接口拿到干净的 JSON 结果 ──────────────────────────────
    print("\n>>> 调用 /v1/infer/stream（流式接收，本地拼装）...")
    try:
        stream_result = infer_via_stream(
            server_url=SERVER_URL,
            model_id=MODEL_ID,
            text=USER_TEXT,
            tool_ids=TOOL_IDS,
            verbose=True,
        )
    except urllib.error.URLError as e:
        print(f"连接失败: {e}  （请确认 server 已启动）")
        sys.exit(1)

    print_result(stream_result, label="stream 拼装结果")

    # ── 方式二（可选）：直接调用 /v1/infer 对比 ───────────────────────────────
    if compare_mode:
        print("\n>>> 调用 /v1/infer（非流式，直接等待）...")
        direct_result = infer_direct(
            server_url=SERVER_URL,
            model_id=MODEL_ID,
            text=USER_TEXT,
            tool_ids=TOOL_IDS,
        )
        print_result(direct_result, label="/v1/infer 直接结果")

        # 对比最终 assistant 回复是否一致
        def last_assistant(result):
            for msg in reversed(result["messages"]):
                if msg.get("role") == "assistant" and msg.get("content"):
                    return msg["content"]
            return ""

        s = last_assistant(stream_result)
        d = last_assistant(direct_result)
        print(f"\n{'─' * 50}")
        print(f"stream 最终回复 : {s[:100]!r}")
        print(f"direct 最终回复 : {d[:100]!r}")
        print(f"内容一致        : {s == d}")


if __name__ == "__main__":
    main()

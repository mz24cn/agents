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
  - 事件可包含 event: / id: / data: 等标准 SSE 字段
  - 首条控制事件：  event: init\ndata: <json>\n\n（不属于推理 messages）
  - 消息事件示例：  id: <seq>\ndata: <message-json>\n\n
  - 最后一条事件：  data: [DONE]\n\n
  - 消息 data 是一个 Message.to_dict() 序列化的 JSON 对象
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

# ── 配置 ──────────────────────────────────────────────────────────────────
SERVER_URL = "http://localhost:7988"
MODEL_ID   = "qwen3.5-9b(local)"  # 替换为你在 server 中注册的 model_id
TOOL_IDS   = []                     # 需要的工具 ID 列表，不需要则留空
USER_TEXT  = "你好，请简单介绍一下你自己。"
# ────────────────────────────────────────────────────────────────────────


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
    调用 /v1/infer/stream，收集 SSE 事件并按服务端 /v1/infer 的合并规则
    还原结果。这里的“一致”指返回 JSON 的结构和语义一致；如果分别调用两次
    模型，生成文本仍可能因为采样而不同。
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

    merged_messages: list[dict] = []
    pending_assistant: Optional[dict] = None
    stream_error: Optional[str] = None
    last_stat: Optional[dict] = None

    def flush_assistant(stat: Optional[dict] = None) -> None:
        """按 merge_stream_messages() 的规则结束当前 assistant turn。"""
        nonlocal pending_assistant
        if pending_assistant is None:
            return

        content = pending_assistant["content"]
        thinking = pending_assistant["thinking"]
        tool_calls = pending_assistant["tool_calls"]
        if not content and not thinking and not tool_calls:
            pending_assistant = None
            return

        msg: dict = {
            "role": "assistant",
            "content": content,
            # /v1/infer 使用 usage.completed_at 作为 assistant 完成时间。
            "timestamp": (stat or {}).get("completed_at")
                         or pending_assistant.get("timestamp"),
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if thinking:
            msg["thinking"] = thinking
        if stat is not None:
            msg["stat"] = stat
        for key in ("agent_id", "name", "mentions"):
            value = pending_assistant.get(key)
            if value is not None:
                msg[key] = value
        if msg["timestamp"] is None:
            msg.pop("timestamp")

        merged_messages.append(msg)
        pending_assistant = None

    def merge_assistant_delta(event: dict) -> None:
        """合并 assistant 的文本、思考、身份信息和 tool_calls delta。"""
        nonlocal pending_assistant
        if pending_assistant is None:
            pending_assistant = {
                "content": "",
                "thinking": "",
                "tool_calls": [],
                "timestamp": None,
                "agent_id": None,
                "name": None,
                "mentions": None,
            }

        pending_assistant["content"] += event.get("content") or ""
        pending_assistant["thinking"] += event.get("thinking") or ""
        if event.get("timestamp"):
            pending_assistant["timestamp"] = event["timestamp"]
        if event.get("agent_id") and not pending_assistant["agent_id"]:
            pending_assistant["agent_id"] = event["agent_id"]
        if event.get("assistant_id") and not pending_assistant["agent_id"]:
            pending_assistant["agent_id"] = event["assistant_id"]
        if event.get("name") and not pending_assistant["name"]:
            pending_assistant["name"] = event["name"]
        if event.get("mentions") is not None:
            pending_assistant["mentions"] = event["mentions"]

        # 与服务端 merge_stream_messages() 保持相同的 tool_calls 合并规则。
        for tc_delta in event.get("tool_calls") or []:
            idx = tc_delta.get("_index")
            if idx is None:
                pending_assistant["tool_calls"].append(dict(tc_delta))
                continue

            while len(pending_assistant["tool_calls"]) <= idx:
                pending_assistant["tool_calls"].append(
                    {"id": "", "name": "", "arguments": ""}
                )
            target = pending_assistant["tool_calls"][idx]
            if tc_delta.get("id"):
                target["id"] = tc_delta["id"]
            if tc_delta.get("tool_use_id"):
                target["id"] = tc_delta["tool_use_id"]
            if tc_delta.get("name"):
                target["name"] = target.get("name", "") + tc_delta["name"]
            if tc_delta.get("arguments"):
                if isinstance(tc_delta["arguments"], dict):
                    target["arguments"] = tc_delta["arguments"]
                else:
                    target["arguments"] = (
                        target.get("arguments", "") + tc_delta["arguments"]
                    )

    with urllib.request.urlopen(req) as resp:
        # 标准 SSE 事件可以同时包含 event:/id:/data:，不能假设事件以 data: 开头。
        event_lines: list[str] = []
        stream_done = False

        def handle_sse_event(lines: list[str]) -> bool:
            """处理一个 SSE 事件；收到 [DONE] 或错误时返回 True。"""
            nonlocal stream_error, last_stat

            event_name = ""
            data_lines: list[str] = []
            for line in lines:
                if not line or line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if not separator:
                    value = ""
                elif value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
                # id: / retry: 是 SSE 元数据，不属于 JSON payload。

            if not data_lines:
                return False

            data_str = "\n".join(data_lines)
            if data_str.strip() == "[DONE]":
                return True

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                if verbose:
                    print(f"  [stream] 忽略无法解析的 SSE data: {data_str[:120]!r}")
                return False

            # init 是控制事件，不属于 /v1/infer 的 messages。
            if event_name == "init" or event.get("type") == "init":
                if verbose:
                    print(f"  [stream] event=init  session_id={event.get('session_id')!r}")
                return False

            if "error" in event and len(event) == 1:
                stream_error = str(event["error"])
                return True

            role = event.get("role", "")
            if verbose:
                preview = str(event.get("content", "") or event.get("thinking", ""))[:60]
                print(f"  [stream] role={role}  {preview!r}")

            if role == "usage":
                try:
                    last_stat = json.loads(event.get("content", "{}"))
                except (json.JSONDecodeError, ValueError, TypeError):
                    last_stat = None
                # usage 同时是当前 assistant round 的结束边界。
                flush_assistant(last_stat)
            elif role == "assistant":
                merge_assistant_delta(event)
            elif role == "tool":
                # 正常情况下 usage 已先 flush；异常流中仍保证 assistant 在 tool 前。
                flush_assistant(last_stat)
                tool_msg: dict = {
                    "role": "tool",
                    "content": event.get("content") or "",
                    "timestamp": event.get("timestamp"),
                    "name": event.get("name") or "",
                }
                for key in ("tool_id", "tool_use_id", "agent_id"):
                    if event.get(key) is not None:
                        tool_msg[key] = event[key]
                if tool_msg["timestamp"] is None:
                    tool_msg.pop("timestamp")
                merged_messages.append(tool_msg)
            # system、无 role 控制帧等不会生成 /v1/infer ConversationTurn。
            return False

        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if event_lines and handle_sse_event(event_lines):
                    stream_done = True
                    break
                event_lines = []
            else:
                event_lines.append(line)

        # 兼容最后一个事件后没有空行便关闭连接的服务端。
        if not stream_done and event_lines:
            handle_sse_event(event_lines)

    flush_assistant(last_stat)

    if stream_error:
        return {"success": False, "messages": merged_messages, "error": stream_error}

    error_message = next(
        (
            msg.get("content", "")
            for msg in merged_messages
            if msg.get("role") == "assistant"
            and msg.get("content", "").startswith("Error:")
        ),
        None,
    )
    result: dict = {"success": error_message is None, "messages": merged_messages}
    if error_message is not None:
        result["error"] = error_message
    if last_stat is not None:
        result["stat"] = last_stat
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

    # ── 方式一：通过流式接口拿到干净的 JSON 结果 ─────────────────────────
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

    # ── 方式二（可选）：直接调用 /v1/infer 对比 ─────────────────────────
    if compare_mode:
        print("\n>>> 调用 /v1/infer（非流式，直接等待）...")
        direct_result = infer_direct(
            server_url=SERVER_URL,
            model_id=MODEL_ID,
            text=USER_TEXT,
            tool_ids=TOOL_IDS,
        )
        print_result(direct_result, label="/v1/infer 直接结果")

        # 这是两次独立推理：若模型启用了采样，生成文本不同是正常现象。
        # --compare 主要用于人工核对两种接口的返回结构和字段；它不能直接
        # 证明同一次模型输出经流式拼装后与非流式结果逐字相同。
        def last_assistant(result):
            for msg in reversed(result["messages"]):
                if msg.get("role") == "assistant" and msg.get("content"):
                    return msg["content"]
            return ""

        def message_shape(result):
            return [
                (msg.get("role"), tuple(sorted(msg.keys())))
                for msg in result.get("messages", [])
            ]

        s = last_assistant(stream_result)
        d = last_assistant(direct_result)
        print(f"\n{'─' * 50}")
        print(f"stream 最终回复 : {s[:100]!r}")
        print(f"direct 最终回复 : {d[:100]!r}")
        print(f"结构一致        : {message_shape(stream_result) == message_shape(direct_result)}")
        print(f"内容一致（仅供参考，两次独立采样）: {s == d}")


if __name__ == "__main__":
    main()

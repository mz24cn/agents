#!/usr/bin/env python3
"""
示例：同一个推理会话里混用「服务端工具」+「客户端工具」(client side tool calls)

概念
----
服务端工具（function / mcp / skill）：可执行实体在 agent service 进程里，服务端自己跑完，
把 role="tool" 结果塞回 message 数组继续推理——这是原有的循环。

客户端工具（tool_type="client_side"）：可以看作 Function 工具「去掉函数实体」的简化版本，
服务端只有注册 JSON（name / description / parameters），没有任何可执行代码。模型照常能看见、
照常调用；一旦模型输出里出现对它的调用，推理循环**正常退出**（不是异常），把这次调用交回
调用方执行。调用方本地跑完，带着同一个 session_id 把结果（role="tool" + tool_use_id）再发
一次 /v1/infer；服务端按 session_id 把会话历史和本地结果合并进 messages，继续推理循环。

同一个会话里两种工具可以混用，甚至同一个模型输出里的**并行工具调用**中既有服务端工具也有
客户端工具：服务端那半在服务端执行（响应里能看到它的 tool 结果），客户端那半交回调用方。

客户端工具注册有两条路：
  1) 预先在服务端 ToolRegistry 注册（本例的 read_local_file）：全局可见、跨会话复用；
  2) 推理请求 JSON 里带 client_tools 数组（本例的 list_dir）：只在本次会话内可见，
     不进 ToolRegistry，GET /v1/tools 里查不到。

用法
----
  python examples/example_client_side_tools.py                 # 原地起服务 + 客户端跑一轮
  python examples/example_client_side_tools.py --stream         # 同样的流程，走 SSE 流式
  AGENTS_URL=http://localhost:7988 MODEL_ID=xxx \
      python examples/example_client_side_tools.py              # 复用已在运行的服务

前置条件：
  - 原地起服务时需要模型服务可用（默认 Ollama localhost:11434 的 qwen3.5:9b，可用
    MODEL_ID / MODEL_API_BASE / MODEL_NAME / MODEL_PROTOCOL 覆盖）；
  - 或者用一个已经启动的 agent service（设 AGENTS_URL + MODEL_ID）。
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ANSI colors
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"
RESET = "\033[0m"
BOLD = "\033[1m"

AGENTS_URL = os.environ.get("AGENTS_URL", "").rstrip("/")
MODEL_ID = os.environ.get("MODEL_ID", "qwen3.5-9b(local)")
MODEL_API_BASE = os.environ.get("MODEL_API_BASE", "http://localhost:11434")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen3.5:9b")
MODEL_PROTOCOL = os.environ.get("MODEL_PROTOCOL", "ollama")
WORKSPACE = os.environ.get("WORKSPACE", os.getcwd())


# ─────────────────────────────────────────────────────────────────────────────
# 1) 客户端工具：服务端只有注册 JSON，实际执行发生在调用方本地
# ─────────────────────────────────────────────────────────────────────────────

# 预先注册到服务端 ToolRegistry 的客户端工具（GET /v1/tools 里能看到）
PRE_REGISTERED_CLIENT_TOOLS = [
    {
        "tool_id": "client_side-read_local_file",
        "tool_type": "client_side",
        "name": "read_local_file",
        "description": "读取调用方本地磁盘上的文本文件（服务端不执行，由调用方读取）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
        },
    },
]

# 只在本次推理请求里即时提供的客户端工具（不进 ToolRegistry，仅本会话可见）
SESSION_CLIENT_TOOLS = [
    {
        "name": "list_dir",
        "description": "列出调用方本地目录下的条目（只在本次会话中定义，服务端看不到）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 30"},
            },
            "required": ["path"],
        },
    },
]

CLIENT_TOOL_NAMES = (
    [t["name"] for t in SESSION_CLIENT_TOOLS]
    + [t["name"] for t in PRE_REGISTERED_CLIENT_TOOLS]
)


def execute_local_tool(name: str, arguments) -> str:
    """调用方本地执行客户端工具，返回值即工具结果文本（作为 role="tool" 回传）。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            arguments = {}
    arguments = arguments or {}
    try:
        if name == "read_local_file":
            with open(os.path.expanduser(arguments["path"]), encoding="utf-8") as fh:
                return fh.read(20000)
        if name == "list_dir":
            entries = sorted(os.listdir(os.path.expanduser(arguments["path"])))
            limit = int(arguments.get("limit") or 30)
            return json.dumps(entries[:limit], ensure_ascii=False)
    except Exception as exc:  # 出错也要正常回答这次工具调用
        return f"本地执行失败: {type(exc).__name__}: {exc}"
    return f"调用方没有实现客户端工具 {name}"


# ─────────────────────────────────────────────────────────────────────────────
# 2) 服务端工具：可执行实体在 agent service 进程里
# ─────────────────────────────────────────────────────────────────────────────

SERVER_SIDE_TOOL_IDS = ["workspace_stats"]


def register_side_tools(base_url: str) -> None:
    """注册一个服务端 function 工具和一个预注册的客户端工具。"""
    server_tool = {
        "tool_id": "workspace_stats",
        "tool_type": "function",
        "name": "workspace_stats",
        "description": "统计服务端 workspace 下的文件数量与总大小（在服务端进程内执行）",
        "parameters": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "目录，默认会话 workspace"},
            },
        },
        # 服务端可执行实体：由 agent service 从本文件加载这个函数
        "function_file_path": os.path.abspath(__file__),
        "function_name": "workspace_stats",
    }
    for config in [server_tool, *PRE_REGISTERED_CLIENT_TOOLS]:
        status, body = post_json(base_url, "/v1/tools", config)
        tag = "服务端工具" if config["tool_type"] == "function" else "客户端工具"
        if status in (200, 201):
            print(f"  {GREEN}✓{RESET} 注册 {tag}: {config['name']}")
        else:
            print(f"  {YELLOW}!{RESET} 注册 {config['name']}: HTTP {status} {body}")


def workspace_stats(root: str = "") -> str:
    """服务端 function 工具的实现（由 agent service 进程调用，不是调用方）。"""
    base = root or os.getcwd()
    files, total = 0, 0
    for dirpath, _dirs, names in os.walk(base):
        if ".git" in dirpath:
            continue
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return json.dumps({"root": base, "files": files, "bytes": total}, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 3) 客户端：把工具调用结果 + session id 回传，继续同一个推理会话
# ─────────────────────────────────────────────────────────────────────────────

def post_json(base_url: str, path: str, payload: dict, timeout: int = 600):
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw}


def _first_request(prompt: str) -> dict:
    return {
        "model_id": MODEL_ID,
        "workspace": WORKSPACE,
        "session_id": "new",
        "messages": [{"role": "user", "content": prompt}],
        "tool_ids": SERVER_SIDE_TOOL_IDS
        + [t["tool_id"] for t in PRE_REGISTERED_CLIENT_TOOLS],
        "client_tools": SESSION_CLIENT_TOOLS,
        "max_tool_rounds": 12,
    }


def _resume_request(session_id: str, results: list) -> dict:
    """本地执行结果 + 会话 session id：服务端按 session id 合并历史继续推理。

    tool_ids 要跟首轮一致（服务端不会替请求补工具）；client_tools 不用重复带，
    服务端已把它们记在会话里，只对本会话可见、不进 ToolRegistry。
    """
    return {
        "model_id": MODEL_ID,
        "workspace": WORKSPACE,
        "session_id": session_id,
        "tool_ids": SERVER_SIDE_TOOL_IDS
        + [t["tool_id"] for t in PRE_REGISTERED_CLIENT_TOOLS],
        "messages": results,
    }


def _run_client_calls_locally(calls: list) -> list:
    """交回来的调用全部在本地执行，返回待回传的 role="tool" 消息。"""
    results = []
    for call in calls:
        print(f"{MAGENTA}  ⤵ 客户端工具{RESET} {call['name']}"
              f" args={call.get('arguments')}")
        output = execute_local_tool(call["name"], call.get("arguments"))
        print(f"{DIM}    结果: {str(output)[:200]}{RESET}")
        results.append({
            "role": "tool",
            "name": call["name"],
            "tool_use_id": call["id"],
            "content": output,
        })
    return results


def run_non_stream(base_url: str, prompt: str) -> bool:
    """非流式：/v1/infer 的 JSON 信封里，client_tool_calls 就是交回来的调用。"""
    payload = _first_request(prompt)
    session_id = None
    round_no = 0
    while True:
        round_no += 1
        print(f"\n{CYAN}▶ POST /v1/infer（第 {round_no} 次）{RESET}")
        status, envelope = post_json(base_url, "/v1/infer", payload)
        session_id = envelope.get("session_id") or session_id
        if status != 200:
            print(f"{RED}✗ HTTP {status}: {envelope.get('error')}{RESET}")
            return False

        # 本轮服务端工具的结果也在 messages 里，看得出服务端确实跑过它们
        for msg in envelope.get("messages") or []:
            if msg.get("role") == "tool":
                print(f"{GREEN}  ✓ 服务端工具结果{RESET} {msg.get('name')}: "
                      f"{str(msg.get('content'))[:120]}")

        pending = envelope.get("client_tool_calls") or []
        if not pending:
            final = None
            for msg in envelope.get("messages") or []:
                if msg.get("role") == "assistant" and (msg.get("content") or "").strip():
                    final = msg["content"]
            print(f"\n{GREEN}✓ 会话结束{RESET} session_id={session_id}")
            print("\n" + (final or "(无最终文本回复)") + "\n")
            return True

        # 一轮里交回来的调用（可能多个并行）一次性本地执行完，再合并成一次请求回传
        payload = _resume_request(session_id, _run_client_calls_locally(pending))


def merge_tool_call_frames(frames: list) -> list:
    """把流式 assistant 帧里被切碎的工具调用片段拼回完整调用。

    服务端按 OpenAI 增量格式下发：片段带 ``_index``（并行调用的序号），name 通常只在首片
    出现，arguments 分片到达。返回 ``[{"id","name","arguments"}]``，按声明顺序。
    """
    merged: dict = {}
    for frame in frames:
        for call in frame.get("tool_calls") or []:
            index = call.get("_index")
            if index is None:
                index = call.get("index")
            if index is None:
                index = call.get("id") or len(merged)
            slot = merged.setdefault(index, {"id": None, "name": "", "arguments": ""})
            if call.get("id"):
                slot["id"] = call["id"]
            nested = call.get("function") or {}
            name = call.get("name") or nested.get("name")
            if name:
                slot["name"] += name
            arguments = call.get("arguments")
            if arguments is None:
                arguments = nested.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if arguments:
                slot["arguments"] += arguments
    return [c for c in merged.values() if c["name"] or c["id"]]


def run_stream(base_url: str, prompt: str) -> bool:
    """流式：按工具名识别客户端工具调用（assistant 帧里声明、始终没有 tool 帧跟上）。"""
    payload = _first_request(prompt)
    session_id = None
    round_no = 0
    while True:
        round_no += 1
        print(f"\n{CYAN}▶ POST /v1/infer/stream（第 {round_no} 次）{RESET}")
        req = urllib.request.Request(
            base_url + "/v1/infer/stream",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        announced = []     # 本轮 assistant 声明过的全部工具调用（片段已拼接）
        answered = set()   # 本轮拿到 tool 结果帧的调用 id —— 服务端已经执行过的
        reported = []      # 服务端显式告知的交回调用（status=client_tool_calls）
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                for line in resp:
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    frame = json.loads(line[6:])
                    session_id = frame.get("session_id") or session_id
                    role = frame.get("role")
                    if role == "assistant" and frame.get("tool_calls"):
                        calls = merge_tool_call_frames([frame])
                        announced.extend(calls)
                        for call in calls:
                            side = ("客户端" if call["name"] in CLIENT_TOOL_NAMES
                                    else "服务端")
                            print(f"\n{DIM}  tool call [{side}] {call['name']}{RESET}")
                    elif role == "assistant" and frame.get("content"):
                        sys.stdout.write(frame["content"])
                        sys.stdout.flush()
                    elif role == "tool":
                        answered.add(frame.get("tool_use_id"))
                        print(f"{GREEN}  ✓ 服务端工具结果{RESET} {frame.get('name')}: "
                              f"{str(frame.get('content'))[:120]}")
                    elif role == "status" and frame.get("status") == "client_tool_calls":
                        reported = frame.get("client_tool_calls") or []
        except urllib.error.HTTPError as exc:
            print(f"{RED}✗ HTTP {exc.code}: "
                  f"{exc.read().decode('utf-8', 'replace')[:300]}{RESET}")
            return False

        # 交回来的调用：优先用服务端显式给出的清单；没有就按「工具名属于客户端工具 +
        # 本轮没有 tool 结果帧回应它」判定（并行调用里服务端那半有结果、客户端那半没有）。
        pending = reported or [
            c for c in announced
            if c["name"] in CLIENT_TOOL_NAMES and c["id"] not in answered
        ]
        if not pending:
            print(f"\n{GREEN}✓ 会话结束{RESET} session_id={session_id}")
            return True
        payload = _resume_request(session_id, _run_client_calls_locally(pending))


# ─────────────────────────────────────────────────────────────────────────────
# 4) 可选：原地起一个 agent service（省去另开终端）
# ─────────────────────────────────────────────────────────────────────────────

def start_service(port: int = 0):
    from runtime.models import ModelConfig, ModelRegistry, ToolRegistry
    from runtime.runtime import Runtime
    from runtime.server import RuntimeHTTPServer

    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id=MODEL_ID,
        api_base=MODEL_API_BASE,
        model_name=MODEL_NAME,
        api_protocol=MODEL_PROTOCOL,
    ))
    runtime = Runtime(model_registry=model_registry, tool_registry=ToolRegistry())
    server = RuntimeHTTPServer(runtime, chats_dir="./chats")
    server.start_background(host="127.0.0.1", port=port)
    return server, f"http://127.0.0.1:{server.port}"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    stream = "--stream" in sys.argv
    prompt = " ".join(args) or (
        "先统计服务端 workspace 的文件规模，再列出当前目录下的条目，"
        "然后读取其中最像 README 的文件，最后用一段话总结这三件事。"
    )

    server = None
    base_url = AGENTS_URL
    if not base_url:
        print(f"{DIM}原地启动 agent service（也可用 AGENTS_URL 指向已运行的服务）{RESET}")
        server, base_url = start_service()
    print(f"{BOLD}Agent service: {base_url}{RESET}  model={MODEL_ID}  workspace={WORKSPACE}")

    try:
        register_side_tools(base_url)
        print(f"\n{BOLD}提问:{RESET} {prompt}")
        ok = run_stream(base_url, prompt) if stream else run_non_stream(base_url, prompt)
        return 0 if ok else 1
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())

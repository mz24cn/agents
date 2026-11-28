"""端到端：标签来源的新会话在真实推理请求里带上来源标签，且标签系统消息不被
AI 代理系统提示词覆盖（第一阶段需求 2 + 4 的集成面）。

与 tests/test_label_rename_and_system_merge.py 的分工：那边验证纯函数与
SessionManager 落盘语义；这里跑真正的 /v1/infer/stream，断言"模型实际收到的
messages"与"index.json / tree.json 的真实状态"，避免只在单元测试里合并、线上
链路却被旧逻辑覆盖的假绿。
"""
from __future__ import annotations

import json
import os
import re

from tests.test_incremental_persist_and_continue import (  # noqa: F401
    _make_resp,
    _openai_sse_text,
    _patch_model_urlopen,
    _post_json,
    _post_stream,
    _url,
    continue_server,
)


def _get_json(server, path: str) -> tuple[int, dict]:
    """GET a JSON endpoint on the running test server (the sidebar's own read)."""
    import urllib.request

    with urllib.request.urlopen(_url(server, path), timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", "replace"))


def _capture(side_effect):
    captured: list = []

    def wrapper(request, **kwargs):
        try:
            captured.append(json.loads(request.data.decode("utf-8")))
        except Exception:
            captured.append({})
        return side_effect(request, **kwargs)

    return captured, wrapper


def _scripted(calls):
    import itertools

    counter = itertools.count()

    def _urlopen(request, **kwargs):
        i = next(counter)
        return _make_resp(calls[min(i, len(calls) - 1)])

    return _urlopen


def _new_session_id(body: str) -> str:
    match = re.search(r'"session_id"\s*:\s*"([^"]+)"', body)
    assert match, f"no session_id in SSE body: {body[:300]}"
    return match.group(1)


def _systems(model_request: dict) -> list:
    return [m for m in model_request.get("messages", [])
            if m.get("role") == "system"]


LABEL_TEXT = "【标签「TTS」聚合概要】\n上一批会话的要点"
AGENT_PROMPT = "你是 TTS 代理的维护助手"

TREE_DOC = {
    "version": 8,
    "stats": {},
    "tree": [
        {"id": 1, "name": "主题", "type": "category", "session_count": 0,
         "children": []},
        {"id": 2, "name": "工作区", "type": "workspace", "session_count": 0,
         "children": []},
        {"id": 6, "name": "标签", "type": "labels", "session_count": 0,
         "children": [], "stats": {}},
        {"id": 3, "name": "本地终端", "type": "terminal", "session_count": 0,
         "environment": "local", "env_id": "", "children": []},
        {"id": 5, "name": "会话历史", "type": "history", "session_count": 0,
         "children": [], "stats": {}},
    ],
}


def _seed_tree(srv) -> None:
    """标签挂载是 tree.json 上的增量操作：没有 tree.json 时 _sync_tree_labels
    直接跳过（线上由分类树构建器保证文件存在），测试里先落一份最小森林。"""
    path = srv._session_manager._category_tree_path
    if callable(path):
        path = path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(TREE_DOC, fh, ensure_ascii=False)


def _index_of(srv) -> dict:
    path = srv._session_manager._index_path
    if callable(path):
        path = path()
    return json.loads(open(path, encoding="utf-8").read())


def _label_children(srv, label: str) -> list:
    tree = srv._session_manager.get_category_tree()
    root = next((t for t in tree["tree"] if t.get("type") == "labels"), None)
    if not root:
        return []
    node = next((c for c in root["children"]
                 if isinstance(c, dict) and c.get("name") == label), None)
    return sorted(node["children"]) if node else []


def test_agent_prompt_merges_with_label_system_message(continue_server):
    """选了 AI 代理：代理提示词与标签聚合内容同时到达模型（旧行为会丢掉后者）。"""
    srv = continue_server
    srv._agent_manager.create(agent_id="TTSAgent", model_id="test-model",
                              nickname="T", system_prompt=AGENT_PROMPT)

    captured, wrapper = _capture(_scripted([_openai_sse_text("好的")]))
    status, body = _post_stream(srv, {
        "session_id": "new",
        "model_id": "test-model",
        "agent_ids": ["TTSAgent"],
        "messages": [
            {"role": "system", "content": LABEL_TEXT, "label_origin": "TTS"},
            {"role": "user", "content": "登录崩溃了，修一下"},
        ],
        "source_labels": ["TTS"],
    }, wrapper)
    assert status == 200, body[:400]
    assert captured, "model API was not called"

    systems = _systems(captured[-1])
    assert systems, "第一轮没有任何 system 消息"
    joined = "\n".join(m.get("content") or "" for m in systems)
    assert AGENT_PROMPT in joined, "代理系统提示词必须仍在"
    assert LABEL_TEXT in joined, "标签聚合的系统消息必须与代理提示词合并共存"
    # 协议层把多条前置 system 并成同一条 → 模型侧只看一次 system
    assert len(systems) == 1, [m.get("content") for m in systems]


def test_source_labels_persist_at_session_creation(continue_server):
    """来源标签随创建会话的请求持久化：index.json + tree.json 标签根都挂上。"""
    srv = continue_server
    _seed_tree(srv)
    captured, wrapper = _capture(_scripted([_openai_sse_text("好的")]))
    status, body = _post_stream(srv, {
        "session_id": "new",
        "model_id": "test-model",
        "messages": [
            {"role": "system", "content": LABEL_TEXT, "label_origin": "TTS"},
            {"role": "user", "content": "登录崩溃了，修一下"},
        ],
        "source_labels": ["TTS", "线上"],
    }, wrapper)
    assert status == 200, body[:400]

    sid = _new_session_id(body)
    entry = _index_of(srv).get(sid)
    assert entry is not None, "会话没有建档"
    assert entry["labels"] == ["TTS", "线上"]
    assert _label_children(srv, "TTS") == [sid]
    assert _label_children(srv, "线上") == [sid]

    # 刷新列表可见：侧栏读的就是 GET /v1/sessions，同一行立刻带上来源标签，
    # 不需要等分类树重建或第二次刷新。
    status, listing = _get_json(srv, "/v1/sessions")
    assert status == 200, listing
    row = next((s for s in listing["sessions"] if s.get("session_id") == sid), None)
    assert row is not None, listing
    assert row.get("labels") == ["TTS", "线上"], row

    # 落盘的对话记录里系统消息仍是独立一条（label_origin 是瞬态字段，不落盘）
    conv_path = os.path.join(srv._context_manager._chats_dir, sid,
                             "conversation.json")
    conv = json.loads(open(conv_path, encoding="utf-8").read())
    first = conv["messages"][0] if isinstance(conv, dict) else conv[0]
    assert first["role"] == "system" and LABEL_TEXT in first["content"]
    assert "label_origin" not in first


def test_label_origin_marker_alone_still_tags_the_session(continue_server):
    """请求体没带 source_labels 时，系统消息上的来源标记也足以打标（少一次漏挂）。"""
    srv = continue_server
    _seed_tree(srv)
    captured, wrapper = _capture(_scripted([_openai_sse_text("好的")]))
    status, body = _post_stream(srv, {
        "session_id": "new",
        "model_id": "test-model",
        "messages": [
            {"role": "system", "content": LABEL_TEXT, "label_origin": "TTS"},
            {"role": "user", "content": "登录崩溃了，修一下"},
        ],
    }, wrapper)
    assert status == 200, body[:400]

    sid = _new_session_id(body)
    assert _index_of(srv)[sid]["labels"] == ["TTS"]
    assert _label_children(srv, "TTS") == [sid]


def test_group_chat_label_message_coexists_with_default_prompt(continue_server):
    """群聊路径同样合并：标签聚合内容 + 群聊默认提示词同时在，且来源标签落盘。

    旧行为下标签那条会被当成"已有系统提示词"，导致默认群聊提示词不注入；
    或者反过来把标签那条丢掉——两头都不对。
    """
    srv = continue_server
    _seed_tree(srv)
    srv._agent_manager.create(agent_id="GC甲", model_id="test-model", nickname="甲")
    srv._agent_manager.create(agent_id="GC乙", model_id="test-model", nickname="乙")

    captured, wrapper = _capture(_scripted([_openai_sse_text("好的")]))
    status, body = _post_stream(srv, {
        "session_id": "new",
        "model_id": "test-model",
        "agent_ids": ["GC甲", "GC乙"],
        "messages": [
            {"role": "system", "content": LABEL_TEXT, "label_origin": "TTS"},
            {"role": "user", "content": "两位一起看看这个问题"},
        ],
        "source_labels": ["TTS"],
    }, wrapper)
    assert status == 200, body[:400]
    assert captured, "model API was not called"

    joined = "\n".join(m.get("content") or ""
                       for req in captured for m in _systems(req))
    assert LABEL_TEXT in joined, "标签聚合的系统消息必须进入群聊 system"
    assert "这是一场群聊对话" in joined, "群聊默认提示词也必须注入"

    sid = _new_session_id(body)
    assert _index_of(srv)[sid]["labels"] == ["TTS"]
    assert _label_children(srv, "TTS") == [sid]


def test_plain_system_message_without_label_is_untouched(continue_server):
    """不带来源标记、也没有代理：系统消息按原样透传，不会被打标签或改写。"""
    srv = continue_server
    captured, wrapper = _capture(_scripted([_openai_sse_text("好的")]))
    status, body = _post_stream(srv, {
        "session_id": "new",
        "model_id": "test-model",
        "messages": [
            {"role": "system", "content": "用户自选的系统提示词"},
            {"role": "user", "content": "问题"},
        ],
    }, wrapper)
    assert status == 200, body[:400]

    systems = _systems(captured[-1])
    assert [m.get("content") for m in systems] == ["用户自选的系统提示词"]
    sid = _new_session_id(body)
    assert "labels" not in _index_of(srv)[sid]

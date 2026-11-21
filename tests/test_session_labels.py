"""会话标签体系（v8）的专项测试。

覆盖：
  * set_session_labels：index.json 清洗落盘 + tree.json 标签根增删挂载；
  * _apply_session_facts：标签随事实根增量补齐（自动压缩路径的根一致性）；
  * list_sessions_by_category("6/x")：标签会话列表按时间倒序；
  * remove_session_from_tree：标签节点随会话清空剪除，标签根保留；
  * label_store：聚合配置众数统计、概要合并（LLM 与降级拼接两条路）、
    未生成概要的会话以首尾摘录代替、文档落 DATA_DIR/labels/。
"""
from __future__ import annotations

import json

import pytest

from runtime import label_store
from runtime.session_manager import SessionManager


def _write_conversation(chats_dir, sid, *, workspace="/cc/agents", remote_env="",
                        model_id="", tool_ids=None, agent_ids=None,
                        created_at="2026-11-20T10:00:00", messages=None):
    d = chats_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"session_id": sid, "workspace": workspace, "created_at": created_at}
    if remote_env:
        meta["remote_env"] = remote_env
    if model_id:
        meta["model_id"] = model_id
    if tool_ids is not None:
        meta["tool_ids"] = list(tool_ids)
    if agent_ids is not None:
        meta["agent_ids"] = list(agent_ids)
    conv = {
        "meta": meta,
        "messages": messages if messages is not None else [
            {"role": "user", "content": f"{sid} 的问题"},
            {"role": "assistant", "content": "回答。"},
        ],
    }
    (d / "conversation.json").write_text(json.dumps(conv, ensure_ascii=False), "utf-8")


def _v8_tree_doc() -> dict:
    """五根 v8 森林最小形（主题/工作区/标签/终端/历史），主题与标签根为空。"""
    return {
        "version": 8,
        "stats": {},
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 2,
             "children": [{"id": 1, "name": "catA", "type": "category",
                           "session_count": 2, "children": ["s1", "keep"]}]},
            {"id": 2, "name": "工作区", "type": "workspace", "session_count": 0, "children": []},
            {"id": 6, "name": "标签", "type": "labels", "session_count": 0, "children": [],
             "stats": {}},
            {"id": 3, "name": "本地终端", "type": "terminal", "session_count": 0,
             "environment": "local", "env_id": "", "children": []},
            {"id": 5, "name": "会话历史", "type": "history", "session_count": 0,
             "children": [], "stats": {}},
        ],
    }


@pytest.fixture()
def env(tmp_path):
    chats = tmp_path / "chat_data"
    chats.mkdir()
    (chats / "tree.json").write_text(
        json.dumps(_v8_tree_doc(), ensure_ascii=False), "utf-8")
    mgr = SessionManager(str(chats))
    return mgr, chats


def _index_add(mgr, sid: str, title: str) -> None:
    """直接落一条 index 条目（绕开 update_index 的标题生成路径）。"""
    index = mgr._read_index()
    index[sid] = {"session_id": sid, "title": title,
                  "created_at": "2026-11-20T10:00:00", "last_inference_at": ""}
    mgr._write_index(index)


def _labels_root(mgr) -> dict:
    tree = mgr.get_category_tree()
    return next(t for t in tree["tree"] if t.get("type") == "labels")


# ---------------------------------------------------------------------------
# set_session_labels / _sync_tree_labels
# ---------------------------------------------------------------------------

def test_set_labels_writes_index_and_tree(env):
    mgr, chats = env
    _write_conversation(chats, "s1")
    _index_add(mgr, "s1", title="t")

    cleaned = mgr.set_session_labels("s1", [" tts ", "部署", "tts", "，", ""])
    assert cleaned == ["tts", "部署"]
    entry = mgr._read_index()["s1"]
    assert entry["labels"] == ["tts", "部署"]

    root = _labels_root(mgr)
    by_name = {c["name"]: c for c in root["children"]}
    assert set(by_name) == {"tts", "部署"}
    assert by_name["tts"]["children"] == ["s1"]
    assert by_name["tts"]["type"] == "label"
    assert root["stats"]["label_count"] == 2

    # 再设一组：旧的清空剪除，新的挂载
    mgr.set_session_labels("s1", ["部署"])
    root = _labels_root(mgr)
    assert [c["name"] for c in root["children"]] == ["部署"]
    assert root["children"][0]["children"] == ["s1"]

    # 清空标签：index 不再带 labels 键，标签根退回空
    mgr.set_session_labels("s1", [])
    assert "labels" not in mgr._read_index()["s1"]
    assert _labels_root(mgr)["children"] == []


def test_set_labels_shared_node_and_missing_session(env):
    mgr, chats = env
    _write_conversation(chats, "s1")
    _write_conversation(chats, "s2")
    _index_add(mgr, "s1", title="a")
    _index_add(mgr, "s2", title="b")
    mgr.set_session_labels("s1", ["x"])
    mgr.set_session_labels("s2", ["x", "y"])
    root = _labels_root(mgr)
    x = next(c for c in root["children"] if c["name"] == "x")
    assert set(x["children"]) == {"s1", "s2"}
    assert x["session_count"] == 2
    # 摘掉 s1 的 x：节点仍在（s2 还挂着）
    mgr.set_session_labels("s1", ["y"])
    root = _labels_root(mgr)
    names = {c["name"] for c in root["children"]}
    assert names == {"x", "y"}

    with pytest.raises(FileNotFoundError):
        mgr.set_session_labels("ghost", ["z"])


def test_labels_listed_by_category_time_desc(env):
    mgr, chats = env
    _write_conversation(chats, "old", created_at="2026-01-01T00:00:00")
    _write_conversation(chats, "new", created_at="2026-06-01T00:00:00")
    _index_add(mgr, "old", title="旧")
    _index_add(mgr, "new", title="新")
    mgr.set_session_labels("old", ["k"])
    mgr.set_session_labels("new", ["k"])
    root = _labels_root(mgr)
    node = root["children"][0]
    rows = mgr.list_sessions_by_category(f"6/{node['id']}")
    assert [r["session_id"] for r in rows] == ["new", "old"]


def test_apply_session_facts_backfills_index_labels(env):
    """index 已有 labels 而树上缺失时，事实根补齐路径要把标签挂载补回来——
    自动压缩与手动重生概要走的都是这条路，两条路结果必须一致。"""
    mgr, chats = env
    _write_conversation(chats, "s1")
    _index_add(mgr, "s1", title="t")
    # 绕过 set_session_labels：直接改 index，模拟旧树/新标签的错位
    index = mgr._read_index()
    index["s1"]["labels"] = ["回填"]
    mgr._write_index(index)
    assert _labels_root(mgr)["children"] == []

    assert mgr.ensure_session_facts("s1") is True
    root = _labels_root(mgr)
    assert root["children"][0]["name"] == "回填"
    assert root["children"][0]["children"] == ["s1"]


def test_remove_session_prunes_label_but_keeps_root(env):
    mgr, chats = env
    _write_conversation(chats, "s1")
    _index_add(mgr, "s1", title="t")
    mgr.set_session_labels("s1", ["k"])
    assert _labels_root(mgr)["children"]

    assert mgr.remove_session_from_tree("s1") is True
    root = _labels_root(mgr)
    assert root["type"] == "labels"
    assert root["children"] == []


# ---------------------------------------------------------------------------
# label_store：聚合与落盘
# ---------------------------------------------------------------------------

def test_aggregate_config_takes_modes(env, monkeypatch, tmp_path):
    mgr, chats = env
    _write_conversation(chats, "s1", workspace="/w/alpha", remote_env="http://e1:7989",
                        model_id="m-a", tool_ids=["read_file", "exec_shell"],
                        agent_ids=["a1"])
    _write_conversation(chats, "s2", workspace="/w/alpha", remote_env="",
                        model_id="m-b", tool_ids=["read_file"], agent_ids=[])
    _write_conversation(chats, "s3", workspace="/w/beta", remote_env="http://e1:7989",
                        model_id="m-a", tool_ids=["read_file", "exec_shell"],
                        agent_ids=["a1"])
    cfg = label_store.aggregate_config(str(chats), ["s1", "s2", "s3"])
    assert cfg["remote_env_id"] == "http://e1:7989"
    assert cfg["workspace"] == "/w/alpha"
    assert cfg["model_id"] == "m-a"
    assert cfg["tool_ids"] == ["read_file", "exec_shell"]
    assert cfg["agent_ids"] == ["a1"]
    assert cfg == label_store.aggregate_config(str(chats), ["s1", "s2", "s3"])


def test_summarize_label_merges_and_persists(env, tmp_path, monkeypatch):
    mgr, chats = env
    for sid in ("s1", "s2"):
        _write_conversation(chats, sid)
        _index_add(mgr, sid, title=f"会话{sid}")
    mgr.set_session_labels("s1", ["TTS"])
    mgr.set_session_labels("s2", ["TTS"])
    # s1 有概要/记忆；s2 没有 → 首尾摘录代替
    (chats / "s1" / "summary.md").write_text("s1 的概要", "utf-8")
    (chats / "s1" / "memory.md").write_text("s1 的记忆", "utf-8")

    monkeypatch.setattr(label_store, "labels_dir", lambda: str(tmp_path / "labels"))

    captured = {}

    def fake_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"summary": "合并概要", "memory": "- 要点一\\n- 要点二"}'

    doc = label_store.summarize_label("TTS", mgr, llm=fake_llm)
    assert doc["summary"] == "合并概要"
    assert doc["memory"] == "- 要点一\n- 要点二"
    assert doc["session_count"] == 2
    assert set(doc["session_ids"]) == {"s1", "s2"}
    # 素材：s1 用概要+记忆，s2 用首尾摘录
    assert "s1 的概要" in captured["prompt"]
    assert "s1 的记忆" in captured["prompt"]
    assert "未生成概要/记忆" in captured["prompt"]
    assert "s2 的问题" in captured["prompt"]

    saved = label_store.load_label_doc("TTS")
    assert saved and saved["summary"] == "合并概要"
    assert (tmp_path / "labels").is_dir()


def test_summarize_label_without_llm_falls_back_to_concat(env, tmp_path, monkeypatch):
    mgr, chats = env
    _write_conversation(chats, "s1")
    _index_add(mgr, "s1", title="t1")
    mgr.set_session_labels("s1", ["solo"])
    monkeypatch.setattr(label_store, "labels_dir", lambda: str(tmp_path / "labels"))

    doc = label_store.summarize_label("solo", mgr, llm=lambda prompt: "")
    assert "s1 的问题" in doc["summary"]  # 降级为直接拼接，聚合仍可用
    assert doc["memory"] == ""

    with pytest.raises(ValueError):
        label_store.summarize_label("不存在", mgr, llm=None)


def test_label_doc_path_isolated_per_label(tmp_path, monkeypatch):
    monkeypatch.setattr(label_store, "labels_dir", lambda: str(tmp_path))
    p1 = label_store.label_doc_path("性能 优化")
    p2 = label_store.label_doc_path("性能/优化")
    assert p1 != p2  # 清洗后同名也不覆盖：后缀带原文哈希
    label_store.save_label_doc({"label": "性能 优化", "summary": "s"})
    assert label_store.load_label_doc("性能 优化")["summary"] == "s"
    assert label_store.load_label_doc("性能/优化") is None

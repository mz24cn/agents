"""会话旗标（index.json 的 ``flag`` 字段）与标签/标题接口的回归测试。

「设置标题」对话框把旗标和标题一起保存，「设置标签」对话框只改标签，因此后端要
保证：

  * ``clean_flag``：候选集把关（不在集合内 400）、变体选择符归一、空值 = 清除；
  * ``SessionManager.set_session_flag``：index.json 落盘、清除即删键（旧数据形状
    不变）、未知会话 FileNotFoundError、非法旗标 ValueError；
  * ``POST /v1/sessions/{id}/flag``：状态码、缺 ``flag`` 键 400、回带 index 元数据；
  * **旗标以 index 为准**：同一会话挂在多个分类副本下，两个分类读到同一份旗标
    （不会只改到某个树节点）；
  * 只改标签不动标题/旗标（``POST /v1/sessions/{id}/labels``），只改标题不动
    标签/旗标（``POST /v1/sessions/{id}/generate-title`` 不带 labels）；
  * 旧会话（没有 ``flag`` 键）依旧可读，清除旗标后回到"无该键"的老形状。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.session_manager import SESSION_FLAGS, SessionManager, clean_flag
from tests.test_server import (  # noqa: F401
    _get,
    _post,
    registries,
    runtime,
    server,
)


# ---------------------------------------------------------------------------
# clean_flag / SessionManager.set_session_flag（单元层）
# ---------------------------------------------------------------------------

def test_clean_flag_accepts_candidates_and_clears():
    """候选集内的旗标原样接受；空值 = 清除；带/不带 VS16 归一到同一写法。"""
    assert list(SESSION_FLAGS) == [
        "🚩", "🏴", "🏳️", "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "✅", "⚠️",
    ]
    for flag in SESSION_FLAGS:
        assert clean_flag(flag) == flag
    assert clean_flag("") == ""
    assert clean_flag("   ") == ""
    assert clean_flag(None) == ""
    # 🏳️ = U+1F3F3 U+FE0F，去掉变体选择符仍是同一个旗标
    assert clean_flag("🏳") == "🏳️"
    assert clean_flag("⚠") == "⚠️"


def test_clean_flag_rejects_unknown():
    for bad in ("❌", "🚩🚩", "flag", "红旗"):
        with pytest.raises(ValueError):
            clean_flag(bad)


@pytest.fixture()
def mgr(tmp_path):
    chats = tmp_path / "chat_data"
    chats.mkdir()
    return SessionManager(str(chats)), chats


def _index_add(mgr: SessionManager, sid: str, **fields) -> None:
    """直接落一条 index 条目（绕开 update_index 的标题生成路径）。"""
    index = mgr._read_index()
    entry = {"session_id": sid, "title": f"t-{sid}",
             "created_at": "2026-11-20T10:00:00", "last_inference_at": ""}
    entry.update(fields)
    index[sid] = entry
    mgr._write_index(index)


def test_set_flag_persists_in_index_and_clear_removes_key(mgr):
    manager, _ = mgr
    _index_add(manager, "s1", labels=["tts"])

    assert manager.set_session_flag("s1", "🟡") == "🟡"
    entry = manager._read_index()["s1"]
    assert entry["flag"] == "🟡"
    # 旗标不碰标签
    assert entry["labels"] == ["tts"]

    # 改旗标是覆盖，不是追加
    assert manager.set_session_flag("s1", "✅") == "✅"
    assert manager._read_index()["s1"]["flag"] == "✅"

    # 清除 = 删键（老会话本来就没有 flag，清完应回到同一形状）
    manager.set_session_flag("s1", "")
    assert "flag" not in manager._read_index()["s1"]


def test_set_flag_validation_and_missing_session(mgr):
    manager, _ = mgr
    _index_add(manager, "s1")

    with pytest.raises(ValueError):
        manager.set_session_flag("s1", "❌")
    assert "flag" not in manager._read_index()["s1"]

    with pytest.raises(FileNotFoundError):
        manager.set_session_flag("ghost", "🚩")


def test_get_session_entry_reads_index_metadata(mgr):
    manager, _ = mgr
    _index_add(manager, "s1", title="标题", labels=["a"], flag="🚩")
    entry = manager.get_session_entry("s1")
    assert entry["title"] == "标题"
    assert entry["labels"] == ["a"]
    assert entry["flag"] == "🚩"
    assert manager.get_session_entry("ghost") is None


def test_labels_and_manual_title_do_not_touch_flag(mgr):
    """标签整组替换与人工标题都不得顺手清掉旗标（两个字段各管各的）。"""
    manager, _ = mgr
    _index_add(manager, "s1", title="旧标题", labels=["旧"], flag="🟢")

    assert manager.set_session_labels("s1", ["新", "tts"]) == ["新", "tts"]
    entry = manager._read_index()["s1"]
    assert entry["labels"] == ["新", "tts"]
    assert entry["flag"] == "🟢"
    assert entry["title"] == "旧标题"

    assert manager.set_title_manually("s1", "新标题") == "新标题"
    entry = manager._read_index()["s1"]
    assert entry["title"] == "新标题"
    assert entry["labels"] == ["新", "tts"]
    assert entry["flag"] == "🟢"


def test_flag_survives_across_category_copies(mgr):
    """同一会话挂在多个分类副本下：旗标存在 index 里，两个分类读到同一份。"""
    manager, chats = mgr
    tree = {
        "version": 8,
        "stats": {},
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 2,
             "children": [
                 {"id": 2, "name": "catA", "type": "category",
                  "session_count": 1, "children": ["s1"]},
                 {"id": 3, "name": "catB", "type": "category",
                  "session_count": 1, "children": ["s1"]},
             ]},
        ],
    }
    (chats / "tree.json").write_text(json.dumps(tree, ensure_ascii=False), "utf-8")
    _index_add(manager, "s1")

    manager.set_session_flag("s1", "🏴")
    for path in ("1/2", "1/3"):
        rows = manager.list_sessions_by_category(path)
        assert [r["session_id"] for r in rows] == ["s1"]
        assert rows[0]["flag"] == "🏴"

    manager.set_session_flag("s1", "")
    for path in ("1/2", "1/3"):
        assert "flag" not in manager.list_sessions_by_category(path)[0]


# ---------------------------------------------------------------------------
# POST /v1/sessions/{id}/flag（HTTP 层）
# ---------------------------------------------------------------------------

def _seed(srv, sid: str, *, title="原标题", labels=None, flag=None) -> None:
    index = srv._session_manager._read_index()
    entry = {"session_id": sid, "title": title,
             "created_at": "2026-11-20T10:00:00", "last_inference_at": ""}
    if labels:
        entry["labels"] = list(labels)
    if flag:
        entry["flag"] = flag
    index[sid] = entry
    srv._session_manager._write_index(index)


def _entry_of(body: dict, sid: str) -> dict:
    return next((s for s in body["sessions"] if s.get("session_id") == sid), {})


def test_flag_endpoint_set_read_and_clear(server):
    srv = server
    _seed(srv, "261120_200000", title="会话甲", labels=["tts"])

    status, body = _post(srv, "/v1/sessions/261120_200000/flag", {"flag": "🚩"})
    assert status == 200, body
    assert body["flag"] == "🚩"
    # 回带 index 里的标题与标签：前端以服务端元数据为准刷新行
    assert body["title"] == "会话甲"
    assert body["labels"] == ["tts"]

    _, listing = _get(srv, "/v1/sessions")
    assert _entry_of(listing, "261120_200000").get("flag") == "🚩"

    status, body = _post(srv, "/v1/sessions/261120_200000/flag", {"flag": ""})
    assert status == 200, body
    assert body["flag"] == ""
    _, listing = _get(srv, "/v1/sessions")
    assert "flag" not in _entry_of(listing, "261120_200000")


def test_flag_endpoint_rejects_bad_input_without_side_effects(server):
    srv = server
    _seed(srv, "261120_200100", flag="🔴")

    # 不在候选集
    status, _ = _post(srv, "/v1/sessions/261120_200100/flag", {"flag": "❌"})
    assert status == 400
    # 缺 flag 键：不能把"忘了传"当成"想清除"
    status, _ = _post(srv, "/v1/sessions/261120_200100/flag", {})
    assert status == 400
    # 会话不存在
    status, _ = _post(srv, "/v1/sessions/261120_209999/flag", {"flag": "🚩"})
    assert status == 404

    _, listing = _get(srv, "/v1/sessions")
    assert _entry_of(listing, "261120_200100").get("flag") == "🔴"


def test_flag_visible_in_category_listing(server):
    """切换分类读的是同一份 index：两个分类副本都看得到旗标。"""
    srv = server
    tree = {
        "version": 8,
        "stats": {},
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 2,
             "children": [
                 {"id": 2, "name": "catA", "type": "category",
                  "session_count": 1, "children": ["261120_200200"]},
                 {"id": 3, "name": "catB", "type": "category",
                  "session_count": 1, "children": ["261120_200200"]},
             ]},
        ],
    }
    tree_path = Path(srv._session_manager.chats_dir) / "tree.json"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(
        json.dumps(tree, ensure_ascii=False), "utf-8")
    _seed(srv, "261120_200200")

    status, body = _post(srv, "/v1/sessions/261120_200200/flag", {"flag": "⚠️"})
    assert status == 200, body

    for path in ("1/2", "1/3"):
        status, listing = _get(srv, f"/v1/sessions?category={path}")
        assert status == 200, listing
        assert _entry_of(listing, "261120_200200").get("flag") == "⚠️"


def test_legacy_session_without_flag_stays_readable(server):
    """旧会话没有 flag 键：列表可读、无旗标；设置→清除后又回到无键形状。"""
    srv = server
    _seed(srv, "261120_200300", title="老会话")

    _, listing = _get(srv, "/v1/sessions")
    legacy = _entry_of(listing, "261120_200300")
    assert "flag" not in legacy
    assert legacy["title"] == "老会话"

    status, _ = _post(srv, "/v1/sessions/261120_200300/flag", {"flag": "🟤"})
    assert status == 200
    assert _entry_of(_get(srv, "/v1/sessions")[1], "261120_200300").get("flag") == "🟤"

    status, _ = _post(srv, "/v1/sessions/261120_200300/flag", {"flag": None})
    assert status == 200
    assert "flag" not in _entry_of(_get(srv, "/v1/sessions")[1], "261120_200300")


def test_labels_endpoint_keeps_title_and_flag(server):
    """「设置标签」只改标签：标题与旗标原样保留，并回带 index 里的当前值。"""
    srv = server
    _seed(srv, "261120_200400", title="别动我", labels=["旧"], flag="🟠")

    status, body = _post(srv, "/v1/sessions/261120_200400/labels",
                         {"labels": ["新", "tts"]})
    assert status == 200, body
    assert body["labels"] == ["新", "tts"]
    assert body["title"] == "别动我"
    assert body["flag"] == "🟠"

    entry = _entry_of(_get(srv, "/v1/sessions")[1], "261120_200400")
    assert entry["labels"] == ["新", "tts"]
    assert entry["title"] == "别动我"
    assert entry["flag"] == "🟠"


def test_live_status_is_the_same_in_every_category_listing(server):
    """实时状态（含客户端工具等待态）按 session_id 覆盖：两个分类读到同一个值。

    列表行本身没有状态字段，状态是内存里的一份；分页 / 分类查询必须在取数那一刻
    把它叠加到每一份副本上（``_status``），否则「A 分类显示推理中、B 分类显示完成」。
    """
    import threading

    from runtime import server_state as ss

    srv = server
    sid = "261120_210000"
    tree = {
        "version": 8,
        "stats": {},
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 2,
             "children": [
                 {"id": 2, "name": "catA", "type": "category",
                  "session_count": 1, "children": [sid]},
                 {"id": 3, "name": "catB", "type": "category",
                  "session_count": 1, "children": [sid]},
             ]},
        ],
    }
    tree_path = Path(srv._session_manager.chats_dir) / "tree.json"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(json.dumps(tree, ensure_ascii=False), "utf-8")
    _seed(srv, sid, title="同一状态", labels=["tts"], flag="🟦")

    def _rows():
        out = {}
        for path in ("1/2", "1/3", ""):
            suffix = f"?category={path}" if path else ""
            status, listing = _get(srv, f"/v1/sessions{suffix}")
            assert status == 200, listing
            out[path] = _entry_of(listing, sid)
        return out

    # 没有实时状态时不应凭空出现 _status
    assert all("_status" not in row for row in _rows().values())

    cancel = threading.Event()
    assert ss.begin_session_stream(sid, cancel) is None
    try:
        assert ss.transition_session_stream_status(
            sid, cancel, ss.SESSION_STATUS_CLIENT_TOOL_CALLS) is True
        rows = _rows()
        for path, row in rows.items():
            assert row.get("_status") == ss.SESSION_STATUS_CLIENT_TOOL_CALLS, (path, row)
            # 元数据同样来自同一份 index：旗标 / 标签 / 标题三处一致
            assert row["flag"] == "🟦" and row["labels"] == ["tts"]
            assert row["title"] == "同一状态"

        # 已读只清徽标，等待态留在状态里（快照 / 列表再读还是蓝色）
        status, _ = _post(srv, f"/v1/sessions/{sid}/read", {})
        assert status == 200, status
        assert sid not in ss._unread_sessions
        assert ss.get_session_status(sid) == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
        assert _rows()["1/3"].get("_status") == ss.SESSION_STATUS_CLIENT_TOOL_CALLS
    finally:
        cancel.set()
        ss.finish_session_stream(sid, cancel)
        ss._session_streams.pop(sid, None)
        ss._session_statuses.pop(sid, None)
        ss._unread_sessions.pop(sid, None)


def test_generate_title_without_labels_keeps_labels_and_flag(server):
    """「设置标题」不带 labels：标签与旗标都不受影响。"""
    srv = server
    _seed(srv, "261120_200500", title="旧标题", labels=["tts", "性能"], flag="🔵")

    status, body = _post(srv, "/v1/sessions/261120_200500/generate-title",
                         {"title": "新标题"})
    assert status == 200, body
    assert body["title"] == "新标题"
    assert body["title_given"] is True
    assert body["labels"] == ["tts", "性能"]

    entry = _entry_of(_get(srv, "/v1/sessions")[1], "261120_200500")
    assert entry["title"] == "新标题"
    assert entry["labels"] == ["tts", "性能"]
    assert entry["flag"] == "🔵"

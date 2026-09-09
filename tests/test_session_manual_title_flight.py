"""人工设定标题（title_given）与飞行模式持久化（index.json）测试。"""
import json

import pytest

from runtime import server_state
from runtime.models import InferenceResult, Message
from runtime.session_manager import SessionManager


def _fake_infer(reply: str):
    def infer(request):
        return InferenceResult(
            success=True,
            messages=[
                Message(role="user", content=request.messages[0].content),
                Message(role="assistant", content=reply),
            ],
        )

    return infer


# ---------------------------------------------------------------------------
# 人工设定标题
# ---------------------------------------------------------------------------

def _write_index(tmp_path, entry):
    (tmp_path / "index.json").write_text(json.dumps(entry, ensure_ascii=False), "utf-8")


def test_set_title_manually_after_model_generation(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")

    index = sm._read_index()
    index["s1"]["title"] = "模型生成的标题"
    index["s1"]["title_generated"] = "模型生成的标题"
    sm._write_index(index)

    title = sm.set_title_manually("s1", "  人工标题  ")
    assert title == "人工标题"

    index = sm._read_index()
    assert index["s1"]["title"] == "人工标题"
    assert index["s1"]["title_given"] is True
    # 原模型生成的标题保留在 title_generated
    assert index["s1"]["title_generated"] == "模型生成的标题"


def test_set_title_manually_again_on_marked_session(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")

    index = sm._read_index()
    index["s1"]["title"] = "模型生成的标题"
    index["s1"]["title_generated"] = "模型生成的标题"
    sm._write_index(index)

    sm.set_title_manually("s1", "人工标题一")
    sm.set_title_manually("s1", "人工标题二")

    index = sm._read_index()
    assert index["s1"]["title"] == "人工标题二"
    assert index["s1"]["title_given"] is True
    # 再次设定时 title_generated 仍是最早的模型生成标题
    assert index["s1"]["title_generated"] == "模型生成的标题"


def test_set_title_manually_without_prior_generation(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")

    sm.set_title_manually("s1", "人工标题")

    index = sm._read_index()
    assert index["s1"]["title"] == "人工标题"
    assert index["s1"]["title_given"] is True
    assert index["s1"]["title_generated"] == ""


def test_set_title_manually_legacy_bool_title_generated(tmp_path):
    # 旧格式：title_generated 为 bool True，当前标题即模型生成标题
    sm = SessionManager(str(tmp_path))
    _write_index(tmp_path, {
        "s1": {
            "session_id": "s1",
            "title": "旧格式生成标题",
            "created_at": "2024-01-01T00:00:00",
            "last_inference_at": "2024-01-01T00:00:00",
            "turn_count": 1,
            "last_total_tokens": 10,
            "title_generated": True,
        }
    })

    sm.set_title_manually("s1", "人工标题")

    index = sm._read_index()
    assert index["s1"]["title"] == "人工标题"
    assert index["s1"]["title_given"] is True
    # 旧格式 True 时，原模型生成标题即当时的 title
    assert index["s1"]["title_generated"] == "旧格式生成标题"


def test_set_title_manually_validation(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")

    with pytest.raises(ValueError):
        sm.set_title_manually("s1", "   ")
    with pytest.raises(FileNotFoundError):
        sm.set_title_manually("missing", "标题")

    # 超过 100 字符截断
    long_title = "标" * 150
    assert sm.set_title_manually("s1", long_title) == "标" * 100
    index = sm._read_index()
    assert len(index["s1"]["title"]) == 100


def test_set_title_manually_broadcasts_title_update(tmp_path):
    events = []
    sm = SessionManager(
        str(tmp_path),
        broadcast_fn=lambda sid, event, payload: events.append((sid, event, payload)),
    )
    sm.on_session_created("s1", "第一条用户消息")

    sm.set_title_manually("s1", "人工标题")

    assert events == [("s1", "title_update", {"title": "人工标题", "title_given": True})]


def test_do_generate_title_resets_title_given(tmp_path):
    """人工设定后再次走模型生成（空输入确定）：title_given 复位。"""
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")
    (tmp_path / "s1").mkdir(exist_ok=True)
    (tmp_path / "s1" / "conversation.json").write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "帮我看下这个问题"},
                {"role": "assistant", "content": "好的"},
            ],
            "meta": {"turn_count": 1},
        }, ensure_ascii=False),
        "utf-8",
    )

    index = sm._read_index()
    index["s1"]["title"] = "模型生成的标题"
    index["s1"]["title_generated"] = "模型生成的标题"
    sm._write_index(index)

    sm.set_title_manually("s1", "人工标题")

    sm._infer_fn = _fake_infer("新生成标题")
    sm.generate_title_forced("s1")

    index = sm._read_index()
    assert index["s1"]["title"] == "新生成标题"
    assert index["s1"]["title_generated"] == "新生成标题"
    assert index["s1"]["title_given"] is False



# ---------------------------------------------------------------------------
# 飞行模式持久化
# ---------------------------------------------------------------------------

def test_flight_mode_persisted_to_index(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")

    sm.set_session_flight_mode("s1", True)
    index = sm._read_index()
    assert index["s1"]["flight_mode"] is True

    sm.set_session_flight_mode("s1", False)
    index = sm._read_index()
    assert index["s1"]["flight_mode"] is False


def test_flight_sessions_lists_persisted_sessions(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "消息一")
    sm.on_session_created("s2", "消息二")
    sm.set_session_flight_mode("s2", True)
    sm.set_session_flight_mode("s1", True)

    assert sm.flight_sessions() == ["s1", "s2"]


def test_flight_mode_creates_missing_index_entry(tmp_path):
    sm = SessionManager(str(tmp_path))

    # 会话尚不在 index 中：补写默认条目
    sm.set_session_flight_mode("s1", True)

    index = sm._read_index()
    assert index["s1"]["flight_mode"] is True
    assert index["s1"]["title"] == "s1"


def test_load_flight_sessions_hydrates_memory(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "消息一")
    sm.set_session_flight_mode("s1", True)

    # 模拟重启：清空内存中的飞行模式集合
    server_state._flight_sessions.clear()
    try:
        assert server_state.is_session_flight_mode("s1") is False

        server_state.load_flight_sessions(sm.flight_sessions())
        assert server_state.is_session_flight_mode("s1") is True
        assert server_state.flight_sessions_snapshot() == ["s1"]
    finally:
        server_state._flight_sessions.clear()


def test_delete_session_removes_flight_mode_from_index(tmp_path):
    sm = SessionManager(str(tmp_path))
    session_id = "s1"
    (tmp_path / session_id).mkdir()
    (tmp_path / session_id / "conversation.json").write_text(
        json.dumps({"messages": [], "meta": {"turn_count": 0}}), "utf-8"
    )
    sm.on_session_created(session_id, "消息一")
    sm.set_session_flight_mode(session_id, True)

    sm.delete_session(session_id)

    assert sm.flight_sessions() == []
    assert session_id not in sm._read_index()

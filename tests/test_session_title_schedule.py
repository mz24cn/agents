from unittest.mock import patch

from runtime.session_manager import SessionManager


def test_automatic_title_runs_once_then_only_after_compression(tmp_path):
    sm = SessionManager(str(tmp_path))
    session_id = "s1"
    sm.on_session_created(session_id, "first user message")

    with patch.object(sm, "_do_generate_title", return_value="generated") as generate:
        # First completed inference: generate promptly, regardless of token count.
        sm.generate_title(session_id, 10)
        assert generate.call_count == 1

        # Simulate the successful generator's index update (new format:
        # title_generated stores the generated title string).
        index = sm._read_index()
        index[session_id]["title"] = "generated"
        index[session_id]["title_generated"] = "generated"
        index[session_id]["title_given"] = False
        sm._write_index(index)

        # Ordinary later inference: no regeneration.
        sm.generate_title(session_id, 20)
        assert generate.call_count == 1

        # A real summary/memory compression update permits one refresh.
        sm.generate_title(session_id, 30, compression_updated=True)
        assert generate.call_count == 2


def test_manual_title_blocks_automatic_generation(tmp_path):
    sm = SessionManager(str(tmp_path))
    session_id = "s1"
    sm.on_session_created(session_id, "first user message")

    with patch.object(sm, "_do_generate_title", return_value="generated") as generate:
        sm.generate_title(session_id, 10)
        index = sm._read_index()
        index[session_id]["title"] = "generated"
        index[session_id]["title_generated"] = "generated"
        sm._write_index(index)

        # 人工设定标题后，自动生成（含压缩后刷新）不再覆盖
        sm.set_title_manually(session_id, "我的标题")
        sm.generate_title(session_id, 20)
        sm.generate_title(session_id, 30, compression_updated=True)
        assert generate.call_count == 1

        index = sm._read_index()
        assert index[session_id]["title"] == "我的标题"
        assert index[session_id]["title_given"] is True


def test_manual_title_before_any_generation_blocks_first_generation(tmp_path):
    sm = SessionManager(str(tmp_path))
    session_id = "s1"
    sm.on_session_created(session_id, "first user message")

    with patch.object(sm, "_do_generate_title", return_value="generated") as generate:
        # 模型尚未生成过标题时人工设定：首次推理后也不应自动生成覆盖
        sm.set_title_manually(session_id, "人工")
        sm.generate_title(session_id, 10)
        assert generate.call_count == 0
        index = sm._read_index()
        assert index[session_id]["title"] == "人工"
        assert index[session_id]["title_generated"] == ""

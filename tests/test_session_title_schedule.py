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

        # Simulate the successful generator's index update.
        index = sm._read_index()
        index[session_id]["title_generated"] = True
        sm._write_index(index)

        # Ordinary later inference: no regeneration.
        sm.generate_title(session_id, 20)
        assert generate.call_count == 1

        # A real summary/memory compression update permits one refresh.
        sm.generate_title(session_id, 30, compression_updated=True)
        assert generate.call_count == 2

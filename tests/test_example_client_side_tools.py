"""End-to-end check of examples/example_client_side_tools.py.

The example is the reference client for mixed execution sides, so it is tested
against a real agent service and a scripted OpenAI SSE model: one model round
declares a server-side tool *and* two client-side tools (one pre-registered,
one shipped in the request's ``client_tools``).  The example must let the
service run its own half, execute the rest locally, and resume the same session
until the model answers.
"""

import io
import json
import os

from examples.example_client_side_tools import (
    execute_local_tool,
    merge_tool_call_frames,
    register_side_tools,
    run_non_stream,
    run_stream,
)
import examples.example_client_side_tools as example

from tests.test_client_side_tools import (
    _conversation,
    _fake_model_server,
    _running_server,
    _sse,
    _sse_content,
)


def _sse_parallel_calls(calls: list[tuple[str, str, str]]) -> io.BytesIO:
    """One model round declaring several tool calls in parallel."""
    lines = []
    for index, (name, call_id, arguments) in enumerate(calls):
        lines.append("data: " + json.dumps({
            "choices": [{
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }],
                }
            }]
        }) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return _sse(lines)


def test_merge_tool_call_frames_reassembles_parallel_fragments() -> None:
    """Streaming clients must be able to rebuild a whole round's calls."""
    frames = [
        {"role": "assistant", "tool_calls": [
            {"_index": 0, "id": "call_a", "name": "list_dir", "arguments": '{"pa'},
        ]},
        {"role": "assistant", "tool_calls": [
            {"_index": 0, "arguments": 'th": "x"}'},
            {"_index": 1, "id": "call_b", "name": "read_local_file",
             "arguments": '{"path": "y"}'},
        ]},
    ]
    assert merge_tool_call_frames(frames) == [
        {"id": "call_a", "name": "list_dir", "arguments": '{"path": "x"}'},
        {"id": "call_b", "name": "read_local_file", "arguments": '{"path": "y"}'},
    ]


def test_execute_local_tool_reports_failures_as_results(tmp_path) -> None:
    """A missing file is a normal tool result, never an exception."""
    output = execute_local_tool("read_local_file", json.dumps({"path": "nope.txt"}))
    assert "本地执行失败" in output
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    assert execute_local_tool("read_local_file", {"path": str(tmp_path / "a.txt")}) == "hello"
    assert json.loads(execute_local_tool("list_dir", {"path": str(tmp_path)})) == ["a.txt"]


def _mixed_round_streams(workspace: str):
    """Round 1: one server call + two client calls in parallel.  Round 2: answer."""
    return [
        _sse_parallel_calls([
            ("workspace_stats", "call_srv", json.dumps({"root": workspace})),
            ("list_dir", "call_c1", json.dumps({"path": workspace})),
            ("read_local_file", "call_c2", json.dumps({"path": os.path.join(workspace, "README.md")})),
        ]),
        _sse_content("服务端统计 + 本地读取都拿到了"),
    ]


def test_example_non_stream_mixed_round(tmp_path, capsys) -> None:
    streams = _mixed_round_streams(str(tmp_path))
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"
            (tmp_path / "README.md").write_text("玲珑 Agent Service", encoding="utf-8")
            example.MODEL_ID = "test-model"
            example.WORKSPACE = str(tmp_path)

            register_side_tools(base)
            ok = run_non_stream(base, "统计服务端并读本地文件")
            capsys.readouterr()

            assert ok is True
            assert len(seen) == 2, "the loop must continue after the local results"

            # The session holds all three results: one from the service, two local.
            turns = _conversation(chats_dir, _session_id(chats_dir))["messages"]
            by_call = {t["tool_use_id"]: t for t in turns if t["role"] == "tool"}
            assert set(by_call) == {"call_srv", "call_c1", "call_c2"}
            assert "files" in by_call["call_srv"]["content"], "server tool ran on the service"
            assert by_call["call_c1"]["content"].count("README.md") == 1, (
                "the ad-hoc client tool ran locally")
            assert by_call["call_c2"]["content"] == "玲珑 Agent Service"

            # The ad-hoc session tool never became visible to the whole service.
            assert srv._runtime._tool_registry.get("list_dir") is None


def test_example_stream_mixed_round(tmp_path, capsys) -> None:
    streams = _mixed_round_streams(str(tmp_path))
    with _fake_model_server(streams) as (api_base, seen):
        with _running_server(tmp_path, api_base) as (srv, chats_dir):
            base = f"http://127.0.0.1:{srv.port}"
            (tmp_path / "README.md").write_text("玲珑 Agent Service", encoding="utf-8")
            example.MODEL_ID = "test-model"
            example.WORKSPACE = str(tmp_path)

            register_side_tools(base)
            ok = run_stream(base, "统计服务端并读本地文件")
            captured = capsys.readouterr()

            assert ok is True
            assert len(seen) == 2
            # The streaming client labelled both execution sides and ran its own.
            assert "tool call [服务端] workspace_stats" in captured.out
            assert "tool call [客户端] list_dir" in captured.out
            assert "read_local_file args=" in captured.out
            turns = _conversation(chats_dir, _session_id(chats_dir))["messages"]
            assert {t["tool_use_id"] for t in turns if t["role"] == "tool"} == {
                "call_srv", "call_c1", "call_c2",
            }
            # The follow-up request restated the session's tool set: the ad-hoc
            # client tool was restored from the session, the pre-registered
            # client tool and the server tool from tool_ids.
            sent_tools = [t["function"]["name"] for t in seen[-1].get("tools", [])]
            assert sorted(sent_tools) == ["list_dir", "read_local_file", "workspace_stats"]


def _session_id(chats_dir: str) -> str:
    names = sorted(
        d for d in os.listdir(chats_dir)
        if os.path.isdir(os.path.join(chats_dir, d))
    )
    assert names, "the example must have created a session"
    return names[0]

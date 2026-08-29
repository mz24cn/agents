import csv
import json
import os
from pathlib import Path

from runtime.execution_analysis import (
    EXECUTION_ANALYSIS_CACHE_FILENAME,
    EXECUTION_ANALYSIS_CSV_FILENAME,
    EXECUTION_ANALYSIS_JSON_FILENAME,
    analyze_session_execution,
)
from runtime.models import ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry


class _Runtime:
    def __init__(self):
        self._model_registry = ModelRegistry()
        self._tool_registry = ToolRegistry()
        self._model_registry.register(ModelConfig(
            model_id="real-model",
            model_name="model",
            api_base="http://example.invalid",
            labels=["fast-label"],
        ))
        self._tool_registry.register(ToolConfig(
            tool_id="real-tool",
            name="shell",
            description="test",
            tool_type="function",
            parameters={},
        ))


class _Agents:
    def get(self, agent_id):
        if agent_id == "agent-a":
            return {"agent_id": agent_id, "model_id": "fast-label"}
        return None


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _csv_rows(root: Path) -> list[dict]:
    with open(root / EXECUTION_ANALYSIS_CSV_FILENAME, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_analysis_writes_raw_csv_and_compact_aggregated_json(tmp_path):
    root = tmp_path / "session"
    _write(root / "conversation.json", {
        "meta": {"session_id": "session", "model_id": "fast-label"},
        "messages": [
            {"role": "user", "content": "go", "timestamp": "2026-01-01T00:00:00", "images": ["x"]},
            {
                "role": "assistant",
                "content": "",
                "timestamp": "2026-01-01T00:00:02",
                "agent_id": "agent-a",
                "tool_calls": [{"id": "call-1", "name": "shell", "arguments": "{}"}],
                "stat": {
                    "request_started_at": "2026-01-01T00:00:00.500000",
                    "completed_at": "2026-01-01T00:00:02",
                    "net_ms": 1500,
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "model_id": "fast-label",
                },
            },
            {
                "role": "tool",
                "content": "ok",
                "timestamp": "2026-01-01T00:00:03",
                "name": "shell",
                "tool_id": "real-tool",
                "tool_use_id": "call-1",
                "agent_id": "agent-a",
                "started_at": "2026-01-01T00:00:02",
            },
            {
                "role": "assistant",
                "content": "done",
                "timestamp": "2026-01-01T00:00:04",
                "agent_id": "agent-a",
                "stat": {
                    "request_started_at": "2026-01-01T00:00:03",
                    "completed_at": "2026-01-01T00:00:04",
                    "net_ms": 1000,
                    "prompt_tokens": 15,
                    "completion_tokens": 3,
                    "model_id": "fast-label",
                },
            },
        ],
    })
    _write(root / "talk_child" / "conversation.json", {
        "meta": {"session_id": "talk_child", "parent_session_id": "session", "model_id": "real-model"},
        "messages": [
            {"role": "user", "content": "sub", "timestamp": "2026-01-01T00:00:02.100000"},
            {
                "role": "assistant",
                "content": "sub done",
                "timestamp": "2026-01-01T00:00:02.900000",
                "agent_id": "agent-a",
                "stat": {
                    "request_started_at": "2026-01-01T00:00:02.100000",
                    "completed_at": "2026-01-01T00:00:02.900000",
                    "net_ms": 800,
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "model_id": "real-model",
                },
            },
        ],
    })

    result = analyze_session_execution(str(root), _Runtime(), _Agents())

    assert result == {
        "summary": {
            "total_execution_net_ms": 4000,
            "model_execution_total_ms": 3300,
            "tool_execution_total_ms": 1000,
        },
        "by_agent": [{
            "agent_id": "agent-a",
            "model_duration_ms": 3300,
            "tool_duration_ms": 1000,
            "total_duration_ms": 4300,
        }],
        "by_model": [
            {
                "model_id": "real-model",
                "model_label": "fast-label",
                "duration_ms": 2500,
                "calls": 2,
                "input_tokens": 26,
                "output_tokens": 10,
            },
            {
                "model_id": "real-model",
                "model_label": None,
                "duration_ms": 800,
                "calls": 1,
                "input_tokens": 5,
                "output_tokens": 2,
            },
        ],
        "by_tool": [{
            "tool_id": "real-tool",
            "tool_name": "shell",
            "duration_ms": 1000,
            "calls": 1,
        }],
    }

    rows = _csv_rows(root)
    assert [row["record_type"] for row in rows] == ["root_user", "model", "tool", "model", "model"]

    first_model = next(row for row in rows if row["record_type"] == "model")
    assert first_model["agent_id"] == "agent-a"
    assert first_model["model_id"] == "real-model"
    assert first_model["model_label"] == "fast-label"
    assert first_model["input_tokens"] == "11"
    assert first_model["output_tokens"] == "7"
    assert first_model["has_multimodal_input"] == "true"
    assert first_model["tool_name"] == "null"

    tool = next(row for row in rows if row["record_type"] == "tool")
    assert tool["tool_name"] == "shell"
    assert tool["tool_id"] == "real-tool"
    assert tool["tool_use_id"] == "call-1"
    assert tool["agent_id"] == "agent-a"
    assert tool["duration_ms"] == "1000.0"
    assert tool["model_id"] == "null"

    json_path = root / EXECUTION_ANALYSIS_JSON_FILENAME
    assert json.loads(json_path.read_text(encoding="utf-8")) == result
    assert "model_records" not in result
    assert "tool_records" not in result
    assert "conversation_turns" not in result


def test_model_csv_prefers_top_level_started_at(tmp_path):
    root = tmp_path / "assistant-started-session"
    _write(root / "conversation.json", {
        "meta": {"session_id": "assistant-started-session"},
        "messages": [
            {"role": "user", "timestamp": "2026-01-01T00:00:00"},
            {
                "role": "assistant",
                "content": "done",
                "started_at": "2026-01-01T00:00:02",
                "timestamp": "2026-01-01T00:00:03",
                "stat": {
                    "request_started_at": "2026-01-01T00:00:01",
                    "completed_at": "2026-01-01T00:00:03",
                    "net_ms": 1000,
                },
            },
        ],
    })

    result = analyze_session_execution(str(root), _Runtime(), _Agents())
    model = next(row for row in _csv_rows(root) if row["record_type"] == "model")

    assert model["started_at"] == "2026-01-01T00:00:02.000000"
    assert model["duration_ms"] == "1000.0"
    assert result["summary"]["model_execution_total_ms"] == 1000


def test_historical_tool_falls_back_to_assistant_timestamp_in_csv(tmp_path):
    root = tmp_path / "legacy-session"
    _write(root / "conversation.json", {
        "meta": {"session_id": "legacy-session"},
        "messages": [
            {
                "role": "assistant",
                "timestamp": "2026-01-01T00:00:02",
                "tool_calls": [{"id": "legacy-call", "name": "shell", "arguments": "{}"}],
            },
            {
                "role": "tool",
                "timestamp": "2026-01-01T00:00:05",
                "name": "shell",
                "tool_use_id": "legacy-call",
            },
        ],
    })

    result = analyze_session_execution(str(root), _Runtime(), _Agents())
    tool = next(row for row in _csv_rows(root) if row["record_type"] == "tool")

    assert tool["started_at"] == "2026-01-01T00:00:02.000000"
    assert tool["duration_ms"] == "3000.0"
    assert tool["agent_id"] == "null"
    assert result["summary"]["tool_execution_total_ms"] == 3000


def test_json_is_returned_directly_while_newer_than_root_conversation(tmp_path):
    root = tmp_path / "cached-session"
    conversation_path = root / "conversation.json"
    _write(conversation_path, {
        "meta": {"session_id": "cached-session"},
        "messages": [
            {"role": "user", "content": "first", "timestamp": "2026-01-01T00:00:00"},
            {
                "role": "assistant",
                "content": "done",
                "timestamp": "2026-01-01T00:00:01",
                "stat": {
                    "request_started_at": "2026-01-01T00:00:00",
                    "completed_at": "2026-01-01T00:00:01",
                    "net_ms": 1000,
                },
            },
        ],
    })

    first = analyze_session_execution(str(root), _Runtime(), _Agents())
    csv_path = root / EXECUTION_ANALYSIS_CSV_FILENAME
    json_path = root / EXECUTION_ANALYSIS_JSON_FILENAME

    assert EXECUTION_ANALYSIS_CACHE_FILENAME == EXECUTION_ANALYSIS_JSON_FILENAME
    assert csv_path.is_file()
    assert json_path.is_file()
    assert json_path.stat().st_mtime_ns > conversation_path.stat().st_mtime_ns

    cached = dict(first)
    cached["cache_marker"] = True
    json_path.write_text(json.dumps(cached), encoding="utf-8")
    json_mtime = max(json_path.stat().st_mtime_ns, conversation_path.stat().st_mtime_ns + 1_000_000)
    os.utime(json_path, ns=(json_mtime, json_mtime))
    csv_mtime = csv_path.stat().st_mtime_ns

    assert analyze_session_execution(str(root), _Runtime(), _Agents())["cache_marker"] is True
    assert csv_path.stat().st_mtime_ns == csv_mtime

    conversation_mtime = json_path.stat().st_mtime_ns + 1_000_000
    os.utime(conversation_path, ns=(conversation_mtime, conversation_mtime))
    refreshed = analyze_session_execution(str(root), _Runtime(), _Agents())

    assert "cache_marker" not in refreshed
    assert csv_path.stat().st_mtime_ns > csv_mtime
    assert json_path.stat().st_mtime_ns > conversation_path.stat().st_mtime_ns


def test_invalid_json_is_recomputed_through_csv(tmp_path):
    root = tmp_path / "invalid-cache-session"
    conversation_path = root / "conversation.json"
    _write(conversation_path, {
        "meta": {"session_id": "invalid-cache-session"},
        "messages": [],
    })
    json_path = root / EXECUTION_ANALYSIS_JSON_FILENAME
    json_path.write_text("not json", encoding="utf-8")
    json_mtime = conversation_path.stat().st_mtime_ns + 1_000_000
    os.utime(json_path, ns=(json_mtime, json_mtime))

    result = analyze_session_execution(str(root), _Runtime(), _Agents())

    assert result["summary"] == {
        "total_execution_net_ms": 0,
        "model_execution_total_ms": 0,
        "tool_execution_total_ms": 0,
    }
    assert (root / EXECUTION_ANALYSIS_CSV_FILENAME).is_file()
    assert json.loads(json_path.read_text(encoding="utf-8")) == result

"""Execution-process analysis for persisted conversations.

The analyzer has two explicit stages:

* ``execution_analysis.csv`` stores raw records extracted from the root and all
  recursively nested ``conversation.json`` files.
* ``execution_analysis.json`` stores only the aggregated result consumed by the
  UI/API.

The JSON result is reused while it is newer than the root conversation file.
On a cache miss the CSV is always regenerated, then read back to produce JSON.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


EXECUTION_ANALYSIS_CSV_FILENAME = "execution_analysis.csv"
EXECUTION_ANALYSIS_JSON_FILENAME = "execution_analysis.json"
# Backward-compatible name used by callers/tests that treated the JSON as a cache.
EXECUTION_ANALYSIS_CACHE_FILENAME = EXECUTION_ANALYSIS_JSON_FILENAME

_CSV_FIELDS = (
    "record_type",
    "session_id",
    "source",
    "message_index",
    "agent_id",
    "model_id",
    "model_label",
    "input_tokens",
    "output_tokens",
    "has_multimodal_input",
    "tool_name",
    "tool_id",
    "tool_use_id",
    "started_at",
    "completed_at",
    "duration_ms",
)


def _load_cached_analysis(root: Path) -> Optional[dict]:
    """Return JSON newer than the root conversation, if one is available."""
    conversation_path = root / "conversation.json"
    result_path = root / EXECUTION_ANALYSIS_JSON_FILENAME
    try:
        if not conversation_path.is_file() or not result_path.is_file():
            return None
        if result_path.stat().st_mtime_ns <= conversation_path.stat().st_mtime_ns:
            return None
        with open(result_path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    return cached if isinstance(cached, dict) else None


def _write_json_result(root: Path, result: dict) -> None:
    """Atomically persist the compact result JSON."""
    result_path = root / EXECUTION_ANALYSIS_JSON_FILENAME
    fd, temporary_path = tempfile.mkstemp(
        dir=str(root),
        prefix=f".{EXECUTION_ANALYSIS_JSON_FILENAME}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, result_path)

        # Preserve the strict "JSON newer than conversation.json" rule even on
        # filesystems with coarse timestamp resolution.
        conversation_path = root / "conversation.json"
        if conversation_path.is_file():
            conversation_mtime = conversation_path.stat().st_mtime_ns
            result_stat = result_path.stat()
            if result_stat.st_mtime_ns <= conversation_mtime:
                os.utime(
                    result_path,
                    ns=(result_stat.st_atime_ns, conversation_mtime + 1),
                )
    except BaseException:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def _csv_value(value: Any) -> Any:
    """Represent nullable CSV values explicitly instead of as empty strings."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_raw_csv(root: Path, records: list[dict]) -> Path:
    """Atomically write records extracted from conversation files."""
    csv_path = root / EXECUTION_ANALYSIS_CSV_FILENAME
    fd, temporary_path = tempfile.mkstemp(
        dir=str(root),
        prefix=f".{EXECUTION_ANALYSIS_CSV_FILENAME}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow({field: _csv_value(record.get(field)) for field in _CSV_FIELDS})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, csv_path)
    except BaseException:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    return csv_path


def _parse_timestamp(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str) or not value.strip() or value == "null":
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat(timespec="microseconds") if value is not None else None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, result)


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _nullable(value: Any) -> Optional[str]:
    if value is None or value == "" or value == "null":
        return None
    return str(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _resolve_model(runtime, recorded: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return (real model ID, label used in the conversation)."""
    if not recorded:
        return None, None
    registry = getattr(runtime, "_model_registry", None)
    config = registry.get(recorded) if registry is not None else None
    if config is None:
        return recorded, None
    real_id = config.model_id
    return real_id, recorded if recorded != real_id else None


def _resolve_tool(
    runtime,
    recorded_id: Optional[str],
    recorded_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    registry = getattr(runtime, "_tool_registry", None)
    config = None
    if registry is not None:
        if recorded_id:
            config = registry.get(recorded_id)
        if config is None and recorded_name:
            config = registry.get(recorded_name)
    if config is not None:
        return config.tool_id, config.name
    return recorded_id, recorded_name


def _agent_model_recording(agent_manager, agent_id: Optional[str]) -> Optional[str]:
    if not agent_id or agent_manager is None:
        return None
    agent = agent_manager.get(agent_id)
    return agent.get("model_id") if isinstance(agent, dict) else None


def _conversation_files(session_dir: str) -> list[Path]:
    root = Path(session_dir).resolve()
    files: list[Path] = []
    for path in root.rglob("conversation.json"):
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            files.append(resolved)
    files.sort(key=lambda item: (len(item.relative_to(root).parts), str(item)))
    return files


def _empty_record(record_type: str, session_id: str, source: str, message_index: int) -> dict:
    return {
        "record_type": record_type,
        "session_id": session_id,
        "source": source,
        "message_index": message_index,
    }


def _extract_raw_records(
    root: Path,
    files: list[Path],
    runtime,
    agent_manager=None,
) -> list[dict]:
    """Extract model, tool, and root user-turn records from conversations."""
    records: list[dict] = []

    for conv_path in files:
        source = str(conv_path.relative_to(root))
        try:
            with open(conv_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            # A bad child conversation must not prevent analysis of valid files.
            continue
        if not isinstance(data, dict):
            continue

        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        session_id = meta.get("session_id") or conv_path.parent.name
        default_model = meta.get("model_id")
        is_root = conv_path.parent == root
        seen_multimodal = False
        pending_calls: dict[str, list[dict]] = defaultdict(list)
        anonymous_calls: list[dict] = []

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if message.get("images") or message.get("audio"):
                seen_multimodal = True

            if is_root and role == "user":
                started = _parse_timestamp(message.get("timestamp"))
                if started is not None:
                    record = _empty_record("root_user", session_id, source, index)
                    record.update({
                        "started_at": _iso(started),
                        "completed_at": _iso(started),
                        "duration_ms": 0.0,
                    })
                    records.append(record)

            if role == "assistant":
                stat = message.get("stat") if isinstance(message.get("stat"), dict) else {}
                completed = (
                    _parse_timestamp(stat.get("completed_at"))
                    or _parse_timestamp(message.get("completed_at"))
                    or _parse_timestamp(message.get("timestamp"))
                )
                started = _parse_timestamp(stat.get("request_started_at"))
                duration_ms = _number(stat.get("net_ms"), -1.0)
                if duration_ms < 0:
                    duration_ms = _number(stat.get("total_ms"), -1.0)
                if duration_ms < 0 and started is not None and completed is not None:
                    duration_ms = max(0.0, (completed - started).total_seconds() * 1000)
                if duration_ms < 0:
                    duration_ms = 0.0
                if started is None and completed is not None and duration_ms:
                    started = completed - datetime.timedelta(milliseconds=duration_ms)
                if completed is None and started is not None:
                    completed = started + datetime.timedelta(milliseconds=duration_ms)

                agent_id = message.get("agent_id") or message.get("assistant_id")
                explicit_label = stat.get("model_label")
                recorded_model_id = stat.get("model_id")
                if explicit_label:
                    # Prefer resolving the recorded label through the registry.
                    # If that label is no longer registered, a separately
                    # persisted model_id remains the best real-ID fallback.
                    model_id, _ = _resolve_model(runtime, explicit_label)
                    if model_id == explicit_label and recorded_model_id and recorded_model_id != explicit_label:
                        model_id, _ = _resolve_model(runtime, recorded_model_id)
                    model_label = explicit_label
                else:
                    recorded_model = (
                        recorded_model_id
                        or _agent_model_recording(agent_manager, agent_id)
                        or default_model
                    )
                    model_id, model_label = _resolve_model(runtime, recorded_model)
                has_multimodal = bool(stat.get("has_multimodal_input", seen_multimodal))

                if stat or message.get("content") or message.get("thinking") or message.get("tool_calls"):
                    record = _empty_record("model", session_id, source, index)
                    record.update({
                        "agent_id": agent_id,
                        "model_id": model_id,
                        "model_label": model_label,
                        "input_tokens": _int(stat.get("prompt_tokens", stat.get("input_tokens"))),
                        "output_tokens": _int(stat.get("completion_tokens", stat.get("output_tokens"))),
                        "has_multimodal_input": has_multimodal,
                        "started_at": _iso(started),
                        "completed_at": _iso(completed),
                        "duration_ms": round(duration_ms, 3),
                    })
                    records.append(record)

                call_started = _parse_timestamp(message.get("timestamp")) or completed
                for call_index, call in enumerate(message.get("tool_calls") or []):
                    if not isinstance(call, dict):
                        continue
                    call_id = call.get("id") or call.get("tool_use_id")
                    pending = {
                        "call_id": str(call_id) if call_id is not None else None,
                        "name": call.get("name"),
                        "agent_id": agent_id,
                        "started": call_started,
                        "call_index": call_index,
                    }
                    if call_id is not None:
                        pending_calls[str(call_id)].append(pending)
                    else:
                        anonymous_calls.append(pending)

            elif role == "tool":
                call_id = message.get("tool_use_id") or message.get("id")
                pending = None
                if call_id is not None and pending_calls.get(str(call_id)):
                    pending = pending_calls[str(call_id)].pop(0)
                if pending is None:
                    tool_name = message.get("name")
                    for pending_index, candidate in enumerate(anonymous_calls):
                        if not tool_name or candidate.get("name") == tool_name:
                            pending = anonymous_calls.pop(pending_index)
                            break

                # New sessions carry the actual worker start. Historical data
                # falls back to the assistant tool-call message timestamp.
                started = _parse_timestamp(message.get("started_at")) or (pending or {}).get("started")
                completed = _parse_timestamp(message.get("timestamp"))
                if started is not None and completed is not None:
                    duration_ms = max(0.0, (completed - started).total_seconds() * 1000)
                else:
                    duration_ms = 0.0

                recorded_name = message.get("name") or (pending or {}).get("name")
                recorded_id = message.get("tool_id")
                tool_id, tool_name = _resolve_tool(runtime, recorded_id, recorded_name)
                record = _empty_record("tool", session_id, source, index)
                record.update({
                    "agent_id": message.get("agent_id") or (pending or {}).get("agent_id"),
                    "tool_name": tool_name,
                    "tool_id": tool_id,
                    "tool_use_id": (
                        str(call_id) if call_id is not None else (pending or {}).get("call_id")
                    ),
                    "started_at": _iso(started),
                    "completed_at": _iso(completed),
                    "duration_ms": round(duration_ms, 3),
                })
                records.append(record)

    return records


def _read_raw_csv(csv_path: Path) -> list[dict]:
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _aggregate_raw_csv(csv_path: Path) -> dict:
    """Read the intermediate CSV and produce only fields displayed by the UI."""
    rows = _read_raw_csv(csv_path)
    model_records: list[dict] = []
    tool_records: list[dict] = []
    root_user_starts: list[datetime.datetime] = []

    for row in rows:
        record_type = row.get("record_type")
        if record_type == "root_user":
            started = _parse_timestamp(row.get("started_at"))
            if started is not None:
                root_user_starts.append(started)
            continue
        if record_type not in {"model", "tool"}:
            continue

        record = {
            "agent_id": _nullable(row.get("agent_id")),
            "started_at": _nullable(row.get("started_at")),
            "completed_at": _nullable(row.get("completed_at")),
            "duration_ms": _number(row.get("duration_ms")),
        }
        if record_type == "model":
            record.update({
                "model_id": _nullable(row.get("model_id")),
                "model_label": _nullable(row.get("model_label")),
                "input_tokens": _int(row.get("input_tokens")),
                "output_tokens": _int(row.get("output_tokens")),
                "has_multimodal_input": _bool(row.get("has_multimodal_input")),
            })
            model_records.append(record)
        else:
            record.update({
                "tool_name": _nullable(row.get("tool_name")),
                "tool_id": _nullable(row.get("tool_id")),
            })
            tool_records.append(record)

    model_total = sum(item["duration_ms"] for item in model_records)
    tool_total = sum(item["duration_ms"] for item in tool_records)

    event_intervals: list[tuple[datetime.datetime, datetime.datetime]] = []
    for item in [*model_records, *tool_records]:
        started = _parse_timestamp(item.get("started_at"))
        completed = _parse_timestamp(item.get("completed_at"))
        if started is not None and completed is not None and completed >= started:
            event_intervals.append((started, completed))

    root_user_starts = sorted(set(root_user_starts))
    total_net_ms = 0.0
    for turn_index, start in enumerate(root_user_starts):
        next_start = (
            root_user_starts[turn_index + 1]
            if turn_index + 1 < len(root_user_starts)
            else None
        )
        relevant = [
            (event_start, event_end)
            for event_start, event_end in event_intervals
            if event_end >= start and (next_start is None or event_start < next_start)
        ]
        if relevant:
            end = max(event_end for _, event_end in relevant)
            if next_start is not None:
                end = min(end, next_start)
            total_net_ms += max(0.0, (end - start).total_seconds() * 1000)

    agents: dict[Optional[str], dict] = {}
    for record_type, records in (("model", model_records), ("tool", tool_records)):
        for item in records:
            key = item.get("agent_id")
            row = agents.setdefault(key, {
                "agent_id": key,
                "model_duration_ms": 0.0,
                "tool_duration_ms": 0.0,
                "total_duration_ms": 0.0,
            })
            duration = item["duration_ms"]
            row[f"{record_type}_duration_ms"] += duration
            row["total_duration_ms"] += duration

    models: dict[tuple[Optional[str], Optional[str]], dict] = {}
    for item in model_records:
        key = (item.get("model_id"), item.get("model_label"))
        row = models.setdefault(key, {
            "model_id": key[0],
            "model_label": key[1],
            "duration_ms": 0.0,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        })
        row["duration_ms"] += item["duration_ms"]
        row["calls"] += 1
        row["input_tokens"] += item["input_tokens"]
        row["output_tokens"] += item["output_tokens"]

    tools: dict[tuple[Optional[str], Optional[str]], dict] = {}
    for item in tool_records:
        key = (item.get("tool_id"), item.get("tool_name"))
        row = tools.setdefault(key, {
            "tool_id": key[0],
            "tool_name": key[1],
            "duration_ms": 0.0,
            "calls": 0,
        })
        row["duration_ms"] += item["duration_ms"]
        row["calls"] += 1

    def _rounded_rows(rows, duration_key: str) -> list[dict]:
        result = list(rows)
        for row in result:
            for key in (
                "duration_ms",
                "model_duration_ms",
                "tool_duration_ms",
                "total_duration_ms",
            ):
                if key in row:
                    row[key] = round(row[key], 3)
        result.sort(key=lambda row: (-row.get(duration_key, 0), str(row)))
        return result

    return {
        "summary": {
            "total_execution_net_ms": round(total_net_ms, 3),
            "model_execution_total_ms": round(model_total, 3),
            "tool_execution_total_ms": round(tool_total, 3),
        },
        "by_agent": _rounded_rows(agents.values(), "total_duration_ms"),
        "by_model": _rounded_rows(models.values(), "duration_ms"),
        "by_tool": _rounded_rows(tools.values(), "duration_ms"),
    }


def analyze_session_execution(session_dir: str, runtime, agent_manager=None) -> dict:
    """Analyze a root session and all recursively nested child conversations."""
    root = Path(session_dir).resolve()
    cached = _load_cached_analysis(root)
    if cached is not None:
        return cached

    files = _conversation_files(str(root))
    if not files:
        raise FileNotFoundError(f"No conversation.json found under: {root}")

    raw_records = _extract_raw_records(root, files, runtime, agent_manager)
    csv_path = _write_raw_csv(root, raw_records)
    result = _aggregate_raw_csv(csv_path)
    _write_json_result(root, result)
    return result

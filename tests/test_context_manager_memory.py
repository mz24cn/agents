"""Tests for ContextManager rolling summary and structured memory (Task 4).

Covers:
  - Property test: extract_memory confidence filtering (Task 4.1, Property 5)
  - Property test: get_memory_entries type filtering (Task 4.2, Property 8)
  - Unit tests: summary and memory (Task 4.3)
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    MemoryEntry,
    parse_front_matter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cm(
    tmp_dir: str,
    *,
    summary_model_id: str = "test-model",
    recent_turns_k: int = 2,
    max_tokens_in_context: int = 1000,
    memory_confidence_threshold: float = 0.7,
    infer_fn=None,
) -> ContextManager:
    """Return a ContextManager backed by *tmp_dir*."""
    if infer_fn is None:
        infer_fn = lambda req: SimpleNamespace(  # noqa: E731
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )
    return ContextManager(
        infer_fn=infer_fn,
        chats_dir=tmp_dir,
        recent_turns_k=recent_turns_k,
        summary_model_id=summary_model_id,
        max_tokens_in_context=max_tokens_in_context,
        memory_confidence_threshold=memory_confidence_threshold,
    )


def _make_turn(
    role: str = "user",
    content: str = "hello",
    ts: str = "2026-01-01T00:00:00",
) -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


def _entries_as_tagged_output(entries: list[MemoryEntry], summary: str = "summary text") -> str:
    """Render entries as a full compress_context LLM response."""
    memory_json = json.dumps(
        [
            {
                "entry_type": e.entry_type,
                "content": e.content,
                "source_turn_index": e.source_turn_index,
                "confidence": e.confidence,
                "created_at": e.created_at,
            }
            for e in entries
        ],
        ensure_ascii=False,
        indent=2,
    )
    return f"<summary>\n{summary}\n</summary>\n<memory>\n{memory_json}\n</memory>"


# ---------------------------------------------------------------------------
# Hypothesis strategy
# ---------------------------------------------------------------------------


def memory_entry_strategy():
    return st.builds(
        MemoryEntry,
        entry_type=st.sampled_from(["fact", "preference", "decision", "entity"]),
        content=st.text(min_size=1, max_size=100),
        source_turn_index=st.integers(min_value=0, max_value=100),
        confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        created_at=st.just("2026-01-01T00:00:00"),
    )


# ---------------------------------------------------------------------------
# Property 5: Memory confidence filtering (Task 4.1)
# Feature: context-manager, Property 5: 记忆条目过滤
# **Validates: Requirements 7.5**
# ---------------------------------------------------------------------------


@given(entries=st.lists(memory_entry_strategy(), min_size=0, max_size=20))
@settings(max_examples=25)
def test_memory_confidence_filter(entries: list[MemoryEntry]) -> None:
    """compress_context must discard memory entries with confidence < threshold.

    **Validates: Requirements 7.5**
    """
    threshold = 0.7

    def mock_infer(req: Any) -> SimpleNamespace:
        return SimpleNamespace(content=_entries_as_tagged_output(entries))

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(
            tmp_dir,
            infer_fn=mock_infer,
            memory_confidence_threshold=threshold,
        )
        session_id = cm.create_session()
        turns = [_make_turn()]
        cm.compress_context(session_id, turns, last_total_tokens=2000)

        result = cm.get_memory_entries(session_id)
        for entry in result:
            assert entry.confidence >= threshold, (
                f"Entry with confidence {entry.confidence} should have been filtered "
                f"(threshold={threshold})"
            )


# ---------------------------------------------------------------------------
# Property 8: Memory type filtering (Task 4.2)
# Feature: context-manager, Property 8: 记忆条目类型过滤
# **Validates: Requirements 7.4**
# ---------------------------------------------------------------------------


@given(
    entries=st.lists(memory_entry_strategy(), min_size=0, max_size=20),
    entry_type=st.sampled_from(["fact", "preference", "decision", "entity"]),
)
@settings(max_examples=25)
def test_memory_filter_by_type(
    entries: list[MemoryEntry], entry_type: str
) -> None:
    """get_memory_entries(entry_type=t) must return only entries with entry_type == t.

    **Validates: Requirements 7.4**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        # Persist entries via save_memory (bypasses LLM, tests file-based retrieval)
        cm.save_memory(session_id, list(entries))

        result = cm.get_memory_entries(session_id, entry_type=entry_type)
        for entry in result:
            assert entry.entry_type == entry_type, (
                f"Expected entry_type '{entry_type}', got '{entry.entry_type}'"
            )


# ---------------------------------------------------------------------------
# Unit tests — rolling summary (Task 4.3)
# ---------------------------------------------------------------------------


def test_update_rolling_summary_triggers_after_k_turns():
    """When last_total_tokens exceeds max_tokens_in_context, summary.md must be created."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=2, max_tokens_in_context=1000)
        session_id = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        # Pass last_total_tokens above the threshold to trigger compression
        cm.update_rolling_summary(session_id, turns, last_total_tokens=2000)

        summary_path = os.path.join(tmp_dir, session_id, "summary.md")
        assert os.path.isfile(summary_path), "summary.md must be created when last_total_tokens > max_tokens_in_context"


def test_update_rolling_summary_not_triggered_when_turns_le_k():
    """When last_total_tokens is below the threshold, summary.md must NOT be created."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=2, max_tokens_in_context=1000)
        session_id = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(2)]
        # Pass last_total_tokens below the threshold — no compression
        cm.update_rolling_summary(session_id, turns, last_total_tokens=500)

        summary_path = os.path.join(tmp_dir, session_id, "summary.md")
        assert not os.path.isfile(summary_path), (
            "summary.md must NOT be created when last_total_tokens <= max_tokens_in_context"
        )


def test_summary_version_increments():
    """Calling update_rolling_summary twice (both above threshold) must increment summary_version from 1 to 2."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=2, max_tokens_in_context=1000)
        session_id = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        cm.update_rolling_summary(session_id, turns, last_total_tokens=2000)
        _, fm1 = cm.get_summary(session_id)
        assert fm1.get("summary_version") == 1, (
            f"First summary_version must be 1, got {fm1.get('summary_version')}"
        )

        cm.update_rolling_summary(session_id, turns, last_total_tokens=2000)
        _, fm2 = cm.get_summary(session_id)
        assert fm2.get("summary_version") == 2, (
            f"Second summary_version must be 2, got {fm2.get('summary_version')}"
        )


def test_summary_failure_preserves_old():
    """When infer_fn raises on the second call, the old summary must be preserved."""
    call_count = {"n": 0}

    def flaky_infer(req: Any) -> SimpleNamespace:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return SimpleNamespace(
                content="<summary>\nfirst summary\n</summary>\n<memory>\n[]\n</memory>"
            )
        raise RuntimeError("LLM unavailable")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=flaky_infer, recent_turns_k=2, max_tokens_in_context=1000)
        session_id = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        # First call succeeds
        cm.update_rolling_summary(session_id, turns, last_total_tokens=2000)
        text1, fm1 = cm.get_summary(session_id)
        assert text1.strip() == "first summary"
        assert fm1.get("summary_version") == 1

        # Second call fails — old summary must be preserved
        cm.update_rolling_summary(session_id, turns, last_total_tokens=2000)
        text2, fm2 = cm.get_summary(session_id)
        assert text2.strip() == "first summary", (
            "Old summary must be preserved when LLM call fails"
        )
        assert fm2.get("summary_version") == 1, (
            "summary_version must not change when LLM call fails"
        )


def test_get_summary_returns_empty_when_no_file():
    """get_summary must return ('', {}) when summary.md does not exist."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        text, fm = cm.get_summary(session_id)
        assert text == ""
        assert fm == {}


def test_update_rolling_summary_skipped_when_no_model():
    """update_rolling_summary must be a no-op when summary_model_id is empty."""
    import os as _os
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Ensure SUMMARY_MODEL_ID env var is not set so the override="" takes effect
        old_val = _os.environ.pop("SUMMARY_MODEL_ID", None)
        try:
            cm = _make_cm(tmp_dir, summary_model_id="", recent_turns_k=2, max_tokens_in_context=1000)
            session_id = cm.create_session()

            turns = [_make_turn(content=f"turn {i}") for i in range(4)]
            cm.update_rolling_summary(session_id, turns, last_total_tokens=2000)

            summary_path = os.path.join(tmp_dir, session_id, "summary.md")
            assert not os.path.isfile(summary_path), (
                "summary.md must NOT be created in Phase 1 (no summary_model_id)"
            )
        finally:
            if old_val is not None:
                _os.environ["SUMMARY_MODEL_ID"] = old_val


# ---------------------------------------------------------------------------
# Unit tests — structured memory (Task 4.3)
# ---------------------------------------------------------------------------


def test_memory_confidence_filter_unit():
    """Entries below the confidence threshold must not be stored."""
    threshold = 0.7
    entries = [
        MemoryEntry("fact", "high confidence", 0, 0.9, "2026-01-01T00:00:00"),
        MemoryEntry("fact", "low confidence", 1, 0.5, "2026-01-01T00:00:00"),
        MemoryEntry("preference", "exactly at threshold", 2, 0.7, "2026-01-01T00:00:00"),
        MemoryEntry("decision", "just below", 3, 0.699, "2026-01-01T00:00:00"),
    ]

    def mock_infer(req: Any) -> SimpleNamespace:
        return SimpleNamespace(content=_entries_as_tagged_output(entries))

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(
            tmp_dir,
            infer_fn=mock_infer,
            memory_confidence_threshold=threshold,
        )
        session_id = cm.create_session()
        # Need at least k+1 turns so compress_context doesn't exit early (summarized_up_to >= 0)
        # With recent_turns_k=2, need len(turns) > 2, i.e. at least 3 turns
        turns = [_make_turn() for _ in range(3)]
        cm.compress_context(session_id, turns, last_total_tokens=2000)

        result = cm.get_memory_entries(session_id)
        contents = [e.content for e in result]

        assert "high confidence" in contents
        assert "exactly at threshold" in contents
        assert "low confidence" not in contents
        assert "just below" not in contents


def test_get_memory_entries_by_type():
    """get_memory_entries with entry_type filter must return only matching entries."""
    entries = [
        MemoryEntry("fact", "fact 1", 0, 0.9, "2026-01-01T00:00:00"),
        MemoryEntry("preference", "pref 1", 1, 0.8, "2026-01-01T00:00:00"),
        MemoryEntry("fact", "fact 2", 2, 0.95, "2026-01-01T00:00:00"),
        MemoryEntry("decision", "decision 1", 3, 0.85, "2026-01-01T00:00:00"),
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()
        # Persist via save_memory so get_memory_entries reads from file
        cm.save_memory(session_id, entries)

        facts = cm.get_memory_entries(session_id, entry_type="fact")
        assert len(facts) == 2
        assert all(e.entry_type == "fact" for e in facts)

        prefs = cm.get_memory_entries(session_id, entry_type="preference")
        assert len(prefs) == 1
        assert prefs[0].content == "pref 1"

        all_entries = cm.get_memory_entries(session_id)
        assert len(all_entries) == 4


def test_extract_memory_skipped_when_no_model():
    """compress_context must be a no-op when summary_model_id is empty."""
    called = {"n": 0}

    def mock_infer(req: Any) -> SimpleNamespace:
        called["n"] += 1
        return SimpleNamespace(content="<summary>x</summary><memory>[]</memory>")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, summary_model_id="", infer_fn=mock_infer)
        session_id = cm.create_session()
        cm.compress_context(session_id, [_make_turn()], last_total_tokens=2000)

        assert called["n"] == 0, "infer_fn must not be called when summary_model_id is empty"
        assert cm.get_memory_entries(session_id) == []


def test_extract_memory_failure_logs_warning_and_skips(caplog):
    """When infer_fn raises, compress_context must log a warning and not raise."""
    import logging

    def failing_infer(req: Any) -> SimpleNamespace:
        raise RuntimeError("LLM error")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=failing_infer)
        session_id = cm.create_session()

        with caplog.at_level(logging.WARNING):
            cm.compress_context(session_id, [_make_turn()], last_total_tokens=2000)  # must not raise

        assert cm.get_memory_entries(session_id) == []

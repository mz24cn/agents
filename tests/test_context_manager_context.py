"""Tests for ContextManager context assembly and observability (Task 5).

Covers:
  - Property test: assemble_context ordering invariant (Task 5.1, Property 3)
  - Property test: token budget constraint (Task 5.2, Property 4)
  - Property test: introspect consistency (Task 5.3, Property 6)
  - Unit tests: ordering, token budget, introspect (Task 5.4)
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    MemoryEntry,
    IntrospectionSnapshot,
    estimate_tokens,
    serialize_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cm(
    tmp_dir: str,
    *,
    recent_turns_k: int = 3,
    summary_model_id: str = "",
    memory_confidence_threshold: float = 0.7,
) -> ContextManager:
    """Return a ContextManager backed by *tmp_dir*."""
    return ContextManager(
        infer_fn=lambda req: SimpleNamespace(content=""),
        chats_dir=tmp_dir,
        recent_turns_k=recent_turns_k,
        summary_model_id=summary_model_id,
        memory_confidence_threshold=memory_confidence_threshold,
    )


def _make_turn(
    role: str = "user",
    content: str = "hello",
    ts: str = "2026-01-01T00:00:00",
) -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


def _write_summary(
    cm: ContextManager,
    session_id: str,
    summary_text: str,
    summary_version: int = 1,
    summarized_up_to_turn: int = 0,
) -> None:
    """Write a summary.md directly into the session directory."""
    fm = {
        "session_id": session_id,
        "summary_version": summary_version,
        "summarized_up_to_turn": summarized_up_to_turn,
        "updated_at": "2026-01-01T00:00:00",
    }
    text = serialize_summary(fm, summary_text)
    summary_path = os.path.join(cm._chats_dir, session_id, "summary.md")
    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


def conversation_turn_strategy():
    return st.builds(
        ConversationTurn,
        role=st.sampled_from(["user", "assistant"]),
        content=st.text(min_size=1, max_size=100),
        timestamp=st.just("2026-01-01T00:00:00"),
    )


def memory_entry_strategy_high_confidence():
    return st.builds(
        MemoryEntry,
        entry_type=st.sampled_from(["fact", "preference", "decision", "entity"]),
        content=st.text(min_size=1, max_size=100),
        source_turn_index=st.integers(min_value=0, max_value=100),
        confidence=st.floats(min_value=0.7, max_value=1.0, allow_nan=False),
        created_at=st.just("2026-01-01T00:00:00"),
    )


def session_state_strategy():
    """Strategy that builds a dict describing a full session state.

    Returns a strategy producing dicts with:
    - "turns": list of ConversationTurn (0-10 items)
    - "summary": optional str (None or short text)
    - "summary_version": int (0 if no summary)
    - "summarized_up_to_turn": int
    - "memory_entries": list of MemoryEntry (0-10 items, all confidence >= 0.7)
    """
    return st.fixed_dictionaries(
        {
            "turns": st.lists(
                conversation_turn_strategy(), min_size=0, max_size=10
            ),
            "summary": st.one_of(st.none(), st.text(min_size=1, max_size=200)),
            "summary_version": st.integers(min_value=0, max_value=5),
            "memory_entries": st.lists(
                memory_entry_strategy_high_confidence(), min_size=0, max_size=10
            ),
        }
    )


def _build_cm_from_state(
    tmp_dir: str, state: dict, recent_turns_k: int = 3
) -> tuple[ContextManager, str]:
    """Create a ContextManager and populate it from *state*.

    Returns ``(cm, session_id)``.
    """
    cm = _make_cm(tmp_dir, recent_turns_k=recent_turns_k)
    session_id = cm.create_session()

    # Save turns
    if state["turns"]:
        cm.save_conversation(session_id, state["turns"])

    # Write summary if present
    if state["summary"] is not None:
        n_turns = len(state["turns"])
        summarized_up_to = max(0, n_turns - 1) if n_turns > 0 else 0
        _write_summary(
            cm,
            session_id,
            state["summary"],
            summary_version=max(1, state["summary_version"]),
            summarized_up_to_turn=summarized_up_to,
        )

    # Set memory entries directly
    cm._memory_store[session_id] = list(state["memory_entries"])

    return cm, session_id


# ---------------------------------------------------------------------------
# Property 3: Context assembly ordering invariant (Task 5.1)
# Feature: context-manager, Property 3: 上下文组装顺序不变式
# **Validates: Requirements 8.2**
# ---------------------------------------------------------------------------


@given(state=session_state_strategy())
@settings(max_examples=25)
def test_assemble_context_ordering(state: dict) -> None:
    """assemble_context must return messages in summary → memory → turns → new order.

    **Validates: Requirements 8.2**
    """
    new_messages = [{"role": "user", "content": "new question"}]

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm, session_id = _build_cm_from_state(tmp_dir, state)
        result = cm.assemble_context(session_id, new_messages)

    # Classify each message
    summary_indices = []
    memory_indices = []
    turn_indices = []
    new_indices = []

    for i, msg in enumerate(result):
        content = msg.get("content", "")
        role = msg.get("role", "")
        if role == "system" and content.startswith("## Summary"):
            summary_indices.append(i)
        elif role == "system" and content.startswith("## Memory"):
            memory_indices.append(i)
        elif msg in new_messages:
            new_indices.append(i)
        else:
            turn_indices.append(i)

    # Verify ordering: summary before memory
    if summary_indices and memory_indices:
        assert max(summary_indices) < min(memory_indices), (
            "Summary messages must appear before memory messages"
        )

    # Verify ordering: memory before turns
    if memory_indices and turn_indices:
        assert max(memory_indices) < min(turn_indices), (
            "Memory messages must appear before turn messages"
        )

    # Verify ordering: turns before new_messages
    if turn_indices and new_indices:
        assert max(turn_indices) < min(new_indices), (
            "Turn messages must appear before new_messages"
        )

    # Verify new_messages are at the end
    if new_indices:
        assert new_indices == list(range(len(result) - len(new_messages), len(result))), (
            "new_messages must be at the end of the assembled context"
        )


# ---------------------------------------------------------------------------
# Property 4: Token budget constraint (Task 5.2)
# Feature: context-manager, Property 4: Token 预算约束
# **Validates: Requirements 8.3, 8.4**
# ---------------------------------------------------------------------------


@given(
    state=session_state_strategy(),
    budget=st.integers(min_value=1, max_value=100000),
)
@settings(max_examples=25)
def test_token_budget_respected(state: dict, budget: int) -> None:
    """assemble_context with token_budget must not exceed the budget when the
    budget is large enough to hold the irreducible turn messages.

    The spec (Requirements 8.3, 8.4) defines truncation only for structured
    memory entries (oldest first) and then the rolling summary.  Conversation
    turns are not truncated.  Therefore the budget constraint is only
    guaranteed when the budget >= tokens consumed by turns alone.

    **Validates: Requirements 8.3, 8.4**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm, session_id = _build_cm_from_state(tmp_dir, state)

        # Compute the irreducible token cost: turns + new_messages only
        # (memory and summary can be removed, but turns cannot per the spec)
        try:
            turns = cm.load_conversation(session_id)
        except (FileNotFoundError, ValueError):
            turns = []
        k = cm._recent_turns_k
        recent_turns = turns[-k:] if len(turns) > k else turns
        turn_msgs = [{"role": t.role, "content": t.content} for t in recent_turns]
        irreducible_tokens = sum(estimate_tokens(str(m)) for m in turn_msgs)

        # Only assert the budget constraint when the budget can actually be met
        if budget < irreducible_tokens:
            return  # spec doesn't define turn truncation; skip this case

        result = cm.assemble_context(session_id, [], token_budget=budget)

    total_tokens = sum(estimate_tokens(str(msg)) for msg in result)
    assert total_tokens <= budget, (
        f"Assembled context ({total_tokens} tokens) exceeds budget ({budget} tokens)"
    )


# ---------------------------------------------------------------------------
# Property 6: Introspect consistency (Task 5.3)
# Feature: context-manager, Property 6: 可观测性快照一致性
# **Validates: Requirements 9.2, 9.5**
# ---------------------------------------------------------------------------


@given(state=session_state_strategy())
@settings(max_examples=25)
def test_introspect_consistency(state: dict) -> None:
    """introspect snapshot must satisfy total_turns == summarized_turns + recent_window_size
    and memory_entry_count == sum(memory_entries_by_type.values()).

    **Validates: Requirements 9.2, 9.5**
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm, session_id = _build_cm_from_state(tmp_dir, state)
        snapshot = cm.introspect(session_id)

    assert snapshot.total_turns == snapshot.summarized_turns + snapshot.recent_window_size, (
        f"total_turns ({snapshot.total_turns}) != "
        f"summarized_turns ({snapshot.summarized_turns}) + "
        f"recent_window_size ({snapshot.recent_window_size})"
    )

    assert snapshot.memory_entry_count == sum(snapshot.memory_entries_by_type.values()), (
        f"memory_entry_count ({snapshot.memory_entry_count}) != "
        f"sum(memory_entries_by_type.values()) "
        f"({sum(snapshot.memory_entries_by_type.values())})"
    )


# ---------------------------------------------------------------------------
# Unit tests (Task 5.4)
# ---------------------------------------------------------------------------


def test_assemble_context_ordering_unit():
    """assemble_context must return summary → memory → turns → new_messages."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=5)
        session_id = cm.create_session()

        # Save 3 turns
        turns = [
            _make_turn("user", "turn 0"),
            _make_turn("assistant", "turn 1"),
            _make_turn("user", "turn 2"),
        ]
        cm.save_conversation(session_id, turns)

        # Write a summary
        _write_summary(cm, session_id, "This is the summary.", summarized_up_to_turn=0)

        # Add memory entries
        cm._memory_store[session_id] = [
            MemoryEntry("fact", "user likes Python", 0, 0.9, "2026-01-01T00:00:00"),
            MemoryEntry("decision", "use async", 1, 0.85, "2026-01-01T00:00:00"),
        ]

        new_msgs = [{"role": "user", "content": "new question"}]
        result = cm.assemble_context(session_id, new_msgs)

        # First message: summary
        assert result[0]["role"] == "system"
        assert result[0]["content"].startswith("## Summary")

        # Next two: memory
        assert result[1]["role"] == "system"
        assert result[1]["content"].startswith("## Memory")
        assert result[2]["role"] == "system"
        assert result[2]["content"].startswith("## Memory")

        # Then turns
        assert result[3]["role"] == "user"
        assert result[3]["content"] == "turn 0"
        assert result[4]["role"] == "assistant"
        assert result[4]["content"] == "turn 1"
        assert result[5]["role"] == "user"
        assert result[5]["content"] == "turn 2"

        # Last: new_messages
        assert result[-1] == new_msgs[0]


def test_assemble_context_empty_session_returns_new_messages():
    """assemble_context with empty session_id must return new_messages as-is."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        new_msgs = [{"role": "user", "content": "hello"}]

        result = cm.assemble_context("", new_msgs)
        assert result == new_msgs


def test_assemble_context_nonexistent_session_returns_new_messages():
    """assemble_context with non-existent session must return new_messages as-is."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        new_msgs = [{"role": "user", "content": "hello"}]

        result = cm.assemble_context("9999-01-01_00-00-00", new_msgs)
        assert result == new_msgs


def test_assemble_context_token_budget_truncates_memory_first():
    """When over budget, memory entries must be truncated before the summary."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=5)
        session_id = cm.create_session()

        # Write a short summary
        _write_summary(cm, session_id, "Short summary.", summarized_up_to_turn=0)

        # Add many memory entries with substantial content
        cm._memory_store[session_id] = [
            MemoryEntry("fact", "x" * 100, i, 0.9, "2026-01-01T00:00:00")
            for i in range(10)
        ]

        # Compute full token count
        full_result = cm.assemble_context(session_id, [])
        full_tokens = sum(estimate_tokens(str(m)) for m in full_result)

        # Set budget to half of full tokens (forces truncation)
        budget = max(1, full_tokens // 2)
        result = cm.assemble_context(session_id, [], token_budget=budget)

        total_tokens = sum(estimate_tokens(str(m)) for m in result)
        assert total_tokens <= budget, (
            f"Result ({total_tokens} tokens) exceeds budget ({budget} tokens)"
        )

        # Summary should still be present (memory truncated first)
        system_msgs = [m for m in result if m.get("role") == "system"]
        summary_msgs = [m for m in system_msgs if m["content"].startswith("## Summary")]
        memory_msgs = [m for m in system_msgs if m["content"].startswith("## Memory")]

        # If summary fits, it should be present; memory should be reduced
        if summary_msgs:
            assert len(memory_msgs) < 10, "Some memory entries should have been truncated"


def test_assemble_context_token_budget_removes_summary_when_needed():
    """When memory is exhausted and still over budget, summary must be removed."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=5)
        session_id = cm.create_session()

        # Write a long summary
        _write_summary(cm, session_id, "S" * 200, summarized_up_to_turn=0)

        # No memory entries
        cm._memory_store[session_id] = []

        # Budget of 1 token — forces summary removal
        result = cm.assemble_context(session_id, [], token_budget=1)

        system_msgs = [m for m in result if m.get("role") == "system"]
        summary_msgs = [m for m in system_msgs if m["content"].startswith("## Summary")]
        assert len(summary_msgs) == 0, "Summary must be removed when budget is too tight"


def test_assemble_context_zero_budget_means_no_limit():
    """token_budget <= 0 must be treated as no limit."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=5)
        session_id = cm.create_session()

        _write_summary(cm, session_id, "Summary text.", summarized_up_to_turn=0)
        cm._memory_store[session_id] = [
            MemoryEntry("fact", "some fact", 0, 0.9, "2026-01-01T00:00:00"),
        ]

        result_no_budget = cm.assemble_context(session_id, [])
        result_zero_budget = cm.assemble_context(session_id, [], token_budget=0)
        result_neg_budget = cm.assemble_context(session_id, [], token_budget=-10)

        assert result_no_budget == result_zero_budget
        assert result_no_budget == result_neg_budget


def test_introspect_consistency_unit():
    """introspect snapshot invariants must hold for a concrete session."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=3)
        session_id = cm.create_session()

        # 5 turns, summary covers first 2 (summarized_up_to_turn=1)
        turns = [_make_turn(content=f"turn {i}") for i in range(5)]
        cm.save_conversation(session_id, turns)
        _write_summary(
            cm, session_id, "Summary of turns 0-1.",
            summary_version=1, summarized_up_to_turn=1
        )

        # 4 memory entries: 3 facts, 1 decision
        cm._memory_store[session_id] = [
            MemoryEntry("fact", "fact 1", 0, 0.9, "2026-01-01T00:00:00"),
            MemoryEntry("fact", "fact 2", 1, 0.85, "2026-01-01T00:00:00"),
            MemoryEntry("fact", "fact 3", 2, 0.8, "2026-01-01T00:00:00"),
            MemoryEntry("decision", "decision 1", 3, 0.95, "2026-01-01T00:00:00"),
        ]

        snapshot = cm.introspect(session_id)

        # Invariant 1
        assert snapshot.total_turns == snapshot.summarized_turns + snapshot.recent_window_size, (
            f"total_turns ({snapshot.total_turns}) != "
            f"summarized_turns ({snapshot.summarized_turns}) + "
            f"recent_window_size ({snapshot.recent_window_size})"
        )

        # Invariant 2
        assert snapshot.memory_entry_count == sum(snapshot.memory_entries_by_type.values()), (
            "memory_entry_count must equal sum of memory_entries_by_type values"
        )

        # Concrete checks
        assert snapshot.total_turns == 5
        assert snapshot.memory_entry_count == 4
        assert snapshot.memory_entries_by_type.get("fact", 0) == 3
        assert snapshot.memory_entries_by_type.get("decision", 0) == 1
        assert snapshot.summary_version == 1
        assert snapshot.token_budget is None


def test_introspect_no_summary_no_turns():
    """introspect on a fresh session must return zeros for turn/summary fields."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        session_id = cm.create_session()

        snapshot = cm.introspect(session_id)

        assert snapshot.total_turns == 0
        assert snapshot.summarized_turns == 0
        assert snapshot.recent_window_size == 0
        assert snapshot.memory_entry_count == 0
        assert snapshot.memory_entries_by_type == {}
        assert snapshot.summary_version == 0
        assert snapshot.total_turns == snapshot.summarized_turns + snapshot.recent_window_size

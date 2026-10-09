"""Tests for the bounded/incremental context compression (compress_context).

Regression tests for the failure mode where a very long session made the
compression prompt exceed the summary model's context window (HTTP 400
"exceeds the available context size"), and the failed call's INPUT prompt was
then parsed as model output — persisting the prompt's own <summary>/<memory>
format example (template placeholders) into summary.md / memory.md.

Covers:
  - incremental compression (only the delta since the last valid summary is
    sent to the LLM; a call without new turns does not call the LLM)
  - failed LLM results (success=False) are never persisted
  - placeholder echoes of the prompt format are never persisted
  - the FULL delta is sent without any artificial prompt budget (only
    pathological single turns are capped per-turn)
  - context-overflow retry: the first attempt is the full delta, the retry
    installs a budget halved from the natural prompt size (oldest turns
    dropped)
  - empty-output retry: an accepted request whose answer is empty is either
    resent (no completion tokens used) or shrunk like an overflow (completion
    tokens were burned on thinking, e.g. the prompt nearly fills the
    provider's total context+output window)
  - failure backoff (no hot-loop re-sends)
  - assemble_context ignores placeholder summaries/memory without re-injecting
    the full history
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from runtime.context_manager import (
    ContextManager,
    ConversationTurn,
    MemoryEntry,
    _estimate_tokens_fast,
    serialize_memory,
    serialize_summary,
)
from runtime.models import ModelConfig
from runtime.registry import ModelRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cm(
    tmp_dir: str,
    *,
    infer_fn=None,
    recent_turns_k: int = 2,
    max_tokens_in_context: int = 1000,
) -> ContextManager:
    if infer_fn is None:
        infer_fn = lambda req: SimpleNamespace(  # noqa: E731
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )
    return ContextManager(
        infer_fn=infer_fn,
        chats_dir=tmp_dir,
        recent_turns_k=recent_turns_k,
        summary_model_id="test-model",
        max_tokens_in_context=max_tokens_in_context,
    )


def _make_turn(role: str = "user", content: str = "hello", ts: str = "2026-01-01T00:00:00") -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


def _capturing_infer(store: dict) -> Callable:
    """infer_fn that records every prompt and returns a valid tagged output."""

    def infer(req: Any) -> SimpleNamespace:
        store["calls"] = store.get("calls", 0) + 1
        store.setdefault("prompts", []).append(req.messages[0].content)
        return SimpleNamespace(
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )

    return infer


def _failed_result(req: Any, error: str) -> SimpleNamespace:
    """Mimic runtime.infer on HTTP failure: success=False and messages carry
    the INPUT (whose last entry is the very prompt sent)."""
    return SimpleNamespace(
        success=False,
        messages=list(req.messages),
        error=error,
        error_code="400",
    )


def _write_placeholder_summary(cm: ContextManager, session_id: str, up_to: int) -> None:
    text = serialize_summary(
        {
            "session_id": session_id,
            "summary_version": 3,
            "summarized_up_to_turn": up_to,
            "updated_at": "2026-01-01T00:00:00",
        },
        "(concise summary prose, typically 2\u20135 paragraphs)",
    )
    cm._atomic_write(cm._summary_path(session_id), text)


# ---------------------------------------------------------------------------
# Incremental compression
# ---------------------------------------------------------------------------


def test_compression_sends_only_delta_since_last_summary() -> None:
    """The second compression must send only the NEW turns, not the full
    history (the previous summary already covers the older turns)."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        prompt1 = store["prompts"][0]
        assert "Turn 0 " in prompt1 and "Turn 1 " in prompt1

        # Add two more turns; the delta is now turns 2..3.
        turns.extend(_make_turn(content=f"turn {i}") for i in range(4, 6))
        cm.compress_context(sid, turns, last_total_tokens=2000)

        prompt2 = store["prompts"][1]
        assert "Turn 2 " in prompt2 and "Turn 3 " in prompt2
        # The turns already covered by the previous summary are NOT resent.
        assert "Turn 0 " not in prompt2
        assert "Turn 1 " not in prompt2
        # The previous summary is included for the merge.
        assert "## Previous summary" in prompt2
        assert "summary text" in prompt2


def test_compression_without_new_turns_does_not_call_llm() -> None:
    """When the previous summary already covers all compressible turns, no
    LLM call is made and the summary file is untouched."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)
        assert store["calls"] == 1
        text_before, fm_before = cm.get_summary(sid)

        cm.compress_context(sid, turns, last_total_tokens=5000)
        assert store["calls"] == 1, "no new turns => no LLM call"
        text_after, fm_after = cm.get_summary(sid)
        assert text_after == text_before
        assert fm_after.get("summary_version") == fm_before.get("summary_version")


# ---------------------------------------------------------------------------
# Failure handling: a failed result must never be persisted
# ---------------------------------------------------------------------------


def test_failed_infer_result_is_not_persisted() -> None:
    """Regression: runtime.infer returns success=False with the INPUT messages
    on HTTP errors.  compress_context must not parse that prompt as model
    output and must leave summary.md / memory.md untouched."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        def failing_infer(req: Any) -> SimpleNamespace:
            return _failed_result(
                req,
                "HTTP 400: Bad Request. {\"error\":{\"message\":\"request "
                "(1512940 tokens) exceeds the available context size "
                "(393216 tokens)\"}}",
            )

        cm = _make_cm(tmp_dir, infer_fn=failing_infer)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)  # must not raise

        assert not os.path.isfile(cm._summary_path(sid)), (
            "summary.md must NOT be created when the LLM call fails"
        )
        assert not os.path.isfile(cm._memory_path(sid)), (
            "memory.md must NOT be created when the LLM call fails"
        )
        text, _fm = cm.get_summary(sid)
        assert text == ""


def test_failed_result_does_not_corrupt_existing_summary() -> None:
    """A failed second compression must preserve a previously good summary
    (no placeholder overwrite, no version bump)."""
    calls: dict = {"n": 0}

    def flaky_infer(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(
                content="<summary>\ngood summary\n</summary>\n<memory>\n[]\n</memory>"
            )
        return _failed_result(req, "HTTP 400: exceeds the available context size")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=flaky_infer)
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)
        assert cm.get_summary(sid)[0].strip() == "good summary"

        turns.append(_make_turn(content="turn 4"))  # force a real second attempt
        cm.compress_context(sid, turns, last_total_tokens=2000)

        text, fm = cm.get_summary(sid)
        assert text.strip() == "good summary", "old summary must survive the failure"
        assert fm.get("summary_version") == 1
        assert "(concise summary prose" not in text


def test_placeholder_echo_output_is_not_persisted() -> None:
    """If the model echoes the prompt's format example (placeholder text),
    neither summary.md nor memory.md may be written."""
    echo_output = (
        "<summary>\n(concise summary prose, typically 2\u20135 paragraphs)\n</summary>\n"
        "<memory>\n[\n  {\n"
        '    "entry_type": "fact|preference|decision|entity",\n'
        '    "content": "self-contained descriptive sentence",\n'
        '    "source_turn_index": 0,\n'
        '    "confidence": 0.9,\n'
        '    "created_at": "2026-01-01T00:00:00"\n'
        "  }\n]\n</memory>"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=lambda req: SimpleNamespace(content=echo_output))
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        assert not os.path.isfile(cm._summary_path(sid))
        assert not os.path.isfile(cm._memory_path(sid))
        assert cm.get_memory_entries(sid) == []


# ---------------------------------------------------------------------------
# Full-delta prompt (no artificial budget)
# ---------------------------------------------------------------------------


def test_compression_sends_full_delta_without_budget() -> None:
    """The full delta (all compressible turns) must be sent to the LLM with
    NO artificial prompt budget: no oldest turns are dropped, no omission
    marker is inserted.  In normal operation the delta is bounded by the
    MAX_TOKENS_IN_CONTEXT trigger threshold anyway."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()

        # 200 turns of ~400 chars (~100 tokens each) => ~20k tokens of delta.
        turns = [_make_turn(content=f"turn {i}: " + "word " * 80) for i in range(200)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        prompt = store["prompts"][0]
        full_delta_estimate = sum(
            _estimate_tokens_fast(f"turn {i}: " + "word " * 80) for i in range(198)
        )
        assert full_delta_estimate > 10000, "test precondition: delta is large"
        # (Turns 198/199 are in the recent-K window and are not part of the
        # compressible delta.)  Every compressible turn is present, oldest
        # included, with no omission marker.
        assert "turn 197:" in prompt
        assert "turn 0:" in prompt
        assert "older turn(s) omitted" not in prompt
        # The prompt carries essentially the whole delta.
        assert _estimate_tokens_fast(prompt) >= full_delta_estimate
        # The summary was still persisted.
        text, fm = cm.get_summary(sid)
        assert text.strip() == "summary text"
        assert fm.get("summarized_up_to_turn") == 197  # 200 - k(2) - 1


def test_single_giant_turn_is_capped_per_turn() -> None:
    """A pathological single turn (100 KB of text) is capped per-turn
    (head 3/4 + tail 1/4, with a marker), but is still represented — the
    per-turn cap is a content-quality limit, not a total budget."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, recent_turns_k=1, infer_fn=_capturing_infer(store))
        sid = cm.create_session()

        giant = "x" * 100_000
        turns = [_make_turn(content=giant), _make_turn(content="last user msg")]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        prompt = store["prompts"][0]
        assert "characters omitted" in prompt, "giant turn must be truncated"
        assert "Turn 0 " in prompt, "the giant turn must still be represented"
        # The very last turn stays out of the compression prompt by design
        # (it is preserved verbatim in the inference context).
        assert "last user msg" not in prompt


# ---------------------------------------------------------------------------
# Overflow retry
# ---------------------------------------------------------------------------


def test_overflow_error_is_single_call_and_not_persisted() -> None:
    """A context-overflow rejection is a single failed call: the prompt is
    already bounded by the compression model's window up front, so there is no
    halving retry — the failure is logged and summary/memory stay untouched."""
    calls: dict = {"n": 0, "prompts": []}

    def overflow(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        calls["prompts"].append(req.messages[0].content)
        return _failed_result(
            req,
            "HTTP 400: Bad Request. {\"error\":{\"message\":\"request "
            "(300000 tokens) exceeds the available context size "
            "(131072 tokens)\"}}",
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=overflow)
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}: " + "word " * 80) for i in range(200)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        assert calls["n"] == 1, "an overflow 400 must not trigger a shrinking retry"
        assert not os.path.isfile(cm._summary_path(sid))
        assert not os.path.isfile(cm._memory_path(sid))
        assert cm.get_summary(sid)[0] == ""


# ---------------------------------------------------------------------------
# Empty-output retry (accepted request, empty answer)
# ---------------------------------------------------------------------------


def _empty_output_result(completion_tokens: int) -> SimpleNamespace:
    """Mimic a provider that ACCEPTED the request (success, no error) but
    returned an assistant message with empty content after using
    *completion_tokens* on reasoning/thinking."""
    return SimpleNamespace(
        success=True,
        messages=[SimpleNamespace(role="assistant", content="")],
        error=None,
        error_code=None,
        stat=SimpleNamespace(completion_tokens=completion_tokens),
    )


def test_empty_output_with_thinking_is_single_call_and_not_persisted() -> None:
    """An empty answer that DID use completion tokens (a thinking model spent
    the budget on reasoning) is a single failed call — the prompt is already
    window-bounded so there is no shrinking retry, and nothing is persisted."""
    calls: dict = {"n": 0, "prompts": []}

    def starved(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        calls["prompts"].append(req.messages[0].content)
        return _empty_output_result(completion_tokens=1616)

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=starved)
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}: " + "word " * 80) for i in range(200)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        assert calls["n"] == 1, "a thinking-starved empty output must not be retried"
        assert not os.path.isfile(cm._summary_path(sid))
        assert cm.get_summary(sid)[0] == ""


def test_empty_output_without_tokens_retries_same_prompt() -> None:
    """An empty answer that used NO completion tokens is a transient provider
    blip: the SAME prompt is resent (no shrinking) until it succeeds."""
    calls: dict = {"n": 0, "prompts": []}

    def blank_then_ok(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        calls["prompts"].append(req.messages[0].content)
        if calls["n"] == 1:
            return _empty_output_result(completion_tokens=0)
        return SimpleNamespace(
            content="<summary>\nrecovered summary\n</summary>\n<memory>\n[]\n</memory>"
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=blank_then_ok)
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}" + " word" * 30) for i in range(20)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        assert calls["n"] == 2, "blank empty output must be retried"
        assert calls["prompts"][0] == calls["prompts"][1], (
            "the retry must resend the SAME prompt (no shrinking)"
        )
        text, _fm = cm.get_summary(sid)
        assert text.strip() == "recovered summary"


def test_persistent_empty_output_with_thinking_is_not_persisted() -> None:
    """If the answer stays empty (and used completion tokens), it is a single
    failed call — no shrinking retry — and nothing is persisted."""
    calls: dict = {"n": 0}

    def always_starved(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        return _empty_output_result(completion_tokens=5)

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=always_starved)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)  # must not raise

        assert calls["n"] == 1, "a thinking-starved empty output is a single call"
        assert not os.path.isfile(cm._summary_path(sid))
        assert not os.path.isfile(cm._memory_path(sid))
        assert cm.get_summary(sid)[0] == ""


def test_non_overflow_error_is_not_retried() -> None:
    calls: dict = {"n": 0}

    def conn_error(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        return _failed_result(req, "Connection error: refused")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=conn_error)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)
        assert calls["n"] == 1, "non-overflow errors must not be retried"


# ---------------------------------------------------------------------------
# Failure backoff
# ---------------------------------------------------------------------------


def test_backoff_stops_hot_loop_after_repeated_failures() -> None:
    """After 3 consecutive failures at the same token count, further attempts
    are skipped until the token count grows by 25 %."""
    calls: dict = {"n": 0}

    def always_fail(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        return _failed_result(req, "HTTP 500: Internal Server Error")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=always_fail)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        for _ in range(3):
            cm.compress_context(sid, turns, last_total_tokens=2000)
        assert calls["n"] == 3

        # Same token count => inside backoff, no LLM call.
        cm.compress_context(sid, turns, last_total_tokens=2000)
        assert calls["n"] == 3, "backoff must suppress the 4th attempt"

        # 25 % token growth lifts the backoff.
        cm.compress_context(sid, turns, last_total_tokens=2600)
        assert calls["n"] == 4, "growth beyond 25 % must lift the backoff"


def test_backoff_requires_three_consecutive_failures() -> None:
    """Two consecutive failures do NOT trigger the backoff, and a success in
    between resets the failure counter."""
    calls: dict = {"n": 0}
    # Pattern: F, F, OK, F, F, OK — after call 5 only 2 consecutive failures
    # are recorded (the OK at call 3 reset the counter), so call 6 is allowed.
    pattern = [False, False, True, False, False, True]

    def flaky(req: Any) -> SimpleNamespace:
        calls["n"] += 1
        if pattern[calls["n"] - 1]:
            return SimpleNamespace(
                content="<summary>\nok\n</summary>\n<memory>\n[]\n</memory>"
            )
        return _failed_result(req, "HTTP 500: Internal Server Error")

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=flaky)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(4)]

        for _ in range(6):
            cm.compress_context(sid, turns, last_total_tokens=2000)
            # Each call must create a fresh delta: extend the conversation.
            turns.append(_make_turn(content=f"extra {len(turns)}"))

        assert calls["n"] == 6, (
            "the 6th attempt must be allowed (success at call 3 reset the "
            "failure counter to 2)"
        )
        # Both successes persisted a summary (version 2).
        _text, fm = cm.get_summary(sid)
        assert fm.get("summary_version") == 2


# ---------------------------------------------------------------------------
# Placeholder artifacts in inference context assembly
# ---------------------------------------------------------------------------


def test_assemble_context_ignores_placeholder_summary() -> None:
    """A corrupted placeholder summary.md must not be injected into the
    inference context, and — crucially — its presence must keep the
    compressed branch active (the full history must NOT be re-injected)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(20)]
        cm.save_conversation(sid, turns)
        _write_placeholder_summary(cm, sid, up_to=15)
        cm.save_memory(
            sid,
            [MemoryEntry("fact", "self-contained descriptive sentence", 0, 0.9, "2026-01-01T00:00:00")],
        )

        assembled = cm.assemble_context(sid, [])

        all_text = "\n".join(str(m.get("content", "")) for m in assembled)
        assert "(concise summary prose" not in all_text, (
            "placeholder summary text must not leak into the inference context"
        )
        assert "self-contained descriptive sentence" not in all_text, (
            "placeholder memory entry must not leak into the inference context"
        )
        # Compressed branch: only the recent window (k=2) of turns, not all 20.
        user_msgs = [m for m in assembled if m.get("role") == "user"]
        assert len(user_msgs) <= 3, (
            f"only the recent window may be present, got {len(user_msgs)} user msgs"
        )


def test_assemble_context_injects_valid_summary() -> None:
    """A real summary is still injected (sanity check for the placeholder path)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        turns = [_make_turn(content=f"turn {i}") for i in range(20)]
        cm.save_conversation(sid, turns)
        cm._atomic_write(
            cm._summary_path(sid),
            serialize_summary(
                {
                    "session_id": sid,
                    "summary_version": 1,
                    "summarized_up_to_turn": 15,
                    "updated_at": "2026-01-01T00:00:00",
                },
                "the real summary prose",
            ),
        )

        assembled = cm.assemble_context(sid, [])
        all_text = "\n".join(str(m.get("content", "")) for m in assembled)
        assert "the real summary prose" in all_text


# ---------------------------------------------------------------------------
# Memory merge / filtering
# ---------------------------------------------------------------------------


def test_new_memory_entries_are_merged_not_wiped() -> None:
    """The model only emits NEW memory entries; persisting them alone would
    wipe the existing entries.  They must be merged (de-duplicated)."""
    existing = [
        MemoryEntry("fact", "server runs on port 8080", 0, 0.95, "2026-01-01T00:00:01"),
        MemoryEntry("decision", "chose Redis for caching", 1, 0.9, "2026-01-01T00:00:02"),
    ]
    new_entries = [
        {
            "entry_type": "fact",
            "content": "newly discovered db name is app_prod",
            "source_turn_index": 5,
            "confidence": 0.9,
            "created_at": "2026-01-02T00:00:00",
        },
        {
            "entry_type": "fact",
            "content": "server runs on port 8080",  # duplicate of existing
            "source_turn_index": 6,
            "confidence": 0.9,
            "created_at": "2026-01-02T00:00:00",
        },
        {
            "entry_type": "fact|preference|decision|entity",  # invalid type
            "content": "self-contained descriptive sentence",
            "source_turn_index": 0,
            "confidence": 0.9,
            "created_at": "2026-01-02T00:00:00",
        },
    ]
    output = "<summary>\ns\n</summary>\n<memory>\n" + json.dumps(new_entries) + "\n</memory>"

    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=lambda req: SimpleNamespace(content=output))
        sid = cm.create_session()
        cm.save_memory(sid, list(existing))

        turns = [_make_turn(content=f"turn {i}") for i in range(4)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        contents = {e.content for e in cm.get_memory_entries(sid)}
        assert contents == {
            "server runs on port 8080",
            "chose Redis for caching",
            "newly discovered db name is app_prod",
        }


def test_placeholder_memory_entries_filtered_on_read() -> None:
    """Memory entries that are the prompt's format example must never be
    returned by get_memory_entries."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir)
        sid = cm.create_session()
        cm.save_memory(
            sid,
            [
                MemoryEntry("fact", "self-contained descriptive sentence", 0, 0.9, "2026-01-01T00:00:00"),
                MemoryEntry("fact", "real memory entry", 1, 0.9, "2026-01-01T00:00:00"),
            ],
        )
        entries = cm.get_memory_entries(sid)
        assert [e.content for e in entries] == ["real memory entry"]


# ---------------------------------------------------------------------------
# Placeholder summary restarts the delta from turn 0
# ---------------------------------------------------------------------------


def test_placeholder_summary_treated_as_no_summary() -> None:
    """A corrupted placeholder summary.md must not be used as the 'previous
    summary': the next compression restarts the delta from turn 0 (sending it
    in full) and replaces the placeholder."""
    store: dict = {}
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm(tmp_dir, infer_fn=_capturing_infer(store))
        sid = cm.create_session()

        turns = [_make_turn(content=f"turn {i}: " + "word " * 40) for i in range(30)]
        cm.save_conversation(sid, turns)
        _write_placeholder_summary(cm, sid, up_to=25)

        cm.compress_context(sid, turns, last_total_tokens=2000)

        prompt = store["prompts"][0]
        assert "## Previous summary" not in prompt, (
            "placeholder summary must not be used as the previous summary"
        )
        assert "Turn 0 " in prompt, "delta must restart from turn 0"
        text, fm = cm.get_summary(sid)
        assert "(concise summary prose" not in text
        assert text.strip() == "summary text"
        assert fm.get("summary_version") == 4  # continues the version counter


# ---------------------------------------------------------------------------
# One-shot success: prompt bounded by the compression model's window
# ---------------------------------------------------------------------------


def _make_cm_with_registry(
    tmp_dir: str, *, summary_model_id: str = "", models: list[ModelConfig]
) -> ContextManager:
    registry = ModelRegistry()
    for m in models:
        registry.register(m)
    infer_fn = lambda req: SimpleNamespace(  # noqa: E731
        content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
    )
    return ContextManager(
        infer_fn=infer_fn,
        chats_dir=tmp_dir,
        recent_turns_k=2,
        summary_model_id=summary_model_id,
        max_tokens_in_context=1000,
        model_registry=registry,
    )


def _cfg(model_id: str, max_context: int) -> ModelConfig:
    return ModelConfig(model_id=model_id, api_base="http://localhost", model_name=model_id, max_context=max_context)


def test_prompt_is_bounded_by_the_compression_window() -> None:
    """When the compression model's window is known, a very large delta must be
    dropped to the window budget so the request fits in ONE call (oldest delta
    turns dropped with a marker) — instead of blowing past the window."""
    store: dict = {}

    def capturing(req: Any) -> SimpleNamespace:
        store.setdefault("prompts", []).append(req.messages[0].content)
        return SimpleNamespace(
            content="<summary>\nsummary text\n</summary>\n<memory>\n[]\n</memory>"
        )

    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = ModelRegistry()
        registry.register(_cfg("summary-model", 20000))
        cm = ContextManager(
            infer_fn=capturing,
            chats_dir=tmp_dir,
            recent_turns_k=2,
            summary_model_id="summary-model",
            max_tokens_in_context=1000,
            model_registry=registry,
        )
        sid = cm.create_session()
        # ~30k tokens of delta: far above the 20000-window budget, so the
        # oldest turns must be dropped to fit in one request.
        turns = [_make_turn(content=f"turn {i}: " + "word " * 80) for i in range(300)]
        cm.compress_context(sid, turns, last_total_tokens=2000)

        prompt = store["prompts"][0]
        assert "older turn(s) omitted" in prompt, (
            "the over-window delta must be dropped to the window budget"
        )
        # The compression still SUCCEEDED in a single call.
        text, fm = cm.get_summary(sid)
        assert text.strip() == "summary text"
        assert fm.get("summary_version", 0) >= 1


def test_prompt_unbounded_when_no_window_known() -> None:
    """When no window is known for any candidate, the delta is sent in full
    (best effort) — no oldest turns are dropped."""
    store: dict = {}

    def capturing(req: Any) -> SimpleNamespace:
        store.setdefault("prompts", []).append(req.messages[0].content)
        return SimpleNamespace(
            content="<summary>\ns\n</summary>\n<memory>\n[]\n</memory>"
        )

    registry = ModelRegistry()
    registry.register(_cfg("summary-model", 0))
    cm = ContextManager(
        infer_fn=capturing,
        chats_dir=tempfile.mkdtemp(),
        recent_turns_k=2,
        summary_model_id="summary-model",
        max_tokens_in_context=1000,
        model_registry=registry,
    )
    sid = cm.create_session()
    turns = [_make_turn(content=f"turn {i}") for i in range(10)]
    cm.compress_context(sid, turns, last_total_tokens=2000)

    first = store["prompts"][0]
    # recent_turns_k=2 keeps the last 2 turns verbatim, so the delta is 0..7.
    assert "Turn 0 " in first and "Turn 7 " in first
    assert "older turn(s) omitted" not in first


def test_selects_summary_model_when_window_large_enough() -> None:
    """Summary model window (>= inference window) -> use the summary model,
    bounded by the (smaller) inference window so the request never exceeds a
    window the endpoint is known to honour."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_registry(
            tmp_dir,
            summary_model_id="summary-model",
            models=[
                _cfg("summary-model", 393216),
                _cfg("infer-model", 131072),
            ],
        )
        model_id, window = cm._select_compression_model(
            inference_model_id="infer-model", inference_max_context=131072
        )
        assert model_id == "summary-model"
        # Bounded by the smaller (inference) window — guarantees one-shot fit.
        assert window == 131072


def test_switches_to_inference_model_when_summary_smaller() -> None:
    """Summary model's window smaller than the inference model's -> run on the
    inference model instead (its window is provably sufficient)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_registry(
            tmp_dir,
            summary_model_id="summary-model",
            models=[
                _cfg("summary-model", 32768),
                _cfg("infer-model", 131072),
            ],
        )
        model_id, window = cm._select_compression_model(
            inference_model_id="infer-model", inference_max_context=131072
        )
        assert model_id == "infer-model"
        assert window == 131072  # bounded by the inference model's window


def test_switches_to_inference_model_when_summary_unset() -> None:
    """No summary model -> run on the inference model."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = _make_cm_with_registry(
            tmp_dir,
            models=[_cfg("infer-model", 131072)],
        )
        # _summary_model_id default is "summary"; with a registry lacking it,
        # it resolves to "" (compression model unavailable) -> inference used.
        model_id, window = cm._select_compression_model(
            inference_model_id="infer-model", inference_max_context=131072
        )
        assert model_id == "infer-model"
        assert window == 131072


def test_disabled_when_no_compression_model_available() -> None:
    """Neither a summary model nor an inference model -> compression disabled."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cm = ContextManager(
            infer_fn=lambda req: SimpleNamespace(content="x"),
            chats_dir=tmp_dir,
            recent_turns_k=2,
            summary_model_id="",
            max_tokens_in_context=1000,
            model_registry=ModelRegistry(),  # empty registry
        )
        assert cm._select_compression_model("", 0) == ("", 0)

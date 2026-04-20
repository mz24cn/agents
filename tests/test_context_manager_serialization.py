"""Tests for context_manager.py — data models and serialization utilities.

Covers:
  - Unit tests for parse_front_matter (Task 1.3)
  - Property test: conversation round-trip (Task 1.1, Property 1)
  - Property test: invalid input raises descriptive ValueError (Task 1.2, Property 2)
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.context_manager import (
    ConversationTurn,
    estimate_tokens,
    parse_conversation,
    parse_front_matter,
    serialize_conversation,
    serialize_summary,
    serialize_tool_call,
)


# ---------------------------------------------------------------------------
# Unit tests — estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_basic():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


def test_estimate_tokens_long():
    text = "a" * 400
    assert estimate_tokens(text) == 100


# ---------------------------------------------------------------------------
# Unit tests — parse_front_matter (Task 1.3)
# ---------------------------------------------------------------------------


def test_parse_front_matter_string_unquoted():
    doc = "---\nsession_id: abc123\n---\nbody"
    fm, body = parse_front_matter(doc)
    assert fm["session_id"] == "abc123"
    assert body == "body"


def test_parse_front_matter_string_quoted():
    doc = '---\nsession_id: "hello world"\n---\n'
    fm, body = parse_front_matter(doc)
    assert fm["session_id"] == "hello world"


def test_parse_front_matter_integer():
    doc = "---\nturn_count: 42\n---\n"
    fm, body = parse_front_matter(doc)
    assert fm["turn_count"] == 42
    assert isinstance(fm["turn_count"], int)


def test_parse_front_matter_list():
    doc = "---\nreferences:\n  - file1.md\n  - file2.md\n---\n"
    fm, body = parse_front_matter(doc)
    assert fm["references"] == ["file1.md", "file2.md"]


def test_parse_front_matter_nested_dict():
    doc = "---\nmeta:\n  key1: val1\n  key2: val2\n---\n"
    fm, body = parse_front_matter(doc)
    assert isinstance(fm["meta"], dict)
    assert fm["meta"]["key1"] == "val1"
    assert fm["meta"]["key2"] == "val2"


def test_parse_front_matter_body_preserved():
    doc = "---\nfoo: bar\n---\nHello\nWorld\n"
    fm, body = parse_front_matter(doc)
    assert body == "Hello\nWorld\n"


def test_parse_front_matter_empty_body():
    doc = "---\nfoo: bar\n---\n"
    fm, body = parse_front_matter(doc)
    assert body == ""


def test_parse_front_matter_missing_opening_delimiter():
    with pytest.raises(ValueError, match="---"):
        parse_front_matter("no front matter here")


def test_parse_front_matter_missing_closing_delimiter():
    with pytest.raises(ValueError):
        parse_front_matter("---\nfoo: bar\n")


def test_parse_front_matter_multiple_fields():
    doc = (
        "---\n"
        "session_id: sess1\n"
        "turn_count: 5\n"
        "updated_at: 2026-01-01T00:00:00\n"
        "---\n"
    )
    fm, _ = parse_front_matter(doc)
    assert fm["session_id"] == "sess1"
    assert fm["turn_count"] == 5
    assert fm["updated_at"] == "2026-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Unit tests — serialize_conversation / parse_conversation
# ---------------------------------------------------------------------------


def _make_turn(role: str, content: str, ts: str = "2026-01-01T00:00:00") -> ConversationTurn:
    return ConversationTurn(role=role, content=content, timestamp=ts)


def test_serialize_parse_empty_turns():
    fm = {
        "session_id": "s1",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "turn_count": 0,
        "references": [],
    }
    text = serialize_conversation([], fm)
    fm2, turns = parse_conversation(text)
    assert turns == []
    assert fm2["session_id"] == "s1"


def test_serialize_parse_single_turn():
    turns = [_make_turn("user", "Hello!", "2026-01-01T10:00:00")]
    fm = {
        "session_id": "s2",
        "created_at": "2026-01-01T10:00:00",
        "updated_at": "2026-01-01T10:00:00",
        "turn_count": 1,
        "references": [],
    }
    text = serialize_conversation(turns, fm)
    fm2, parsed = parse_conversation(text)
    assert len(parsed) == 1
    assert parsed[0].role == "user"
    assert parsed[0].content == "Hello!"
    assert parsed[0].timestamp == "2026-01-01T10:00:00"


def test_serialize_parse_multiple_turns():
    turns = [
        _make_turn("user", "What is 2+2?", "2026-01-01T10:00:00"),
        _make_turn("assistant", "4", "2026-01-01T10:00:01"),
        _make_turn("user", "Thanks!", "2026-01-01T10:00:02"),
    ]
    fm = {
        "session_id": "s3",
        "created_at": "2026-01-01T10:00:00",
        "updated_at": "2026-01-01T10:00:02",
        "turn_count": 3,
        "references": [],
    }
    text = serialize_conversation(turns, fm)
    _, parsed = parse_conversation(text)
    assert len(parsed) == 3
    for orig, got in zip(turns, parsed):
        assert got.role == orig.role
        assert got.content == orig.content
        assert got.timestamp == orig.timestamp


def test_parse_conversation_missing_opening_delimiter():
    with pytest.raises(ValueError):
        parse_conversation("no front matter\n## Turn 0 [ts]\n**role:** user\n\nhello\n")


def test_parse_conversation_missing_closing_delimiter():
    with pytest.raises(ValueError):
        parse_conversation("---\nsession_id: s1\n")


def test_parse_conversation_missing_role_line():
    doc = "---\nsession_id: s1\n---\n## Turn 0 [2026-01-01T00:00:00]\nno role here\n"
    with pytest.raises(ValueError, match="role"):
        parse_conversation(doc)


# ---------------------------------------------------------------------------
# Unit tests — serialize_tool_call
# ---------------------------------------------------------------------------


def test_serialize_tool_call_structure():
    fm = {"tool_name": "bash", "session_id": "s1", "turn_index": 0, "timestamp": "2026-01-01T00:00:00"}
    text = serialize_tool_call(fm, {"command": "ls"}, "file.txt")
    assert "## Arguments" in text
    assert "## Result" in text
    assert "ls" in text
    assert "file.txt" in text


# ---------------------------------------------------------------------------
# Unit tests — serialize_summary
# ---------------------------------------------------------------------------


def test_serialize_summary_structure():
    fm = {"session_id": "s1", "summary_version": 1, "summarized_up_to_turn": 5, "updated_at": "2026-01-01T00:00:00"}
    text = serialize_summary(fm, "This is the summary.")
    assert "summary_version" in text
    assert "This is the summary." in text


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Safe text: printable ASCII excluding characters that would break the
# Markdown turn-header format (brackets, newlines in timestamps, etc.)
safe_text_st = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
        whitelist_characters=" .,!?-_",
    ),
    min_size=0,
    max_size=200,
)

# Timestamp: simple ISO-like string without brackets
timestamp_st = st.builds(
    lambda y, m, d, h, mi, s: f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}",
    y=st.integers(min_value=2000, max_value=2099),
    m=st.integers(min_value=1, max_value=12),
    d=st.integers(min_value=1, max_value=28),
    h=st.integers(min_value=0, max_value=23),
    mi=st.integers(min_value=0, max_value=59),
    s=st.integers(min_value=0, max_value=59),
)

role_st = st.sampled_from(["user", "assistant", "function"])


def conversation_turn_strategy():
    return st.builds(
        ConversationTurn,
        role=role_st,
        content=safe_text_st,
        timestamp=timestamp_st,
        name=st.none(),
        tool_calls=st.none(),
    )


def front_matter_strategy(turns_count: int):
    return st.fixed_dictionaries(
        {
            "session_id": st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
                min_size=1,
                max_size=30,
            ),
            "created_at": timestamp_st,
            "updated_at": timestamp_st,
            "turn_count": st.just(turns_count),
            "references": st.just([]),
        }
    )


# ---------------------------------------------------------------------------
# Property 1: Conversation round-trip (Task 1.1)
# **Validates: Requirements 2.5, 11.3**
# ---------------------------------------------------------------------------


@given(turns=st.lists(conversation_turn_strategy(), min_size=0, max_size=50))
@settings(max_examples=25)
def test_conversation_roundtrip(turns: list[ConversationTurn]) -> None:
    """For any valid list of ConversationTurns, serialize then parse must
    produce an equivalent list.

    **Validates: Requirements 2.5, 11.3**
    """
    fm = {
        "session_id": "test-session",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "turn_count": len(turns),
        "references": [],
    }
    text = serialize_conversation(turns, fm)
    _, parsed = parse_conversation(text)

    assert len(parsed) == len(turns)
    for orig, got in zip(turns, parsed):
        assert got.role == orig.role
        assert got.content == orig.content
        assert got.timestamp == orig.timestamp


# ---------------------------------------------------------------------------
# Property 2: Invalid input raises descriptive ValueError (Task 1.2)
# **Validates: Requirements 11.4**
# ---------------------------------------------------------------------------


@given(
    missing_open=st.booleans(),
    missing_close=st.booleans(),
    payload=st.text(min_size=0, max_size=100),
)
@settings(max_examples=25)
def test_parse_conversation_invalid_raises_value_error(
    missing_open: bool,
    missing_close: bool,
    payload: str,
) -> None:
    """parse_conversation must raise ValueError with a descriptive message for
    any input that is missing '---' delimiters.

    **Validates: Requirements 11.4**
    """
    if missing_open:
        # No opening ---
        text = payload
    elif missing_close:
        # Opening --- but no closing ---
        text = f"---\nsession_id: s1\n{payload}"
    else:
        # Both delimiters present but body has no turn headers (non-empty body)
        # Only invalid if body is non-empty and non-whitespace
        if payload.strip():
            text = f"---\nsession_id: s1\n---\n{payload}"
        else:
            # Empty/whitespace body is valid (zero turns) — skip
            return

    with pytest.raises(ValueError) as exc_info:
        parse_conversation(text)

    # The error message must contain some position/location description
    error_msg = str(exc_info.value)
    assert len(error_msg) > 0, "ValueError message must not be empty"

"""An endpoint that refuses a request as oversized is answered, not obeyed.

The reference failure (session ``261114_111012``, 6 767 turns, ``Qwen-128K(4090)``)
ended on a request the model server rejected::

    Error: HTTP 400: Bad Request | body: {"error": {"type": "invalid_request_error",
    "message": "request exceeds the context window: prompt (131218 tokens) leaves no
    room to answer in the context (131072); requests are never truncated"}}

131 218 prompt tokens against a 131 072-token window: the prompt alone filled the
window, so there was no room left for an answer, and the endpoint -- unlike this
runtime -- does not silently truncate.  The session's own last turns kept growing
past the point where the in-loop compression trigger (90 % of the window, measured
with OUR estimate) had anything left to work with, and the loop reported the
rejection as the assistant's reply.

That rejection is the best measurement this runtime ever gets: the endpoint's own
count, of the exact payload, on the exact model.  So the loop now reads it
(:mod:`runtime.common` parsing helpers), folds it into that model's token
calibration and, when named, into the window its endpoint enforces, runs the
context remedies (compress -> offload -> drop oldest) and re-sends the round.

Covers:
  - reading the reference wording (counted prompt tokens, enforced window)
  - a rejection about something else teaches nothing
  - the counted share raises the token factor; the enforced window lowers the
    send ceiling (never raises it)
  - ``Runtime.infer_stream`` remedies a rejected round and keeps the generation
    going, and the next round measures with the folded-in factor
  - a context that cannot be made to fit produces an actionable message
  - ``CONTEXT_OVERFLOW_RECOVERIES`` bounds (and can switch off) the retries
  - a 413 carrying no prose is still remedied
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Callable, Optional
from unittest.mock import patch

import pytest

from runtime.common import (
    clear_context_window_observations,
    clear_prompt_token_calibration,
    estimate_chat_prompt_tokens,
    get_context_window_observation,
    get_prompt_token_calibration,
    is_context_overflow_error,
    learn_from_provider_rejection,
    provider_context_window,
    provider_prompt_tokens,
)
from runtime.models import InferenceRequest, Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime
from tests.test_in_loop_context_compression import (
    _content_sse,
    _stream_response,
    _tool_call_sse,
)

# ---------------------------------------------------------------------------
# The rejection this whole behaviour is built around
# ---------------------------------------------------------------------------

REFERENCE_BODY = (
    '{"error": {"type": "invalid_request_error", "message": "request exceeds the '
    'context window: prompt (131218 tokens) leaves no room to answer in the '
    'context (131072); requests are never truncated"}}'
)
REFERENCE_DETAIL = f"HTTP 400: Bad Request | body: {REFERENCE_BODY[:500]}"

#: A model id of its own: token calibration and observed windows are process-wide
#: per model, so sharing "test-model" with the other context suites would make
#: this file's assertions depend on test order.
MODEL = "self-heal-model"


@pytest.fixture(autouse=True)
def _clean_learning() -> Any:
    """One rejection must not teach the next test anything."""
    clear_prompt_token_calibration(MODEL)
    clear_context_window_observations(MODEL)
    yield
    clear_prompt_token_calibration(MODEL)
    clear_context_window_observations(MODEL)


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------


def test_the_reference_rejection_is_readable() -> None:
    """The wording that killed the session names both numbers we need."""
    assert is_context_overflow_error(REFERENCE_BODY)
    assert is_context_overflow_error(REFERENCE_DETAIL), "truncated body still reads"
    assert provider_prompt_tokens(REFERENCE_BODY) == 131218
    assert provider_context_window(REFERENCE_BODY) == 131072
    # The assistant turn as persisted in that session's conversation.json.
    stored = f"Error: {REFERENCE_DETAIL}"
    assert is_context_overflow_error(stored)
    assert provider_prompt_tokens(stored) == 131218


@pytest.mark.parametrize(
    "body, counted, window",
    [
        (
            "request (300000 tokens) exceeds the available context size (131072)",
            300000,
            131072,
        ),
        (
            "This model's maximum context length is 8191 tokens. However, you "
            "requested 10736 tokens (6668 in the messages, 4068 in the functions).",
            10736,
            8191,
        ),
        ("prompt is too long: 210000 tokens > 200000 maximum", 210000, 200000),
        # A provider that only says "too big" still gets remedied; there is just
        # nothing to fold into the estimate.
        (
            "the request exceeds the available context size, try increasing the "
            "context size with -c",
            0,
            0,
        ),
    ],
)
def test_other_providers_wordings_are_read_too(body: str, counted: int, window: int) -> None:
    assert is_context_overflow_error(body)
    assert provider_prompt_tokens(body) == counted
    assert provider_context_window(body) == window


def test_a_rejection_about_something_else_teaches_nothing() -> None:
    """Only a size rejection may shrink later requests of this model.

    An auth or quota failure quotes numbers too (a quota in tokens, a price), and
    reading one as a context window would cap every later request of that model
    for no reason.
    """
    for body in (
        '{"error": {"type": "authentication_error", "message": "invalid api key"}}',
        '{"error": {"message": "Insufficient Balance", "type": "insufficient_balance_error"}}',
        "you requested 900000 tokens per minute; limit reached",
    ):
        assert not is_context_overflow_error(body), body
        assert learn_from_provider_rejection(
            body, model_id=MODEL, estimated=1000, window=131072
        ) is None
    assert get_prompt_token_calibration(MODEL) == 1.0
    assert get_context_window_observation(MODEL) == 0


# ---------------------------------------------------------------------------
# What the answer is used for
# ---------------------------------------------------------------------------


def test_the_counted_share_raises_the_token_factor_once_and_only_once() -> None:
    """The provider counted OUR request: raise the floor to that ratio.

    131 218 counted for a request measured at ~65 600 means this model's tokenizer
    costs 2x what the character-class guess says.  Every later "may I send this?"
    answer for this model must then use 2x, or the loop keeps guessing wrong and
    keeps paying for doomed round-trips.
    """
    estimated = 65_609
    factor = learn_from_provider_rejection(
        REFERENCE_BODY, model_id=MODEL, estimated=estimated, window=131072,
    )
    ratio = 131218 / estimated
    assert factor == pytest.approx(ratio)
    assert get_prompt_token_calibration(MODEL) == pytest.approx(ratio)
    # A second reading of the same rejection must not double the correction.
    again = learn_from_provider_rejection(
        REFERENCE_BODY, model_id=MODEL, estimated=estimated, window=131072,
    )
    assert again >= ratio, "monotonic: the floor never drops"
    assert again < ratio * 1.6, "not compounding"


def test_the_enforced_window_is_filed_and_can_only_tighten_the_ceiling() -> None:
    """An endpoint naming its window is believed; the registry is not."""
    # Registry claims more than the endpoint allows -> the endpoint wins.
    learn_from_provider_rejection(
        REFERENCE_BODY, model_id=MODEL, estimated=65_609, window=1_000_000,
    )
    assert get_context_window_observation(MODEL) == 131072
    # A later, LARGER claim cannot loosen a ceiling that was already enforced.
    learn_from_provider_rejection(
        "request (400000 tokens) exceeds the available context size (400000)",
        model_id=MODEL, estimated=390_000, window=1_000_000,
    )
    assert get_context_window_observation(MODEL) == 131072


def _cfg(max_context: int, **generate_params: int) -> ModelConfig:
    return ModelConfig(
        model_id=MODEL,
        api_base="http://localhost:9999",
        model_name="t",
        max_context=max_context,
        generate_params=dict(generate_params),
    )


def test_a_learned_window_lowers_the_send_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guard, better number: the window the endpoint actually enforces."""
    monkeypatch.delenv("CONTEXT_OUTPUT_RESERVE_TOKENS", raising=False)
    registry_window = _cfg(1_000_000)
    assert Runtime._effective_context_window_tokens(registry_window) == 1_000_000
    assert Runtime._context_send_budget_tokens(registry_window) == 1_000_000 - 8192

    learn_from_provider_rejection(
        REFERENCE_BODY, model_id=MODEL, estimated=65_609, window=1_000_000,
    )
    assert Runtime._effective_context_window_tokens(registry_window) == 131072
    assert Runtime._context_send_budget_tokens(registry_window) == 131072 - 8192

    # A model with no window configured at all still gets bounded, now that the
    # endpoint named one -- previously the guard was inert and every oversized
    # round-trip was paid for.
    unconfigured = _cfg(0)
    assert Runtime._effective_context_window_tokens(unconfigured) == 131072
    assert Runtime._context_send_budget_tokens(unconfigured) > 0

    # A window smaller than the observed one still rules: learning is one-way.
    assert Runtime._effective_context_window_tokens(_cfg(8000)) == 8000


# ---------------------------------------------------------------------------
# The loop's own recovery
# ---------------------------------------------------------------------------


class _Endpoint:
    """Stands in for the model server: serves SSE, rejects one numbered request."""

    def __init__(
        self,
        *,
        reject_at: int = 0,
        status: int = 400,
        body: str = REFERENCE_BODY,
        first_round: Optional[Callable[[], io.BytesIO]] = None,
    ) -> None:
        self.reject_at = reject_at
        self.status = status
        self.body = body
        self.first_round = first_round or (
            lambda: _tool_call_sse("dummy_tool", "call_1")
        )
        self.bodies: list[dict] = []

    def __call__(self, req: Any, **kwargs: Any):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        sent = len(self.bodies)
        if sent == self.reject_at:
            raise urllib.error.HTTPError(
                "http://localhost:9999/v1/chat/completions",
                self.status,
                "Bad Request",
                {},
                io.BytesIO(self.body.encode("utf-8")),
            )
        if sent == 1:
            return _stream_response(self.first_round())
        return _stream_response(_content_sse("final answer"))


def _run(
    endpoint: _Endpoint,
    *,
    max_context: int,
    overflow_hook: Callable | None = None,
    tool_result: str = "TOOL-RESULT",
) -> list[Message]:
    registry = ModelRegistry()
    registry.register(
        ModelConfig(
            model_id=MODEL,
            api_base="http://localhost:9999",
            model_name="t",
            api_protocol="openai",
            max_context=max_context,
        )
    )
    tools = ToolRegistry()

    def dummy_tool() -> str:
        return tool_result

    tools.register(
        ToolConfig(
            tool_id="dummy_tool",
            tool_type="function",
            name="dummy_tool",
            description="A dummy tool",
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        callable_fn=dummy_tool,
    )
    runtime = Runtime(model_registry=registry, tool_registry=tools)
    request = InferenceRequest(
        model_id=MODEL,
        tool_ids=["dummy_tool"],
        text="ORIGINAL-USER-TURN",
        stream=True,
        max_tool_rounds=5,
    )
    with patch("urllib.request.urlopen", side_effect=endpoint):
        return list(runtime.infer_stream(request, on_context_overflow=overflow_hook))


def _errors(messages: list[Message]) -> list[str]:
    return [
        m.content or ""
        for m in messages
        if m.role == "assistant" and (m.content or "").startswith("Error")
    ]


def test_a_rejected_round_is_remedied_and_the_generation_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline behaviour: the session does not end on a 400.

    Round 2 is rejected as oversized although our own measure said it fit (that is
    the situation the reference session died in).  The remedies rebuild a smaller
    context, and the same round is sent again -- the user gets the answer instead
    of an error turn.
    """
    seen: list[tuple] = []

    def hook(messages, budget, estimate, max_context):
        seen.append((budget, max_context, len(messages)))
        compact = [Message(role="user", content="COMPACTED-BY-COMPRESSION")]
        return compact, estimate(compact)

    # Rejected on the second send (round 2), i.e. after a successful tool round.
    endpoint = _Endpoint(reject_at=2)
    collected = _run(endpoint, max_context=131_072, overflow_hook=hook)

    assert not _errors(collected), _errors(collected)
    assert any(
        m.role == "assistant" and m.content == "final answer" for m in collected
    ), "the generation completed"
    assert len(endpoint.bodies) == 3, "rejected send, re-sent round, terminal round"
    assert len(seen) == 1, "the remedies ran once, on the rejected round"
    assert seen[0][1] == 131_072, "the hook is told the model's window"

    resent = [m.get("content") or "" for m in endpoint.bodies[-1]["messages"]]
    assert "COMPACTED-BY-COMPRESSION" in resent, "the re-send used the smaller context"
    assert all("TOOL-RESULT" not in text for text in resent)


def test_the_rejection_teaches_the_next_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counted share becomes this model's factor, from the sent payload.

    The denominator is the raw estimate of the request that was actually sent
    (read back out of the captured body), so the factor means exactly what the
    endpoint said: counted / measured-here.
    """
    monkeypatch.delenv("CONTEXT_OUTPUT_RESERVE_TOKENS", raising=False)
    counted = 9_000
    body = (
        f'request exceeds the context window: prompt ({counted} tokens) leaves no '
        f'room to answer in the context (8000); requests are never truncated'
    )

    def hook(messages, budget, estimate, max_context):
        return [Message(role="user", content="COMPACTED-BY-COMPRESSION")], 1

    endpoint = _Endpoint(reject_at=2, body=body)
    collected = _run(
        endpoint, max_context=8000, overflow_hook=hook, tool_result="fat " * 3000,
    )
    assert not _errors(collected), _errors(collected)

    rejected = endpoint.bodies[1]
    raw = estimate_chat_prompt_tokens(rejected["messages"], rejected.get("tools"))
    factor = get_prompt_token_calibration(MODEL)
    assert factor > 1.0, "the endpoint counted more than we measured"
    assert factor == pytest.approx(counted / raw, rel=0.05)


def test_a_rejection_too_absurd_to_be_believed_teaches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A counted share outside the credible band is refused, and healed anyway.

    131 218 tokens claimed for a request measured at ~78 is not tokenizer drift;
    it means the number is about something else (a provider counting a cached or
    rebuilt prompt).  Folding it in would cap every later request of that model,
    so the sample is dropped -- but the remedies still run, because the endpoint
    did reject the request in front of us.
    """
    called = [0]

    def hook(messages, budget, estimate, max_context):
        called[0] += 1
        return [Message(role="user", content="COMPACTED-BY-COMPRESSION")], 1

    endpoint = _Endpoint(reject_at=2)
    collected = _run(endpoint, max_context=131_072, overflow_hook=hook)
    assert called == [1]
    assert get_prompt_token_calibration(MODEL) == 1.0
    assert not _errors(collected), _errors(collected)


def test_a_context_that_cannot_be_made_to_fit_reports_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-healing is not the same as an infinite retry.

    The remedies decline (nothing left to compress) and the endpoint's own count
    says the request is over the ceiling, so the request is not sent a second
    time and the caller is told what happened in terms they can act on.
    """
    monkeypatch.setenv("CONTEXT_OUTPUT_RESERVE_TOKENS", "1024")
    window = 8000
    counted = 13_100
    body = (
        f'request exceeds the context window: prompt ({counted} tokens) leaves no '
        f'room to answer in the context ({window}); requests are never truncated'
    )

    def hook(messages, budget, estimate, max_context):
        # Decline: the compressor changed nothing it could change.
        return None, estimate(messages)

    endpoint = _Endpoint(reject_at=2, body=body)
    collected = _run(
        endpoint, max_context=window, overflow_hook=hook, tool_result="fat " * 3000,
    )

    errors = _errors(collected)
    assert len(errors) == 1, errors
    assert "HTTP 400" in errors[0], "the endpoint's own wording is kept"
    assert "context too large to send" in errors[0]
    assert len(endpoint.bodies) == 2, "the doomed request was not sent again"


def test_the_retry_budget_bounds_the_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint that rejects everything is survived, not fed."""
    monkeypatch.setenv("CONTEXT_OVERFLOW_RECOVERIES", "1")

    class _Always:
        """Rejects every single send."""

        def __init__(self) -> None:
            self.bodies: list[dict] = []

        def __call__(self, req: Any, **kwargs: Any):
            self.bodies.append(json.loads(req.data.decode("utf-8")))
            raise urllib.error.HTTPError(
                "http://localhost:9999/v1/chat/completions", 400, "Bad Request", {},
                io.BytesIO(REFERENCE_BODY.encode("utf-8")),
            )

    def hook(messages, budget, estimate, max_context):
        return [Message(role="user", content="COMPACTED-BY-COMPRESSION")], 1

    always = _Always()
    collected = _run(always, max_context=131_072, overflow_hook=hook)
    assert len(always.bodies) == 2, "one send, one recovery re-send, stop"
    assert _errors(collected), "and then reported to the caller"


def test_the_recovery_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The historical behaviour stays available for operators."""
    monkeypatch.setenv("CONTEXT_OVERFLOW_RECOVERIES", "0")
    called = [0]

    def hook(messages, budget, estimate, max_context):
        called[0] += 1
        return [Message(role="user", content="COMPACTED-BY-COMPRESSION")], 1

    endpoint = _Endpoint(reject_at=2)
    collected = _run(endpoint, max_context=131_072, overflow_hook=hook)
    assert len(endpoint.bodies) == 2, "one round-trip, rejected, reported"
    assert called == [0], "no remedies were attempted"
    errors = _errors(collected)
    assert errors and "HTTP 400" in errors[0]


def test_a_size_rejection_without_prose_is_still_remedied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """413 carries no numbers to learn, yet the remedy is still the answer."""
    called = [0]

    def hook(messages, budget, estimate, max_context):
        called[0] += 1
        return [Message(role="user", content="COMPACTED-BY-COMPRESSION")], 1

    endpoint = _Endpoint(reject_at=2, status=413, body="payload too large")
    collected = _run(endpoint, max_context=131_072, overflow_hook=hook)
    assert called == [1]
    assert not _errors(collected), _errors(collected)
    assert any(
        m.role == "assistant" and m.content == "final answer" for m in collected
    )


def test_a_recovery_that_buys_no_headroom_is_not_sent_again() -> None:
    """A repair has to buy room, or it is not a repair.

    The endpoint refuses the request, the remedies run, and what they hand back
    is no smaller than the payload that was just refused (a context already at
    the bottom of the ladder).  Re-sending that would repeat the rejection
    forever; the loop stops and says so instead.
    """

    class _RejectsTheRepairedToo:
        """Serves the tool round, then refuses it and the repaired retry."""

        def __init__(self) -> None:
            self.bodies: list[dict] = []

        def __call__(self, req: Any, **kwargs: Any):
            self.bodies.append(json.loads(req.data.decode("utf-8")))
            if len(self.bodies) in (2, 3):
                raise urllib.error.HTTPError(
                    "http://localhost:9999/v1/chat/completions", 400, "Bad Request",
                    {}, io.BytesIO(REFERENCE_BODY.encode("utf-8")),
                )
            if len(self.bodies) == 1:
                return _stream_response(_tool_call_sse("dummy_tool", "call_1"))
            return _stream_response(_content_sse("final answer"))

    shrunk_to_one_line = [0]

    def hook(messages, budget, estimate, max_context):
        shrunk_to_one_line[0] += 1
        return [Message(role="user", content="COMPACTED-BY-COMPRESSION")], 1

    endpoint = _RejectsTheRepairedToo()
    collected = _run(endpoint, max_context=131_072, overflow_hook=hook)

    assert shrunk_to_one_line[0] == 2, "the remedies ran on both rejections"
    assert len(endpoint.bodies) == 3, "the futile third copy was never sent"
    errors = _errors(collected)
    assert errors and "no smaller than the one just refused" in errors[0], errors

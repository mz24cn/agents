"""What a model-side failure costs: replayed with backoff, unless it says why.

The retry rule used to be an allowlist of HTTP statuses (408/425/429/5xx), which
put the burden of proof on the failure being *recognised* as transient.  In
practice most model-side failures are unnamed -- a gateway answering 400 with a
body we cannot parse, a stream that ends with no content at all, a provider field
that is not the shape we expected -- and an unnamed failure is far more often a
momentary one than a permanent one.  So the rule is inverted: **replay with
backoff by default**, and stop early only when the failure names a reason a
resend demonstrably cannot change (rejected credential, unknown model, no credit,
malformed request) or when another remedy owns it (an oversized payload goes to
the context ladder, not to a resend of the same bytes).

Both entry points share one predicate (``Runtime._is_retryable_model_error``,
backed by ``runtime.common.is_fatal_model_error``), and the cost of being wrong
is bounded by ``MODEL_API_MAX_RETRIES``.

Covers:
  - the fail-fast wording/status matrix (and what stays replayable)
  - an unexplained 400 / an unparsable provider payload is replayed
  - a rejected credential or an unknown model costs one send, not three
  - a rate limit backs off; an empty credit balance does not
  - an oversized rejection skips the backoff queue and goes to the remedies
  - the empty-response error says how many sends were made
  - the non-streaming entry point follows the same rule
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Callable, Optional

import pytest
from unittest.mock import patch

from runtime.common import is_fatal_model_error
from runtime.models import InferenceRequest, Message, ModelConfig, ToolConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime, _is_retryable_model_error
from tests.test_in_loop_context_compression import _content_sse, _stream_response

URL = "http://localhost:9999/v1/chat/completions"


def _http_error(code: int, reason: str, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, reason, {}, io.BytesIO(body.encode("utf-8")))


# ---------------------------------------------------------------------------
# The fail-fast list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, code, why",
    [
        ("", 401, "credential rejected"),
        ('{"error":{"message":"Incorrect API key provided: sk-abc"}}', 401, "bad key"),
        ('{"error":{"message":"invalid x-api-key"}}', 403, "bad key"),
        ('{"error":{"message":"permission denied"}}', 403, "no permission"),
        ("model gpt-9 does not exist", 404, "unknown model"),
        ("model_not_found", 404, "unknown model"),
        ("", 422, "request body rejected by schema"),
        ("Invalid parameter: 'temperature' must be a number", 400, "caller must fix it"),
        ("You exceeded your current quota, check your plan and billing details",
         429, "credit, not rate limit"),
        ("insufficient balance in account", 400, "no credit"),
        ("your message was flagged by our systems", 400, "content policy"),
    ],
)
def test_a_named_reason_ends_the_round(body: str, code: int, why: str) -> None:
    """A reason a resend cannot change is reported now, not after three sends."""
    assert is_fatal_model_error(body, code), why


@pytest.mark.parametrize(
    "body, code, why",
    [
        ("", 500, "gateway hiccup"),
        ("upstream connect error or disconnect/reset before headers", 502, "mesh hiccup"),
        ("<!doctype html><title>403 Forbidden</title>", 502, "a proxy page, not a refusal"),
        ("500 Internal Server Error: upstream reported unauthorized", 500,
         "a server error keeps the benefit of the doubt"),
        ("<!doctype html>502 Bad Gateway</html>", 400, "a proxy answering 400"),
        ("Something went wrong", 400, "no reason named"),
        ("Rate limit reached for this deployment. Please slow down.", 429, "rate limit"),
        ("", 0, "a bare exception with no message"),
        ("peer closed connection without sending complete message body", 0, "transport"),
    ],
)
def test_a_reason_a_resend_might_fix_is_replayed(
    body: str, code: int, why: str
) -> None:
    assert not is_fatal_model_error(body, code), why


def test_an_oversized_rejection_belongs_to_the_context_ladder() -> None:
    """Too big for the window is fatal-needing-a-repair, not fatal-ending-the-turn.

    Replaying the identical bytes is the one thing the endpoint just told us will
    not work, so the predicate refuses the backoff replay and the caller's own
    context handler runs the remedies instead.
    """
    body = (
        '{"error": {"type": "invalid_request_error", "message": "request exceeds the '
        'context window: prompt (131218 tokens) leaves no room to answer in the '
        'context (131072); requests are never truncated"}}'
    )
    assert not is_fatal_model_error(body, 400)
    assert not is_fatal_model_error("payload too large", 413)


def test_an_unexpected_error_shape_is_replayed() -> None:
    """A provider payload we cannot parse gets the same treatment as a dead socket."""
    class _UnexpectedShape(ValueError):
        pass

    assert _is_retryable_model_error(_UnexpectedShape("provider response missing 'choices'"))
    assert _is_retryable_model_error(RuntimeError("NoneType has no attribute 'text'"))
    # ... unless its own text names a fatal reason.
    assert not _is_retryable_model_error(_UnexpectedShape("model not found: gpt-9"))


# ---------------------------------------------------------------------------
# Behaviour through infer_stream
# ---------------------------------------------------------------------------


class _Endpoint:
    """Counts sends; raises or answers per the script."""

    def __init__(
        self,
        failures: Optional[dict[int, Exception]] = None,
        *,
        always_fail: Optional[Callable[[int], Exception]] = None,
        answers: Optional[list[bytes]] = None,
    ) -> None:
        self.failures = dict(failures or {})
        self.always_fail = always_fail
        self.answers = answers or [_content_sse("final answer")]
        self.bodies: list[dict] = []

    def __call__(self, req: Any, **kwargs: Any):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        sent = len(self.bodies)
        exc = self.failures.get(sent)
        if exc is None and self.always_fail is not None:
            exc = self.always_fail(sent)
        if exc is not None:
            raise exc
        return _stream_response(
            self.answers[sent - 1] if sent <= len(self.answers) else self.answers[-1]
        )


def _registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(
        ModelConfig(
            model_id="retry-policy-model",
            api_base="http://localhost:9999",
            model_name="t",
            api_protocol="openai",
            max_context=32000,
        )
    )
    return registry


def _run(
    endpoint: _Endpoint,
    *,
    retries: str = "2",
    overflow_hook: Callable | None = None,
) -> list[Message]:
    runtime = Runtime(model_registry=_registry(), tool_registry=ToolRegistry())
    request = InferenceRequest(
        model_id="retry-policy-model", text="hi", stream=True, max_tool_rounds=3,
    )
    env = {"MODEL_API_MAX_RETRIES": retries, "MODEL_API_RETRY_DELAY": "0"}
    with patch.dict("os.environ", env), patch("urllib.request.urlopen", side_effect=endpoint):
        return list(runtime.infer_stream(request, on_context_overflow=overflow_hook))


def test_an_unexplained_400_is_replayed() -> None:
    """A body we cannot read a reason from is treated as transient, not final."""
    endpoint = _Endpoint(always_fail=lambda n: _http_error(400, "Bad Request", "proxy hiccup"))
    messages = _run(endpoint)
    assert len(endpoint.bodies) == 3, "one send plus MODEL_API_MAX_RETRIES replays"
    errors = [m.content for m in messages if (m.content or "").startswith("Error")]
    assert len(errors) == 1, "the failure is reported once, after the last attempt"
    assert "HTTP 400" in errors[0]
    assert "not retried" not in errors[0], "it was replayed, so it says no such thing"


def test_a_rejected_credential_costs_one_send() -> None:
    """No backoff loop for a reason that will not change: the round ends now."""
    endpoint = _Endpoint(
        always_fail=lambda n: _http_error(
            401, "Unauthorized", '{"error":{"message":"Incorrect API key provided"}}'
        )
    )
    messages = _run(endpoint)
    assert len(endpoint.bodies) == 1
    errors = [m.content for m in messages if (m.content or "").startswith("Error")]
    assert len(errors) == 1 and "401" in errors[0]
    assert "not retried" in errors[0], "says why the backoff queue was skipped"


def test_a_rate_limit_backs_off_but_an_empty_balance_does_not() -> None:
    """The same 429 status, two different reasons -- two different policies."""
    rate_limited = _Endpoint(
        always_fail=lambda n: _http_error(429, "Too Many Requests", "Rate limit reached, slow down")
    )
    _run(rate_limited)
    assert len(rate_limited.bodies) == 3, "rate limit: back off and come back"

    broke = _Endpoint(
        always_fail=lambda n: _http_error(
            429, "Too Many Requests",
            "You exceeded your current quota, please check your plan and billing details",
        )
    )
    _run(broke)
    assert len(broke.bodies) == 1, "no credit: resending buys nothing"


def test_a_payload_we_cannot_parse_is_replayed() -> None:
    """An exception the retry allowlist never imagined is a model-side failure too."""
    endpoint = _Endpoint(
        always_fail=lambda n: ValueError("provider response missing 'choices'"),
    )
    messages = _run(endpoint)
    assert len(endpoint.bodies) == 3
    errors = [m.content for m in messages if (m.content or "").startswith("Error")]
    assert errors and "choices" in errors[0]


def test_an_empty_stream_says_how_often_it_was_retried() -> None:
    """The empty-response turn reports the backoff that already happened."""
    empty = lambda: io.BytesIO(b"data: [DONE]\n\n")  # a fresh body per send
    endpoint = _Endpoint(answers=[empty(), empty(), empty()])
    messages = _run(endpoint)
    assert len(endpoint.bodies) == 3, "an empty round is replayed like any other failure"
    errors = [m.content for m in messages if (m.content or "").startswith("Error")]
    assert errors == ["Error: model API returned an empty response after 3 attempt(s)."]


def test_an_oversized_rejection_skips_the_backoff_queue() -> None:
    """Transport retries are not the remedy for a too-big payload; the ladder is.

    With ``MODEL_API_MAX_RETRIES=0`` the round must still be repaired: the
    rejection goes to the context remedies, and the second send carries the
    smaller context instead of the same bytes again.
    """
    body = (
        '{"error": {"message": "request exceeds the context window: prompt (131218 '
        'tokens) leaves no room to answer in the context (131072)"}}'
    )
    endpoint = _Endpoint(
        failures={1: _http_error(400, "Bad Request", body)},
        answers=[_content_sse("answer after repair")],
    )

    calls: list[tuple] = []

    def _shrink(messages: list, input_budget: int, estimator: Any, max_context: int):
        calls.append((len(messages), input_budget, max_context))
        kept = list(messages)
        if len(kept) > 1:
            kept = kept[-1:]
        return kept, 1000

    messages = _run(endpoint, retries="0", overflow_hook=_shrink)
    assert calls, "the remedies ran instead of a resend of the same payload"
    assert len(endpoint.bodies) == 2, "one rejected send, one repaired send"
    contents = [m.content for m in messages if m.role == "assistant"]
    assert "answer after repair" in contents
    assert not [c for c in contents if (c or "").startswith("Error")]


# ---------------------------------------------------------------------------
# The non-streaming entry point follows the same rule
# ---------------------------------------------------------------------------


def _run_infer(endpoint: _Endpoint, retries: str = "2") -> Any:
    runtime = Runtime(model_registry=_registry(), tool_registry=ToolRegistry())
    env = {"MODEL_API_MAX_RETRIES": retries, "MODEL_API_RETRY_DELAY": "0"}
    with patch.dict("os.environ", env), patch("urllib.request.urlopen", side_effect=endpoint):
        return runtime.infer(
            InferenceRequest(model_id="retry-policy-model", text="hi", stream=False)
        )


def test_infer_replays_an_unexplained_400() -> None:
    endpoint = _Endpoint(always_fail=lambda n: _http_error(400, "Bad Request", "gateway hiccup"))
    result = _run_infer(endpoint)
    assert len(endpoint.bodies) == 3
    assert result.success is False


def test_infer_does_not_replay_an_unknown_model() -> None:
    endpoint = _Endpoint(always_fail=lambda n: _http_error(404, "Not Found", "model not found"))
    result = _run_infer(endpoint)
    assert len(endpoint.bodies) == 1
    assert result.success is False and result.error_code == "404"
    assert "not retried" in (result.error or "")

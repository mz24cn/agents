"""The pre-send context guard measures what the provider actually counts.

The failing request was measured by the guard at 181 983 prompt tokens against an
188 416 budget -- 3.4 % of headroom -- and the provider counted 471 777, i.e.
2.59x more, answering

    request exceeds the context window: prompt (471777 tokens) leaves no room to
    answer in the context (196608); requests are never truncated

The gap was specific, not random: that message was 99.7 % ASCII, and 78 145 of
its escapes were ``\\uXXXX`` sequences (65.9 % of its characters) written by
``json.dumps`` re-encoding a shell tool's Chinese output.  Charged at four
characters per token, six characters of escape read as ~1.5 tokens; the real
tokenizer spends 3-6 on each.

Two changes: charge literal escapes for what they cost, and fold each model's
measured ``prompt_tokens`` back into its estimate.
"""

from __future__ import annotations

import io

import pytest

from runtime.common import (
    clear_prompt_token_calibration,
    estimate_chat_prompt_tokens,
    estimate_chat_prompt_tokens_calibrated,
    estimate_llm_tokens,
    get_prompt_token_calibration,
    record_prompt_token_calibration,
    set_prompt_token_calibration,
)
from runtime.models import InferenceRequest, Message, ModelConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


@pytest.fixture(autouse=True)
def _clean_calibration():
    clear_prompt_token_calibration()
    yield
    clear_prompt_token_calibration()


# ----------------------------------------------------------------------
# Escapes are not Latin prose
# ----------------------------------------------------------------------

def test_plain_text_estimate_is_unchanged():
    assert estimate_llm_tokens("") == 0
    assert estimate_llm_tokens("hello world") == 3        # 11 chars / 4
    assert estimate_llm_tokens("a" * 12) == 3
    assert estimate_llm_tokens("开发" * 5) == 10          # CJK: one token each


def test_unicode_escapes_cost_more_than_their_characters_suggest():
    escaped = "\\u5f00" * 10                             # 60 ASCII characters
    assert estimate_llm_tokens(escaped) == 40            # 4 tokens per escape
    assert estimate_llm_tokens(escaped) > estimate_llm_tokens("x" * 60)
    # The same payload decoded: Chinese characters count one token each, and the
    # escaped form is what the model was actually shown.
    assert estimate_llm_tokens("开发" * 5) == 10


def test_two_character_escapes_count_one_token():
    assert estimate_llm_tokens("\\n" * 10) == 10
    assert estimate_llm_tokens('\\"' * 10) == 10
    assert estimate_llm_tokens("\\\\" * 10) == 10


def test_a_double_encoded_tool_payload_measures_in_the_right_ballpark():
    # What exec_shell actually puts into a conversation: JSON of JSON, every
    # Chinese character rewritten as an escape.
    payload = '{"stdout": "' + "\\u5f00\\u53d1" * 400 + '"}'
    escapes = 800
    tokens = estimate_llm_tokens(payload)
    # A real BPE tokenizer spends 3-6 tokens on one \uXXXX escape.
    assert tokens >= escapes * 3, "escapes were priced as ordinary ASCII"
    assert tokens <= escapes * 5 + 32, "the estimate overshot the tokenizer"


# ----------------------------------------------------------------------
# Calibration: measured prompt_tokens become the yardstick
# ----------------------------------------------------------------------

def test_an_unmeasured_model_is_unadjusted():
    assert get_prompt_token_calibration("never-seen") == 1.0
    assert estimate_chat_prompt_tokens_calibrated(
        [{"role": "user", "content": "x" * 400}], None, "never-seen",
    ) == estimate_chat_prompt_tokens([{"role": "user", "content": "x" * 400}], None)


def test_a_measurement_reveals_the_gap_and_a_second_one_smooths_it():
    factor = record_prompt_token_calibration("qwen-iq3", 181_983, 471_777)
    assert factor == pytest.approx(0.6 + 0.4 * (471_777 / 181_983), rel=1e-6)
    assert get_prompt_token_calibration("qwen-iq3") == factor

    second = record_prompt_token_calibration("qwen-iq3", 100_000, 260_000)
    assert second == pytest.approx(factor * 0.6 + 0.4 * 2.6, rel=1e-6)
    assert second != factor


def test_implausible_samples_are_refused_rather_than_believed():
    assert record_prompt_token_calibration("m", 0, 500) is None
    assert record_prompt_token_calibration("m", 500, 0) is None
    assert record_prompt_token_calibration("m", 500, None) is None
    assert record_prompt_token_calibration("", 500, 900) is None
    # A 20x ratio means the estimate was taken over a different message list;
    # folding it in would poison every later decision.
    assert record_prompt_token_calibration("m", 100, 100_000) is None
    assert get_prompt_token_calibration("m") == 1.0


def test_the_factor_stays_inside_its_bounds():
    for _ in range(12):
        factor = record_prompt_token_calibration("m", 100, 900)
    assert factor <= 8.0
    for _ in range(12):
        factor = record_prompt_token_calibration("m", 100, 1)
    assert factor >= 0.5


def test_calibration_is_per_model():
    record_prompt_token_calibration("a", 100, 400)
    assert get_prompt_token_calibration("b") == 1.0


def test_the_table_stays_bounded(tmp_path):
    for index in range(80):
        record_prompt_token_calibration(f"model-{index}", 100, 200)
    measured = sum(
        1 for index in range(80)
        if get_prompt_token_calibration(f"model-{index}") != 1.0
    )
    assert measured <= 64


def test_calibrated_estimate_scales_the_whole_request():
    messages = [{"role": "user", "content": "x" * 400}]
    set_prompt_token_calibration("m", 2.0)

    calibrated = estimate_chat_prompt_tokens_calibrated(messages, None, "m")

    assert calibrated == 2 * estimate_chat_prompt_tokens(messages, None)


def test_a_low_factor_does_not_shorten_the_capped_walk():
    """factor < 1 must not let the capped walk stop before the real verdict."""
    messages = [{"role": "user", "content": "x" * 4000}]
    full = estimate_chat_prompt_tokens(messages, None)
    set_prompt_token_calibration("m", 0.6)
    cap = full // 2

    calibrated = estimate_chat_prompt_tokens_calibrated(messages, None, "m", cap)

    # Stopping the walk at *cap* and then scaling would have reported ~cap*0.6
    # and pronounced this request a fit.  The walk runs to cap/factor instead.
    assert calibrated > cap, "the request was pronounced a fit on a truncated walk"


# ----------------------------------------------------------------------
# The guard shares one yardstick with the compression hook
# ----------------------------------------------------------------------

def _runtime() -> Runtime:
    return Runtime(ModelRegistry(), ToolRegistry())


def test_guard_and_hook_use_the_calibrated_number():
    runtime = _runtime()
    config = ModelConfig(model_id="qwen-iq3", api_base="http://x",
                         model_name="qwen", max_context=196_608)
    # Sits under the budget for the raw estimate and well over it once this
    # model's measured 2.6x is applied -- exactly the shape that got rejected.
    messages = [Message(role="user", content="x" * 400_000)]
    seen: dict = {}

    def _hook(msgs, budget, estimate, window):
        seen["estimate"] = estimate
        seen["budget"] = budget
        return None, estimate(msgs)          # "I could not make it fit"

    set_prompt_token_calibration("qwen-iq3", 2.6)
    raw = estimate_chat_prompt_tokens(messages, None)

    sent, error, estimated = runtime._guard_context_before_send(
        messages, None, config, _hook,
    )

    assert seen["estimate"](messages) == pytest.approx(raw * 2.6, rel=0.01)
    assert error, "a request the provider will reject was let through"
    assert estimated > seen["budget"]
    assert "Error: context too large to send" in error


def test_a_request_that_fits_reports_its_size_and_sends():
    runtime = _runtime()
    config = ModelConfig(model_id="small", api_base="http://x",
                         model_name="m", max_context=196_608)
    messages = [Message(role="user", content="短")]

    sent, error, estimated = runtime._guard_context_before_send(
        messages, None, config, None,
    )

    assert error == ""
    assert estimated > 0


def test_unknown_window_is_still_not_measured():
    runtime = _runtime()
    config = ModelConfig(model_id="unknown", api_base="http://x",
                         model_name="m", max_context=0)

    sent, error, estimated = runtime._guard_context_before_send(
        [Message(role="user", content="x" * 100_000)], None, config, None,
    )

    assert (error, estimated) == ("", 0)


# ----------------------------------------------------------------------
# The measurement comes back from the provider
# ----------------------------------------------------------------------

class _Response:
    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def __iter__(self):
        return iter(self._buf.readlines())

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_a_provider_report_teaches_the_next_request(monkeypatch):
    registry = ModelRegistry()
    registry.register(ModelConfig(model_id="iq3", api_base="http://x",
                                  model_name="iq3", max_context=196_608))
    runtime = Runtime(registry, ToolRegistry())
    assert get_prompt_token_calibration("iq3") == 1.0

    sse = (
        b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{}}],'
        b'"usage":{"prompt_tokens":471777,"completion_tokens":3}}\n\n'
        b'data: [DONE]\n\n'
    )
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, **kwargs: _Response(sse))

    # A prompt the guard would have to judge: the estimate lands under the
    # budget, the provider's count lands far over it.  (A two-token prompt
    # against a 471 777 report is not a usable measurement and is refused.)
    list(runtime.infer_stream(InferenceRequest(model_id="iq3",
                                               text="x" * 400_000)))

    factor = get_prompt_token_calibration("iq3")
    assert factor > 1.5, "the provider's own count was thrown away"
    assert estimate_chat_prompt_tokens_calibrated(
        [{"role": "user", "content": "x" * 4000}], None, "iq3",
    ) > estimate_chat_prompt_tokens([{"role": "user", "content": "x" * 4000}], None)


def test_an_echoed_estimate_is_not_counted_as_a_measurement(monkeypatch):
    """Protocols without provider usage fall back to our own number; folding
    that back in would compound the factor on every round."""
    registry = ModelRegistry()
    registry.register(ModelConfig(model_id="quiet", api_base="http://x",
                                  model_name="quiet", max_context=196_608))
    runtime = Runtime(registry, ToolRegistry())
    set_prompt_token_calibration("quiet", 2.5)

    sse = (
        b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, **kwargs: _Response(sse))

    list(runtime.infer_stream(InferenceRequest(model_id="quiet", text="hi")))

    assert get_prompt_token_calibration("quiet") == pytest.approx(2.5)

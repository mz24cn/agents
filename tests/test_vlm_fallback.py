"""Tests for the VLM image fallback in Runtime message normalization.

When the target model is not VLM-capable (its labels lack "vlm"), attached
images are transcribed to text through the built-in read_image tool before
the request is sent, mirroring the text-file attachment format:

    [Image file attached: <labels>]
    ```
    <transcription>
    ```
"""

import json
from unittest.mock import MagicMock, patch

from runtime.builtin_tools_misc import READ_IMAGE_TOOL_CONFIG, _make_read_image_fn
from runtime.models import InferenceRequest, Message, ModelConfig
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime

# A base64-looking payload (>= 1024 chars, no path separators) that needs no
# filesystem access: is_likely_base64() -> True, convert_image_to_base64()
# returns it as-is.
FAKE_IMAGE_B64 = "A" * 1200


def _make_openai_text_response(content: str) -> bytes:
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode("utf-8")


def _make_resp(data: bytes):
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_runtime(main_model_labels=None, read_image_labels=("vlm", "read-image")):
    """Build a Runtime with a main model, an optional read-image VLM model,
    and the read_image tool registered with a runtime-aware callable."""
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="main-model",
        api_base="http://localhost:9999",
        model_name="main",
        api_protocol="openai",
        labels=main_model_labels or [],
    ))
    if read_image_labels is not None:
        model_registry.register(ModelConfig(
            model_id="read-image",
            api_base="http://localhost:9998",
            model_name="vlm",
            api_protocol="openai",
            labels=list(read_image_labels),
        ))
    tool_registry = ToolRegistry()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    tool_registry.register(READ_IMAGE_TOOL_CONFIG, callable_fn=_make_read_image_fn(runtime))
    return runtime


def _run_infer(runtime, content, images):
    """Run a single-round infer against the main model, capturing every
    HTTP request body that hits urlopen."""
    captured = []

    def mock_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        captured.append((request.full_url, body))
        if "9998" in request.full_url:  # read-image VLM model
            return _make_resp(_make_openai_text_response("图片中有一只猫，上面写着 HELLO。"))
        return _make_resp(_make_openai_text_response("我看到了图片描述。"))

    request = InferenceRequest(
        model_id="main-model",
        messages=[Message(role="user", content=content, images=images)],
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)
    return result, captured


def test_non_vlm_model_falls_back_to_read_image():
    """A model without the 'vlm' label must have images transcribed by the
    read_image tool; the final request must be plain text."""
    runtime = _make_runtime()
    result, captured = _run_infer(
        runtime,
        content="这是什么图片？ [Image file attached: /tmp/a.png]",
        images=[FAKE_IMAGE_B64],
    )

    assert result.success
    assert len(captured) == 2, f"expected VLM + main calls, got {len(captured)}"

    vlm_url, vlm_body = captured[0]
    main_url, main_body = captured[1]
    assert "9998" in vlm_url  # VLM call went to the read-image model

    # The VLM request carries the user's ORIGINAL query (placeholder stripped)
    # and the image in multimodal format.
    vlm_user = vlm_body["messages"][0]
    assert vlm_user["content"][0]["type"] == "text"
    assert vlm_user["content"][0]["text"] == "这是什么图片？"
    assert any(p.get("type") == "image_url" for p in vlm_user["content"])

    # The main model request is plain text: no image parts, transcription block
    # prepended in the text-file attachment format.
    assert "9999" in main_url
    main_user = main_body["messages"][0]
    assert isinstance(main_user["content"], str)
    assert main_user["content"].startswith("[Image file attached: (inline image)]\n```\n图片中有一只猫")
    assert "这是什么图片？" in main_user["content"]


def test_vlm_model_keeps_images():
    """A model with the 'vlm' label must keep multimodal encoding — no
    read_image round-trip at all."""
    runtime = _make_runtime(main_model_labels=["vlm"])
    result, captured = _run_infer(runtime, content="看图", images=[FAKE_IMAGE_B64])

    assert result.success
    assert len(captured) == 1, f"expected a single call, got {len(captured)}"
    user = captured[0][1]["messages"][0]
    assert isinstance(user["content"], list)
    assert any(p.get("type") == "image_url" for p in user["content"])


def test_multiple_images_use_single_read_image_call():
    """Multiple images in one query must be handled by a single read_image
    call (so the VLM can compare them), with one combined block for the
    non-VLM model."""
    runtime = _make_runtime()
    captured = []

    def mock_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        captured.append((request.full_url, body))
        if "9998" in request.full_url:
            return _make_resp(_make_openai_text_response("左图是苹果，右图是香蕉，两者都是水果。"))
        return _make_resp(_make_openai_text_response("ok"))

    request = InferenceRequest(
        model_id="main-model",
        messages=[Message(
            role="user",
            content="对比这两张图 [Image file attached: /tmp/a.png] [Image file attached: /tmp/b.png]",
            images=[FAKE_IMAGE_B64 + "A", FAKE_IMAGE_B64 + "B"],
        )],
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = runtime.infer(request)

    assert result.success
    assert len(captured) == 2  # one VLM call + one main call

    # VLM prompt keeps the user's comparison query and receives BOTH images.
    vlm_body = captured[0][1]
    assert vlm_body["messages"][0]["content"][0]["text"] == "对比这两张图"
    image_url_parts = [p for p in vlm_body["messages"][0]["content"] if p.get("type") == "image_url"]
    assert len(image_url_parts) == 2

    # Main model gets one combined transcription block listing both labels.
    main_body = captured[1][1]
    main_user = main_body["messages"][0]["content"]
    assert isinstance(main_user, str)
    assert main_user.startswith("[Image file attached: (inline image), (inline image)]\n```\n左图是苹果，右图是香蕉")
    # Only the transcription block header at the top; inline path lines remain.
    assert main_user.count("[Image file attached:") == 3  # 1 block header + 2 inline path lines


def test_missing_read_image_model_produces_actionable_error():
    """When no read-image VLM model is registered, the fallback must surface
    the actionable guidance from read_image instead of failing silently."""
    runtime = _make_runtime(read_image_labels=None)
    result, captured = _run_infer(runtime, content="看看这张图", images=[FAKE_IMAGE_B64])

    assert result.success  # main model still responds
    assert len(captured) == 1  # no VLM call was possible
    user = captured[0][1]["messages"][0]
    assert isinstance(user["content"], str)
    assert "read-image" in user["content"]
    assert "Error:" in user["content"]


def test_recursion_guard_when_read_image_model_lacks_vlm_label():
    """A read-image model missing the 'vlm' label must not cause infinite
    recursion; the fallback skips the inner transcription and completes."""
    runtime = _make_runtime(read_image_labels=["read-image"])
    result, captured = _run_infer(
        runtime,
        content="这是什么？ [Image file attached: /tmp/a.png]",
        images=[FAKE_IMAGE_B64],
    )

    assert result.success
    assert len(captured) == 2  # inner (read-image) + outer (main) — no loop
    main_user = captured[1][1]["messages"][0]
    assert isinstance(main_user["content"], str)
    assert "图片中有一只猫" in main_user["content"]

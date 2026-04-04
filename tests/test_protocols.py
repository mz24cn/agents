# Feature: composable-agent-runtime, Property 3: OpenAI 协议多模态请求构造
"""Property-based tests for protocol adapters."""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.models import Message, ModelConfig
from runtime.protocols import OpenAIProtocol


# --- Hypothesis strategies ---

# Strategy for base64-like image data (non-empty strings without data: prefix)
raw_base64_image_st = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="),
    min_size=4,
    max_size=100,
)

# Strategy for a Message with non-empty images list
message_with_images_st = st.builds(
    Message,
    role=st.just("user"),
    content=st.text(min_size=1, max_size=100),
    images=st.lists(raw_base64_image_st, min_size=1, max_size=5),
)

# Strategy for a list of messages where at least one has images
messages_with_images_st = st.lists(
    message_with_images_st,
    min_size=1,
    max_size=5,
)

# A fixed ModelConfig for building requests
model_config_st = st.builds(
    ModelConfig,
    model_id=st.just("test-model"),
    api_base=st.just("http://localhost:11434"),
    model_name=st.just("qwen3.5:9b"),
    api_key=st.just(""),
    model_type=st.just("vlm"),
    api_protocol=st.just("openai"),
)


# --- Property test ---

# **Validates: Requirements 1.3**
@given(messages=messages_with_images_st, config=model_config_st)
@settings(max_examples=100)
def test_openai_multimodal_content_is_array_with_image_url(
    messages: list, config: ModelConfig
) -> None:
    """For any Message list with images, OpenAIProtocol.build_request() should produce
    content as an array containing image_url objects with correct structure."""
    proto = OpenAIProtocol()
    _, _, body_bytes = proto.build_request(config, messages)
    body = json.loads(body_bytes)

    api_messages = body["messages"]
    assert len(api_messages) == len(messages)

    for i, (api_msg, orig_msg) in enumerate(zip(api_messages, messages)):
        if orig_msg.images:
            # content must be an array for messages with images
            content = api_msg["content"]
            assert isinstance(content, list), (
                f"Message {i}: content should be a list, got {type(content).__name__}"
            )

            # Collect all image_url parts
            image_url_parts = [
                part for part in content if part.get("type") == "image_url"
            ]

            # There should be exactly as many image_url objects as images
            assert len(image_url_parts) == len(orig_msg.images), (
                f"Message {i}: expected {len(orig_msg.images)} image_url parts, "
                f"got {len(image_url_parts)}"
            )

            # Each image_url object must have the correct structure
            for j, part in enumerate(image_url_parts):
                assert "image_url" in part, (
                    f"Message {i}, image {j}: missing 'image_url' key"
                )
                assert "url" in part["image_url"], (
                    f"Message {i}, image {j}: missing 'url' in image_url"
                )
                url = part["image_url"]["url"]
                assert url.startswith("data:image"), (
                    f"Message {i}, image {j}: url should start with 'data:image', "
                    f"got '{url[:30]}...'"
                )


# Feature: composable-agent-runtime, Property 4: Ollama 协议多模态请求构造

from runtime.protocols import OllamaProtocol


# Strategy: reuse existing model_config_st but with ollama protocol
ollama_model_config_st = st.builds(
    ModelConfig,
    model_id=st.just("test-ollama-model"),
    api_base=st.just("http://localhost:11434"),
    model_name=st.just("qwen3.5:9b"),
    api_key=st.just(""),
    model_type=st.just("vlm"),
    api_protocol=st.just("ollama"),
)


# **Validates: Requirements 1.4**
@given(messages=messages_with_images_st, config=ollama_model_config_st)
@settings(max_examples=100)
def test_ollama_multimodal_images_at_same_level_as_content(
    messages: list, config: ModelConfig
) -> None:
    """For any Message list with images, OllamaProtocol.build_request() should produce
    messages where images is a list of raw base64 strings at the same level as content,
    and content remains a plain text string (not an array)."""
    proto = OllamaProtocol()
    _, _, body_bytes = proto.build_request(config, messages)
    body = json.loads(body_bytes)

    api_messages = body["messages"]
    assert len(api_messages) == len(messages)

    for i, (api_msg, orig_msg) in enumerate(zip(api_messages, messages)):
        if orig_msg.images:
            # content must be a plain text string, NOT an array
            content = api_msg["content"]
            assert isinstance(content, str), (
                f"Message {i}: content should be a str, got {type(content).__name__}"
            )

            # images field must exist at the same level as content
            assert "images" in api_msg, (
                f"Message {i}: 'images' field missing from message"
            )

            images = api_msg["images"]
            assert isinstance(images, list), (
                f"Message {i}: images should be a list, got {type(images).__name__}"
            )

            # There should be exactly as many images as the original message
            assert len(images) == len(orig_msg.images), (
                f"Message {i}: expected {len(orig_msg.images)} images, "
                f"got {len(images)}"
            )

            # Each image must be a raw base64 string (no data: prefix)
            for j, img in enumerate(images):
                assert isinstance(img, str), (
                    f"Message {i}, image {j}: image should be a str, "
                    f"got {type(img).__name__}"
                )
                assert not img.startswith("data:"), (
                    f"Message {i}, image {j}: image should be raw base64 "
                    f"without data: prefix, got '{img[:30]}...'"
                )


# Feature: composable-agent-runtime, Property 6: 协议响应解析

# --- Hypothesis strategies for response data ---

# Strategy for random assistant content (non-empty strings)
assistant_content_st = st.text(min_size=0, max_size=200)

# Strategy for valid OpenAI-format response JSON bytes
openai_response_st = st.builds(
    lambda content, model_name: json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    ).encode("utf-8"),
    content=assistant_content_st,
    model_name=st.text(min_size=1, max_size=30, alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-.")),
)

# Strategy for valid Ollama-format response JSON bytes
ollama_response_st = st.builds(
    lambda content, model_name: json.dumps(
        {
            "model": model_name,
            "message": {
                "role": "assistant",
                "content": content,
            },
            "done": True,
        }
    ).encode("utf-8"),
    content=assistant_content_st,
    model_name=st.text(min_size=1, max_size=30, alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-.")),
)


# **Validates: Requirements 1.9**
@given(response_data=openai_response_st)
@settings(max_examples=100)
def test_openai_parse_response_produces_assistant_message(response_data: bytes) -> None:
    """For any valid OpenAI-format response data, parse_response() should produce
    at least one Message with role='assistant' and content as a non-None string."""
    proto = OpenAIProtocol()
    messages = proto.parse_response(response_data, stream=False)

    # Should produce at least one message
    assert len(messages) >= 1, "parse_response should return at least one Message"

    # Find assistant messages
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert len(assistant_msgs) >= 1, (
        "parse_response should produce at least one Message with role='assistant'"
    )

    # Each assistant message should have non-None string content
    for msg in assistant_msgs:
        assert msg.content is not None, "assistant Message content should not be None"
        assert isinstance(msg.content, str), (
            f"assistant Message content should be a str, got {type(msg.content).__name__}"
        )


# **Validates: Requirements 1.9**
@given(response_data=ollama_response_st)
@settings(max_examples=100)
def test_ollama_parse_response_produces_assistant_message(response_data: bytes) -> None:
    """For any valid Ollama-format response data, parse_response() should produce
    at least one Message with role='assistant' and content as a non-None string."""
    proto = OllamaProtocol()
    messages = proto.parse_response(response_data, stream=False)

    # Should produce at least one message
    assert len(messages) >= 1, "parse_response should return at least one Message"

    # Find assistant messages
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert len(assistant_msgs) >= 1, (
        "parse_response should produce at least one Message with role='assistant'"
    )

    # Each assistant message should have non-None string content
    for msg in assistant_msgs:
        assert msg.content is not None, "assistant Message content should not be None"
        assert isinstance(msg.content, str), (
            f"assistant Message content should be a str, got {type(msg.content).__name__}"
        )

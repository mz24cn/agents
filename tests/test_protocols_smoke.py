"""Smoke tests for OpenAIProtocol - verifying task 4.1 requirements."""

import json
from runtime.models import Message, ModelConfig, ToolConfig
from runtime.protocols import OpenAIProtocol


def test_basic_text_message():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_key="sk-test"
    )
    msgs = [Message(role="user", content="Hello")]
    url, headers, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    assert url == "http://localhost:11434/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert body["model"] == "qwen3.5:9b"
    assert body["messages"][0]["content"] == "Hello"
    assert body["stream"] is False


def test_multimodal_image_url_encoding():
    """Req 1.3: multimodal content encoded as image_url objects in content array."""
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    msgs = [Message(role="user", content="Describe this", images=["abc123base64"])]
    url, headers, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Describe this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,abc123base64"


def test_multimodal_multiple_images():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    msgs = [Message(role="user", content="Compare", images=["img1", "img2"])]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    content = body["messages"][0]["content"]
    assert len(content) == 3  # 1 text + 2 images
    assert content[1]["type"] == "image_url"
    assert content[2]["type"] == "image_url"


def test_image_with_data_uri_prefix():
    """Images already having data: prefix should not be double-prefixed."""
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    img = "data:image/png;base64,abc123"
    msgs = [Message(role="user", content="Look", images=[img])]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    content = body["messages"][0]["content"]
    assert content[1]["image_url"]["url"] == img


def test_tools_encoding():
    """Req 1.7: tools encoded as tools field in request."""
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    tool = ToolConfig(
        tool_id="t1", tool_type="function", name="get_weather",
        description="Get weather",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    msgs = [Message(role="user", content="Weather?")]
    _, _, body_bytes = proto.build_request(config, msgs, tools=[tool])
    body = json.loads(body_bytes)
    assert "tools" in body
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "get_weather"
    assert body["tools"][0]["function"]["description"] == "Get weather"
    assert body["tools"][0]["function"]["parameters"] == tool.parameters


def test_no_tools_when_none():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    msgs = [Message(role="user", content="Hi")]
    _, _, body_bytes = proto.build_request(config, msgs, tools=None)
    body = json.loads(body_bytes)
    assert "tools" not in body


def test_generate_params_merged():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b",
        generate_params={"temperature": 0.7, "top_p": 0.9},
    )
    msgs = [Message(role="user", content="Hi")]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9


def test_parse_non_stream_response():
    """Req 1.9: parse non-streaming response."""
    proto = OpenAIProtocol()
    resp = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "Hello there"}}]
    }).encode()
    result = proto.parse_response(resp, stream=False)
    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].content == "Hello there"
    assert result[0].tool_calls is None


def test_parse_non_stream_with_tool_calls():
    proto = OpenAIProtocol()
    resp = json.dumps({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Beijing"}',
                    },
                }],
            }
        }]
    }).encode()
    result = proto.parse_response(resp, stream=False)
    assert result[0].tool_calls is not None
    assert result[0].tool_calls[0]["name"] == "get_weather"
    assert '"city"' in result[0].tool_calls[0]["arguments"]


def test_parse_stream_response():
    """Req 1.9: parse streaming SSE response."""
    proto = OpenAIProtocol()
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"Hi "}}]}',
        'data: {"choices":[{"delta":{"content":"there"}}]}',
        'data: [DONE]',
    ]
    sse = "\n".join(lines) + "\n"
    result = proto.parse_response(sse.encode(), stream=True)
    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].content == "Hi there"


def test_parse_stream_with_tool_calls():
    proto = OpenAIProtocol()
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"get_weather","arguments":"{\\\"city\\\""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":":\\\"BJ\\\"}"}}]}}]}',
        'data: [DONE]',
    ]
    sse = "\n".join(lines) + "\n"
    result = proto.parse_response(sse.encode(), stream=True)
    assert result[0].tool_calls is not None
    assert result[0].tool_calls[0]["name"] == "get_weather"


def test_parse_empty_choices():
    proto = OpenAIProtocol()
    resp = json.dumps({"choices": []}).encode()
    result = proto.parse_response(resp, stream=False)
    assert result == []


def test_tool_calls_message_encoding():
    """Assistant messages with tool_calls should encode as tool_calls."""
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    msgs = [
        Message(role="assistant", content="", tool_calls=[{
            "name": "get_weather", "arguments": '{"city":"BJ"}'
        }])
    ]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    msg = body["messages"][0]
    assert "tool_calls" in msg
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"


def test_no_api_key_no_auth_header():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_key=""
    )
    msgs = [Message(role="user", content="Hi")]
    _, headers, _ = proto.build_request(config, msgs)
    assert "Authorization" not in headers


def test_api_base_trailing_slash():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434/",
        model_name="qwen3.5:9b"
    )
    msgs = [Message(role="user", content="Hi")]
    url, _, _ = proto.build_request(config, msgs)
    assert url == "http://localhost:11434/v1/chat/completions"


def test_stream_flag_in_body():
    proto = OpenAIProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b"
    )
    msgs = [Message(role="user", content="Hi")]
    _, _, body_bytes = proto.build_request(config, msgs, stream=True)
    body = json.loads(body_bytes)
    assert body["stream"] is True


# ---- OllamaProtocol tests (Task 4.2) ----

from runtime.protocols import OllamaProtocol, PROTOCOL_MAP


def test_ollama_basic_text_message():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Hello")]
    url, headers, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    assert url == "http://localhost:11434/api/chat"
    assert body["model"] == "qwen3.5:9b"
    assert body["messages"][0]["content"] == "Hello"
    assert body["stream"] is False


def test_ollama_url_trailing_slash():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434/",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Hi")]
    url, _, _ = proto.build_request(config, msgs)
    assert url == "http://localhost:11434/api/chat"


def test_ollama_multimodal_images_at_content_level():
    """Req 1.4: images field at same level as content, raw base64 without prefix."""
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Describe this", images=["abc123base64"])]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    msg = body["messages"][0]
    # content stays as plain text string
    assert isinstance(msg["content"], str)
    assert msg["content"] == "Describe this"
    # images is a list at the same level as content
    assert "images" in msg
    assert msg["images"] == ["abc123base64"]


def test_ollama_multimodal_strips_data_uri_prefix():
    """Images with data: URI prefix should have the prefix stripped for Ollama."""
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(
        role="user", content="Look",
        images=["data:image/jpeg;base64,abc123"]
    )]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    msg = body["messages"][0]
    assert msg["images"] == ["abc123"]


def test_ollama_multimodal_multiple_images():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Compare", images=["img1", "img2"])]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    msg = body["messages"][0]
    assert isinstance(msg["content"], str)
    assert msg["images"] == ["img1", "img2"]


def test_ollama_no_images_field_when_no_images():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Hello")]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    assert "images" not in body["messages"][0]


def test_ollama_tools_encoding():
    """Req 1.7: tools encoded in Ollama format."""
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    tool = ToolConfig(
        tool_id="t1", tool_type="function", name="get_weather",
        description="Get weather",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    msgs = [Message(role="user", content="Weather?")]
    _, _, body_bytes = proto.build_request(config, msgs, tools=[tool])
    body = json.loads(body_bytes)
    assert "tools" in body
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "get_weather"


def test_ollama_generate_params_as_options():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama",
        generate_params={"temperature": 0.7, "top_p": 0.9},
    )
    msgs = [Message(role="user", content="Hi")]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    assert body["options"]["temperature"] == 0.7
    assert body["options"]["top_p"] == 0.9


def test_ollama_stream_flag():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Hi")]
    _, _, body_bytes = proto.build_request(config, msgs, stream=True)
    body = json.loads(body_bytes)
    assert body["stream"] is True


def test_ollama_parse_non_stream_response():
    """Req 1.9: parse Ollama non-streaming response."""
    proto = OllamaProtocol()
    resp = json.dumps({
        "model": "qwen3.5:9b",
        "message": {"role": "assistant", "content": "Hello there"},
        "done": True,
    }).encode()
    result = proto.parse_response(resp, stream=False)
    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].content == "Hello there"
    assert result[0].tool_calls is None


def test_ollama_parse_non_stream_with_tool_calls():
    proto = OllamaProtocol()
    resp = json.dumps({
        "model": "qwen3.5:9b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "get_weather",
                    "arguments": {"city": "Beijing"},
                },
            }],
        },
        "done": True,
    }).encode()
    result = proto.parse_response(resp, stream=False)
    assert result[0].tool_calls is not None
    assert result[0].tool_calls[0]["name"] == "get_weather"
    assert '"city"' in result[0].tool_calls[0]["arguments"]


def test_ollama_parse_stream_response():
    """Req 1.9: parse Ollama streaming response (newline-delimited JSON)."""
    proto = OllamaProtocol()
    lines = [
        json.dumps({"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "Hi "}, "done": False}),
        json.dumps({"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "there"}, "done": False}),
        json.dumps({"model": "qwen3.5:9b", "message": {"role": "assistant", "content": ""}, "done": True}),
    ]
    data = "\n".join(lines) + "\n"
    result = proto.parse_response(data.encode(), stream=True)
    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].content == "Hi there"


def test_ollama_parse_empty_message():
    proto = OllamaProtocol()
    resp = json.dumps({"model": "qwen3.5:9b", "done": True}).encode()
    result = proto.parse_response(resp, stream=False)
    assert result == []


def test_ollama_tool_calls_message_encoding():
    """Assistant messages with tool_calls should encode as tool_calls."""
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_protocol="ollama"
    )
    msgs = [
        Message(role="assistant", content="", tool_calls=[{
            "name": "get_weather", "arguments": '{"city":"BJ"}'
        }])
    ]
    _, _, body_bytes = proto.build_request(config, msgs)
    body = json.loads(body_bytes)
    msg = body["messages"][0]
    assert "tool_calls" in msg
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"


def test_ollama_api_key_header():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_key="my-key", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Hi")]
    _, headers, _ = proto.build_request(config, msgs)
    assert headers["Authorization"] == "Bearer my-key"


def test_ollama_no_api_key_no_auth_header():
    proto = OllamaProtocol()
    config = ModelConfig(
        model_id="test", api_base="http://localhost:11434",
        model_name="qwen3.5:9b", api_key="", api_protocol="ollama"
    )
    msgs = [Message(role="user", content="Hi")]
    _, headers, _ = proto.build_request(config, msgs)
    assert "Authorization" not in headers


def test_protocol_map_contains_both():
    """PROTOCOL_MAP should map protocol names to adapter classes."""
    assert "openai" in PROTOCOL_MAP
    assert "ollama" in PROTOCOL_MAP
    assert PROTOCOL_MAP["openai"] is OpenAIProtocol
    assert PROTOCOL_MAP["ollama"] is OllamaProtocol


def test_protocol_map_instantiation():
    """Protocol classes from PROTOCOL_MAP should be instantiable."""
    for name, cls in PROTOCOL_MAP.items():
        instance = cls()
        assert isinstance(instance, BaseProtocol)


# Import BaseProtocol for the instantiation test above
from runtime.protocols import BaseProtocol

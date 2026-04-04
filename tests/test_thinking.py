"""Tests for thinking/reasoning output support.

Covers:
- Message thinking field serialization roundtrip
- Ollama non-streaming and streaming thinking parsing
- OpenAI non-streaming and streaming thinking parsing
- infer_stream() thinking vs content distinction
- Ollama build_request think parameter
"""

import io
import json
from unittest.mock import patch, MagicMock

import pytest

from runtime.models import Message, ModelConfig, InferenceRequest
from runtime.protocols import OpenAIProtocol, OllamaProtocol
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


# ------------------------------------------------------------------
# Message thinking field
# ------------------------------------------------------------------


class TestMessageThinkingField:
    def test_thinking_none_by_default(self):
        msg = Message(role="assistant", content="hello")
        assert msg.thinking is None

    def test_thinking_set(self):
        msg = Message(role="assistant", content="answer", thinking="let me think...")
        assert msg.thinking == "let me think..."

    def test_to_dict_omits_none_thinking(self):
        msg = Message(role="assistant", content="hi")
        d = msg.to_dict()
        assert "thinking" not in d

    def test_to_dict_includes_thinking(self):
        msg = Message(role="assistant", content="42", thinking="6*7=42")
        d = msg.to_dict()
        assert d["thinking"] == "6*7=42"

    def test_roundtrip_with_thinking(self):
        original = Message(role="assistant", content="yes", thinking="reasoning here")
        restored = Message.from_dict(original.to_dict())
        assert restored.thinking == original.thinking
        assert restored.content == original.content

    def test_roundtrip_without_thinking(self):
        original = Message(role="user", content="hello")
        restored = Message.from_dict(original.to_dict())
        assert restored.thinking is None

    def test_from_dict_with_thinking_key(self):
        data = {"role": "assistant", "content": "ok", "thinking": "step 1..."}
        msg = Message.from_dict(data)
        assert msg.thinking == "step 1..."


# ------------------------------------------------------------------
# Ollama non-streaming thinking
# ------------------------------------------------------------------


class TestOllamaThinkingNonStream:
    def test_parse_response_with_thinking(self):
        protocol = OllamaProtocol()
        response = json.dumps({
            "model": "qwen3:14b",
            "message": {
                "role": "assistant",
                "content": "The answer is 42.",
                "thinking": "Let me calculate... 6 times 7 equals 42.",
            },
            "done": True,
        }).encode("utf-8")

        messages = protocol.parse_response(response, stream=False)
        assert len(messages) == 1
        assert messages[0].content == "The answer is 42."
        assert messages[0].thinking == "Let me calculate... 6 times 7 equals 42."

    def test_parse_response_without_thinking(self):
        protocol = OllamaProtocol()
        response = json.dumps({
            "model": "qwen3:14b",
            "message": {"role": "assistant", "content": "Hello!"},
            "done": True,
        }).encode("utf-8")

        messages = protocol.parse_response(response, stream=False)
        assert len(messages) == 1
        assert messages[0].thinking is None

    def test_parse_response_empty_thinking(self):
        protocol = OllamaProtocol()
        response = json.dumps({
            "model": "qwen3:14b",
            "message": {"role": "assistant", "content": "Hi", "thinking": ""},
            "done": True,
        }).encode("utf-8")

        messages = protocol.parse_response(response, stream=False)
        assert messages[0].thinking is None  # empty string → None


# ------------------------------------------------------------------
# Ollama streaming thinking
# ------------------------------------------------------------------


class TestOllamaThinkingStream:
    def _make_stream(self, chunks: list[dict]) -> io.BytesIO:
        lines = [json.dumps(c) + "\n" for c in chunks]
        return io.BytesIO("".join(lines).encode("utf-8"))

    def test_stream_thinking_then_content(self):
        """Thinking chunks come first, then content chunks."""
        chunks = [
            {"message": {"role": "assistant", "content": "", "thinking": "Step 1"}, "done": False},
            {"message": {"role": "assistant", "content": "", "thinking": " Step 2"}, "done": False},
            {"message": {"role": "assistant", "content": "Answer"}, "done": False},
            {"message": {"role": "assistant", "content": " is 42"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        stream = self._make_stream(chunks)

        runtime = Runtime(ModelRegistry(), ToolRegistry())
        messages = list(runtime._parse_ollama_stream(stream))

        # Should have 2 thinking + 2 content messages
        thinking_msgs = [m for m in messages if m.thinking]
        content_msgs = [m for m in messages if m.content]

        assert len(thinking_msgs) == 2
        assert thinking_msgs[0].thinking == "Step 1"
        assert thinking_msgs[1].thinking == " Step 2"
        assert thinking_msgs[0].content == ""

        assert len(content_msgs) == 2
        assert content_msgs[0].content == "Answer"
        assert content_msgs[1].content == " is 42"

    def test_stream_no_thinking(self):
        """Regular stream without thinking field."""
        chunks = [
            {"message": {"role": "assistant", "content": "Hello"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        stream = self._make_stream(chunks)

        runtime = Runtime(ModelRegistry(), ToolRegistry())
        messages = list(runtime._parse_ollama_stream(stream))

        assert len(messages) == 1
        assert messages[0].content == "Hello"
        assert messages[0].thinking is None


# ------------------------------------------------------------------
# OpenAI non-streaming thinking
# ------------------------------------------------------------------


class TestOpenAIThinkingNonStream:
    def test_parse_response_with_reasoning_content(self):
        protocol = OpenAIProtocol()
        response = json.dumps({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "42",
                    "reasoning_content": "6 * 7 = 42",
                },
            }],
        }).encode("utf-8")

        messages = protocol.parse_response(response, stream=False)
        assert len(messages) == 1
        assert messages[0].content == "42"
        assert messages[0].thinking == "6 * 7 = 42"

    def test_parse_response_without_reasoning_content(self):
        protocol = OpenAIProtocol()
        response = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "Hi"}}],
        }).encode("utf-8")

        messages = protocol.parse_response(response, stream=False)
        assert messages[0].thinking is None


# ------------------------------------------------------------------
# OpenAI streaming thinking
# ------------------------------------------------------------------


class TestOpenAIThinkingStream:
    def _make_sse_stream(self, chunks: list[dict]) -> io.BytesIO:
        lines = []
        for c in chunks:
            lines.append(f"data: {json.dumps(c)}\n\n")
        lines.append("data: [DONE]\n\n")
        return io.BytesIO("".join(lines).encode("utf-8"))

    def test_stream_reasoning_then_content(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "reasoning_content": "Think"}}]},
            {"choices": [{"delta": {"reasoning_content": "ing..."}}]},
            {"choices": [{"delta": {"content": "Answer"}}]},
            {"choices": [{"delta": {"content": " here"}}]},
        ]
        stream = self._make_sse_stream(chunks)

        runtime = Runtime(ModelRegistry(), ToolRegistry())
        messages = list(runtime._parse_openai_stream(stream))

        thinking_msgs = [m for m in messages if m.thinking]
        content_msgs = [m for m in messages if m.content]

        assert len(thinking_msgs) == 2
        assert thinking_msgs[0].thinking == "Think"
        assert thinking_msgs[1].thinking == "ing..."

        assert len(content_msgs) == 2
        assert content_msgs[0].content == "Answer"
        assert content_msgs[1].content == " here"

    def test_stream_no_reasoning(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": "Hello"}}]},
        ]
        stream = self._make_sse_stream(chunks)

        runtime = Runtime(ModelRegistry(), ToolRegistry())
        messages = list(runtime._parse_openai_stream(stream))

        assert len(messages) == 1
        assert messages[0].content == "Hello"
        assert messages[0].thinking is None


# ------------------------------------------------------------------
# OpenAI _parse_stream_response (accumulated, in protocols.py)
# ------------------------------------------------------------------


class TestOpenAIAccumulatedStreamThinking:
    def test_accumulated_stream_with_reasoning(self):
        protocol = OpenAIProtocol()
        sse = (
            'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"Think"}}]}\n\n'
            'data: {"choices":[{"delta":{"reasoning_content":"ing"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"Result"}}]}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")

        messages = protocol.parse_response(sse, stream=True)
        assert len(messages) == 1
        assert messages[0].content == "Result"
        assert messages[0].thinking == "Thinking"

    def test_accumulated_stream_without_reasoning(self):
        protocol = OpenAIProtocol()
        sse = (
            'data: {"choices":[{"delta":{"role":"assistant","content":"Hi"}}]}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")

        messages = protocol.parse_response(sse, stream=True)
        assert messages[0].thinking is None


# ------------------------------------------------------------------
# Ollama build_request think parameter
# ------------------------------------------------------------------


class TestOllamaBuildRequestThink:
    def test_think_param_in_request_body(self):
        protocol = OllamaProtocol()
        config = ModelConfig(
            model_id="test", api_base="http://localhost:11434",
            model_name="qwen3:14b", api_protocol="ollama",
            generate_params={"think": True, "temperature": 0.7},
        )
        messages = [Message(role="user", content="hello")]
        url, headers, body_bytes = protocol.build_request(config, messages)
        body = json.loads(body_bytes)

        assert body["think"] is True
        assert "think" not in body.get("options", {})
        assert body["options"]["temperature"] == 0.7

    def test_no_think_param(self):
        protocol = OllamaProtocol()
        config = ModelConfig(
            model_id="test", api_base="http://localhost:11434",
            model_name="qwen3:14b", api_protocol="ollama",
            generate_params={"temperature": 0.7},
        )
        messages = [Message(role="user", content="hello")]
        url, headers, body_bytes = protocol.build_request(config, messages)
        body = json.loads(body_bytes)

        assert "think" not in body
        assert body["options"]["temperature"] == 0.7


# ------------------------------------------------------------------
# infer_stream() end-to-end thinking distinction
# ------------------------------------------------------------------


class TestInferStreamThinking:
    def test_ollama_infer_stream_thinking(self):
        model_registry = ModelRegistry()
        model_registry.register(ModelConfig(
            model_id="test", api_base="http://localhost:11434",
            model_name="qwen3:14b", api_protocol="ollama",
        ))
        runtime = Runtime(model_registry, ToolRegistry())

        ollama_stream = (
            json.dumps({"message": {"role": "assistant", "content": "", "thinking": "Reasoning..."}, "done": False}) + "\n"
            + json.dumps({"message": {"role": "assistant", "content": "Final answer"}, "done": False}) + "\n"
            + json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}) + "\n"
        ).encode("utf-8")

        def mock_urlopen(request, **kwargs):
            mock_resp = MagicMock()
            mock_resp.__iter__ = lambda s: iter(io.BytesIO(ollama_stream).readlines())
            mock_resp.read = io.BytesIO(ollama_stream).read
            mock_resp.close = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        request = InferenceRequest(model_id="test", text="hi", stream=True)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            messages = list(runtime.infer_stream(request))

        thinking_msgs = [m for m in messages if m.thinking]
        content_msgs = [m for m in messages if m.content]

        assert len(thinking_msgs) == 1
        assert thinking_msgs[0].thinking == "Reasoning..."
        assert thinking_msgs[0].content == ""

        assert len(content_msgs) == 1
        assert content_msgs[0].content == "Final answer"
        assert content_msgs[0].thinking is None

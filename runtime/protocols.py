"""Protocol adapters for the Composable Agent Runtime.

Provides BaseProtocol abstract base class and concrete protocol implementations
for constructing API requests and parsing responses from different LLM backends.
Only uses Python standard library modules.
"""

import json
import uuid
from abc import ABC, abstractmethod
from typing import Optional
import os
import base64
import urllib.request

from runtime.models import Message, ModelConfig, TokenStat, ToolConfig


class BaseProtocol(ABC):
    """Abstract base class for LLM API protocol adapters.

    Each protocol adapter knows how to construct requests and parse responses
    for a specific LLM API format (e.g. OpenAI, Ollama).
    """

    @abstractmethod
    def build_request(
        self,
        config: ModelConfig,
        messages: list,
        tools: Optional[list] = None,
        stream: bool = False,
    ) -> tuple:
        """Build an HTTP request for the LLM API.

        Args:
            config: Model configuration with endpoint details.
            messages: List of Message objects for the conversation.
            tools: Optional list of ToolConfig objects to include.
            stream: Whether to request streaming response.

        Returns:
            A tuple of (url, headers, body_bytes) ready to send.
        """

    @abstractmethod
    def parse_response(self, response_data: bytes, stream: bool = False) -> tuple:
        """Parse an LLM API response into Message objects and token usage.

        Args:
            response_data: Raw response bytes from the API.
            stream: Whether the response is in streaming (SSE) format.

        Returns:
            A tuple of (messages, usage) where messages is a list of Message
            objects and usage is a TokenStat instance (may have all zeros if
            the backend does not report usage).
        """


    @staticmethod
    def _convert_image_to_base64(img_data: str) -> str:
        """Convert an image source (URL, local path, or data URI) to a raw base64 string."""
        if img_data.startswith("data:"):
            return img_data.split(",", 1)[1]
        
        if img_data.startswith("http://") or img_data.startswith("https://"):
            try:
                with urllib.request.urlopen(img_data) as response:
                    return base64.b64encode(response.read()).decode("utf-8")
            except Exception as e:
                raise ValueError(f"Failed to download image: {e}")
        
        expanded_path = os.path.expanduser(img_data)
        try:
            with open(expanded_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
            raise ValueError(f"Failed to read image: {e}")


class OpenAIProtocol(BaseProtocol):
    """OpenAI Chat Completions API protocol adapter.

    Constructs requests in the OpenAI Chat Completions format:
    - URL: {api_base}/chat/completions
    - Multimodal: images encoded as image_url objects in content array
    - Tools: standard OpenAI tools format with type "function"
    - Supports both streaming and non-streaming responses
    """

    def __init__(self):
        self._tool_call_ids: dict[str, str] = {}

    def build_request(
        self,
        config: ModelConfig,
        messages: list,
        tools: Optional[list] = None,
        stream: bool = False,
    ) -> tuple:
        """Build an OpenAI Chat Completions API request.

        Args:
            config: Model configuration with endpoint details.
            messages: List of Message objects for the conversation.
            tools: Optional list of ToolConfig objects to include.
            stream: Whether to request streaming response.

        Returns:
            A tuple of (url, headers, body_bytes).
        """
        url = config.api_base.rstrip("/") + "/chat/completions"

        # Reset tool_call_id mapping for this request
        self._tool_call_ids = {}

        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = "Bearer " + config.api_key

        body = {
            "model": config.model_name,
            "messages": [self._encode_message(msg) for msg in messages],
            "stream": stream,
        }

        # Merge generate_params (temperature, top_p, etc.)
        if config.generate_params:
            for key, value in config.generate_params.items():
                body[key] = value

        # Encode tools if provided
        if tools:
            body["tools"] = [self._encode_tool(tool) for tool in tools]

        body_bytes = json.dumps(body).encode("utf-8")
        return (url, headers, body_bytes)

    def parse_response(self, response_data: bytes, stream: bool = False) -> tuple:
        """Parse an OpenAI Chat Completions API response.

        For non-streaming: parses JSON and extracts choices[0].message.
        For streaming: parses SSE lines (data: {...}) and accumulates deltas.

        Args:
            response_data: Raw response bytes.
            stream: Whether the response is in SSE streaming format.

        Returns:
            A tuple of (messages, usage).
        """
        if stream:
            return self._parse_stream_response(response_data)
        return self._parse_non_stream_response(response_data)

    def _encode_message(self, msg: Message) -> dict:
        """Encode a Message into OpenAI API message format.

        For messages with images, content becomes an array of content parts
        with text and image_url objects.
        """
        # Handle tool result messages:
        # OpenAI API requires role="tool" with a tool_call_id
        if msg.role == "tool":
            tool_call_id = self._tool_call_ids.get(msg.name, "call_" + (msg.name or "unknown"))
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": msg.content or "",
            }

        result = {"role": msg.role}

        # Handle assistant messages with tool_calls
        if msg.tool_calls is not None:
            result["content"] = msg.content if msg.content else None
            result["tool_calls"] = []
            for tc in msg.tool_calls:
                call_id = "call_" + uuid.uuid4().hex[:8]
                fn_name = tc.get("name", "unknown")
                self._tool_call_ids[fn_name] = call_id
                result["tool_calls"].append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": tc.get("arguments", "{}"),
                    },
                })
            return result

        # Handle tool role messages (legacy, shouldn't reach here)
        if msg.name is not None:
            result["name"] = msg.name

        # Handle multimodal messages with images
        if msg.images:
            content_parts = []
            if msg.content:
                content_parts.append({"type": "text", "text": msg.content})
            for img_data in msg.images:
                raw_base64 = self._convert_image_to_base64(img_data)
                content_parts.append(
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + raw_base64}}
                )
            result["content"] = content_parts
        else:
            result["content"] = msg.content

        return result

    def _encode_tool(self, tool: ToolConfig) -> dict:
        """Encode a ToolConfig into OpenAI tools format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _parse_non_stream_response(self, response_data: bytes) -> tuple:
        """Parse a non-streaming OpenAI response."""
        data = json.loads(response_data.decode("utf-8"))
        messages = []

        choices = data.get("choices", [])
        if not choices:
            return messages, TokenStat()

        choice = choices[0]
        msg_data = choice.get("message", {})

        content = msg_data.get("content") or ""
        # OpenAI uses "reasoning_content"; Ollama's OpenAI-compat endpoint uses "thinking"
        thinking = msg_data.get("reasoning_content") or msg_data.get("thinking") or None

        # Handle tool_calls in response
        tool_calls = msg_data.get("tool_calls")
        all_tool_calls = None
        if tool_calls and len(tool_calls) > 0:
            all_tool_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                all_tool_calls.append({
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })

        messages.append(
            Message(
                role=msg_data.get("role", "assistant"),
                content=content,
                tool_calls=all_tool_calls,
                thinking=thinking,
            )
        )

        # Extract token usage
        raw_usage = data.get("usage", {})
        usage = TokenStat(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
        )

        return messages, usage

    def _parse_stream_response(self, response_data: bytes) -> tuple:
        """Parse a streaming (SSE) OpenAI response.

        Accumulates delta content, reasoning_content, and tool_calls from SSE data lines.
        Usage is reported in the final chunk when stream_options.include_usage is set;
        falls back to zeros if not present.
        """
        text = response_data.decode("utf-8")
        accumulated_content = ""
        accumulated_thinking = ""
        # Dict keyed by tool call index: {index: {"name": str, "arguments": str}}
        accumulated_tool_calls: dict[int, dict] = {}
        role = "assistant"
        usage = TokenStat()

        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue

            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue

            # Some providers send usage in the final chunk
            raw_usage = chunk.get("usage")
            if raw_usage:
                usage = TokenStat(
                    prompt_tokens=raw_usage.get("prompt_tokens", 0),
                    completion_tokens=raw_usage.get("completion_tokens", 0),
                    total_tokens=raw_usage.get("total_tokens", 0),
                )

            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})

            if "role" in delta:
                role = delta["role"]

            if "content" in delta and delta["content"] is not None:
                accumulated_content += delta["content"]

            # Accumulate reasoning_content / thinking
            # OpenAI uses "reasoning_content"; Ollama's OpenAI-compat endpoint uses "thinking"
            reasoning = delta.get("reasoning_content") or delta.get("thinking")
            if reasoning is not None:
                accumulated_thinking += reasoning

            # Handle streamed tool_calls
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    idx = tc.get("index", 0)
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"name": "", "arguments": ""}
                    fn = tc.get("function", {})
                    if "name" in fn:
                        accumulated_tool_calls[idx]["name"] += fn["name"]
                    if "arguments" in fn:
                        accumulated_tool_calls[idx]["arguments"] += fn["arguments"]

        all_tool_calls = None
        if accumulated_tool_calls:
            all_tool_calls = []
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                all_tool_calls.append({
                    "name": tc["name"],
                    "arguments": tc["arguments"] or "{}",
                })

        return [
            Message(
                role=role,
                content=accumulated_content,
                tool_calls=all_tool_calls,
                thinking=accumulated_thinking or None,
            )
        ], usage


class OllamaProtocol(BaseProtocol):
    """Ollama native /api/chat protocol adapter.

    Constructs requests in the Ollama native format:
    - URL: {api_base}/api/chat
    - Multimodal: images field at same level as content (raw base64, no data URI prefix)
    - Tools: Ollama native tool_calls format (if supported)
    - Supports both streaming and non-streaming responses
    """

    def build_request(
        self,
        config: ModelConfig,
        messages: list,
        tools: Optional[list] = None,
        stream: bool = False,
    ) -> tuple:
        """Build an Ollama /api/chat request.

        Args:
            config: Model configuration with endpoint details.
            messages: List of Message objects for the conversation.
            tools: Optional list of ToolConfig objects to include.
            stream: Whether to request streaming response.

        Returns:
            A tuple of (url, headers, body_bytes).
        """
        url = config.api_base.rstrip("/") + "/api/chat"

        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = "Bearer " + config.api_key

        body = {
            "model": config.model_name,
            "messages": [self._encode_message(msg) for msg in messages],
            "stream": stream,
        }

        # Merge generate_params into options
        if config.generate_params:
            params = dict(config.generate_params)
            # Extract think param (Ollama top-level, not inside options)
            think = params.pop("think", None)
            if think is not None:
                body["think"] = think
            if params:
                body["options"] = params

        # Encode tools if provided (Ollama native tools format)
        if tools:
            body["tools"] = [self._encode_tool(tool) for tool in tools]

        body_bytes = json.dumps(body).encode("utf-8")
        return (url, headers, body_bytes)

    def parse_response(self, response_data: bytes, stream: bool = False) -> tuple:
        """Parse an Ollama /api/chat response.

        For non-streaming: parses JSON and extracts message field.
        For streaming: parses newline-delimited JSON objects.

        Args:
            response_data: Raw response bytes.
            stream: Whether the response is in streaming format.

        Returns:
            A tuple of (messages, usage).
        """
        if stream:
            return self._parse_stream_response(response_data)
        return self._parse_non_stream_response(response_data)

    def _encode_message(self, msg: Message) -> dict:
        """Encode a Message into Ollama API message format.

        For messages with images, the images field is placed at the same level
        as content. Images are raw base64 strings without data URI prefix.
        """
        # Ollama uses "tool" role for tool results
        role = msg.role
        result = {"role": role, "content": msg.content or ""}

        # Handle tool role messages
        if msg.name is not None:
            result["name"] = msg.name

        # Handle assistant messages with tool_calls
        if msg.tool_calls is not None:
            encoded_tool_calls = []
            for tc in msg.tool_calls:
                arguments = tc.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (json.JSONDecodeError, ValueError):
                        arguments = {}
                encoded_tool_calls.append({
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": arguments,
                    },
                })
            result["tool_calls"] = encoded_tool_calls

        # Handle multimodal messages with images
        # Images are placed at the same level as content, as raw base64 strings
        if msg.images:
            result["images"] = [self._convert_image_to_base64(img) for img in msg.images]

        return result

    def _encode_tool(self, tool: ToolConfig) -> dict:
        """Encode a ToolConfig into Ollama tools format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _parse_non_stream_response(self, response_data: bytes) -> tuple:
        """Parse a non-streaming Ollama response."""
        data = json.loads(response_data.decode("utf-8"))
        messages = []

        msg_data = data.get("message", {})
        if not msg_data:
            return messages, TokenStat()

        content = msg_data.get("content", "")
        thinking = msg_data.get("thinking") or None
        all_tool_calls = None

        # Handle tool_calls in response
        tool_calls = msg_data.get("tool_calls")
        if tool_calls and len(tool_calls) > 0:
            all_tool_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                arguments = fn.get("arguments", {})
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments)
                all_tool_calls.append({
                    "name": fn.get("name", ""),
                    "arguments": arguments,
                })

        messages.append(
            Message(
                role=msg_data.get("role", "assistant"),
                content=content,
                tool_calls=all_tool_calls,
                thinking=thinking,
            )
        )

        # Ollama reports usage at the top level of the response
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)
        usage = TokenStat(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        return messages, usage

    def _parse_stream_response(self, response_data: bytes) -> tuple:
        """Parse a streaming Ollama response.

        Ollama streaming returns newline-delimited JSON objects:
        {"model":"...","message":{"role":"assistant","content":"Hi "},"done":false}
        {"model":"...","message":{"role":"assistant","content":"there"},"done":false}
        {"model":"...","message":{"role":"assistant","content":""},"done":true,"prompt_eval_count":10,"eval_count":5}
        """
        text = response_data.decode("utf-8")
        accumulated_content = ""
        role = "assistant"
        all_tool_calls = None
        usage = TokenStat()

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            try:
                chunk = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            msg_data = chunk.get("message", {})
            if msg_data:
                if "role" in msg_data:
                    role = msg_data["role"]

                content = msg_data.get("content", "")
                if content:
                    accumulated_content += content

                # Handle tool_calls in streaming (usually in the final chunk)
                tool_calls = msg_data.get("tool_calls")
                if tool_calls and len(tool_calls) > 0:
                    all_tool_calls = []
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        arguments = fn.get("arguments", {})
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments)
                        all_tool_calls.append({
                            "name": fn.get("name", ""),
                            "arguments": arguments,
                        })

            # Usage is in the final "done" chunk
            if chunk.get("done"):
                prompt_tokens = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
                usage = TokenStat(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                )

        return [
            Message(
                role=role,
                content=accumulated_content,
                tool_calls=all_tool_calls,
            )
        ], usage


# Protocol name to adapter class mapping
PROTOCOL_MAP = {
    "openai": OpenAIProtocol,
    "ollama": OllamaProtocol,
}

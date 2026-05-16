"""Protocol adapters for the Composable Agent Runtime.

Provides BaseProtocol abstract base class and concrete protocol implementations
for constructing API requests and parsing responses from different LLM backends.
Only uses Python standard library modules.
"""

import json
import uuid
from abc import ABC, abstractmethod
from typing import Optional, Iterator
import os
import base64
import urllib.request
import datetime

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

    @abstractmethod
    def parse_stream(self, http_resp: object) -> Iterator[Message]:
        """Parse a streaming HTTP response, yielding Message objects incrementally.

        Args:
            http_resp: The HTTP response object with a readable stream.

        Yields:
            Message objects with incremental content, thinking, or tool calls.
            At stream end, yields a role="usage" Message with token counts.
        """

    @staticmethod
    def _dump_request_if_debug(body: dict) -> None:
        """If DEBUG_INFER_REQUEST=true, pretty-print the request body to
        <chats_dir>/<session_path>/request_YYMMDD_HHmmss_SSS.json.

        The session path is derived from session_id by replacing '-' with '/'.
        For example, session_id "260501_143022-sub_260501_143025" maps to
        "<chats_dir>/260501_143022/sub_260501_143025/".

        Falls back to <chats_dir>/request_<timestamp>.json when no session is
        active, and silently skips the dump if chats_dir is also unavailable.
        """
        if os.environ.get("DEBUG_INFER_REQUEST", "").lower() != "true":
            return
        try:
            from runtime.builtin_tools import _thread_local
            context_manager = getattr(_thread_local, "context_manager", None)
            session_id = getattr(_thread_local, "session_id", None)
        except Exception:
            return
        if not context_manager:
            return
        chats_dir = context_manager._chats_dir
        if not chats_dir:
            return
        now = datetime.datetime.now()
        date_time = now.strftime("%y%m%d_%H%M%S")
        millisec = now.microsecond // 1000
        filename = f"request_{date_time}_{millisec:03d}.json"
        if session_id:
            # Replace '-' with os.sep to convert session hierarchy into a path
            out_dir = os.path.join(chats_dir, session_id.replace("-", os.sep))
        else:
            out_dir = chats_dir
        os.makedirs(out_dir, exist_ok=True)
        filepath = os.path.join(out_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)

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
        pass

    def parse_stream(self, http_resp: object) -> Iterator[Message]:
        """Parse an OpenAI SSE stream, yielding Message objects for each delta.

        Supports ``reasoning_content`` (thinking) alongside regular ``content``.
        Thinking chunks yield Message(thinking=..., content=""); content
        chunks yield Message(content=...).
        At stream end, yields a role="usage" Message with token counts in content as JSON.
        """
        prompt_tokens = 0
        completion_tokens = 0

        for raw_line in http_resp:
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace")
            else:
                line = raw_line
            line = line.rstrip("\r\n")

            if not line.startswith("data:"):
                continue

            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue

            # Usage may appear in the final chunk (when stream_options.include_usage is set)
            raw_usage = chunk.get("usage")
            if raw_usage:
                prompt_tokens = raw_usage.get("prompt_tokens", 0)
                completion_tokens = raw_usage.get("completion_tokens", 0)

            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})

            # Thinking / reasoning content
            # OpenAI uses "reasoning_content"; Ollama's OpenAI-compat endpoint uses "thinking"
            reasoning = delta.get("reasoning_content") or delta.get("thinking")
            if reasoning:
                yield Message(role="assistant", content="", thinking=reasoning)

            # Regular content
            content = delta.get("content")
            if content:
                yield Message(role="assistant", content=content)

            # Handle streamed tool_calls (yield as tool_calls message)
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    idx = tc.get("index", 0)
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "")
                    if fn_name or fn_args:
                        tc_dict: dict = {"_index": idx}
                        if tc.get("id"):
                            tc_dict["id"] = tc["id"]
                        if fn_name:
                            tc_dict["name"] = fn_name
                        if fn_args:
                            tc_dict["arguments"] = fn_args
                        yield Message(
                            role="assistant",
                            content="",
                            tool_calls=[tc_dict],
                        )

        # Yield usage summary for this round
        yield Message(
            role="usage",
            content=json.dumps({
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }),
        )

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
        self._dump_request_if_debug(body)
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
            tool_call_id = msg.tool_use_id or "call_" + uuid.uuid4().hex[:8]
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": msg.content or "",
            }

        result = {"role": msg.role}

        # Handle assistant messages with tool_calls
        if msg.tool_calls is not None:
            # Only include content if non-empty to avoid content: null (some providers reject it)
            if msg.content:
                result["content"] = msg.content
            result["tool_calls"] = []
            for tc in msg.tool_calls:
                call_id = tc.get("id") or tc.get("tool_use_id") or "call_" + uuid.uuid4().hex[:8]
                fn_name = tc.get("name", "unknown")
                arguments = tc.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                result["tool_calls"].append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": arguments,
                    },
                })
            # Pass back reasoning_content for thinking-mode models (required by API)
            if msg.thinking is not None:
                result["reasoning_content"] = msg.thinking
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

        # Pass back reasoning_content for thinking-mode models (required by API)
        if msg.thinking is not None:
            result["reasoning_content"] = msg.thinking

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
                    "id": tc.get("id", ""),
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
        # Dict keyed by tool call index: {index: {\"name\": str, \"arguments\": str}}
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
                        accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.get("id"):
                        accumulated_tool_calls[idx]["id"] = tc["id"]
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
                    "id": tc.get("id", ""),
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

    def parse_stream(self, http_resp: object) -> Iterator[Message]:
        """Parse an Ollama newline-delimited JSON stream, yielding Messages.

        Ollama thinking-capable models emit a ``thinking`` field in each chunk
        before the final ``content``. Thinking chunks yield a Message with
        ``thinking`` set and ``content`` empty; content chunks yield a Message
        with ``content`` set.

        Tool calls may arrive across multiple chunks (one per chunk) or all
        in a single final chunk. We accumulate them and yield once at the end.
        At stream end, yields a role="usage" Message with token counts in content as JSON.

        Args:
            http_resp: The HTTP response object with a readable stream.

        Yields:
            Message objects with incremental content or thinking.
        """
        # Accumulate tool calls across chunks — they may arrive one per chunk
        accumulated_tool_calls: dict[int, dict] = {}
        prompt_tokens = 0
        completion_tokens = 0

        for raw_line in http_resp:
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace")
            else:
                line = raw_line
            line = line.strip()

            if not line:
                continue

            try:
                chunk = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            msg_data = chunk.get("message", {})
            if not msg_data:
                continue

            # Thinking content (reasoning trace)
            thinking = msg_data.get("thinking", "")
            if thinking:
                yield Message(role="assistant", content="", thinking=thinking)

            # Regular content
            content = msg_data.get("content", "")
            if content:
                yield Message(role="assistant", content=content)

            # Accumulate tool_calls across chunks
            tool_calls = msg_data.get("tool_calls")
            if tool_calls and len(tool_calls) > 0:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    # Use explicit index if present, otherwise append
                    idx = fn.get("index", len(accumulated_tool_calls))
                    arguments = fn.get("arguments", {})
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments)
                    accumulated_tool_calls[idx] = {
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": arguments,
                    }

            # Stop if done — also grab usage from the final chunk
            if chunk.get("done", False):
                prompt_tokens = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
                break

        # Yield accumulated tool calls as a single message at the end
        if accumulated_tool_calls:
            all_tool_calls = [
                accumulated_tool_calls[idx]
                for idx in sorted(accumulated_tool_calls.keys())
            ]
            yield Message(
                role="assistant",
                content="",
                tool_calls=all_tool_calls,
            )

        # Yield usage summary for this round
        yield Message(
            role="usage",
            content=json.dumps({
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }),
        )

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
        self._dump_request_if_debug(body)
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
                    "id": tc.get("id", ""),
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
                            "id": tc.get("id", ""),
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


class AnthropicProtocol(BaseProtocol):
    """Anthropic Messages API protocol adapter.

    Constructs requests in the Anthropic Messages API format:
    - URL: {api_base}/messages
    - Headers: x-api-key, anthropic-version, Content-Type
    - System prompt at top level
    - Messages with role/content format
    - Anthropic-specific tool format with input_schema
    - Supports both streaming and non-streaming responses
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self):
        pass

    @staticmethod
    def _extract_usage(chunk: dict) -> dict:
        usage_data = {}
        if isinstance(chunk.get("message"), dict):
            usage_data = chunk["message"].get("usage") or usage_data
        usage_data = chunk.get("usage") or usage_data
        if isinstance(chunk.get("delta"), dict):
            usage_data = chunk["delta"].get("usage") or usage_data
        if not usage_data and any(key in chunk for key in ("input_tokens", "output_tokens")):
            usage_data = chunk
        return usage_data if isinstance(usage_data, dict) else {}

    @staticmethod
    def _input_tokens_from_usage(usage_data: dict) -> int:
        return (
            usage_data.get("input_tokens", 0)
            + usage_data.get("cache_creation_input_tokens", 0)
            + usage_data.get("cache_read_input_tokens", 0)
        )

    def parse_stream(self, http_resp: object) -> Iterator[Message]:
        """Parse an Anthropic Messages API SSE stream, yielding Messages.

        Anthropic streaming uses named events:
        - message_start: usage info
        - content_block_start: new content block (text, tool_use, thinking)
        - content_block_delta: delta within a content block (text_delta, thinking_delta, input_json_delta)
        - content_block_stop: end of a content block
        - message_delta: delta for the message (stop_reason, usage)
        - message_stop: end of message
        - ping: keepalive

        Yields:
            Message objects with incremental content, thinking, or tool calls.
        """
        accumulated_content = ""
        accumulated_thinking = ""
        accumulated_tool_calls: dict[int, dict] = {}
        current_tool_use_idx: Optional[int] = None
        prompt_tokens = 0
        completion_tokens = 0

        for raw_line in http_resp:
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace")
            else:
                line = raw_line
            line = line.rstrip("\r\n")

            if not line.startswith("data:"):
                continue

            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue

            event_type = chunk.get("type")
            raw_usage_data = self._extract_usage(chunk)
            if not event_type and raw_usage_data:
                prompt_tokens = max(prompt_tokens, self._input_tokens_from_usage(raw_usage_data))
                completion_tokens = max(completion_tokens, raw_usage_data.get("output_tokens", 0))
                continue

            if event_type == "message_start":
                usage_data = self._extract_usage(chunk)
                prompt_tokens = self._input_tokens_from_usage(usage_data)

            elif event_type == "content_block_start":
                block = chunk.get("content_block", {})
                block_type = block.get("type")
                if block_type == "text":
                    accumulated_content = ""
                elif block_type == "thinking":
                    accumulated_thinking = ""
                elif block_type == "tool_use":
                    idx = len(accumulated_tool_calls)
                    accumulated_tool_calls[idx] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": "",
                    }
                    current_tool_use_idx = idx

            elif event_type == "content_block_delta":
                delta = chunk.get("delta", {})
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        accumulated_content += text
                        yield Message(role="assistant", content=text)
                elif delta_type == "thinking_delta":
                    thinking = delta.get("thinking", "")
                    if thinking:
                        accumulated_thinking += thinking
                        yield Message(role="assistant", content="", thinking=thinking)
                elif delta_type == "input_json_delta":
                    partial_json = delta.get("partial_json", "")
                    if partial_json and current_tool_use_idx is not None:
                        # Only accumulate into the currently active tool call
                        accumulated_tool_calls[current_tool_use_idx]["arguments"] += partial_json

            elif event_type == "content_block_stop":
                # End of a content block - if it was a tool_use, yield it
                if current_tool_use_idx is not None and current_tool_use_idx in accumulated_tool_calls:
                    tc = accumulated_tool_calls[current_tool_use_idx]
                    if tc["arguments"]:
                        try:
                            tc["arguments"] = json.loads(tc["arguments"])
                        except (json.JSONDecodeError, ValueError):
                            pass
                        yield Message(
                            role="assistant",
                            content="",
                            tool_calls=[tc],
                        )
                current_tool_use_idx = None

            elif event_type == "message_delta":
                delta = chunk.get("delta", {})
                stop_reason = delta.get("stop_reason")
                usage_data = self._extract_usage(chunk)
                if usage_data:
                    prompt_tokens = max(prompt_tokens, self._input_tokens_from_usage(usage_data))
                    completion_tokens = max(completion_tokens, usage_data.get("output_tokens", 0))


            elif event_type == "message_stop":
                pass

        # Yield usage summary for this round
        yield Message(
            role="usage",
            content=json.dumps({
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }),
        )

    def build_request(
        self,
        config: ModelConfig,
        messages: list,
        tools: Optional[list] = None,
        stream: bool = False,
    ) -> tuple:
        """Build an Anthropic Messages API request.

        Args:
            config: Model configuration with endpoint details.
            messages: List of Message objects for the conversation.
            tools: Optional list of ToolConfig objects to include.
            stream: Whether to request streaming response.

        Returns:
            A tuple of (url, headers, body_bytes).
        """
        url = config.api_base.rstrip("/") + "/messages"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": config.api_key or "",
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

        # Anthropic accepts one top-level system field; preserve system order.
        system_parts = []
        filtered_messages = []
        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
            else:
                filtered_messages.append(msg)

        system = "\n\n".join(system_parts)

        body = {
            "model": config.model_name,
            "messages": [self._encode_message(msg) for msg in filtered_messages],
            "stream": stream,
        }

        if system:
            body["system"] = system

        # Merge generate_params (temperature, top_p, max_tokens, etc.)
        if config.generate_params:
            params = dict(config.generate_params)
            # Anthropic uses max_tokens (not max_completion_tokens)
            if "max_completion_tokens" in params:
                params["max_tokens"] = params.pop("max_completion_tokens")
            for key, value in params.items():
                body[key] = value

        # Encode tools if provided
        if tools:
            body["tools"] = [self._encode_tool(tool) for tool in tools]

        body_bytes = json.dumps(body).encode("utf-8")
        self._dump_request_if_debug(body)
        return (url, headers, body_bytes)

    def parse_response(self, response_data: bytes, stream: bool = False) -> tuple:
        """Parse an Anthropic Messages API response.

        For non-streaming: parses JSON and extracts content blocks.
        For streaming: parses SSE events (content_block_start/delta/stop, message_delta, etc.).

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
        """Encode a Message into Anthropic API message format."""
        # Handle system role (shouldn't reach here, but safety net)
        if msg.role == "system":
            return {"role": "user", "content": msg.content or ""}

        # Handle tool result messages
        if msg.role == "tool":
            tool_use_id = msg.tool_use_id or msg.name or "unknown"
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": msg.content or "",
                }],
            }

        # Handle assistant messages with tool_calls
        if msg.tool_calls is not None:
            content_blocks = []
            for tc in msg.tool_calls:
                call_id = tc.get("id") or tc.get("tool_use_id") or "call_" + uuid.uuid4().hex[:8]
                fn_name = tc.get("name", "unknown")
                arguments = tc.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (json.JSONDecodeError, ValueError):
                        arguments = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": fn_name,
                    "input": arguments,
                })
            return {"role": "assistant", "content": content_blocks}

        # Handle multimodal messages with images
        if msg.images:
            content_parts = []
            if msg.content:
                content_parts.append({"type": "text", "text": msg.content})
            for img_data in msg.images:
                raw_base64 = self._convert_image_to_base64(img_data)
                media_type = "image/jpeg"
                if img_data.startswith("data:"):
                    meta = img_data.split(",", 1)[0]
                    if "image/png" in meta:
                        media_type = "image/png"
                    elif "image/gif" in meta:
                        media_type = "image/gif"
                    elif "image/webp" in meta:
                        media_type = "image/webp"
                content_parts.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": raw_base64,
                    },
                })
            return {"role": msg.role, "content": content_parts}

        return {"role": msg.role, "content": msg.content or ""}

    def _encode_tool(self, tool: ToolConfig) -> dict:
        """Encode a ToolConfig into Anthropic tools format."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    def _parse_non_stream_response(self, response_data: bytes) -> tuple:
        """Parse a non-streaming Anthropic response."""
        data = json.loads(response_data.decode("utf-8"))
        messages = []

        content_blocks = data.get("content", [])
        accumulated_content = ""
        accumulated_thinking = ""
        all_tool_calls = None

        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                accumulated_content += block.get("text", "")
            elif block_type == "thinking":
                accumulated_thinking += block.get("thinking", "")
            elif block_type == "tool_use":
                if all_tool_calls is None:
                    all_tool_calls = []
                all_tool_calls.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                })

        messages.append(Message(
            role="assistant",
            content=accumulated_content,
            tool_calls=all_tool_calls,
            thinking=accumulated_thinking or None,
        ))

        # Token usage
        usage_data = data.get("usage", {})
        usage = TokenStat(
            prompt_tokens=usage_data.get("input_tokens", 0),
            completion_tokens=usage_data.get("output_tokens", 0),
            total_tokens=usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0),
        )

        return messages, usage

    def _parse_stream_response(self, response_data: bytes) -> tuple:
        """Parse a streaming (SSE) Anthropic response.

        Anthropic streaming uses named events:
        - content_block_start / content_block_delta / content_block_stop
        - message_start / message_delta / message_stop
        - ping
        """
        text = response_data.decode("utf-8")
        accumulated_content = ""
        accumulated_thinking = ""
        role = "assistant"
        all_tool_calls: list = []
        current_tool_use_idx: Optional[int] = None
        usage = TokenStat()

        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1

            if not line:
                continue

            if line.startswith("event:"):
                # Peek at next line for data
                if i < len(lines):
                    data_line = lines[i].strip()
                    if data_line.startswith("data:"):
                        data_str = data_line[len("data:"):].strip()
                        (
                            accumulated_content, accumulated_thinking,
                            role, current_tool_use_idx, usage,
                        ) = self._process_anthropic_event(
                            data_str,
                            accumulated_content, accumulated_thinking,
                            role, all_tool_calls, current_tool_use_idx, usage,
                        )
                        i += 1
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    continue
                event_type = chunk.get("type")
                raw_usage_data = self._extract_usage(chunk)
                if not event_type and raw_usage_data:
                    prompt_tokens = max(usage.prompt_tokens, self._input_tokens_from_usage(raw_usage_data))
                    completion_tokens = max(usage.completion_tokens, raw_usage_data.get("output_tokens", 0))
                    usage = TokenStat(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    )
                    continue
                if event_type in ("content_block_start", "content_block_delta",
                                  "content_block_stop", "message_start",
                                  "message_delta", "message_stop"):
                    (
                        accumulated_content, accumulated_thinking,
                        role, current_tool_use_idx, usage,
                    ) = self._process_anthropic_event(
                        data_str,
                        accumulated_content, accumulated_thinking,
                        role, all_tool_calls, current_tool_use_idx, usage,
                    )

        # Clean up tool call arguments (ensure valid JSON)
        for tc in all_tool_calls:
            if isinstance(tc["arguments"], str) and tc["arguments"]:
                try:
                    tc["arguments"] = json.loads(tc["arguments"])
                except (json.JSONDecodeError, ValueError):
                    pass

        return [Message(
            role=role,
            content=accumulated_content,
            tool_calls=all_tool_calls if all_tool_calls else None,
            thinking=accumulated_thinking or None,
        )], usage

    def _process_anthropic_event(
        self,
        data_str: str,
        accumulated_content: str,
        accumulated_thinking: str,
        role: str,
        all_tool_calls: list,
        current_tool_use_idx: Optional[int],
        usage: TokenStat,
    ) -> tuple:
        """Process a single Anthropic streaming SSE event.

        Returns updated (accumulated_content, accumulated_thinking, role,
        current_tool_use_idx, usage).
        """
        try:
            chunk = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            return accumulated_content, accumulated_thinking, role, current_tool_use_idx, usage

        event_type = chunk.get("type")
        raw_usage_data = self._extract_usage(chunk)
        if not event_type and raw_usage_data:
            prompt_tokens = max(usage.prompt_tokens, self._input_tokens_from_usage(raw_usage_data))
            completion_tokens = max(usage.completion_tokens, raw_usage_data.get("output_tokens", 0))
            return (
                accumulated_content,
                accumulated_thinking,
                role,
                current_tool_use_idx,
                TokenStat(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )
        if not event_type:
            return accumulated_content, accumulated_thinking, role, current_tool_use_idx, usage

        if event_type == "message_start":
            usage_data = self._extract_usage(chunk)
            input_tokens = self._input_tokens_from_usage(usage_data)
            usage = TokenStat(
                prompt_tokens=input_tokens,
                completion_tokens=0,
                total_tokens=input_tokens,
            )

        elif event_type == "content_block_start":
            block = chunk.get("content_block", {})
            block_type = block.get("type")
            if block_type == "text":
                accumulated_content = ""
            elif block_type == "thinking":
                accumulated_thinking = ""
            elif block_type == "tool_use":
                idx = len(all_tool_calls)
                all_tool_calls.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": "",
                })
                current_tool_use_idx = idx

        elif event_type == "content_block_delta":
            delta = chunk.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                accumulated_content += delta.get("text", "")
            elif delta_type == "thinking_delta":
                accumulated_thinking += delta.get("thinking", "")
            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json", "")
                if partial and current_tool_use_idx is not None and current_tool_use_idx < len(all_tool_calls):
                    all_tool_calls[current_tool_use_idx]["arguments"] += partial

        elif event_type == "content_block_stop":
            current_tool_use_idx = None

        elif event_type == "message_delta":
            delta = chunk.get("delta", {})
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                role = "assistant"
            usage_data = self._extract_usage(chunk)
            if usage_data:
                prompt_tokens = max(usage.prompt_tokens, self._input_tokens_from_usage(usage_data))
                completion_tokens = max(usage.completion_tokens, usage_data.get("output_tokens", 0))
                usage = TokenStat(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                )

        return accumulated_content, accumulated_thinking, role, current_tool_use_idx, usage


# Protocol name to adapter class mapping
PROTOCOL_MAP = {
    "openai": OpenAIProtocol,
    "ollama": OllamaProtocol,
    "anthropic": AnthropicProtocol,
}

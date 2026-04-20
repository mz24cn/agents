"""Data models for the Composable Agent Runtime.

Defines Message, ModelConfig, ToolConfig, InferenceRequest, and InferenceResult
as dataclasses with JSON serialization/deserialization support.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Message:
    """A single message in a conversation.

    Attributes:
        role: Message role - "system", "user", "assistant", or "tool".
        content: Text content of the message.
        name: Optional name (used for tool role messages).
        tool_calls: Optional list of tool call dicts for parallel tool calls.
        images: Optional list of base64-encoded image strings (multimodal).
        audio: Optional base64-encoded audio string (multimodal).
        thinking: Optional thinking/reasoning content from the model.
    """

    role: str
    content: str = ""
    name: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    images: Optional[list] = None
    audio: Optional[str] = None
    thinking: Optional[str] = None
    prompt_template: Optional[str] = None
    arguments: Optional[dict] = None

    def to_dict(self) -> dict:
        """Serialize to a dictionary, omitting None fields."""
        d: dict = {"role": self.role, "content": self.content}
        if self.name is not None:
            d["name"] = self.name
        if self.tool_calls is not None:
            d["tool_calls"] = [dict(tc) for tc in self.tool_calls]
        if self.images is not None:
            d["images"] = list(self.images)
        if self.audio is not None:
            d["audio"] = self.audio
        if self.thinking is not None:
            d["thinking"] = self.thinking
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Deserialize from a dictionary."""
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            name=data.get("name"),
            tool_calls=data.get("tool_calls"),
            images=data.get("images"),
            audio=data.get("audio"),
            thinking=data.get("thinking"),
            prompt_template=data.get("prompt_template"),
            arguments=data.get("arguments"),
        )


@dataclass
class ModelConfig:
    """Configuration for a model endpoint.

    Attributes:
        model_id: Unique identifier for this model configuration.
        api_base: Base URL of the model API (e.g. "http://localhost:11434").
        model_name: Model name as recognized by the API (e.g. "qwen3.5:9b").
        api_key: API key for authentication (empty string if not needed).
        model_type: Type of model - "llm" or "vlm".
        api_protocol: API protocol - "openai" or "ollama".
        generate_params: Additional generation parameters (temperature, top_p, etc.).
    """

    model_id: str
    api_base: str
    model_name: str
    api_key: str = ""
    model_type: str = "llm"
    api_protocol: str = "openai"
    generate_params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to a dictionary."""
        return {
            "model_id": self.model_id,
            "api_base": self.api_base,
            "model_name": self.model_name,
            "api_key": self.api_key,
            "model_type": self.model_type,
            "api_protocol": self.api_protocol,
            "generate_params": dict(self.generate_params),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Deserialize from a dictionary."""
        return cls(
            model_id=data["model_id"],
            api_base=data["api_base"],
            model_name=data["model_name"],
            api_key=data.get("api_key", ""),
            model_type=data.get("model_type", "llm"),
            api_protocol=data.get("api_protocol", "openai"),
            generate_params=data.get("generate_params", {}),
        )


@dataclass
class ToolConfig:
    """Configuration for a tool (function, MCP, or skill).

    Attributes:
        tool_id: Unique identifier for this tool.
        tool_type: Type of tool - "function", "mcp", or "skill".
        name: Display name of the tool.
        description: Human-readable description.
        parameters: OpenAI function calling JSON Schema for parameters.
        mcp_server_name: MCP server name (MCP tools only).
        tool_name: Original tool name on the MCP server (MCP tools only).
        steps: List of step definitions (skill tools only).
    """

    tool_id: str
    tool_type: str
    name: str
    description: str
    parameters: dict
    mcp_server_name: Optional[str] = None
    tool_name: Optional[str] = None
    steps: Optional[list] = None
    function_file_path: Optional[str] = None
    function_name: Optional[str] = None
    skill_dir: Optional[str] = None
    builtin: bool = False

    def to_dict(self) -> dict:
        """Serialize to a dictionary, omitting None optional fields."""
        d: dict = {
            "tool_id": self.tool_id,
            "tool_type": self.tool_type,
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }
        if self.mcp_server_name is not None:
            d["mcp_server_name"] = self.mcp_server_name
        if self.tool_name is not None:
            d["tool_name"] = self.tool_name
        if self.steps is not None:
            d["steps"] = [dict(s) for s in self.steps]
        if self.function_file_path is not None:
            d["function_file_path"] = self.function_file_path
        if self.function_name is not None:
            d["function_name"] = self.function_name
        if self.skill_dir is not None:
            d["skill_dir"] = self.skill_dir
        if self.builtin:
            d["builtin"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ToolConfig":
        """Deserialize from a dictionary."""
        return cls(
            tool_id=data["tool_id"],
            tool_type=data["tool_type"],
            name=data["name"],
            description=data["description"],
            parameters=data["parameters"],
            mcp_server_name=data.get("mcp_server_name"),
            tool_name=data.get("tool_name"),
            steps=data.get("steps"),
            function_file_path=data.get("function_file_path"),
            function_name=data.get("function_name"),
            skill_dir=data.get("skill_dir"),
            builtin=data.get("builtin", False),
        )


@dataclass
class InferenceRequest:
    """A request to perform model inference.

    Attributes:
        model_id: ID of the model to use (must exist in ModelRegistry).
        tool_ids: List of tool IDs to make available during inference.
        messages: Optional pre-built message list.
        text: Optional plain text input (convenience shortcut).
        stream: Whether to use streaming response mode.
        max_tool_rounds: Maximum number of tool call rounds before stopping.
    """

    model_id: str
    tool_ids: list = field(default_factory=list)
    messages: Optional[list] = None
    text: Optional[str] = None
    stream: bool = False
    max_tool_rounds: int = 20


@dataclass
class TokenStat:
    """Per-round inference statistics: token counts and timing.

    Attributes:
        prompt_tokens: Input tokens consumed this round.
        completion_tokens: Output tokens generated this round.
        total_tokens: prompt_tokens + completion_tokens for this round.
        ttft_ms: Time-to-first-token in milliseconds (stream only; None for non-stream).
        net_ms: Net inference time in milliseconds for this round (request → last token).
        total_ms: Wall-clock time for this round including tool calls.
        overall_ms: Total wall-clock time for the entire multi-round loop (last round only).
        total_prompt_tokens: Cumulative prompt tokens across all rounds so far.
        total_completion_tokens: Cumulative completion tokens across all rounds so far.
        total_all_tokens: Cumulative total tokens across all rounds so far.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    ttft_ms: Optional[float] = None
    net_ms: Optional[float] = None
    total_ms: Optional[float] = None
    overall_ms: Optional[float] = None
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_all_tokens: int = 0

    def to_dict(self) -> dict:
        d: dict = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_all_tokens": self.total_all_tokens,
        }
        if self.ttft_ms is not None:
            d["ttft_ms"] = round(self.ttft_ms, 1)
        if self.net_ms is not None:
            d["net_ms"] = round(self.net_ms, 1)
        if self.total_ms is not None:
            d["total_ms"] = round(self.total_ms, 1)
        if self.overall_ms is not None:
            d["overall_ms"] = round(self.overall_ms, 1)
        return d


@dataclass
class InferenceResult:
    """Result of a model inference call.

    Attributes:
        success: Whether the inference completed successfully.
        messages: Complete conversation history including tool calls.
        error: Error description if success is False.
        error_code: Machine-readable error code if success is False.
        stat: Aggregated token and timing statistics across all inference rounds.
    """

    success: bool
    messages: list = field(default_factory=list)
    error: Optional[str] = None
    error_code: Optional[str] = None
    stat: Optional["TokenStat"] = None

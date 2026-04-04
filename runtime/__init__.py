"""Composable Agent Runtime - a minimal, zero-dependency agent runtime engine."""

from runtime.models import (
    InferenceRequest,
    InferenceResult,
    Message,
    ModelConfig,
    ToolConfig,
)
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.mcp_client import MCPClientManager
from runtime.skill_manager import SkillManager
from runtime.runtime import Runtime
from runtime.server import RuntimeHTTPServer
from runtime.tools import register_function_tool
from runtime.builtin_tools import register_builtin_tools

__all__ = [
    # Data models
    "Message",
    "ModelConfig",
    "ToolConfig",
    "InferenceRequest",
    "InferenceResult",
    # Registries
    "ModelRegistry",
    "ToolRegistry",
    # Core runtime
    "Runtime",
    # MCP
    "MCPClientManager",
    # Skill
    "SkillManager",
    # HTTP server
    "RuntimeHTTPServer",
    # Decorators
    "register_function_tool",
    # Built-in tools
    "register_builtin_tools",
]

"""Function Tool registration via decorator.

Provides `register_function_tool`, a decorator that automatically extracts
parameter types from annotations and descriptions from docstrings to generate
an OpenAI function calling compatible ToolConfig, then registers it in a
ToolRegistry.
"""

import inspect
import re
from typing import Optional

from runtime.models import ToolConfig
from runtime.registry import ToolRegistry

# Python type -> JSON Schema type mapping
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json_schema(annotation: type) -> str:
    """Map a Python type annotation to a JSON Schema type string.

    Falls back to "string" for unknown types.
    """
    return _TYPE_MAP.get(annotation, "string")


def _parse_docstring(fn) -> tuple[str, dict[str, str]]:
    """Extract function description and parameter descriptions from docstring.

    Supports Google-style docstrings with an ``Args:`` section::

        def foo(x: int, y: str):
            \"\"\"Short description.

            Args:
                x: The x value.
                y: The y value.
            \"\"\"

    Returns:
        A tuple of (function_description, {param_name: param_description}).
    """
    doc = inspect.getdoc(fn) or ""
    if not doc:
        return "", {}

    # Split on the "Args:" header (case-insensitive)
    parts = re.split(r"^\s*Args\s*:\s*$", doc, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)

    func_desc = parts[0].strip()

    param_descs: dict[str, str] = {}
    if len(parts) > 1:
        args_block = parts[1]
        # Stop at the next section header (e.g. Returns:, Raises:)
        next_section = re.search(r"^\s*\w+\s*:", args_block, flags=re.MULTILINE)
        # Be more careful: only match section headers that start at the beginning of a line
        # and are NOT indented like param lines
        next_section = re.search(r"^[A-Z]\w*\s*:", args_block, flags=re.MULTILINE)
        if next_section:
            args_block = args_block[: next_section.start()]

        # Parse individual parameter lines: "    param_name: description"
        # May continue on subsequent indented lines
        current_param: Optional[str] = None
        current_desc_lines: list[str] = []

        for line in args_block.splitlines():
            # Match "    param_name: description" or "    param_name (type): description"
            m = re.match(r"^\s{2,}(\w+)(?:\s*\([^)]*\))?\s*:\s*(.*)", line)
            if m:
                # Save previous param
                if current_param is not None:
                    param_descs[current_param] = " ".join(current_desc_lines).strip()
                current_param = m.group(1)
                current_desc_lines = [m.group(2).strip()] if m.group(2).strip() else []
            elif current_param is not None and line.strip():
                # Continuation line
                current_desc_lines.append(line.strip())

        # Save last param
        if current_param is not None:
            param_descs[current_param] = " ".join(current_desc_lines).strip()

    return func_desc, param_descs


def _extract_tool_config(fn, name: Optional[str] = None, description: Optional[str] = None) -> ToolConfig:
    """Build a ToolConfig from a Python function's signature and docstring.

    Args:
        fn: The function to inspect.
        name: Override for the tool/function name.
        description: Override for the tool description.

    Returns:
        A ToolConfig with tool_type="function" and an OpenAI-compatible
        JSON Schema in the parameters field.
    """
    tool_name = name or fn.__name__

    doc_desc, param_descs = _parse_docstring(fn)
    tool_description = description or doc_desc or f"Function {tool_name}"

    sig = inspect.signature(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        # Determine JSON Schema type from annotation
        if param.annotation is not inspect.Parameter.empty:
            json_type = _python_type_to_json_schema(param.annotation)
        else:
            json_type = "string"

        prop: dict = {"type": json_type}
        if param_name in param_descs:
            prop["description"] = param_descs[param_name]

        properties[param_name] = prop

        # Parameters without defaults are required
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    parameters = {
        "type": "object",
        "properties": properties,
        "required": required,
    }

    return ToolConfig(
        tool_id=tool_name,
        tool_type="function",
        name=tool_name,
        description=tool_description,
        parameters=parameters,
    )


def register_function_tool(registry: ToolRegistry, name: str = None, description: str = None):
    """Decorator: register a Python function as a Function Tool.

    Automatically extracts parameter types from annotations and descriptions
    from the docstring to build an OpenAI function calling compatible ToolConfig,
    then registers both the config and the callable in the given ToolRegistry.

    Args:
        registry: The ToolRegistry to register the tool in.
        name: Optional custom tool name (defaults to function name).
        description: Optional custom description (defaults to docstring).

    Returns:
        A decorator that registers the function and returns it unchanged.

    Example::

        registry = ToolRegistry()

        @register_function_tool(registry)
        def get_weather(city: str, units: str = "celsius"):
            \"\"\"Get weather for a city.

            Args:
                city: The city name.
                units: Temperature units.
            \"\"\"
            ...
    """
    def decorator(fn):
        tool_config = _extract_tool_config(fn, name, description)
        registry.register(tool_config, callable_fn=fn)
        return fn
    return decorator

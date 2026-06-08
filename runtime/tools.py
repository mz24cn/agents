"""Function Tool registration via decorator.

Provides `register_function_tool`, a decorator that automatically extracts
parameter types from annotations and descriptions from docstrings to generate
an OpenAI function calling compatible ToolConfig, then registers it in a
ToolRegistry.
"""

import inspect
import re
import base64
import os
import time
import logging

from typing import Optional

logger = logging.getLogger(__name__)

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
            json_type = _TYPE_MAP.get(param.annotation, "string")
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


def is_likely_base64(value: str, threshold: int = 256) -> bool:
    """判断字符串是否看起来像 base64 编码内容。

    检测逻辑：
    1. 长度必须大于阈值（默认256字符）
    2. 只包含 base64 合法字符（A-Z, a-z, 0-9, +, /, =）
    3. 末尾可能有 0-2 个 '=' 填充符

    Args:
        value: 要检测的字符串
        threshold: 长度阈值，低于此值认为不是 base64（可能是文件路径）

    Returns:
        True 如果看起来像 base64，False 否则
    """
    if not isinstance(value, str):
        return False
    
    # 长度检查：base64 编码的文件内容通常很长
    if len(value) < threshold:
        return False
    
    # 字符合法性检查：只包含 base64 字符集
    # 移除末尾的 '=' 填充符后检查
    content = value.rstrip('=')
    if not content:
        return False
    
    # base64 字符集：A-Z, a-z, 0-9, +, /
    import string
    valid_chars = set(string.ascii_letters + string.digits + '+/')
    return all(c in valid_chars for c in content)


def convert_file_path_to_base64(value: str) -> tuple[str, bool]:
    """尝试将文件路径转换为 base64 编码内容。

    如果输入看起来不是 base64（长度较短），则尝试将其作为文件路径打开，
    成功则返回 base64 编码内容。

    Args:
        value: 可能是文件路径的字符串

    Returns:
        tuple: (结果字符串, 是否进行了转换)
        - 如果转换成功：(base64_content, True)
        - 如果无需转换或失败：(original_value, False)
    """
    # 如果已经是 base64，直接返回
    if is_likely_base64(value):
        return value, False
    
    # 尝试作为文件路径打开
    try:
        # 处理路径：支持正斜杠和反斜杠
        file_path = value.replace('/', os.sep).replace('\\', os.sep)
        
        # 检查文件是否存在
        if not os.path.exists(file_path):
            return value, False
        
        # 检查是否是文件（不是目录）
        if not os.path.isfile(file_path):
            return value, False
        
        # 读取文件并转换为 base64
        with open(file_path, 'rb') as f:
            file_content = f.read()
        
        # 转换为 base64 字符串
        base64_content = base64.b64encode(file_content).decode('utf-8')
        return base64_content, True
        
    except (OSError, IOError, PermissionError):
        # 文件读取失败，返回原值
        return value, False
    except Exception:
        # 其他异常，返回原值
        return value, False


def process_tool_arguments_for_base64(arguments: dict) -> dict:
    """处理工具调用参数，自动将文件路径转换为 base64。

    检测参数名包含 'base64' 的字段，如果其值看起来不是 base64
    （长度小于阈值），则尝试将其作为文件路径打开并转换。

    Args:
        arguments: 工具调用参数字典

    Returns:
        处理后的参数字典
    """
    for param_name, original_value in list(arguments.items()):
        if 'base64' not in param_name.lower():
            continue

        # 只处理字符串类型
        if isinstance(original_value, str):
            converted_value, was_converted = convert_file_path_to_base64(original_value)

            if was_converted:
                arguments[param_name] = converted_value
                # 添加元数据，便于调试
                arguments['_original_path'] = original_value
                arguments['_converted_to_base64'] = True
                logger.info(f"自动转换文件路径到 base64: {param_name} = {original_value} -> (已转换, 长度 {len(converted_value)})")
            else:
                logger.debug(f"参数 {param_name} 无需转换 (长度 {len(original_value)}, 看起来像 base64: {is_likely_base64(original_value)})")

    return arguments


def save_and_replace_base64(text: str, output_dir: str ="/tmp"):
    # 1. 确保输出目录存在
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 2. 正则表达式：匹配 "key": "base64内容"
    # 这里的 key 涵盖了 windows-mcp、chrome-devtools 和 file-transfer-mcp 常用的字段名
    # {100,} 确保只抓取长字符串，避免误伤普通的 JSON 字段
    pattern = r'"(screenshot|data|image|base64|base64_content|base64_data|image_base64)":\s*"([A-Za-z0-9+/]{100,}={0,2})"'

    def replace_logic(match):
        # match.group(1) 是原来的 key，group(2) 是 base64 内容
        b64_content = match.group(2)
        
        # 尝试解码 base64，失败则返回原始匹配
        try:
            decoded_bytes = base64.b64decode(b64_content)
        except Exception:
            # 解码失败，说明不是真正的 base64，返回原始匹配
            return match.group(0)
        
        # 生成唯一文件名
        file_name = f"snap_{int(time.time()*1000)}.png"
        file_path = os.path.abspath(os.path.join(output_dir, file_name))
        
        # 写入文件
        with open(file_path, "wb") as f:
            f.write(decoded_bytes)
        
        # 返回替换后的内容：将 key 换成 filePath，将内容换成路径
        # 注意：Windows 路径需要处理斜杠，这里统一用正斜杠
        safe_path = file_path.replace("\\", "/")
        return f'"filePath": "{safe_path}"'

    # 3. 执行全局替换
    return re.sub(pattern, replace_logic, text)

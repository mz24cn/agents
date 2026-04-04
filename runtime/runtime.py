"""Runtime Engine for the Composable Agent Runtime.

Provides the Runtime class which orchestrates model inference and tool execution.
Supports dynamic composition of models and tools at runtime, with automatic
tool call loop handling. Only uses Python standard library modules.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Iterator, Optional

from runtime.models import (
    InferenceRequest,
    InferenceResult,
    Message,
    ToolConfig,
)
from runtime.protocols import PROTOCOL_MAP
from runtime.registry import ModelRegistry, ToolRegistry


class Runtime:
    """Core runtime engine that coordinates model inference and tool execution.

    Accepts a model_id and a set of tool_ids to dynamically compose an
    inference session. Handles the tool call loop automatically,
    dispatching tool_calls responses to the appropriate tool and feeding
    results back to the model.
    """

    def __init__(
        self,
        model_registry: ModelRegistry,
        tool_registry: ToolRegistry,
        mcp_manager: Optional[object] = None,
        skill_manager: Optional[object] = None,
    ) -> None:
        """Initialize the Runtime.

        Args:
            model_registry: Registry containing model configurations.
            tool_registry: Registry containing tool configurations and callables.
            mcp_manager: Optional MCPClientManager for MCP tool execution.
            skill_manager: Optional SkillManager for Skill progressive disclosure.
        """
        self._model_registry = model_registry
        self._tool_registry = tool_registry
        self._mcp_manager = mcp_manager
        self._skill_manager = skill_manager

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_messages(request: InferenceRequest) -> list:
        """Normalize request input into a list of Message objects.

        If request.text is set, wraps it as a single user message.
        If request.messages is set, uses them directly.
        If both are set, messages takes precedence.

        Args:
            request: The inference request to normalize.

        Returns:
            A list of Message objects ready for the protocol adapter.
        """
        if request.messages is not None and len(request.messages) > 0:
            return list(request.messages)
        if request.text is not None:
            return [Message(role="user", content=request.text)]
        return []

    # ------------------------------------------------------------------
    # Core inference
    # ------------------------------------------------------------------

    def infer(self, request: InferenceRequest) -> InferenceResult:
        """Execute a model inference with optional tool call loop.

        Steps:
            1. Get model config from ModelRegistry
            2. Get tool configs and schemas from ToolRegistry
            3. Select Protocol Adapter based on api_protocol
            4. Normalize input messages
            5. Build and send HTTP request via urllib.request
            6. Parse response; if tool_calls present, execute tools and loop
            7. Stop when no tool calls or max_tool_rounds reached

        Args:
            request: The inference request specifying model, tools, and input.

        Returns:
            An InferenceResult with the conversation history and status.
        """
        # 1. Get model config
        model_config = self._model_registry.get(request.model_id)
        if model_config is None:
            return InferenceResult(
                success=False,
                error=f"Model '{request.model_id}' not found in registry",
                error_code="MODEL_NOT_FOUND",
            )

        # 2. Get tool configs
        tools: list[ToolConfig] = []
        for tool_id in request.tool_ids:
            tool_config = self._tool_registry.get(tool_id)
            if tool_config is not None:
                tools.append(tool_config)

        # 3. Select protocol adapter
        protocol_name = model_config.api_protocol
        protocol_cls = PROTOCOL_MAP.get(protocol_name)
        if protocol_cls is None:
            return InferenceResult(
                success=False,
                error=f"Unsupported api_protocol: '{protocol_name}'",
                error_code="PROTOCOL_NOT_FOUND",
            )
        protocol = protocol_cls()

        # 4. Normalize input messages
        messages = self._normalize_messages(request)

        # 5-7. Inference + tool call loop
        tool_round = 0
        while True:
            # Build HTTP request
            url, headers, body_bytes = protocol.build_request(
                config=model_config,
                messages=messages,
                tools=tools if tools else None,
                stream=False,
            )

            # Send HTTP request
            try:
                http_req = urllib.request.Request(
                    url, data=body_bytes, headers=headers, method="POST"
                )
                with urllib.request.urlopen(http_req) as http_resp:
                    response_data = http_resp.read()
            except urllib.error.HTTPError as exc:
                error_body = ""
                try:
                    error_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                return InferenceResult(
                    success=False,
                    messages=messages,
                    error=f"HTTP {exc.code}: {exc.reason}. {error_body}".strip(),
                    error_code=str(exc.code),
                )
            except urllib.error.URLError as exc:
                return InferenceResult(
                    success=False,
                    messages=messages,
                    error=f"Connection error: {exc.reason}",
                    error_code="CONNECTION_ERROR",
                )
            except Exception as exc:
                return InferenceResult(
                    success=False,
                    messages=messages,
                    error=f"Request failed: {exc}",
                    error_code="REQUEST_ERROR",
                )

            # Parse response
            try:
                response_messages = protocol.parse_response(response_data, stream=False)
            except Exception as exc:
                return InferenceResult(
                    success=False,
                    messages=messages,
                    error=f"Response parse error: {exc}",
                    error_code="PARSE_ERROR",
                )

            if not response_messages:
                return InferenceResult(
                    success=False,
                    messages=messages,
                    error="Empty response from model",
                    error_code="EMPTY_RESPONSE",
                )

            # Add assistant response to conversation
            assistant_msg = response_messages[0]
            messages.append(assistant_msg)

            # Determine tool calls to execute
            tool_calls_to_execute = assistant_msg.tool_calls

            if not tool_calls_to_execute:
                # No tool call — inference complete
                break

            # Check max_tool_rounds (once per inference round, not per tool call)
            tool_round += 1
            if tool_round > request.max_tool_rounds:
                # Exceeded max rounds — stop looping
                break

            # Execute all tool calls sequentially in this round
            skill_triggered = False
            for fn_call in tool_calls_to_execute:
                tool_name = fn_call.get("name", "")
                arguments_str = fn_call.get("arguments", "{}")

                # Check if this is a Skill — trigger progressive disclosure
                if self._is_skill_tool(tool_name):
                    # Progressive disclosure: inject SKILL.md body + built-in tools
                    skill_body, skill_dir = self._get_skill_body_and_dir(tool_name)

                    # Inject the full SKILL.md body as a system message
                    if skill_body:
                        cwd_hint = f"\n\n技能工作目录: {skill_dir}" if skill_dir else ""
                        messages.append(
                            Message(
                                role="system",
                                content=(
                                    f"用户选择了 {tool_name} 技能。以下是该技能的详细文档，"
                                    f"请根据文档内容和用户的原始请求，使用 bash 或 fetch 等"
                                    f"内置工具来执行相应操作。{cwd_hint}\n\n{skill_body}"
                                ),
                            )
                        )

                    # Add built-in tools (bash, fetch) to the tools list
                    self._ensure_builtin_tools(tools)

                    # Remove the Skill itself from tools to avoid re-selection
                    tools = [t for t in tools if t.tool_id != tool_name]

                    # Don't consume a tool_round for skill disclosure
                    tool_round -= 1
                    skill_triggered = True
                    break  # Break inner loop; continue outer while loop

                # Parse arguments
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except (json.JSONDecodeError, ValueError):
                    arguments = {}

                # Execute tool and get result
                tool_result = self._execute_tool_call(tool_name, arguments, tool_scope=tools)

                # Add tool result as function role message
                messages.append(
                    Message(
                        role="function",
                        content=tool_result,
                        name=tool_name,
                    )
                )

            if skill_triggered:
                continue

        return InferenceResult(success=True, messages=messages)

    # ------------------------------------------------------------------
    # Direct tool call (public API)
    # ------------------------------------------------------------------

    def call_tool(self, tool_id: str, arguments: dict) -> str:
        """Directly call a tool by its tool_id, bypassing model inference.

        Args:
            tool_id: The unique identifier of the tool to call.
            arguments: The arguments dict to pass to the tool.

        Returns:
            The tool result as a string, or an error message string.
        """
        tool_config = self._tool_registry.get(tool_id)
        if tool_config is None:
            # Also try by name
            tool_config = self._find_tool_by_name(tool_id)
        if tool_config is None:
            return f"Error: tool '{tool_id}' not found in registry"

        if tool_config.tool_type == "function":
            return self._execute_function_tool(tool_config, arguments)
        elif tool_config.tool_type == "mcp":
            return self._execute_mcp_tool(tool_config, arguments)
        else:
            return f"Error: unsupported tool_type '{tool_config.tool_type}' for tool '{tool_id}'"

    # ------------------------------------------------------------------
    # Tool execution helpers
    # ------------------------------------------------------------------

    def _execute_tool_call(
        self, tool_name: str, arguments: dict, tool_scope: Optional[list] = None
    ) -> str:
        """Execute a tool call by name.

        Looks up the tool within tool_scope first (the set of tools sent in the
        current inference request), then falls back to the full ToolRegistry.
        This prevents name collisions when multiple tools share the same name
        (e.g. a function tool and an MCP tool both named "fetch").

        Args:
            tool_name: The name of the tool to execute.
            arguments: The arguments dict to pass to the tool.
            tool_scope: The list of ToolConfig objects that were included in the
                current inference request. When provided, name lookup is
                restricted to this set before falling back to the registry.

        Returns:
            The tool result as a string, or an error message string.
        """
        tool_config = self._find_tool_by_name(tool_name, scope=tool_scope)

        if tool_config is None:
            return f"Error: tool '{tool_name}' not found in registry"

        if tool_config.tool_type == "function":
            return self._execute_function_tool(tool_config, arguments)
        elif tool_config.tool_type == "mcp":
            return self._execute_mcp_tool(tool_config, arguments)
        elif tool_config.tool_type == "skill":
            return f"Error: skill '{tool_name}' should be triggered via progressive disclosure, not direct execution"
        else:
            return f"Error: unsupported tool_type '{tool_config.tool_type}' for tool '{tool_name}'"

    def _find_tool_by_name(
        self, tool_name: str, scope: Optional[list] = None
    ) -> Optional[ToolConfig]:
        """Find a tool config by name.

        When scope is provided, searches within that list first (by name field,
        then by tool_id). Only falls back to the full ToolRegistry if not found
        in scope. This ensures that when the same tool name exists in both a
        function tool and an MCP tool, the one actually sent to the model is
        the one that gets executed.

        Args:
            tool_name: The tool name to search for.
            scope: Optional list of ToolConfig objects to search within first.

        Returns:
            The matching ToolConfig, or None if not found.
        """
        # Search within the request scope first
        if scope:
            for tc in scope:
                if tc.name == tool_name or tc.tool_id == tool_name:
                    return tc

        # Fall back: try direct lookup by tool_id in registry
        config = self._tool_registry.get(tool_name)
        if config is not None:
            return config

        # Fall back: search by name field in registry
        for tc in self._tool_registry.list_all():
            if tc.name == tool_name:
                return tc

        return None

    def _ensure_builtin_tools(self, tools: list) -> None:
        """Ensure built-in tools (bash, fetch) are registered and in the tools list.

        Lazily registers callables if missing, and appends ToolConfigs to the
        provided tools list if not already present. Does not overwrite existing
        tools that share the same tool_id (e.g. a user-registered fetch tool).
        """
        from runtime.builtin_tools import BUILTIN_TOOLS, register_builtin_tools
        if self._tool_registry.get_callable("bash") is None:
            register_builtin_tools(self._tool_registry)
        for bt_config, bt_fn in BUILTIN_TOOLS:
            # Only register if no callable exists yet for this tool_id
            if self._tool_registry.get_callable(bt_config.tool_id) is None:
                self._tool_registry.register(bt_config, callable_fn=bt_fn)
            if bt_config not in tools:
                tools.append(bt_config)

    def _get_skill_body_and_dir(self, tool_name: str) -> tuple[Optional[str], Optional[str]]:
        """Get skill body and dir, preferring skill_manager then falling back to ToolConfig.skill_dir.

        This makes skill progressive disclosure resilient to skill_manager state loss
        (e.g. after server restart without proper SkillManager restoration).

        Returns:
            Tuple of (skill_body, skill_dir), either may be None.
        """
        # Try skill_manager first (has parsed body in memory)
        if self._skill_manager and self._skill_manager.is_skill(tool_name):
            return (
                self._skill_manager.get_skill_body(tool_name),
                self._skill_manager.get_skill_dir(tool_name),
            )

        # Fall back: read SKILL.md directly from ToolConfig.skill_dir
        tool_config = self._find_tool_by_name(tool_name)
        if tool_config and tool_config.tool_type == "skill" and tool_config.skill_dir:
            skill_md_path = os.path.join(tool_config.skill_dir, "SKILL.md")
            try:
                with open(skill_md_path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Strip front-matter to get body
                content = content.strip()
                if content.startswith("---"):
                    end_idx = content.find("---", 3)
                    if end_idx != -1:
                        body = content[end_idx + 3:].strip()
                        return body, tool_config.skill_dir
            except OSError:
                pass

        return None, None

    def _is_skill_tool(self, tool_name: str) -> bool:
        """Check if tool_name refers to a skill, using both skill_manager and ToolRegistry."""
        if self._skill_manager and self._skill_manager.is_skill(tool_name):
            return True
        tool_config = self._find_tool_by_name(tool_name)
        return tool_config is not None and tool_config.tool_type == "skill"

    def _execute_function_tool(self, tool_config: ToolConfig, arguments: dict) -> str:
        """Execute a function-type tool.

        Args:
            tool_config: The tool configuration.
            arguments: Arguments to pass to the function.

        Returns:
            The function result as a string, or an error message.
        """
        callable_fn = self._tool_registry.get_callable(tool_config.tool_id)
        if callable_fn is None:
            return f"Error: no callable registered for tool '{tool_config.tool_id}'"

        try:
            result = callable_fn(**arguments)
            return str(result) if result is not None else ""
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    def _execute_mcp_tool(self, tool_config: ToolConfig, arguments: dict) -> str:
        """Execute an MCP-type tool via MCPClientManager.

        Args:
            tool_config: The tool configuration with mcp_server_name and tool_name.
            arguments: Arguments to pass to the MCP tool.

        Returns:
            The tool result as a string, or an error message.
        """
        if self._mcp_manager is None:
            return f"Error: MCPClientManager not available for tool '{tool_config.name}'"

        server_name = tool_config.mcp_server_name
        mcp_tool_name = tool_config.tool_name or tool_config.name

        if server_name is None:
            return f"Error: mcp_server_name not set for tool '{tool_config.name}'"

        try:
            result = self._mcp_manager.call_tool(server_name, mcp_tool_name, arguments)
            return str(result) if result is not None else ""
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # Skill execution
    # ------------------------------------------------------------------

    def execute_skill(self, skill_id: str, context: dict) -> InferenceResult:
        """Execute a Skill multi-step workflow.

        Looks up the skill by skill_id in the ToolRegistry, then iterates
        through its steps in order. Each step's output becomes the next
        step's input (prev_result).

        Step types:
            - "tool": Looks up tool by target name in ToolRegistry and executes it.
              Arguments are mapped via args_mapping, where "prev_result" values
              are replaced with the actual previous step's result.
            - "inference": Creates an InferenceRequest with the specified model_id,
              formats prompt_template with prev_result, and calls self.infer().

        Args:
            skill_id: The tool_id of the skill to execute.
            context: Initial context dict (e.g. {"video_path": "/path/to/video"}).

        Returns:
            InferenceResult with success=True and conversation history on success,
            or success=False with error message including the failed step index.
        """
        # Look up the skill in ToolRegistry
        skill_config = self._tool_registry.get(skill_id)
        if skill_config is None:
            return InferenceResult(
                success=False,
                error=f"Skill '{skill_id}' not found in registry",
                error_code="SKILL_NOT_FOUND",
            )

        if skill_config.tool_type != "skill":
            return InferenceResult(
                success=False,
                error=f"Tool '{skill_id}' is not a skill (type='{skill_config.tool_type}')",
                error_code="NOT_A_SKILL",
            )

        steps = skill_config.steps
        if not steps:
            return InferenceResult(
                success=False,
                error=f"Skill '{skill_id}' has no steps defined",
                error_code="NO_STEPS",
            )

        prev_result = ""
        all_messages: list = []

        for step_index, step in enumerate(steps):
            step_type = step.get("type", "")

            if step_type == "tool":
                target = step.get("target", "")
                args_mapping = step.get("args_mapping", {})

                # Resolve arguments: replace "prev_result" with actual value,
                # otherwise look up from context
                resolved_args: dict = {}
                for param_name, source in args_mapping.items():
                    if source == "prev_result":
                        resolved_args[param_name] = prev_result
                    else:
                        resolved_args[param_name] = context.get(source, "")

                # Find and execute the tool
                tool_config = self._find_tool_by_name(target)
                if tool_config is None:
                    return InferenceResult(
                        success=False,
                        messages=all_messages,
                        error=f"Step {step_index} failed: tool '{target}' not found in registry",
                        error_code="STEP_TOOL_NOT_FOUND",
                    )

                if tool_config.tool_type == "function":
                    result_str = self._execute_function_tool(tool_config, resolved_args)
                elif tool_config.tool_type == "mcp":
                    result_str = self._execute_mcp_tool(tool_config, resolved_args)
                else:
                    return InferenceResult(
                        success=False,
                        messages=all_messages,
                        error=f"Step {step_index} failed: unsupported tool_type '{tool_config.tool_type}' for target '{target}'",
                        error_code="STEP_TOOL_TYPE_ERROR",
                    )

                # Check if the tool returned an error string
                if result_str.startswith("Error:"):
                    return InferenceResult(
                        success=False,
                        messages=all_messages,
                        error=f"Step {step_index} failed: {result_str}",
                        error_code="STEP_TOOL_ERROR",
                    )

                prev_result = result_str

            elif step_type == "inference":
                model_id = step.get("model_id", "")
                prompt_template = step.get("prompt_template", "{prev_result}")

                # Format the prompt with prev_result
                prompt = prompt_template.replace("{prev_result}", prev_result)

                # Create an inference request and call self.infer()
                inference_request = InferenceRequest(
                    model_id=model_id,
                    text=prompt,
                )
                inference_result = self.infer(inference_request)

                if not inference_result.success:
                    return InferenceResult(
                        success=False,
                        messages=all_messages + inference_result.messages,
                        error=f"Step {step_index} failed: {inference_result.error}",
                        error_code=inference_result.error_code or "STEP_INFERENCE_ERROR",
                    )

                # Extract the assistant's response as prev_result
                all_messages.extend(inference_result.messages)
                # Get the last assistant message content as prev_result
                for msg in reversed(inference_result.messages):
                    if msg.role == "assistant":
                        prev_result = msg.content
                        break

            else:
                return InferenceResult(
                    success=False,
                    messages=all_messages,
                    error=f"Step {step_index} failed: unknown step type '{step_type}'",
                    error_code="STEP_UNKNOWN_TYPE",
                )

        return InferenceResult(success=True, messages=all_messages)

    # ------------------------------------------------------------------
    # Streaming inference
    # ------------------------------------------------------------------

    def infer_stream(self, request: InferenceRequest, cancel_event: Optional[object] = None) -> Iterator[Message]:
        """Streaming inference with full tool call loop and Skill progressive disclosure.

        Each inference round streams thinking/content tokens as they arrive.
        When a tool call is detected, the tool is executed and the result
        is yielded as a function-role Message, then the next round begins
        streaming. If no tools are specified, behaves as a simple single-round
        streaming call.

        Yields special marker Messages to help callers distinguish phases:
        - role="function": tool execution result (name field = tool name)
        - role="system": skill disclosure injection
        - role="assistant" with tool_calls: tool call intent from model
        - role="assistant" with thinking: reasoning trace chunk
        - role="assistant" with content: response content chunk

        Args:
            request: The inference request.

        Yields:
            Message objects incrementally.
        """
        # 1. Setup (same as infer)
        model_config = self._model_registry.get(request.model_id)
        if model_config is None:
            yield Message(role="assistant",
                          content=f"Error: Model '{request.model_id}' not found")
            return

        tools: list[ToolConfig] = []
        for tool_id in request.tool_ids:
            tc = self._tool_registry.get(tool_id)
            if tc is not None:
                tools.append(tc)

        protocol_name = model_config.api_protocol
        protocol_cls = PROTOCOL_MAP.get(protocol_name)
        if protocol_cls is None:
            yield Message(role="assistant",
                          content=f"Error: Unsupported protocol '{protocol_name}'")
            return
        protocol = protocol_cls()

        messages = self._normalize_messages(request)

        # 2. Streaming tool call loop
        tool_round = 0
        while True:
            url, headers, body_bytes = protocol.build_request(
                config=model_config, messages=messages,
                tools=tools if tools else None, stream=True,
            )

            try:
                http_req = urllib.request.Request(
                    url, data=body_bytes, headers=headers, method="POST")
                http_resp = urllib.request.urlopen(http_req)
            except urllib.error.HTTPError as exc:
                yield Message(role="assistant",
                              content=f"Error: HTTP {exc.code}: {exc.reason}")
                return
            except Exception as exc:
                yield Message(role="assistant", content=f"Error: {exc}")
                return

            # Stream this round and collect the full assistant message
            full_content = ""
            full_thinking = ""
            # Track tool calls by index for multi-tool support
            accumulated_tool_calls: dict[int, dict] = {}

            try:
                if protocol_name == "openai":
                    stream_iter = self._parse_openai_stream(http_resp)
                elif protocol_name == "ollama":
                    stream_iter = self._parse_ollama_stream(http_resp)
                else:
                    response_data = http_resp.read()
                    stream_iter = iter(protocol.parse_response(response_data, stream=True))

                for msg in stream_iter:
                    # Check for cancellation before yielding
                    if cancel_event is not None and cancel_event.is_set():
                        http_resp.close()
                        return
                    # Yield each chunk to the caller for real-time display
                    yield msg

                    # Accumulate for the conversation history
                    if msg.thinking:
                        full_thinking += msg.thinking
                    if msg.content:
                        full_content += msg.content
                    if msg.tool_calls:
                        # Tool calls arrive as a complete list (Ollama) or
                        # as individual delta dicts with _index (OpenAI)
                        for tc in msg.tool_calls:
                            idx = tc.pop("_index", None)
                            if idx is None:
                                idx = len(accumulated_tool_calls)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {"name": "", "arguments": ""}
                            if tc.get("name"):
                                accumulated_tool_calls[idx]["name"] += tc["name"]
                            if tc.get("arguments"):
                                accumulated_tool_calls[idx]["arguments"] += tc["arguments"]
            except Exception as exc:
                yield Message(role="assistant", content=f"Error: stream parse: {exc}")
                return
            finally:
                http_resp.close()

            # Build tool calls list from accumulated data
            all_tool_calls = None
            if accumulated_tool_calls:
                all_tool_calls = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]

            # Build the complete assistant message for conversation history
            assistant_msg = Message(
                role="assistant",
                content=full_content,
                thinking=full_thinking if full_thinking else None,
                tool_calls=all_tool_calls,
            )
            messages.append(assistant_msg)

            # Determine tool calls to execute
            tool_calls_to_execute = all_tool_calls

            # No tool call — done
            if not tool_calls_to_execute:
                return

            # Max rounds check
            tool_round += 1
            if tool_round > request.max_tool_rounds:
                return

            # Execute all tool calls sequentially in this round
            skill_triggered = False
            for fn_call in tool_calls_to_execute:
                tool_name = fn_call.get("name", "")
                arguments_str = fn_call.get("arguments", "{}")

                # Skill progressive disclosure
                if self._is_skill_tool(tool_name):
                    skill_body, skill_dir = self._get_skill_body_and_dir(tool_name)

                    if skill_body:
                        cwd_hint = f"\n\n技能工作目录: {skill_dir}" if skill_dir else ""
                        sys_msg = Message(
                            role="system",
                            content=(
                                f"用户选择了 {tool_name} 技能。以下是该技能的详细文档，"
                                f"请根据文档内容和用户的原始请求，使用 bash 或 fetch 等"
                                f"内置工具来执行相应操作。{cwd_hint}\n\n{skill_body}"
                            ),
                        )
                        messages.append(sys_msg)
                        yield sys_msg

                    self._ensure_builtin_tools(tools)
                    tools = [t for t in tools if t.tool_id != tool_name]
                    tool_round -= 1
                    skill_triggered = True
                    break  # Break inner loop; continue outer while loop

                # Execute tool
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except (json.JSONDecodeError, ValueError):
                    arguments = {}

                tool_result = self._execute_tool_call(tool_name, arguments, tool_scope=tools)

                tool_msg = Message(role="function", content=tool_result, name=tool_name)
                messages.append(tool_msg)
                yield tool_msg

            if skill_triggered:
                continue

    def _parse_openai_stream(self, http_resp: object) -> Iterator[Message]:
        """Parse an OpenAI SSE stream, yielding Message objects for each delta.

        Supports ``reasoning_content`` (thinking) alongside regular ``content``.
        Thinking chunks yield Message(thinking=..., content=""); content
        chunks yield Message(content=...).
        """
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
                return

            try:
                chunk = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue

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
                        if fn_name:
                            tc_dict["name"] = fn_name
                        if fn_args:
                            tc_dict["arguments"] = fn_args
                        yield Message(
                            role="assistant",
                            content="",
                            tool_calls=[tc_dict],
                        )

    def _parse_ollama_stream(self, http_resp: object) -> Iterator[Message]:
        """Parse an Ollama newline-delimited JSON stream, yielding Messages.

        Ollama thinking-capable models emit a ``thinking`` field in each chunk
        before the final ``content``. Thinking chunks yield a Message with
        ``thinking`` set and ``content`` empty; content chunks yield a Message
        with ``content`` set.

        Tool calls may arrive across multiple chunks (one per chunk) or all
        in a single final chunk. We accumulate them and yield once at the end.

        Args:
            http_resp: The HTTP response object with a readable stream.

        Yields:
            Message objects with incremental content or thinking.
        """
        # Accumulate tool calls across chunks — they may arrive one per chunk
        accumulated_tool_calls: dict[int, dict] = {}

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
                        "name": fn.get("name", ""),
                        "arguments": arguments,
                    }

            # Stop if done
            if chunk.get("done", False):
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



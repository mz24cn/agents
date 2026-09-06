"""Runtime Engine for the Agent Service.

Provides the Runtime class which orchestrates model inference and tool execution.
Supports dynamic composition of models and tools at runtime, with automatic
tool call loop handling. Only uses Python standard library modules.
"""

import concurrent.futures
import datetime
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from dataclasses import replace
from typing import Callable, Iterator, Optional

from runtime.common import (
    _thread_local,
    now_iso as _now_iso,
    is_likely_base64,
    snapshot_request_context,
    restore_request_context,
    estimate_chat_prompt_tokens,
    estimate_message_payload_tokens,
    env_int,
)

_logger = logging.getLogger("runtime.runtime")


def _now_precise_iso() -> str:
    """Return a wall-clock timestamp precise enough for execution analysis."""
    return datetime.datetime.now().isoformat(timespec="microseconds")


def _snapshot_tool_request_context() -> dict:
    """Capture worker context with a shared request-level journal holder.

    HTTP inference installs the holder during request preparation. Direct
    Runtime users and tests may not, so lazily add one on the caller thread
    before copying the context into a function-tool worker.
    """
    ctx = snapshot_request_context()
    if ctx.get("file_journal_holder") is None and ctx.get("session_dir"):
        from runtime.builtin_tools_coding import _FileJournalManagerHolder
        holder = _FileJournalManagerHolder()
        _thread_local.file_journal_holder = holder
        ctx["file_journal_holder"] = holder
    return ctx


# Socket timeout (seconds) for model API calls (connect + read).
# Covers the entire lifecycle: TCP handshake, TLS negotiation, waiting for
# first response bytes, and each subsequent read on streaming responses.
# Read dynamically via _get_model_api_timeout() so runtime env changes are
# picked up without restart.
def _get_model_api_timeout() -> int:
    """Return MODEL_API_TIMEOUT (seconds), read dynamically each call."""
    try:
        return int(os.environ.get("MODEL_API_TIMEOUT", "180"))
    except (TypeError, ValueError):
        return 300


# MODEL_INFER_TIMEOUT (seconds) caps one continuous streaming inference output:
# a single model round. It does NOT span tool-call rounds; each round restarts
# the clock. 0/empty/unset disables the guard (the default), and invalid values
# log a warning and leave the guard disabled.
def _get_model_infer_timeout() -> Optional[float]:
    """Return MODEL_INFER_TIMEOUT (seconds), read dynamically each call."""
    raw = os.environ.get("MODEL_INFER_TIMEOUT", "0").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "invalid MODEL_INFER_TIMEOUT=%r; inference timeout disabled", raw,
        )
        return None
    if val <= 0:
        return None
    return val


# Experimental repetitive-output detection. After MODEL_INFER_TIMEOUT fires,
# the final 100 chars are used as a fingerprint and searched backwards for two
# earlier complete matches. Visible content and hidden thinking are inspected
# independently. This is diagnostic-only because the timeout already ended the
# model round.
_LOOP_FINGERPRINT_CHARS = 100

# Window used by timeout diagnostics to distinguish active generation from a
# stream which produced output earlier and then went idle.
_INFER_RATE_WINDOW_SECONDS = 10.0


def _find_repetitive_output_tail(full_content: str) -> Optional[str]:
    """Return content from the second-last repeated tail occurrence onward.

    The final occurrence of the last 100 chars is the live tail itself and is
    excluded from the first backwards search. When a second earlier complete
    match is also found, the returned text spans from that match to the end so
    operators can inspect exactly what the model kept repeating.
    """
    if len(full_content) < _LOOP_FINGERPRINT_CHARS:
        return None
    tail = full_content[-_LOOP_FINGERPRINT_CHARS:]
    # Search once in everything except the final 100 chars.
    first = full_content.rfind(tail, 0, len(full_content) - _LOOP_FINGERPRINT_CHARS)
    if first < 0:
        return None
    # Search again strictly before the first match.
    second = full_content.rfind(tail, 0, first)
    if second < 0:
        return None
    return full_content[second:]


# Model transport retries are deliberately implemented below the inference/tool
# loop.  A retry therefore re-sends only the current model request and never
# repeats an already executed tool call.  MODEL_API_MAX_RETRIES is the number
# of additional attempts after the first request.
def _get_model_api_max_retries() -> int:
    """Return the number of transient model API retries (default: 2)."""
    try:
        return max(0, int(os.environ.get("MODEL_API_MAX_RETRIES", "2")))
    except (TypeError, ValueError):
        return 2


def _get_model_api_retry_delay() -> float:
    """Return the initial exponential-backoff delay in seconds (default: 1)."""
    try:
        return max(0.0, float(os.environ.get("MODEL_API_RETRY_DELAY", "1")))
    except (TypeError, ValueError):
        return 1.0


_RETRYABLE_MODEL_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 524})


def _is_timeout_error(exc: BaseException) -> bool:
    """Recognize direct and urllib-wrapped socket timeout failures."""
    reason = getattr(exc, "reason", exc)
    return (
        isinstance(exc, (socket.timeout, TimeoutError))
        or isinstance(reason, (socket.timeout, TimeoutError))
        or "timed out" in str(reason).lower()
        or "timeout" in str(reason).lower()
    )


def _is_retryable_model_error(exc: BaseException) -> bool:
    """Whether a model transport failure is safe to retry before output."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_MODEL_HTTP_CODES
    if isinstance(exc, (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError)):
        return True
    return _is_timeout_error(exc)


def _wait_model_retry(attempt: int, cancel_event: Optional[object] = None) -> bool:
    """Wait before retrying; return False if cancellation was requested.

    ``attempt`` is 1 for the first retry, producing delays of base, 2*base,
    4*base, ... .  The short polling interval keeps streaming abort responsive.
    """
    delay = _get_model_api_retry_delay() * (2 ** max(0, attempt - 1))
    deadline = time.monotonic() + delay
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 0.2))


def _get_tool_exec_workers() -> int:
    """Return the maximum number of concurrent function-tool workers.

    The value is read dynamically so deployments can tune it without changing
    Runtime construction.  ``1`` preserves declaration-order execution while
    still running every function callable on its isolated worker thread.
    Invalid and non-positive values fall back to the safe default of ``1``.
    """
    raw = os.environ.get("TOOL_EXEC_WORKERS", "1").strip()
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "invalid TOOL_EXEC_WORKERS=%r; using default 1", raw,
        )
        return 1
    if workers <= 0:
        _logger.warning(
            "non-positive TOOL_EXEC_WORKERS=%r; using default 1", raw,
        )
        return 1
    return workers


def _get_tool_exec_timeout() -> Optional[float]:
    """Return TOOL_EXEC_TIMEOUT (seconds), read dynamically each call.

    Single wall-clock cap shared by EVERY tool-call path -- this is the
    total, maximum execution time allowed for a tool call:

      - function tools (in-process callables run on a worker thread),
      - stdio MCP (whole request/response round),
      - HTTP MCP (whole request/response round, including SSE reads and
        session re-initialize retries).

    When a tool exceeds the cap the caller proceeds with an error result
    instead of blocking the inference worker indefinitely -- including
    group-chat workers, whose hang cannot be reached by the model HTTP
    timeouts.  A timed-out function tool keeps running on a daemon worker
    thread in the background.

    Returns None when the guard is disabled (env var empty / <= 0).
    Default: 120 s.
    """
    raw = os.environ.get("TOOL_EXEC_TIMEOUT", "120").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "invalid TOOL_EXEC_TIMEOUT=%r; using default 120s", raw,
        )
        return 120.0
    if val <= 0:
        return None
    return val


def _get_effective_tool_exec_timeout(
    tool_config: "ToolConfig", arguments: Optional[dict] = None,
) -> Optional[float]:
    """Return the hard deadline for one concrete tool invocation.

    Normal tools get the configured base timeout plus an optional timeout
    requested in their arguments.  Argument values >= 5000 are interpreted as
    milliseconds; smaller values are seconds.  Tools tagged ``long-execution``
    instead get 200 times the base timeout, because these tools commonly run
    their own model/tool inference loops.
    """
    base = _get_tool_exec_timeout()
    if base is None:
        return None
    if "slow-execution" in (tool_config.labels or []):
        return base * 5
    if "long-execution" in (tool_config.labels or []):
        return base * 200
    if not isinstance(arguments, dict) or "timeout" not in arguments:
        return base
    try:
        requested = float(arguments["timeout"])
    except (TypeError, ValueError):
        return base
    if requested < 0:
        return base
    if requested >= 5000:
        requested /= 1000
    return base + requested


# Default User-Agent for outbound model API requests.
# urllib.request auto-fills "Python-urllib/3.x" when no UA is set, and many
# Cloudflare-fronted LLM gateways (e.g. those using Bot Fight Mode or WAF
# custom rules) block that UA string with HTTP 403 / error code: 1010.
# Injecting a generic browser-style UA here keeps all protocols (OpenAI,
# Ollama, Anthropic, ...) compatible with such gateways without requiring
# each protocol adapter to set its own UA. Protocol-level User-Agent
# values, if any, take precedence (see setdefault below).
_DEFAULT_USER_AGENT = os.environ.get(
    "AGENTS_USER_AGENT",
    "Mozilla/5.0 (compatible; AgentService/1.0)",
)

from runtime.models import (
    InferenceRequest,
    InferenceResult,
    Message,
    TokenStat,
    ToolConfig,
)
from runtime.protocols import PROTOCOL_MAP
from runtime.registry import ModelRegistry, ToolRegistry


def _max_rounds_note(max_rounds: int, pending_calls: Optional[list] = None) -> str:
    """Build the plain-text note appended when the max tool-call rounds is hit.

    Replaces the old "fake tool reply" behaviour: the tools were never
    executed, so fabricating a role='tool' result is semantically wrong and
    can produce an invalid message sequence when the conversation is resumed
    (tool result placed before its assistant(tool_calls) declaration).  A
    plain assistant text note keeps the history protocol-valid while telling
    the user exactly what happened.
    """
    names = ", ".join(
        sorted({str(c.get("name", "unknown_tool")) for c in (pending_calls or [])})
    ) or "unknown_tool"
    return (
        f"Error: maximum tool-call rounds ({max_rounds}) reached. "
        f"Tool call(s) were NOT executed: {names}."
    )


def _normalize_tool_call_order(messages: list) -> list:
    """Reorder tool messages that precede their assistant(tool_calls) declaration.

    OpenAI / Anthropic require every role='tool' (tool_result) message to
    FOLLOW the assistant message that declared the matching tool_call id.
    If a tool message references an id declared by a LATER assistant message
    (e.g. after a max-tool-rounds recovery with legacy history), move the
    tool message to immediately after that assistant message.

    This is a defensive safety net on the request-serialization path; it is
    idempotent and a no-op for correctly ordered history.
    """
    # Map tool_use_id -> index of the assistant message that declared it.
    assistant_idx_by_call_id: dict[str, int] = {}
    for i, m in enumerate(messages):
        if m.role != "assistant" or not m.tool_calls:
            continue
        for tc in m.tool_calls:
            cid = tc.get("id") or tc.get("tool_use_id")
            if cid:
                assistant_idx_by_call_id.setdefault(cid, i)

    # Defer misplaced tool messages until their matching assistant is reached.
    deferred: dict[int, list] = {}
    result: list = []
    moved = False
    for i, m in enumerate(messages):
        if m.role == "tool":
            cid = getattr(m, "tool_use_id", None)
            a_idx = assistant_idx_by_call_id.get(cid) if cid else None
            if a_idx is not None and a_idx > i:
                deferred.setdefault(a_idx, []).append(m)
                moved = True
                continue
        result.append(m)
        if m.role == "assistant":
            pending = deferred.pop(i, None)
            if pending:
                result.extend(pending)
    if not moved:
        return messages
    # Any leftover deferred tools (defensive) — append at the end.
    for pending in deferred.values():
        result.extend(pending)
    return result


def _ensure_tool_call_results(messages: list) -> list:
    """Return protocol-valid history with every tool call paired to a result.

    Provider APIs reject dangling assistant ``tool_calls`` as well as orphan
    ``tool`` messages.  Interrupted streams can leave either shape on disk,
    and can also leave a truncated JSON argument string.  For the current model
    request malformed arguments become ``{}``, missing IDs are assigned
    deterministically, orphan results are dropped, and an explicit interrupted
    result is inserted for each unmatched call.  The persistence layer separately
    prunes the malformed source segment on the next conversation rewrite.
    """
    messages = _normalize_tool_call_order(messages)
    result: list = []
    pending: list[tuple[str, str]] = []

    def _flush_pending() -> None:
        nonlocal pending
        for call_id, name in pending:
            result.append(Message(
                role="tool",
                name=name,
                tool_use_id=call_id,
                content=(
                    f"[interrupted] The tool call `{name}` did not produce a "
                    "result. Continue from the available conversation context; "
                    "only call the tool again if necessary."
                ),
            ))
        pending = []

    for message_index, msg in enumerate(messages):
        if msg.role == "assistant" and msg.tool_calls:
            _flush_pending()
            repaired_calls: list[dict] = []
            for call_index, raw_call in enumerate(msg.tool_calls):
                tc = dict(raw_call) if isinstance(raw_call, dict) else {}
                call_id = tc.get("id") or tc.get("tool_use_id")
                if not call_id:
                    call_id = f"call_recovered_{message_index}_{call_index}"
                name = str(tc.get("name") or "__interrupted__")
                arguments = tc.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        json.loads(arguments)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        arguments = "{}"
                elif not isinstance(arguments, dict):
                    arguments = {}
                tc["id"] = call_id
                tc["name"] = name
                tc["arguments"] = arguments
                repaired_calls.append(tc)
                pending.append((call_id, name))
            msg.tool_calls = repaired_calls
            result.append(msg)
            continue

        if msg.role == "tool":
            if not pending:
                # Strict providers reject tool results without a preceding call.
                continue
            tool_id = getattr(msg, "tool_use_id", None)
            match_index = next(
                (i for i, (call_id, _) in enumerate(pending) if call_id == tool_id),
                None,
            ) if tool_id else None
            if match_index is None and not tool_id:
                # Legacy/Ollama histories may omit IDs; pair by tool name first,
                # then by declaration order.
                match_index = next(
                    (i for i, (_, name) in enumerate(pending) if name == msg.name),
                    0,
                )
                msg.tool_use_id = pending[match_index][0]
            if match_index is None:
                continue
            pending.pop(match_index)
            result.append(msg)
            continue

        _flush_pending()
        result.append(msg)

    _flush_pending()
    return result


def _prepare_reasoning_for_tool_rounds(
    messages: list,
    model_config,
) -> list:
    """Return request-only messages with missing tool-round reasoning repaired.

    Some thinking-mode providers require every assistant message in the active
    tool round to carry its reasoning field when tool results are submitted.
    Apply that compatibility repair only when the model opts in through the
    ``require-thinking`` label and the current history ends in
    a tool result.

    Repaired assistant messages are shallow copies.  The conversation's source
    Message objects remain unchanged, so synthetic reasoning is never returned
    as model output or persisted to conversation.json.
    """
    if (
        not messages
        or messages[-1].role != "tool"
        or "require-thinking" not in (model_config.labels or [])
    ):
        return messages

    last_user_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            last_user_index = index
            break

    repaired = None
    for index in range(last_user_index + 1, len(messages)):
        msg = messages[index]
        if msg.role != "assistant" or msg.thinking:
            continue
        if repaired is None:
            repaired = list(messages)
        repaired[index] = replace(
            msg,
            thinking="Reasoning content was unavailable in the stored conversation.",
        )

    return repaired if repaired is not None else messages


# ---------------------------------------------------------------------------
# VLM fallback helpers
#
# A model whose labels lack "vlm" cannot consume multimodal payloads.  When
# such a model receives images, the images are transcribed to text through
# the built-in read_image tool (single call, carrying the user's original
# query), and the attachment is replaced with a text block that mirrors the
# text-file attachment format used by expand_workspace_file_refs_in_message:
#
#     [Image file attached: <labels>]
#     ```
#     <transcription>
#     ```
# ---------------------------------------------------------------------------

_IMAGE_ATTACHED_RE = re.compile(r"\[Image file attached: [^\]]*\]")


def _image_attachment_label(img_data: str) -> str:
    """Return a short human-readable label for an image source.

    Base64 payloads are shown as "(inline image)" to avoid dumping huge
    encoded strings into the prompt; URLs and local paths are shown as-is.
    """
    if img_data.startswith(("http://", "https://")):
        return img_data
    if is_likely_base64(img_data):
        return "(inline image)"
    return img_data


def _strip_image_attached_lines(content: str) -> str:
    """Strip ``[Image file attached: ...]`` placeholder lines from user content.

    Used to recover the user's original query text before handing it to the
    VLM transcription prompt, so the VLM is guided by what the user actually
    asked (e.g. comparing two images) instead of the placeholder syntax.
    """
    if not content:
        return ""
    return _IMAGE_ATTACHED_RE.sub("", content).strip()


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
        prompt_template_manager: Optional[object] = None,
    ) -> None:
        """Initialize the Runtime.

        Args:
            model_registry: Registry containing model configurations.
            tool_registry: Registry containing tool configurations and callables.
            mcp_manager: Optional MCPClientManager for MCP tool execution.
            skill_manager: Optional SkillManager for Skill progressive disclosure.
            prompt_template_manager: Optional PromptTemplateManager for resolving
                prompt_template references in user messages.
        """
        self._model_registry = model_registry
        self._tool_registry = tool_registry
        self._mcp_manager = mcp_manager
        self._skill_manager = skill_manager
        self._prompt_template_manager = prompt_template_manager

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------

    def _normalize_messages(self, request: InferenceRequest, model_config=None) -> list:
        """Normalize request input into a list of Message objects.

        If request.text is set, wraps it as a single user message.
        If request.messages is set, uses them directly.
        If both are set, messages takes precedence.

        For user-role messages without content but with a prompt_template key,
        the template body is fetched from PromptTemplateManager and any
        {placeholder} variables are replaced using the message's arguments dict.
        The resolved text is written into the message's content field.

        When ``model_config`` is provided and the model has no VLM capability
        (its labels lack "vlm"), attached images are transcribed to text via
        the built-in read_image tool before returning (see
        ``_apply_vlm_image_fallback``).

        Args:
            request: The inference request to normalize.
            model_config: Optional resolved ModelConfig for the target model.
                When None (e.g. direct calls), no VLM fallback is applied.

        Returns:
            A list of Message objects ready for the protocol adapter.
        """
        if request.messages is not None and len(request.messages) > 0:
            messages = list(request.messages)
            for msg in messages:
                if msg.timestamp is None:
                    msg.timestamp = _now_iso()
                if (
                    msg.prompt_template is not None
                    and self._prompt_template_manager is not None
                ):
                    template = self._prompt_template_manager.get(msg.prompt_template)
                    if template is not None:
                        # Re-render template-backed messages on every inference.
                        # A persisted message may still contain content rendered
                        # with the previous turn's AGENTS/TOOLS arguments.  The
                        # current request is authoritative for those dynamic
                        # placeholders, so cached rendered content must not win.
                        content = template.content
                        if msg.arguments:
                            for key, value in msg.arguments.items():
                                content = content.replace(f"{{{{{key}}}}}", str(value))
                        msg.content = content
            return self._apply_vlm_image_fallback(model_config, messages)
        if request.text is not None:
            messages = [Message(role="user", content=request.text, timestamp=_now_iso())]
            return self._apply_vlm_image_fallback(model_config, messages)
        return []

    def _apply_vlm_image_fallback(self, model_config, messages: list) -> list:
        """Transcribe attached images into text when the model cannot see them.

        A model whose labels do not include "vlm" cannot consume multimodal
        payloads.  When any user message carries ``images``, this method hands
        ALL of them (plus the user's original query, stripped of the
        ``[Image file attached: ...]`` placeholders) to the built-in read_image
        tool in a single call, then replaces the image attachment with a text
        block mirroring the text-file attachment format:

            [Image file attached: <labels>]
            ```
            <transcription>
            ```

        The transcription block is prepended to the message; the original
        ``[Image file attached: ...]`` path lines (if any) are kept, exactly
        like text-file path descriptions are kept next to their content block.

        The message list is returned unchanged when model_config is None, when
        the model supports VLM ("vlm" label), when no user message has images,
        when already inside a fallback transcription (recursion guard), or when
        the read_image tool is not registered.
        """
        if model_config is None or "vlm" in (model_config.labels or []):
            return messages
        image_msgs = [m for m in messages if m.role == "user" and getattr(m, "images", None)]
        if not image_msgs:
            return messages
        if getattr(_thread_local, "_vlm_fallback_depth", 0) > 0:
            _logger.warning(
                "vlm fallback skipped: already inside a fallback transcription "
                "(read-image model is probably missing the 'vlm' label)"
            )
            return messages
        read_image_fn = None
        if self._tool_registry is not None:
            read_image_fn = self._tool_registry.get_callable("read_image")
        if read_image_fn is None:
            _logger.warning("vlm fallback skipped: read_image tool is not registered")
            return messages

        depth = getattr(_thread_local, "_vlm_fallback_depth", 0)
        try:
            _thread_local._vlm_fallback_depth = depth + 1
            for msg in image_msgs:
                images = list(msg.images)
                prompt = _strip_image_attached_lines(msg.content)
                try:
                    result = read_image_fn(base64_contents=images, prompt=prompt)
                except Exception as exc:
                    _logger.error("vlm fallback: read_image failed: %s", exc)
                    result = f"Error: VLM image transcription failed: {exc}"
                labels = ", ".join(_image_attachment_label(img) for img in images)
                block = f"[Image file attached: {labels}]\n```\n{result}\n```"
                msg.content = block + "\n\n" + (msg.content or "").lstrip()
                msg.images = None
        finally:
            _thread_local._vlm_fallback_depth = depth
        return messages

    # ------------------------------------------------------------------
    # Core inference
    # ------------------------------------------------------------------

    def _maybe_throttle_inference_loop(
        self,
        loop_start: float,
        infer_round: int,
        cancel_event: Optional[object] = None,
    ) -> bool:
        """Throttle fast multi-round inference loops based on MAX_INFER_PER_MINUTE.

        The limit is intentionally scoped to one inference loop.  It is checked
        dynamically before every model API call, so changing the environment
        variable affects an already-running loop on its next round.

        ``infer_round`` follows the same unit as ``max_tool_rounds``: the number
        of completed tool-call rounds so far.  To avoid slowing down normal
        short conversations, throttling starts only after 10 rounds.

        Returns:
            True if inference should continue, False if ``cancel_event`` was set
            while sleeping.
        """
        if infer_round < 10:
            return True

        raw_limit = os.environ.get("MAX_INFER_PER_MINUTE", "").strip()
        if not raw_limit:
            return True

        try:
            max_infer_per_minute = float(raw_limit)
        except (TypeError, ValueError):
            _logger.warning("invalid MAX_INFER_PER_MINUTE=%r; throttling disabled", raw_limit)
            return True

        if max_infer_per_minute <= 0:
            return True

        min_avg_interval = 60.0 / max_infer_per_minute
        now = time.monotonic()
        elapsed = max(0.0, now - loop_start)
        current_avg_interval = elapsed / infer_round
        if current_avg_interval >= min_avg_interval:
            return True

        target_elapsed = min_avg_interval * infer_round
        sleep_seconds = target_elapsed - elapsed
        if sleep_seconds <= 0:
            return True

        _logger.info(
            "throttling inference loop | round=%d max_per_minute=%s "
            "avg_interval=%.3fs min_interval=%.3fs sleep=%.3fs",
            infer_round,
            raw_limit,
            current_avg_interval,
            min_avg_interval,
            sleep_seconds,
        )

        sleep_until = time.monotonic() + sleep_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            remaining = sleep_until - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.5))

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
        model_config = request.model_config_override
        if model_config is None:
            model_config = self._model_registry.get(request.model_id)
        if model_config is None:
            return InferenceResult(
                success=False,
                error=f"Model '{request.model_id}' not found in registry",
                error_code="MODEL_NOT_FOUND",
            )
        # Resolve endpoint placeholders only for the live inference request.
        # The registry/override object retains its original placeholder text.
        inference_model_config = model_config.resolved_for_inference()

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
        messages = self._normalize_messages(request, model_config)

        # 5-7. Inference + tool call loop
        tool_round = 0
        total_prompt = 0
        total_completion = 0
        overall_start = time.monotonic()
        last_stat: Optional[TokenStat] = None
        while True:
            # Dynamically throttle very long/fast tool-call loops before the
            # next model API request, if MAX_INFER_PER_MINUTE is configured.
            self._maybe_throttle_inference_loop(overall_start, tool_round)

            # Repair legacy/interrupted tool-call history before serialization.
            messages = _ensure_tool_call_results(messages)
            request_messages = _prepare_reasoning_for_tool_rounds(messages, model_config)
            url, headers, body_bytes = protocol.build_request(
                config=inference_model_config,
                messages=request_messages,
                tools=tools if tools else None,
                stream=False,
            )

            # Send HTTP request. Transient failures are retried here, below
            # the tool loop, so an already completed tool invocation is never
            # repeated. Non-streaming calls cannot have exposed model output
            # before urlopen/read completes, making the whole request retry-safe.
            round_start = time.monotonic()
            api_timeout = _get_model_api_timeout()
            max_retries = _get_model_api_max_retries()
            headers.setdefault("User-Agent", _DEFAULT_USER_AGENT)
            http_req = urllib.request.Request(
                url, data=body_bytes, headers=headers, method="POST"
            )
            for attempt in range(max_retries + 1):
                try:
                    with urllib.request.urlopen(http_req, timeout=api_timeout) as http_resp:
                        response_data = http_resp.read()
                    break
                except urllib.error.HTTPError as exc:
                    error_body = ""
                    try:
                        error_body = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    if _is_retryable_model_error(exc) and attempt < max_retries:
                        _logger.warning(
                            "infer transient HTTP error; retrying | url=%s code=%s "
                            "attempt=%d/%d body=%s",
                            url, exc.code, attempt + 1, max_retries, error_body[:500],
                        )
                        _wait_model_retry(attempt + 1)
                        continue
                    _logger.error(
                        "infer HTTP error | url=%s code=%s reason=%s body=%s",
                        url, exc.code, exc.reason, error_body[:2000],
                    )
                    return InferenceResult(
                        success=False,
                        messages=messages,
                        error=f"HTTP {exc.code}: {exc.reason}. {error_body}".strip(),
                        error_code=str(exc.code),
                    )
                except (socket.timeout, urllib.error.URLError) as exc:
                    if _is_retryable_model_error(exc) and attempt < max_retries:
                        _logger.warning(
                            "infer transient connection error; retrying | url=%s "
                            "attempt=%d/%d err=%s",
                            url, attempt + 1, max_retries, getattr(exc, "reason", exc),
                        )
                        _wait_model_retry(attempt + 1)
                        continue
                    reason = getattr(exc, "reason", exc)
                    if _is_timeout_error(exc):
                        _logger.error("infer timeout | url=%s timeout=%ds", url, api_timeout)
                        return InferenceResult(
                            success=False,
                            messages=messages,
                            error=f"Model API request timed out after {api_timeout}s",
                            error_code="TIMEOUT",
                        )
                    return InferenceResult(
                        success=False,
                        messages=messages,
                        error=f"Connection error: {reason}",
                        error_code="CONNECTION_ERROR",
                    )
                except Exception as exc:
                    if _is_retryable_model_error(exc) and attempt < max_retries:
                        _logger.warning(
                            "infer transient request error; retrying | url=%s "
                            "attempt=%d/%d err=%s",
                            url, attempt + 1, max_retries, exc,
                        )
                        _wait_model_retry(attempt + 1)
                        continue
                    return InferenceResult(
                        success=False,
                        messages=messages,
                        error=f"Request failed: {exc}",
                        error_code="REQUEST_ERROR",
                    )
            net_ms = (time.monotonic() - round_start) * 1000

            # Parse response
            try:
                response_messages, round_token_stat = protocol.parse_response(response_data, stream=False)
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

            total_prompt += round_token_stat.prompt_tokens
            total_completion += round_token_stat.completion_tokens
            round_total_ms = (time.monotonic() - round_start) * 1000
            last_stat = TokenStat(
                prompt_tokens=round_token_stat.prompt_tokens,
                completion_tokens=round_token_stat.completion_tokens,
                total_tokens=round_token_stat.prompt_tokens + round_token_stat.completion_tokens,
                cached_input_tokens=round_token_stat.cached_input_tokens,
                new_token_cache=round_token_stat.new_token_cache,
                net_ms=net_ms,
                total_ms=round_total_ms,
                total_prompt_tokens=total_prompt,
                total_completion_tokens=total_completion,
                total_all_tokens=total_prompt + total_completion,
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
                # Exceeded max rounds — do NOT fabricate a role='tool' reply for
                # tool calls that were never executed (semantically wrong, and it
                # can yield an invalid [tool, assistant] sequence when the session
                # is resumed).  Instead strip the pending tool_calls from the
                # assistant message and append a plain-text note, so the history
                # stays valid for OpenAI/Anthropic (no dangling tool_calls, no
                # orphan tool result).
                assistant_msg.tool_calls = None
                note = _max_rounds_note(request.max_tool_rounds, tool_calls_to_execute)
                assistant_msg.content = ((assistant_msg.content or "") + "\n\n" + note).strip()
                break

            # Skills mutate the round's tool scope/cwd and therefore retain the
            # existing declaration-order path. Ordinary function-only batches
            # are dispatched together below.
            skill_triggered = False
            _logger.info(
                "infer: executing %d tool calls in round %d with up to %d function workers",
                len(tool_calls_to_execute), tool_round, _get_tool_exec_workers(),
            )
            if not any(
                self._is_skill_tool(fn_call.get("name", ""))
                for fn_call in tool_calls_to_execute
            ):
                for tool_msg in self._execute_tool_call_round(
                    tool_calls_to_execute, tools, timestamp=False,
                ):
                    messages.append(tool_msg)
                _logger.info("infer: tool-call batch done, continuing to next round")
                continue

            for fn_call in tool_calls_to_execute:
                tool_name = fn_call.get("name", "")
                arguments_str = fn_call.get("arguments", "{}")

                # Check if this is a Skill — trigger progressive disclosure
                if self._is_skill_tool(tool_name):
                    # Progressive disclosure: inject SKILL.md body + built-in tools
                    skill_body, skill_dir = self._get_skill_body_and_dir(tool_name)

                    # Change working directory to the skill's directory
                    if skill_dir:
                        os.chdir(skill_dir)

                    # Inject the full SKILL.md body as a function/tool result message
                    if skill_body:
                        cwd_hint = f"\n\n技能工作目录: {skill_dir}" if skill_dir else ""
                        messages.append(
                            Message(
                                role="tool",
                                name=tool_name,
                                tool_use_id=fn_call.get("id") or fn_call.get("tool_use_id"),
                                content=(
                                    f"用户选择了 {tool_name} 技能。以下是该技能的详细文档，"
                                    f"请根据文档内容和用户的原始请求，使用 write_file、exec_shell "
                                    f"等内置工具来执行相应操作。如需网络请求，可使用 exec_shell 调用 curl。{cwd_hint}\n\n{skill_body}"
                                ),
                            )
                        )

                    # Add built-in tools (write_file, exec_shell) to the tools list
                    self._ensure_builtin_tools(tools)

                    # Remove the Skill itself from tools to avoid re-selection
                    tools = [t for t in tools if t.tool_id != tool_name and t.name != tool_name]

                    # Don't consume a tool_round for skill disclosure
                    tool_round -= 1
                    skill_triggered = True
                    continue  # Skill result injected; continue processing remaining tool calls

                # Parse arguments
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except (json.JSONDecodeError, ValueError):
                    arguments = {}

                # Execute tool and get result. Function tools report their real
                # start from inside the worker immediately before the callable.
                execution_start = {}
                tool_result, tool_config = self._execute_tool_call(
                    tool_name,
                    arguments,
                    tool_scope=tools,
                    tool_use_id=fn_call.get("id") or fn_call.get("tool_use_id"),
                    on_started=lambda value: execution_start.setdefault("value", value),
                )

                # Add tool result as tool role message
                messages.append(
                    Message(
                        role="tool",
                        started_at=execution_start.get("value"),
                        content=tool_result,
                        name=tool_name,
                        tool_id=tool_config.tool_id if tool_config else None,
                        tool_use_id=fn_call.get("id") or fn_call.get("tool_use_id"),
                    )
                )

            if skill_triggered:
                _logger.info("infer_stream: skill triggered, continuing to next round")
            else:
                _logger.info("infer_stream: normal tool call done, continuing to next round")
            # Continue to next round of inference (both skill and normal tools)
            continue

        # Attach overall_ms to the last round's stat
        if last_stat is not None:
            last_stat.overall_ms = (time.monotonic() - overall_start) * 1000
        return InferenceResult(success=True, messages=messages, stat=last_stat)

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
        self,
        tool_name: str,
        arguments: dict,
        tool_scope: Optional[list] = None,
        tool_use_id: Optional[str] = None,
        on_started: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, Optional[ToolConfig]]:
        """Execute a tool call by name.

        Looks up the tool within tool_scope first (the set of tools sent in the
        current inference request), then falls back to the full ToolRegistry.
        This prevents name collisions when multiple tools share the same name
        (e.g. a function tool and an MCP tool both named "fetch").

        Before execution, argument names are validated against the tool's
        declared parameters.  When an argument key does not match any declared
        parameter, a fuzzy match is attempted (substring containment, both
        directions).  If a single unambiguous match is found the argument is
        silently corrected and a compatibility note is appended to the result.

        Args:
            tool_name: The name of the tool to execute.
            arguments: The arguments dict to pass to the tool.
            tool_scope: The list of ToolConfig objects that were included in the
                current inference request. When provided, name lookup is
                restricted to this set before falling back to the registry.
            tool_use_id: Protocol-level ID of the current model tool call. It is
                exposed through the request context while the callable runs so
                self-streaming built-ins can use the canonical call ID.
            on_started: Optional callback receiving the actual execution-start
                timestamp. Function tools invoke it inside the worker immediately
                before entering the callable.

        Returns:
            A tuple of (result_str, tool_config). tool_config is None if tool not found.
        """
        tool_config = self._find_tool_by_name(tool_name, scope=tool_scope)

        if tool_config is None:
            if tool_scope is not None:
                return (
                    f"Error: specified tool '{tool_name}' is temporarily unavailable "
                    "(not found in the current tool list).",
                    None,
                )
            return f"Error: tool '{tool_name}' not found in registry", None

        # --- Compatible argument name correction ---
        arguments, compat_notes = self._normalize_argument_names(tool_config, arguments)
        if compat_notes is None:
            # _normalize_argument_names returned error -> arguments is the error string
            return arguments, tool_config

        had_tool_use_id = hasattr(_thread_local, "tool_use_id")
        previous_tool_use_id = getattr(_thread_local, "tool_use_id", None)
        _thread_local.tool_use_id = tool_use_id
        try:
            if tool_config.tool_type == "function":
                result_str = self._execute_function_tool(
                    tool_config, arguments, on_started=on_started,
                )
            elif tool_config.tool_type == "mcp":
                # --- Base64 file path auto-conversion (for file transfer MCP) ---
                # 检测参数中的 base64_content 等字段，如果值看起来是文件路径（非 base64），
                # 则自动读取文件并转换为 base64，避免大模型处理长 base64 字符串。
                from runtime.tools import process_tool_arguments_for_base64
                arguments = process_tool_arguments_for_base64(arguments)
            
                if on_started is not None:
                    on_started(_now_precise_iso())
                result_str = self._execute_mcp_tool(tool_config, arguments)

                # --- Base64 image interception (inference loop only) ---
                # Long base64 payloads (e.g. screenshots from windows-mcp / chrome-devtools)
                # are harmful to the model context. Replace them with saved file paths.
                if len(result_str) > env_int("BASE64_CHECK_THRESHOLD", 1024):
                    if is_likely_base64(result_str):
                        result_str = '{"data":"' + result_str + '"}'
                    from runtime.tools import save_and_replace_base64
                    result_str = save_and_replace_base64(result_str)
            elif tool_config.tool_type == "skill":
                result_str = f"Error: skill '{tool_name}' should be triggered via progressive disclosure, not direct execution"
            else:
                result_str = f"Error: unsupported tool_type '{tool_config.tool_type}' for tool '{tool_name}'"
        finally:
            if had_tool_use_id:
                _thread_local.tool_use_id = previous_tool_use_id
            else:
                try:
                    delattr(_thread_local, "tool_use_id")
                except AttributeError:
                    pass

        # Append compatibility notes if any argument names were corrected
        if compat_notes:
            result_str = self._append_compat_notes(result_str, compat_notes)

        # --- Content length guard ---
        # When a tool returns an excessively long result it can blow up the
        # model context.  Save oversized results to a temp file and return a
        # hint so the model can fetch slices with read_file / exec_shell.
        result_str = self._guard_tool_result_length(result_str)

        return result_str, tool_config

    # ------------------------------------------------------------------
    # Tool result post-processing
    # ------------------------------------------------------------------

    @staticmethod
    def _guard_tool_result_length(result_str: str) -> str:
        """Cap oversized tool results by writing them to a temp file.

        The threshold is controlled by the TOOL_RESULT_MAX_LENGTH environment
        variable (default 262144 = 256 KiB).  When exceeded the full result is
        written to a temporary file under /tmp and a short hint is returned
        instead, telling the caller how many characters / lines were produced
        and where the file was saved.

        Args:
            result_str: The original tool result string.

        Returns:
            The original string when under the threshold, or a short hint
            pointing at a temp file.
        """
        max_len = env_int("TOOL_RESULT_MAX_LENGTH", 262144)
        if len(result_str) <= max_len:
            return result_str

        import tempfile
        fd, tmp_path = tempfile.mkstemp(
            prefix="tool_result_", suffix=".txt", dir="/tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(result_str)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        chars = len(result_str)
        lines = result_str.count("\n") + 1
        return (
            f"工具返回了长度超长的内容（{chars}字符，{lines}行），"
            f"内容已写入临时文件: {tmp_path}。"
            f"请使用工具局部获取查看（如 read_file 指定 start_line/end_line、"
            f"exec_shell 调用 head/tail/sed 等）。"
        )

    # ------------------------------------------------------------------
    # Argument name compatibility
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_argument_names(
        tool_config: ToolConfig, arguments: dict
    ) -> tuple[dict, Optional[list]]:
        """Validate and correct argument names against the tool's declared parameters.

        When a key in *arguments* is not found in the tool's parameter list, a
        fuzzy match is attempted: if a parameter name **contains** the argument
        key, or the argument key **contains** a parameter name, the first such
        match is used.  Only one-to-one matches are attempted; ambiguous cases
        (one key matching multiple parameters) still pick the first match.

        Args:
            tool_config: The resolved ToolConfig.
            arguments: The original arguments dict (may be empty).

        Returns:
            A tuple of ``(normalized_args, compat_notes)``.
            ``compat_notes`` is a list of human-readable correction strings
            (empty when no corrections were needed), or **None** when a fatal
            error occurred — in that case ``normalized_args`` is an error
            message string (not a dict).
        """
        param_properties = tool_config.parameters.get("properties", {})
        param_names = list(param_properties.keys()) if param_properties else []

        if not param_names or not arguments:
            return dict(arguments), []

        corrected: dict = {}
        notes: list = []

        for arg_key, arg_value in arguments.items():
            if arg_key in param_names:
                corrected[arg_key] = arg_value
                continue

            # Fuzzy match: find a parameter name that contains arg_key,
            # or that arg_key contains.
            matched: Optional[str] = None
            for pname in param_names:
                if arg_key in pname or pname in arg_key:
                    matched = pname
                    break

            if matched is not None:
                corrected[matched] = arg_value
                notes.append(
                    f"请求{arg_key}参数已按{matched}纠正，下次调用应使用{matched}参数"
                )
            else:
                return (
                    f"Error: tool '{tool_config.name}' has no parameter "
                    f"'{arg_key}', available parameters: {param_names}",
                    None,
                )

        return corrected, notes

    @staticmethod
    def _append_compat_notes(result_str: str, notes: list) -> str:
        """Append compatibility notes to a tool result string.

        If *result_str* appears to be JSON (ends with ``}``), the notes are
        injected as a ``"_notes"`` key before the closing brace.  Otherwise
        they are appended as a newline-prefixed "注：" paragraph.

        Args:
            result_str: The original tool result string.
            notes: List of human-readable note strings.

        Returns:
            The result string with compatibility notes included.
        """
        if not notes:
            return result_str

        note_text = "；".join(notes)

        if result_str.rstrip().endswith("}"):
            # JSON-like: inject "_notes" before the final "}"
            stripped = result_str.rstrip()
            # Find the matching closing brace at the top level
            inner = stripped.rstrip()[:-1].rstrip()
            if inner.endswith(",") or inner.endswith(":"):
                # Already has a trailing separator, just append
                return inner + f'"_notes": "{note_text}"}}'
            else:
                return inner + f', "_notes": "{note_text}"}}'
        else:
            return result_str + f"\n注：{note_text}"

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
        if scope is not None:
            for tc in scope:
                if tc.name == tool_name or tc.tool_id == tool_name:
                    return tc
            # An explicit request scope is an allow-list.  Do not fall back to
            # the global registry: a model can repeat a tool call seen in old
            # conversation history after the user has removed that tool.
            return None

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
        """Ensure built-in tools (write_file, exec_shell) are registered and in the tools list.

        Only write_file and exec_shell are auto-enabled during skill progressive disclosure.
        Other built-in tools (read_file, edit_file, etc.) are NOT added here;
        they must be explicitly requested in tool_ids by the client.

        Note: fetch functionality can be achieved using exec_shell + curl
        (curl is now available on Windows as well).

        Lazily registers callables if missing, and appends ToolConfigs to the
        provided tools list if not already present. Does not overwrite existing
        tools that share the same tool_id (e.g. a user-registered fetch tool).
        """
        from runtime.builtin_tools import (
            WRITE_FILE_TOOL_CONFIG, EXEC_SHELL_TOOL_CONFIG,
            _write_file, _exec_shell,
        )
        skill_builtin_tools = [
            (WRITE_FILE_TOOL_CONFIG, _write_file),
            (EXEC_SHELL_TOOL_CONFIG, _exec_shell),
        ]
        for bt_config, bt_fn in skill_builtin_tools:
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
            expanded_dir = os.path.expanduser(tool_config.skill_dir)
            skill_md_path = os.path.join(expanded_dir, "SKILL.md")
            try:
                with open(skill_md_path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Strip front-matter to get body
                content = content.strip()
                if content.startswith("---"):
                    end_idx = content.find("---", 3)
                    if end_idx != -1:
                        body = content[end_idx + 3:].strip()
                        return body, expanded_dir
            except OSError:
                pass

        return None, None

    def _is_skill_tool(self, tool_name: str) -> bool:
        """Check if tool_name refers to a skill, using both skill_manager and ToolRegistry."""
        if self._skill_manager and self._skill_manager.is_skill(tool_name):
            return True
        tool_config = self._find_tool_by_name(tool_name)
        return tool_config is not None and tool_config.tool_type == "skill"

    def _prepare_tool_call(
        self,
        fn_call: dict,
        tool_scope: Optional[list],
    ) -> tuple[str, dict, Optional[str], Optional[ToolConfig], Optional[str], list]:
        """Parse and resolve one model tool call before round scheduling.

        Returns ``(name, arguments, tool_use_id, config, immediate_result,
        compat_notes)``.  ``immediate_result`` is populated for lookup or
        argument-normalization failures, which therefore do not need a worker.
        """
        tool_name = fn_call.get("name", "")
        arguments_str = fn_call.get("arguments", "{}")
        tool_use_id = fn_call.get("id") or fn_call.get("tool_use_id")
        try:
            arguments = (
                json.loads(arguments_str)
                if isinstance(arguments_str, str)
                else arguments_str
            )
        except (json.JSONDecodeError, ValueError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        tool_config = self._find_tool_by_name(tool_name, scope=tool_scope)
        if tool_config is None:
            if tool_scope is not None:
                immediate_result = (
                    f"Error: specified tool '{tool_name}' is temporarily unavailable "
                    "(not found in the current tool list)."
                )
            else:
                immediate_result = f"Error: tool '{tool_name}' not found in registry"
            return tool_name, arguments, tool_use_id, None, immediate_result, []

        arguments, compat_notes = self._normalize_argument_names(tool_config, arguments)
        if compat_notes is None:
            return tool_name, {}, tool_use_id, tool_config, arguments, []
        return tool_name, arguments, tool_use_id, tool_config, None, compat_notes

    def _postprocess_tool_result(self, result_str: str, compat_notes: list) -> str:
        """Apply the common result transforms after a concrete tool finishes."""
        if compat_notes:
            result_str = self._append_compat_notes(result_str, compat_notes)
        return self._guard_tool_result_length(result_str)

    @staticmethod
    def _tool_message(
        tool_name: str,
        tool_use_id: Optional[str],
        result_str: str,
        tool_config: Optional[ToolConfig],
        *,
        timestamp: bool,
        started_at: Optional[str] = None,
    ) -> Message:
        return Message(
            role="tool",
            timestamp=_now_precise_iso() if timestamp else None,
            started_at=started_at,
            content=result_str,
            name=tool_name,
            tool_id=tool_config.tool_id if tool_config else None,
            tool_use_id=tool_use_id,
        )

    def _execute_function_tool_calls(
        self,
        prepared_calls: list[tuple[str, dict, Optional[str], ToolConfig, list]],
        *,
        timestamp: bool,
    ) -> Iterator[Message]:
        """Execute prepared function calls on bounded batches of worker threads.

        Each callable receives its own request-context snapshot and canonical
        ``tool_use_id``. Self-streaming tools such as ``talk_to`` run and update
        the frontend concurrently; final tool messages are emitted in declaration
        order for stable conversation persistence.

        Calls are submitted in batches of at most ``TOOL_EXEC_WORKERS``. A new
        executor is used for each batch so a timed-out callable which keeps
        running in the background cannot permanently occupy a pool slot and
        prevent later calls from starting. With ``TOOL_EXEC_WORKERS=1`` this is
        equivalent to the previous sequential execution order, while still
        isolating every callable on its own worker thread.
        """
        if not prepared_calls:
            return

        max_workers = min(_get_tool_exec_workers(), len(prepared_calls))
        ready_messages: dict[int, Message] = {}
        runnable_calls = []
        next_index = 0

        def _yield_ready_in_order() -> Iterator[Message]:
            nonlocal next_index
            while next_index in ready_messages:
                yield ready_messages.pop(next_index)
                next_index += 1

        for index, (tool_name, arguments, tool_use_id, tool_config, compat_notes) in enumerate(prepared_calls):
            callable_fn = self._tool_registry.get_callable(tool_config.tool_id)
            if callable_fn is None:
                result_str = self._postprocess_tool_result(
                    f"Error: no callable registered for tool '{tool_config.tool_id}'",
                    compat_notes,
                )
                ready_messages[index] = self._tool_message(
                    tool_name, tool_use_id, result_str, tool_config, timestamp=timestamp,
                )
                continue

            validation_error = self._validate_talk_to_target(tool_config, arguments)
            if validation_error is not None:
                result_str = self._postprocess_tool_result(validation_error, compat_notes)
                ready_messages[index] = self._tool_message(
                    tool_name, tool_use_id, result_str, tool_config, timestamp=timestamp,
                )
                continue

            caller_ctx = _snapshot_tool_request_context()
            caller_ctx["tool_use_id"] = tool_use_id
            runnable_calls.append((
                index,
                tool_name,
                arguments,
                tool_use_id,
                tool_config,
                compat_notes,
                callable_fn,
                caller_ctx,
                _get_effective_tool_exec_timeout(tool_config, arguments),
            ))

        yield from _yield_ready_in_order()

        for batch_start in range(0, len(runnable_calls), max_workers):
            batch = runnable_calls[batch_start:batch_start + max_workers]
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=len(batch),
                thread_name_prefix="fn-tool",
            )
            futures = {}
            submitted_at = {}
            execution_started = {}
            execution_started_lock = threading.Lock()
            try:
                for spec in batch:
                    (
                        _index, _tool_name, arguments, _tool_use_id,
                        _tool_config, _compat_notes, callable_fn,
                        caller_ctx, _timeout,
                    ) = spec

                    def _run_with_context(
                        fn=callable_fn,
                        fn_arguments=arguments,
                        ctx=caller_ctx,
                        call_index=_index,
                    ):
                        restore_request_context(ctx)
                        try:
                            # Capture the real execution start in the worker,
                            # immediately before entering the tool callable.
                            # Thread-pool scheduling time must not be attributed
                            # to the tool itself.
                            with execution_started_lock:
                                execution_started[call_index] = _now_precise_iso()
                            return fn(**fn_arguments)
                        finally:
                            _thread_local.__dict__.clear()

                    future = executor.submit(_run_with_context)
                    futures[future] = spec
                    submitted_at[future] = time.monotonic()

                pending = set(futures)
                while pending:
                    done, _ = concurrent.futures.wait(
                        pending,
                        timeout=0.05,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    now = time.monotonic()
                    expired = {
                        future for future in pending
                        if futures[future][-1] is not None
                        and now - submitted_at[future] >= futures[future][-1]
                    }

                    for future in done | expired:
                        if future not in pending:
                            continue
                        pending.remove(future)
                        (
                            index, tool_name, _arguments, tool_use_id,
                            tool_config, compat_notes, _callable_fn,
                            _caller_ctx, timeout,
                        ) = futures[future]
                        if future in expired and not future.done():
                            future.cancel()
                            result_str = (
                                f"Error: tool '{tool_config.name}' timed out "
                                f"after {timeout:g}s"
                            )
                        else:
                            try:
                                result = future.result()
                                result_str = str(result) if result is not None else ""
                            except Exception as exc:
                                result_str = f"Error: {type(exc).__name__}: {exc}"
                        result_str = self._postprocess_tool_result(
                            result_str, compat_notes,
                        )
                        with execution_started_lock:
                            started = execution_started.get(index)
                        ready_messages[index] = self._tool_message(
                            tool_name,
                            tool_use_id,
                            result_str,
                            tool_config,
                            timestamp=timestamp,
                            started_at=started,
                        )
                    yield from _yield_ready_in_order()
            finally:
                # Never wait for timed-out/hung callables. Running Python
                # threads cannot be force-cancelled and may finish later.
                executor.shutdown(wait=False, cancel_futures=True)

        yield from _yield_ready_in_order()

    def _execute_tool_call_round(
        self,
        tool_calls: list[dict],
        tool_scope: Optional[list],
        *,
        timestamp: bool,
    ) -> Iterator[Message]:
        """Execute one non-Skill tool-call batch.

        A batch is parallelized only when every call resolves to a function
        tool. Mixed/unknown/MCP batches keep the previous declaration-order
        path, so this first step does not change MCP concurrency semantics.
        """
        prepared_batch = [
            self._prepare_tool_call(fn_call, tool_scope)
            for fn_call in tool_calls
        ]
        all_functions = bool(tool_calls) and all(
            immediate_result is not None
            or (tool_config is not None and tool_config.tool_type == "function")
            for (
                _tool_name, _arguments, _tool_use_id, tool_config,
                immediate_result, _compat_notes,
            ) in prepared_batch
        )

        if all_functions:
            function_calls = []
            immediate_by_index = {}
            function_index_by_local_index = {}
            for batch_index, prepared in enumerate(prepared_batch):
                (
                    tool_name, arguments, tool_use_id, tool_config,
                    immediate_result, compat_notes,
                ) = prepared
                if immediate_result is not None:
                    immediate_by_index[batch_index] = self._tool_message(
                        tool_name,
                        tool_use_id,
                        self._postprocess_tool_result(immediate_result, compat_notes),
                        tool_config,
                        timestamp=timestamp,
                    )
                else:
                    function_index_by_local_index[len(function_calls)] = batch_index
                    function_calls.append(
                        (tool_name, arguments, tool_use_id, tool_config, compat_notes)
                    )

            # _execute_function_tool_calls preserves the relative declaration
            # order of submitted function calls. Merge immediate validation
            # errors back into their original positions before yielding.
            function_messages = iter(self._execute_function_tool_calls(
                function_calls, timestamp=timestamp,
            ))
            local_function_index = 0
            for batch_index in range(len(prepared_batch)):
                if batch_index in immediate_by_index:
                    yield immediate_by_index[batch_index]
                else:
                    assert function_index_by_local_index[local_function_index] == batch_index
                    yield next(function_messages)
                    local_function_index += 1
            return

        for fn_call in tool_calls:
            tool_name = fn_call.get("name", "")
            arguments_str = fn_call.get("arguments", "{}")
            try:
                arguments = (
                    json.loads(arguments_str)
                    if isinstance(arguments_str, str)
                    else arguments_str
                )
            except (json.JSONDecodeError, ValueError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_use_id = fn_call.get("id") or fn_call.get("tool_use_id")
            execution_start = {}
            result_str, tool_config = self._execute_tool_call(
                tool_name,
                arguments,
                tool_scope=tool_scope,
                tool_use_id=tool_use_id,
                on_started=lambda value: execution_start.setdefault("value", value),
            )
            yield self._tool_message(
                tool_name,
                tool_use_id,
                result_str,
                tool_config,
                timestamp=timestamp,
                started_at=execution_start.get("value"),
            )

    @staticmethod
    def _validate_talk_to_target(tool_config: ToolConfig, arguments: dict) -> Optional[str]:
        """Reject ``talk_to`` calls that target the currently executing agent.

        The built-in callable has its own guard, but generic execution must also
        enforce this invariant before dispatching any registered callable.
        """
        if tool_config.tool_id != "talk_to" and tool_config.name != "talk_to":
            return None

        caller_agent_id = getattr(_thread_local, "agent_id", None)
        if not caller_agent_id:
            return None

        targets = arguments.get("agents", []) if isinstance(arguments, dict) else []
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, (list, tuple)):
            return None

        agent_manager = getattr(_thread_local, "agent_manager", None)
        available_agent_ids = set(
            getattr(_thread_local, "all_agent_ids", None)
            or getattr(_thread_local, "agent_ids", None)
            or []
        )
        for target in targets:
            target_name = str(target)
            resolved = agent_manager.get(target_name) if agent_manager is not None else None
            target_agent_id = resolved.get("agent_id") if isinstance(resolved, dict) else target_name
            if target_agent_id == caller_agent_id:
                return "Error: talk_to cannot target the calling agent itself."
            if available_agent_ids and target_agent_id not in available_agent_ids:
                return (
                    f"Error: specified agent '{target_name}' does not exist "
                    "in the current conversation or has left."
                )
        return None

    def _execute_function_tool(
        self,
        tool_config: ToolConfig,
        arguments: dict,
        on_started: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Execute a function-type tool.

        Args:
            tool_config: The tool configuration.
            arguments: Arguments to pass to the function.
            on_started: Optional callback invoked from the worker immediately
                before entering the callable.

        Returns:
            The function result as a string, or an error message.
        """
        validation_error = self._validate_talk_to_target(tool_config, arguments)
        if validation_error is not None:
            return validation_error

        callable_fn = self._tool_registry.get_callable(tool_config.tool_id)
        if callable_fn is None:
            return f"Error: no callable registered for tool '{tool_config.tool_id}'"

        timeout = _get_effective_tool_exec_timeout(tool_config, arguments)
        # Function callables always run on a worker, even when the timeout guard
        # is disabled. This keeps exceptions and thread-local mutations isolated
        # from the inference loop and makes the direct-call path consistent with
        # round-level parallel execution.
        caller_ctx = _snapshot_tool_request_context()

        def _run_with_context() -> str:
            restore_request_context(caller_ctx)
            try:
                if on_started is not None:
                    # This is the actual callable entry point in the worker;
                    # executor queueing time is intentionally excluded.
                    on_started(_now_precise_iso())
                return callable_fn(**arguments)
            finally:
                # Do not leak this session's context into a reused worker.
                _thread_local.__dict__.clear()

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fn-tool")
        future = executor.submit(_run_with_context)
        try:
            result = future.result(timeout=timeout)
            return str(result) if result is not None else ""
        except concurrent.futures.TimeoutError:
            future.cancel()
            return (f"Error: tool '{tool_config.name}' timed out "
                    f"after {timeout:g}s")
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"
        finally:
            # Never block on a hung callable during executor shutdown.
            executor.shutdown(wait=False, cancel_futures=True)

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
            timeout = _get_effective_tool_exec_timeout(tool_config, arguments)
            result = self._mcp_manager.call_tool(
                server_name, mcp_tool_name, arguments, timeout=timeout)
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
        model_config = request.model_config_override
        if model_config is None:
            model_config = self._model_registry.get(request.model_id)
        if model_config is None:
            yield Message(role="assistant", timestamp=_now_iso(),
                          content=f"Error: Model '{request.model_id}' not found")
            return
        # Resolve endpoint placeholders only for the live inference request.
        # The registry/override object retains its original placeholder text.
        inference_model_config = model_config.resolved_for_inference()

        tools: list[ToolConfig] = []
        for tool_id in request.tool_ids:
            tc = self._tool_registry.get(tool_id)
            if tc is not None:
                tools.append(tc)

        protocol_name = model_config.api_protocol
        protocol_cls = PROTOCOL_MAP.get(protocol_name)
        if protocol_cls is None:
            yield Message(role="assistant", timestamp=_now_iso(),
                          content=f"Error: Unsupported protocol '{protocol_name}'")
            return
        protocol = protocol_cls()

        messages = self._normalize_messages(request, model_config)

        # 2. Streaming tool call loop
        tool_round = 0
        total_prompt = 0
        total_completion = 0
        # overall_ms is measured from entry into the inference/tool loop until
        # the final model round completes. It therefore includes every model
        # request, tool execution, and inter-round throttle delay.
        overall_start = time.monotonic()
        # Shared retry budget for connection and read/parse failures before the
        # protocol emits its first model output. It is reset after a model round
        # completes successfully.
        pre_output_retry_count = 0
        while True:
            # Check cancel_event before each round (including before model API call)
            # so we don't block on a long urlopen timeout after a forced abort.
            if cancel_event is not None and cancel_event.is_set():
                yield Message(role="assistant", timestamp=_now_iso(), content="Error: user interrupted.")
                return

            # Dynamically throttle very long/fast tool-call loops before the
            # next model API request, if MAX_INFER_PER_MINUTE is configured.
            if not self._maybe_throttle_inference_loop(overall_start, tool_round, cancel_event):
                yield Message(role="assistant", timestamp=_now_iso(), content="Error: user interrupted.")
                return

            messages = _ensure_tool_call_results(messages)
            request_messages = _prepare_reasoning_for_tool_rounds(messages, model_config)
            url, headers, body_bytes = protocol.build_request(
                config=inference_model_config, messages=request_messages,
                tools=tools if tools else None, stream=True,
            )

            api_timeout = _get_model_api_timeout()
            infer_timeout = _get_model_infer_timeout()
            # MODEL_INFER_TIMEOUT is a per-round wall-clock cap, while
            # MODEL_API_TIMEOUT remains the socket-level connect/read timeout.
            # For urlopen we use the smaller of the two so a stream that goes
            # idle can still be interrupted before a per-round guard would fire.
            effective_timeout = (
                api_timeout if infer_timeout is None else min(api_timeout, infer_timeout)
            )
            max_retries = _get_model_api_max_retries()
            headers.setdefault("User-Agent", _DEFAULT_USER_AGENT)
            http_req = urllib.request.Request(
                url, data=body_bytes, headers=headers, method="POST")
            http_resp = None
            connect_error = None
            while True:
                # Capture monotonic and wall-clock values immediately before
                # each actual send. Retry timing therefore describes the
                # successful attempt rather than including earlier failures.
                round_start = time.monotonic()
                request_started_datetime = datetime.datetime.now()
                request_started_at = request_started_datetime.isoformat(timespec="microseconds")
                try:
                    http_resp = urllib.request.urlopen(http_req, timeout=effective_timeout)
                    connect_error = None
                    break
                except Exception as exc:
                    connect_error = exc
                    if (
                        _is_retryable_model_error(exc)
                        and pre_output_retry_count < max_retries
                    ):
                        pre_output_retry_count += 1
                        _logger.warning(
                            "infer_stream transient connection error; retrying | "
                            "url=%s attempt=%d/%d err=%s",
                            url, pre_output_retry_count, max_retries,
                            getattr(exc, "reason", exc),
                        )
                        if not _wait_model_retry(pre_output_retry_count, cancel_event):
                            yield Message(role="assistant", timestamp=_now_iso(),
                                          content="Error: user interrupted.")
                            return
                        continue
                    break

            if connect_error is not None:
                exc = connect_error
                if isinstance(exc, urllib.error.HTTPError):
                    error_body = ""
                    try:
                        error_body = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    _logger.error(
                        "infer_stream HTTP error | url=%s code=%s reason=%s body=%s",
                        url, exc.code, exc.reason, error_body[:2000],
                    )
                    detail = f"HTTP {exc.code}: {exc.reason}"
                    if error_body:
                        detail += f" | body: {error_body[:500]}"
                    yield Message(role="assistant", timestamp=_now_iso(), content=f"Error: {detail}")
                elif _is_timeout_error(exc):
                    _logger.error("infer_stream timeout | url=%s timeout=%ss", url, effective_timeout)
                    yield Message(role="assistant", timestamp=_now_iso(),
                                  content=f"Error: model API request timed out after {effective_timeout}s")
                else:
                    reason = getattr(exc, "reason", exc)
                    _logger.error("infer_stream connection error | url=%s err=%s", url, reason)
                    yield Message(role="assistant", timestamp=_now_iso(), content=f"Error: {exc}")
                return

            # Stream this round and collect the full assistant message
            full_content = ""
            full_thinking = ""
            # Track tool calls by index for multi-tool support
            accumulated_tool_calls: dict[int, dict] = {}
            round_prompt = 0
            round_completion = 0
            round_cached_input: Optional[int] = None
            round_new_token_cache: Optional[int] = None
            round_usage_reported = False
            first_token_time: Optional[float] = None
            first_token_timestamp: Optional[str] = None  # ISO timestamp of first token
            tool_calls_first_ts: Optional[str] = None  # Reset for each round
            content_loop_detected = False
            thinking_loop_detected = False

            # Per-round stream activity diagnostics. These counters include
            # content, thinking, and tool-call chunks. Character rates count
            # content + thinking because tool calls are structured output.
            output_chunk_count = 0
            last_output_time: Optional[float] = None
            max_output_gap = 0.0
            recent_output: deque[tuple[float, int]] = deque()

            def _inspect_timeout_output() -> None:
                """Inspect content and thinking once, only after inference timeout."""
                nonlocal content_loop_detected, thinking_loop_detected

                for channel, output in (
                    ("thinking", full_thinking),
                    ("content", full_content),
                ):
                    repeated = _find_repetitive_output_tail(output)
                    if repeated is not None:
                        if channel == "thinking":
                            thinking_loop_detected = True
                        else:
                            content_loop_detected = True
                        second_match_pos = len(output) - len(repeated)
                        _logger.warning(
                            "infer_stream repetitive output detected | "
                            "round=%d channel=%s output_len=%d "
                            "second_match_pos=%d repeated_content=%r",
                            tool_round, channel, len(output), second_match_pos,
                            repeated,
                        )
                    else:
                        _logger.info(
                            "infer_stream timeout output tail | "
                            "round=%d channel=%s output_len=%d last_500_chars=%r",
                            tool_round, channel, len(output), output[-500:],
                        )

            def _timeout_diagnostics(now: float) -> str:
                while (
                    recent_output
                    and now - recent_output[0][0] > _INFER_RATE_WINDOW_SECONDS
                ):
                    recent_output.popleft()
                recent_chars = sum(chars for _, chars in recent_output)
                first_output_s = (
                    first_token_time - round_start
                    if first_token_time is not None else -1.0
                )
                last_output_gap_s = (
                    now - last_output_time
                    if last_output_time is not None else -1.0
                )
                last_output_s = (
                    last_output_time - round_start
                    if last_output_time is not None else -1.0
                )
                return (
                    f"first_output={first_output_s:.3f}s "
                    f"last_output={last_output_s:.3f}s "
                    f"last_output_gap={last_output_gap_s:.3f}s "
                    f"max_output_gap={max_output_gap:.3f}s "
                    f"chunks={output_chunk_count} "
                    f"content_chars={len(full_content)} "
                    f"thinking_chars={len(full_thinking)} "
                    f"recent_{_INFER_RATE_WINDOW_SECONDS:g}s_chars={recent_chars} "
                    f"recent_chars_per_sec="
                    f"{recent_chars / _INFER_RATE_WINDOW_SECONDS:.1f} "
                    f"repetitive_content={content_loop_detected} "
                    f"repetitive_thinking={thinking_loop_detected}"
                )

            try:
                stream_iter = protocol.parse_stream(http_resp)

                for msg in stream_iter:
                    # Check for cancellation before yielding
                    if cancel_event is not None and cancel_event.is_set():
                        http_resp.close()
                        yield Message(role="assistant", timestamp=_now_iso(), content="Error: user interrupted.")
                        return

                    # Observe this item before enforcing the wall-clock limit.
                    # A model chunk arriving just beyond the deadline is not
                    # yielded, but is still useful timeout diagnostic evidence.
                    guard_now = time.monotonic()
                    if msg.role != "usage" and (msg.content or msg.thinking or msg.tool_calls):
                        if first_token_time is None:
                            first_token_time = guard_now
                            first_token_datetime = request_started_datetime + datetime.timedelta(
                                seconds=first_token_time - round_start)
                            first_token_timestamp = first_token_datetime.isoformat(timespec="microseconds")
                        if last_output_time is not None:
                            max_output_gap = max(max_output_gap, guard_now - last_output_time)
                        output_chunk_count += 1
                        last_output_time = guard_now
                        recent_output.append((
                            guard_now, len(msg.content or "") + len(msg.thinking or ""),
                        ))
                        while (
                            recent_output
                            and guard_now - recent_output[0][0] > _INFER_RATE_WINDOW_SECONDS
                        ):
                            recent_output.popleft()

                        if msg.thinking:
                            full_thinking += msg.thinking
                        if msg.content:
                            full_content += msg.content

                    # Per-round continuous-output guard (MODEL_INFER_TIMEOUT).
                    # The clock restarts on every model round and is separate
                    # from the socket-level MODEL_API_TIMEOUT.
                    if infer_timeout is not None:
                        elapsed_round = guard_now - round_start
                        if elapsed_round > infer_timeout:
                            http_resp.close()
                            _inspect_timeout_output()
                            _logger.error(
                                "infer_stream inference timeout | url=%s "
                                "round=%d elapsed=%.3fs limit=%.3fs %s",
                                url, tool_round, elapsed_round, infer_timeout,
                                _timeout_diagnostics(guard_now),
                            )
                            yield Message(
                                role="assistant",
                                timestamp=_now_iso(),
                                content=(
                                    f"Error: model inference timed out after "
                                    f"{elapsed_round:.1f}s (limit: {infer_timeout:.1f}s)"
                                ),
                            )
                            return

                    # Intercept usage messages — accumulate, don't yield raw
                    if msg.role == "usage":
                        try:
                            u = json.loads(msg.content)
                            round_prompt = u.get("prompt_tokens", 0)
                            round_completion = u.get("completion_tokens", 0)
                            round_cached_input = u.get("cached_input_tokens")
                            round_new_token_cache = u.get("new_token_cache")
                            round_usage_reported = u.get("usage_reported") is not False
                        except (json.JSONDecodeError, ValueError, AttributeError):
                            pass
                        continue

                    # Set timing before yielding. ``started_at`` is the real
                    # outbound request-send time, so outer schedulers (including
                    # group-chat thread pools) are excluded automatically.
                    if msg.role == "assistant":
                        msg.started_at = request_started_at
                        if msg.tool_calls:
                            # For tool calls, record the first timestamp
                            if tool_calls_first_ts is None:
                                tool_calls_first_ts = _now_iso()
                            msg.timestamp = tool_calls_first_ts
                        else:
                            # For regular content/thinking, use current time
                            msg.timestamp = _now_iso()

                    # Yield each chunk to the caller for real-time display
                    yield msg

                    if msg.tool_calls:
                        # Tool calls arrive as a complete list (Ollama) or
                        # as individual delta dicts with _index (OpenAI)
                        for tc in msg.tool_calls:
                            idx = tc.get("_index")
                            if idx is None:
                                idx = len(accumulated_tool_calls)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc.get("id"):
                                accumulated_tool_calls[idx]["id"] = tc["id"]
                            if tc.get("tool_use_id"):
                                accumulated_tool_calls[idx]["id"] = tc["tool_use_id"]
                            if tc.get("name"):
                                accumulated_tool_calls[idx]["name"] += tc["name"]
                            if tc.get("arguments"):
                                # Anthropic protocol sends arguments as a dict (already parsed JSON),
                                # while OpenAI/Ollama send them as strings (delta chunks)
                                if isinstance(tc["arguments"], dict):
                                    accumulated_tool_calls[idx]["arguments"] = tc["arguments"]
                                else:
                                    accumulated_tool_calls[idx]["arguments"] += tc["arguments"]
            except Exception as exc:
                # A streaming request may connect successfully and then fail
                # while waiting for its first SSE/NDJSON item. Retry only when
                # absolutely no content, thinking, or tool-call output has been
                # observed; after first output, replaying could duplicate text
                # or repeat a model-generated tool call.
                no_model_output = first_token_time is None
                if (
                    no_model_output
                    and _is_retryable_model_error(exc)
                    and pre_output_retry_count < max_retries
                ):
                    pre_output_retry_count += 1
                    _logger.warning(
                        "infer_stream failed before first output; retrying | "
                        "url=%s attempt=%d/%d err=%s",
                        url, pre_output_retry_count, max_retries, exc,
                    )
                    if not _wait_model_retry(pre_output_retry_count, cancel_event):
                        yield Message(role="assistant", timestamp=_now_iso(),
                                      content="Error: user interrupted.")
                        return
                    continue
                if (
                    infer_timeout is not None
                    and _is_timeout_error(exc)
                    and (time.monotonic() - round_start) >= infer_timeout
                ):
                    timeout_now = time.monotonic()
                    elapsed_round = timeout_now - round_start
                    _inspect_timeout_output()
                    _logger.error(
                        "infer_stream inference timeout during stream | url=%s "
                        "round=%d elapsed=%.3fs limit=%.3fs %s",
                        url, tool_round, elapsed_round, infer_timeout,
                        _timeout_diagnostics(timeout_now),
                    )
                    yield Message(
                        role="assistant",
                        timestamp=_now_iso(),
                        content=(
                            f"Error: model inference timed out after "
                            f"{elapsed_round:.1f}s (limit: {infer_timeout:.1f}s)"
                        ),
                    )
                    return
                if no_model_output and _is_timeout_error(exc):
                    yield Message(role="assistant", timestamp=_now_iso(),
                                  content=f"Error: model API request timed out after {effective_timeout}s")
                else:
                    yield Message(role="assistant", timestamp=_now_iso(),
                                  content=f"Error: stream parse: {exc}")
                return
            finally:
                stream_end_time = time.monotonic()
                http_resp.close()

            # A transport can return HTTP 200 and terminate the SSE/NDJSON body
            # without ever producing content, thinking, or a tool call.  Treat
            # that as a transient pre-output failure rather than a successful
            # empty assistant round.  Retrying is safe because no model output
            # (and therefore no new tool call) has been exposed or executed.
            if first_token_time is None:
                if pre_output_retry_count < max_retries:
                    pre_output_retry_count += 1
                    _logger.warning(
                        "infer_stream empty response before first output; retrying | "
                        "url=%s attempt=%d/%d",
                        url, pre_output_retry_count, max_retries,
                    )
                    if not _wait_model_retry(pre_output_retry_count, cancel_event):
                        yield Message(role="assistant", timestamp=_now_iso(),
                                      content="Error: user interrupted.")
                        return
                    continue
                _logger.error(
                    "infer_stream empty response after retries | url=%s attempts=%d",
                    url, pre_output_retry_count + 1,
                )
                yield Message(
                    role="assistant",
                    timestamp=_now_iso(),
                    content="Error: model API returned an empty response.",
                )
                return

            pre_output_retry_count = 0

            # net_ms = time from request start to stream fully received
            # (empty streams return above and are never recorded as success).
            net_ms = (stream_end_time - round_start) * 1000
            ttft_ms = (first_token_time - round_start) * 1000 if first_token_time else None
            # Build tool calls list from accumulated data
            all_tool_calls = None
            if accumulated_tool_calls:
                all_tool_calls = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]

            # Protocols that cannot report stream usage still need logical
            # per-request statistics. Estimate from the exact messages/tool
            # schemas sent in this model round and the fully assembled response,
            # not from this HTTP request's newest user-message characters.
            if not round_usage_reported:
                round_prompt = estimate_chat_prompt_tokens(
                    request_messages, tools if tools else None,
                )
                round_completion = estimate_message_payload_tokens({
                    "content": full_content,
                    "thinking": full_thinking,
                    "tool_calls": all_tool_calls,
                })
                round_cached_input = None
                round_new_token_cache = None

            total_prompt += round_prompt
            total_completion += round_completion

            # Build the complete assistant message for conversation history
            # timestamp should be the inference completion time (now)
            assistant_ts = _now_iso()
            assistant_msg = Message(
                role="assistant",
                content=full_content,
                timestamp=assistant_ts,
                thinking=full_thinking if full_thinking else None,
                tool_calls=all_tool_calls,
            )
            messages.append(assistant_msg)

            # Determine tool calls to execute
            tool_calls_to_execute = all_tool_calls

            # No tool call — done; yield stat and return
            if not tool_calls_to_execute:
                stat_dict: dict = {
                    "prompt_tokens": round_prompt,
                    "completion_tokens": round_completion,
                    "total_tokens": round_prompt + round_completion,
                    "cached_input_tokens": round_cached_input,
                    "new_token_cache": round_new_token_cache,
                    "usage_reported": round_usage_reported,
                    "total_prompt_tokens": total_prompt,
                    "total_completion_tokens": total_completion,
                    "total_all_tokens": total_prompt + total_completion,
                    "net_ms": round(net_ms, 1),
                    "total_ms": round(net_ms, 1),  # no tool calls, total == net
                    "overall_ms": round((time.monotonic() - overall_start) * 1000, 1),
                    "request_started_at": request_started_at,
                    "completed_at": _now_iso(),
                }
                if not round_usage_reported:
                    stat_dict["estimated"] = True
                if first_token_timestamp:
                    stat_dict["first_token_timestamp"] = first_token_timestamp
                if ttft_ms is not None:
                    stat_dict["ttft_ms"] = round(ttft_ms, 1)
                yield Message(role="usage", timestamp=_now_iso(), name="round", content=json.dumps(stat_dict))
                return

            # Max rounds check
            tool_round += 1
            if tool_round > request.max_tool_rounds:
                # Exceeded max rounds — do NOT fabricate role='tool' replies for
                # tool calls that were never executed.  Fabricated tool results
                # were previously yielded BEFORE the usage/stat message, which
                # broke the "stat flushes the assistant turn first" invariant in
                # merge_stream_messages and persisted [tool, assistant] in the
                # wrong order.  Instead, strip the pending tool_calls from the
                # local assistant message and yield a plain-text assistant note
                # carrying the tool_calls_dropped marker so the persistence layer
                # drops the already-streamed (unexecuted) tool_calls deltas.
                assistant_msg.tool_calls = None
                note = _max_rounds_note(request.max_tool_rounds, tool_calls_to_execute)
                assistant_msg.content = ((assistant_msg.content or "") + "\n\n" + note).strip()
                yield Message(
                    role="assistant",
                    timestamp=_now_iso(),
                    content=note,
                    tool_calls_dropped=True,
                )
                stat_dict = {
                    "prompt_tokens": round_prompt,
                    "completion_tokens": round_completion,
                    "total_tokens": round_prompt + round_completion,
                    "cached_input_tokens": round_cached_input,
                    "new_token_cache": round_new_token_cache,
                    "usage_reported": round_usage_reported,
                    "total_prompt_tokens": total_prompt,
                    "total_completion_tokens": total_completion,
                    "total_all_tokens": total_prompt + total_completion,
                    "net_ms": round(net_ms, 1),
                    "total_ms": round(net_ms, 1),
                    "overall_ms": round((time.monotonic() - overall_start) * 1000, 1),
                    "request_started_at": request_started_at,
                    "completed_at": _now_iso(),
                }
                if not round_usage_reported:
                    stat_dict["estimated"] = True
                if first_token_timestamp:
                    stat_dict["first_token_timestamp"] = first_token_timestamp
                if ttft_ms is not None:
                    stat_dict["ttft_ms"] = round(ttft_ms, 1)
                yield Message(role="usage", timestamp=_now_iso(), name="round", content=json.dumps(stat_dict))
                return

            # Yield stat BEFORE tool execution — usage info is already available
            # from the LLM response (round_prompt/round_completion), no need to wait
            stat_dict = {
                "prompt_tokens": round_prompt,
                "completion_tokens": round_completion,
                "total_tokens": round_prompt + round_completion,
                "cached_input_tokens": round_cached_input,
                "new_token_cache": round_new_token_cache,
                "usage_reported": round_usage_reported,
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_all_tokens": total_prompt + total_completion,
                "net_ms": round(net_ms, 1),
                "total_ms": round(net_ms, 1),  # inference-only for tool-call rounds
                "request_started_at": request_started_at,
                "completed_at": _now_iso(),
            }
            if not round_usage_reported:
                stat_dict["estimated"] = True
            if first_token_timestamp:
                stat_dict["first_token_timestamp"] = first_token_timestamp
            if ttft_ms is not None:
                stat_dict["ttft_ms"] = round(ttft_ms, 1)
            yield Message(role="usage", name="round", content=json.dumps(stat_dict))

            # Skills keep the existing sequential progressive-disclosure path.
            # A function-only batch shares one worker pool and yields tool
            # results as individual callables complete.
            skill_triggered = False
            if not any(
                self._is_skill_tool(fn_call.get("name", ""))
                for fn_call in tool_calls_to_execute
            ):
                _logger.info(
                    "infer_stream: executing %d tool calls in round %d with up to %d function workers",
                    len(tool_calls_to_execute), tool_round, _get_tool_exec_workers(),
                )
                for tool_msg in self._execute_tool_call_round(
                    tool_calls_to_execute, tools, timestamp=True,
                ):
                    messages.append(tool_msg)
                    yield tool_msg
                continue

            for fn_call in tool_calls_to_execute:
                tool_name = fn_call.get("name", "")
                arguments_str = fn_call.get("arguments", "{}")

                # Skill progressive disclosure
                if self._is_skill_tool(tool_name):
                    skill_body, skill_dir = self._get_skill_body_and_dir(tool_name)

                    # Change working directory to the skill's directory
                    if skill_dir:
                        os.chdir(skill_dir)

                    # Inject the full SKILL.md body as a function/tool result message
                    if skill_body:
                        cwd_hint = f"\n\n技能工作目录: {skill_dir}" if skill_dir else ""
                        fn_msg = Message(
                            role="tool",
                            timestamp=_now_iso(),
                            name=tool_name,
                            tool_use_id=fn_call.get("id") or fn_call.get("tool_use_id"),
                            content=(
                                f"用户选择了 {tool_name} 技能。以下是该技能的详细文档，"
                                f"请根据文档内容和用户的原始请求，使用 write_file、exec_shell "
                                f"等内置工具来执行相应操作。如需网络请求，可使用 exec_shell 调用 curl。{cwd_hint}\n\n{skill_body}"
                            ),
                        )
                        messages.append(fn_msg)
                        yield fn_msg

                    self._ensure_builtin_tools(tools)
                    tools = [t for t in tools if t.tool_id != tool_name and t.name != tool_name]
                    tool_round -= 1
                    skill_triggered = True
                    continue  # Skill result injected; continue processing remaining tool calls

                # Execute tool
                try:
                    arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                except (json.JSONDecodeError, ValueError):
                    arguments = {}

                execution_start = {}
                tool_result, tool_config = self._execute_tool_call(
                    tool_name,
                    arguments,
                    tool_scope=tools,
                    tool_use_id=fn_call.get("id") or fn_call.get("tool_use_id"),
                    on_started=lambda value: execution_start.setdefault("value", value),
                )

                tool_msg = Message(
                    role="tool",
                    timestamp=_now_precise_iso(),
                    content=tool_result,
                    name=tool_name,
                    tool_id=tool_config.tool_id if tool_config else None,
                    tool_use_id=fn_call.get("id") or fn_call.get("tool_use_id"),
                    started_at=execution_start.get("value"),
                )
                messages.append(tool_msg)
                yield tool_msg

            if skill_triggered:
                continue



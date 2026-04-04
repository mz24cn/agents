"""Unit tests for Runtime.execute_skill() method.

Tests the Skill multi-step workflow execution including:
- Full workflow with tool and inference steps
- prev_result chaining between steps
- Error handling: skill not found, step failure, tool not found
- Step failure stops subsequent steps
"""

import json
from unittest.mock import patch, MagicMock

from hypothesis import given, settings, strategies as st

from runtime.models import ModelConfig, ToolConfig, InferenceResult
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


def _make_registries():
    """Create model and tool registries with basic test fixtures."""
    model_registry = ModelRegistry()
    model_registry.register(ModelConfig(
        model_id="test-vlm", api_base="http://localhost:9999",
        model_name="test", api_protocol="openai",
    ))

    tool_registry = ToolRegistry()

    def extract_frames(path=""):
        return f"frames_from_{path}"

    def save_description(text=""):
        return f"saved:{text}"

    tool_registry.register(ToolConfig(
        tool_id="extract_frames", tool_type="function", name="extract_frames",
        description="Extract frames",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    ), callable_fn=extract_frames)

    tool_registry.register(ToolConfig(
        tool_id="save_description", tool_type="function", name="save_description",
        description="Save description",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    ), callable_fn=save_description)

    return model_registry, tool_registry


def _mock_urlopen_factory(content="A beautiful scene"):
    """Create a mock urlopen that returns a fixed assistant response."""
    def mock_urlopen(request, **kwargs):
        resp_data = json.dumps({
            "id": "test", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        }).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp
    return mock_urlopen


def test_execute_skill_full_workflow():
    """A 3-step skill (tool -> inference -> tool) completes successfully."""
    model_registry, tool_registry = _make_registries()
    skill = ToolConfig(
        tool_id="video-skill", tool_type="skill", name="video_analysis",
        description="Analyze video",
        parameters={"type": "object", "properties": {"video_path": {"type": "string"}}, "required": ["video_path"]},
        steps=[
            {"type": "tool", "target": "extract_frames", "args_mapping": {"path": "video_path"}},
            {"type": "inference", "model_id": "test-vlm", "prompt_template": "Describe: {prev_result}"},
            {"type": "tool", "target": "save_description", "args_mapping": {"text": "prev_result"}},
        ],
    )
    tool_registry.register(skill)
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)

    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_factory("A beautiful scene")):
        result = runtime.execute_skill("video-skill", {"video_path": "/tmp/video.mp4"})

    assert result.success is True
    assert result.error is None


def test_execute_skill_not_found():
    """Executing a non-existent skill returns an error."""
    model_registry, tool_registry = _make_registries()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("nonexistent", {})
    assert result.success is False
    assert "not found" in result.error.lower()


def test_execute_skill_not_a_skill_type():
    """Executing a function tool as a skill returns an error."""
    model_registry, tool_registry = _make_registries()
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("extract_frames", {})
    assert result.success is False
    assert "not a skill" in result.error.lower()


def test_execute_skill_step_tool_not_found():
    """A step targeting a non-existent tool fails with step index in error."""
    model_registry, tool_registry = _make_registries()
    skill = ToolConfig(
        tool_id="bad-skill", tool_type="skill", name="bad_skill",
        description="Bad skill", parameters={},
        steps=[{"type": "tool", "target": "nonexistent_tool", "args_mapping": {}}],
    )
    tool_registry.register(skill)
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("bad-skill", {})
    assert result.success is False
    assert "Step 0" in result.error


def test_execute_skill_failure_stops_subsequent_steps():
    """If step 0 fails, step 1 should not execute."""
    model_registry, tool_registry = _make_registries()
    call_log = []

    def good_tool(x=""):
        call_log.append("called")
        return "ok"

    tool_registry.register(ToolConfig(
        tool_id="good_tool", tool_type="function", name="good_tool",
        description="Good tool",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": []},
    ), callable_fn=good_tool)

    skill = ToolConfig(
        tool_id="fail-skill", tool_type="skill", name="fail_skill",
        description="Fail skill", parameters={},
        steps=[
            {"type": "tool", "target": "nonexistent_tool", "args_mapping": {}},
            {"type": "tool", "target": "good_tool", "args_mapping": {"x": "prev_result"}},
        ],
    )
    tool_registry.register(skill)
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("fail-skill", {})
    assert result.success is False
    assert len(call_log) == 0


def test_execute_skill_prev_result_chaining():
    """prev_result from step 0 is passed to step 1 via args_mapping."""
    model_registry, tool_registry = _make_registries()
    received = []

    def step1():
        return "output_from_step1"

    def step2(input_val=""):
        received.append(input_val)
        return "done"

    tool_registry.register(ToolConfig(
        tool_id="s1", tool_type="function", name="s1",
        description="Step 1", parameters={"type": "object", "properties": {}, "required": []},
    ), callable_fn=step1)
    tool_registry.register(ToolConfig(
        tool_id="s2", tool_type="function", name="s2",
        description="Step 2",
        parameters={"type": "object", "properties": {"input_val": {"type": "string"}}, "required": []},
    ), callable_fn=step2)

    skill = ToolConfig(
        tool_id="chain-skill", tool_type="skill", name="chain",
        description="Chain", parameters={},
        steps=[
            {"type": "tool", "target": "s1", "args_mapping": {}},
            {"type": "tool", "target": "s2", "args_mapping": {"input_val": "prev_result"}},
        ],
    )
    tool_registry.register(skill)
    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("chain-skill", {})
    assert result.success is True
    assert received == ["output_from_step1"]


# Feature: composable-agent-runtime, Property 13: Skill 步骤顺序执行
# **Validates: Requirements 4.2, 4.4**
@settings(max_examples=100)
@given(n_steps=st.integers(min_value=1, max_value=5))
def test_property_skill_steps_execute_sequentially(n_steps):
    """For any Skill with N tool steps, execution proceeds in order
    steps[0] -> steps[1] -> ... -> steps[N-1], and each step receives
    the previous step's output as input."""
    model_registry = ModelRegistry()
    tool_registry = ToolRegistry()

    # Track execution order and inputs received by each tool
    call_log = []  # list of (step_index, input_received)

    # Register N function tools, each recording its call order and input
    for i in range(n_steps):
        step_idx = i  # capture for closure

        def make_fn(idx):
            def fn(input_val=""):
                call_log.append((idx, input_val))
                return f"output_from_step_{idx}"
            return fn

        tool_registry.register(
            ToolConfig(
                tool_id=f"tool_{i}",
                tool_type="function",
                name=f"tool_{i}",
                description=f"Step {i} tool",
                parameters={
                    "type": "object",
                    "properties": {"input_val": {"type": "string"}},
                    "required": [],
                },
            ),
            callable_fn=make_fn(i),
        )

    # Build skill steps: step 0 gets a fixed context value,
    # steps 1..N-1 each receive prev_result from the prior step
    steps = []
    for i in range(n_steps):
        if i == 0:
            args_mapping = {"input_val": "seed"}
        else:
            args_mapping = {"input_val": "prev_result"}
        steps.append({
            "type": "tool",
            "target": f"tool_{i}",
            "args_mapping": args_mapping,
        })

    skill = ToolConfig(
        tool_id="seq-skill",
        tool_type="skill",
        name="seq_skill",
        description="Sequential skill",
        parameters={"type": "object", "properties": {"seed": {"type": "string"}}, "required": []},
        steps=steps,
    )
    tool_registry.register(skill)

    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("seq-skill", {"seed": "initial_input"})

    # Skill should succeed
    assert result.success is True, f"Skill failed: {result.error}"

    # All N tools must have been called
    assert len(call_log) == n_steps, (
        f"Expected {n_steps} calls, got {len(call_log)}"
    )

    # Verify execution order: step indices must be 0, 1, ..., N-1
    for expected_idx, (actual_idx, _) in enumerate(call_log):
        assert actual_idx == expected_idx, (
            f"Expected step {expected_idx} but got step {actual_idx}"
        )

    # Verify prev_result chaining:
    # Step 0 receives the seed context value "initial_input"
    assert call_log[0][1] == "initial_input", (
        f"Step 0 should receive 'initial_input', got '{call_log[0][1]}'"
    )
    # Steps 1..N-1 each receive the output of the previous step
    for i in range(1, n_steps):
        expected_input = f"output_from_step_{i - 1}"
        actual_input = call_log[i][1]
        assert actual_input == expected_input, (
            f"Step {i} should receive '{expected_input}', got '{actual_input}'"
        )


# Feature: composable-agent-runtime, Property 14: Skill 失败中止
# **Validates: Requirements 4.5**
@settings(max_examples=100)
@given(data=st.data())
def test_property_skill_failure_aborts_subsequent_steps(data):
    """For any Skill with N steps, if step K fails (0 <= K < N),
    steps K+1..N-1 are NOT executed, and the error message contains
    'Step K' (the failed step index). result.success is False."""
    n_steps = data.draw(st.integers(min_value=2, max_value=5), label="n_steps")
    fail_index = data.draw(st.integers(min_value=0, max_value=n_steps - 1), label="fail_index")

    model_registry = ModelRegistry()
    tool_registry = ToolRegistry()

    # Track which steps were actually executed
    call_log = []

    # Register N function tools; step at fail_index targets a non-existent tool
    for i in range(n_steps):
        if i == fail_index:
            # Skip registration — the skill step will target a missing tool
            continue

        step_idx = i

        def make_fn(idx):
            def fn(input_val=""):
                call_log.append(idx)
                return f"output_from_step_{idx}"
            return fn

        tool_registry.register(
            ToolConfig(
                tool_id=f"tool_{i}",
                tool_type="function",
                name=f"tool_{i}",
                description=f"Step {i} tool",
                parameters={
                    "type": "object",
                    "properties": {"input_val": {"type": "string"}},
                    "required": [],
                },
            ),
            callable_fn=make_fn(step_idx),
        )

    # Build skill steps: each step targets tool_<i>, but step fail_index
    # targets "nonexistent_tool_<fail_index>" which is not registered
    steps = []
    for i in range(n_steps):
        if i == fail_index:
            target = f"nonexistent_tool_{fail_index}"
        else:
            target = f"tool_{i}"

        if i == 0:
            args_mapping = {"input_val": "seed"}
        else:
            args_mapping = {"input_val": "prev_result"}

        steps.append({
            "type": "tool",
            "target": target,
            "args_mapping": args_mapping,
        })

    skill = ToolConfig(
        tool_id="abort-skill",
        tool_type="skill",
        name="abort_skill",
        description="Skill with injected failure",
        parameters={"type": "object", "properties": {"seed": {"type": "string"}}, "required": []},
        steps=steps,
    )
    tool_registry.register(skill)

    runtime = Runtime(model_registry=model_registry, tool_registry=tool_registry)
    result = runtime.execute_skill("abort-skill", {"seed": "init"})

    # result.success must be False
    assert result.success is False, "Skill should have failed"

    # Steps 0..K-1 should have been executed
    assert call_log == list(range(fail_index)), (
        f"Expected steps {list(range(fail_index))} executed before failure at {fail_index}, "
        f"got {call_log}"
    )

    # Steps K+1..N-1 should NOT have been executed (already guaranteed by
    # the assertion above, but let's be explicit)
    for idx in range(fail_index + 1, n_steps):
        assert idx not in call_log, f"Step {idx} should not have executed after failure at step {fail_index}"

    # Error message must contain "Step <K>"
    assert f"Step {fail_index}" in result.error, (
        f"Error should mention 'Step {fail_index}', got: {result.error}"
    )

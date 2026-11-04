"""Runtime-owned context reaches the model even when the template ignores it.

Regression for the second half of the 400-token-window failure: the failing
session's system message was 1 670 characters -- template body only.  Group-chat
framing, ``## Summary`` and ``## Memory`` were handed over as the template
argument ``GC_FRAMING``, but the agent's template (``SoftwareDevManagerPrompt``)
names only ``{{AGENTS}}``, so the renderer dropped them without a trace.  The
catch-up line the model did receive promised "部分早期对话已被压缩为摘要，详见上方
Summary" -- a summary the model had never been given.

Fix: an internal ``Message.render_suffix`` appended after rendering, with no
placeholder required anywhere.
"""

from __future__ import annotations

from runtime.common import _thread_local, set_request_context
from runtime.context_manager import MemoryEntry
from runtime.group_chat import _build_agent_system_messages
from runtime.models import InferenceRequest, Message
from runtime.registry import ModelRegistry, ToolRegistry
from runtime.runtime import Runtime


class FakeTemplateManager:
    def __init__(self, templates: dict):
        self._templates = templates

    def get(self, template_id: str):
        body = self._templates.get(template_id)
        if body is None:
            return None

        class _T:
            content = body

        return _T()


def _runtime(templates: dict) -> Runtime:
    return Runtime(ModelRegistry(), ToolRegistry(),
                   prompt_template_manager=FakeTemplateManager(templates))


def _memory() -> list:
    return [MemoryEntry(entry_type="fact", content="发布脚本在 scripts/publish.py",
                        source_turn_index=3, confidence=0.9, created_at="2026-11-04T11:18:46")]


def test_render_suffix_is_internal_only():
    msg = Message(role="system", content="正文", render_suffix="## Summary\n摘要")
    payload = msg.to_dict()

    assert "render_suffix" not in payload
    assert payload["content"] == "正文"
    # Round-tripping must not invent the field from untrusted client input.
    assert getattr(Message.from_dict(payload), "render_suffix", None) is None


def test_group_framing_and_summary_no_longer_use_template_arguments():
    agent = {
        "agent_id": "DevManager", "nickname": "开发经理", "system_prompt": "",
        "template_id": "SoftwareDevManagerPrompt",
        "template_arguments": {"AGENTS": "{{AGENTS}}"},
        "tool_ids": ["talk_to"],
    }

    msgs = _build_agent_system_messages(
        agent, "DevManager",
        agents_markdown="| 用户 | user |\n| 孙悟空 | SunWuKong |",
        dispatch_agents_markdown="| 孙悟空 | SunWuKong |",
        promoted_turn=None,
        summary_text="已经完成了采集与去重。",
        memory_entries=_memory(),
    )

    sys_msg = msgs[0]
    assert "GC_FRAMING" not in (sys_msg.arguments or {})
    assert sys_msg.render_suffix
    assert "## Summary" in sys_msg.render_suffix
    assert "已经完成了采集与去重。" in sys_msg.render_suffix
    assert "## Memory" in sys_msg.render_suffix
    assert "scripts/publish.py" in sys_msg.render_suffix


def test_summary_survives_a_template_that_names_only_agents():
    """The exact bug: an unnamed placeholder means the text never arrives."""
    templates = {"SoftwareDevManagerPrompt": "你是开发经理。\n可调度目录：{{AGENTS}}"}
    agent = {
        "agent_id": "DevManager", "nickname": "开发经理", "system_prompt": "",
        "template_id": "SoftwareDevManagerPrompt", "template_arguments": {},
        "tool_ids": ["talk_to"],
    }
    msgs = _build_agent_system_messages(
        agent, "DevManager",
        agents_markdown="| 用户 | user |",
        dispatch_agents_markdown="| 孙悟空 | SunWuKong |",
        promoted_turn=None,
        summary_text="早前对话摘要内容。",
        memory_entries=_memory(),
    )
    runtime = _runtime(templates)

    sent = runtime._normalize_messages(
        InferenceRequest(model_id="unused", messages=msgs, tool_ids=[]))[0].content

    assert "早前对话摘要内容。" in sent, "the summary never reached the model"
    assert "## Memory" in sent
    assert "| 用户 | user |" in sent          # framing survived too
    assert "{{AGENTS}}" not in sent
    assert "{{GC_FRAMING}}" not in sent


def test_retired_placeholder_never_leaks_as_a_literal():
    templates = {"legacy": "旧模板：{{AGENTS}} 与 {{GC_FRAMING}} 结尾"}
    runtime = _runtime(templates)
    msgs = [Message(role="system", content="", prompt_template="legacy",
                    arguments={"AGENTS": "名单"}, render_suffix="## Summary\n摘要")]

    sent = runtime._normalize_messages(
        InferenceRequest(model_id="unused", messages=msgs, tool_ids=[]))[0].content

    assert "{{GC_FRAMING}}" not in sent
    assert sent.count("## Summary") == 1
    assert sent.endswith("结尾\n\n## Summary\n摘要") or "结尾" in sent


def test_rendering_twice_does_not_stack_the_summary():
    """The tool loop re-normalizes the same request on every round."""
    templates = {"t": "正文 {{AGENTS}}"}
    msgs = [Message(role="system", content="", prompt_template="t",
                    arguments={"AGENTS": "名单"}, render_suffix="## Summary\n摘要")]
    runtime = _runtime(templates)
    request = InferenceRequest(model_id="unused", messages=msgs, tool_ids=[])

    first = runtime._normalize_messages(request)[0].content
    second = runtime._normalize_messages(request)[0].content

    assert first.count("## Summary") == 1
    assert second == first


def test_plain_prompt_agents_keep_the_summary_in_content():
    """Non-template agents have no renderer to fight, so content stays simple."""
    agent = {"agent_id": "DevManager", "nickname": "开发经理",
             "system_prompt": "你是开发经理。", "template_id": None, "tool_ids": []}

    msgs = _build_agent_system_messages(
        agent, "DevManager", agents_markdown="", dispatch_agents_markdown=None,
        promoted_turn=None, summary_text="摘要正文。", memory_entries=None,
    )

    assert "## Summary" in msgs[0].content
    assert not msgs[0].render_suffix


def test_talk_to_subagent_carries_the_framing_in_render_suffix():
    from runtime.builtin_tools_agent import _make_talk_to_fn

    captured: list = []

    class _Runtime:
        _tool_registry = {}

        def infer_stream(self, request, cancel_event=None, **_pipeline_kwargs):
            captured.append(request)
            yield Message(role="assistant", content="子 agent 完成")

    manager_agents = [
        {"agent_id": "DevManager", "nickname": "开发经理", "system_prompt": "",
         "template_id": "SoftwareDevManagerPrompt", "template_arguments": {},
         "tool_ids": ["talk_to"], "model_id": "m"},
        # The framing is injected when the TARGET agent can dispatch, i.e. when
        # the sub-session runs with talk_to available (see _make_talk_to_fn).
        {"agent_id": "CodeMaster", "nickname": "代码经理", "system_prompt": "",
         "template_id": "SoftwareDevManagerPrompt", "template_arguments": {},
         "tool_ids": ["talk_to"], "model_id": "m"},
    ]

    class _Manager:
        def __init__(self):
            self._by_id = {a["agent_id"]: a for a in manager_agents}

        def get(self, key):
            for a in self._by_id.values():
                if a.get("nickname") == key:
                    return a
            return self._by_id.get(key)

    talk_to = _make_talk_to_fn(_Runtime(), _thread_local)
    set_request_context(
        agent_id="DevManager",
        agent_manager=_Manager(),
        all_agent_ids=["DevManager", "CodeMaster"],
        available_tool_ids=["talk_to"],
    )
    try:
        talk_to(["CodeMaster"], "帮忙看一下")
    finally:
        _thread_local.__dict__.clear()

    assert captured, "the sub-agent never ran"
    system_msgs = [m for m in captured[0].messages if m.role == "system"]
    assert system_msgs
    assert any("这是一场群聊对话" in (m.render_suffix or "") for m in system_msgs), \
        [m.render_suffix for m in system_msgs]
    assert not any("GC_FRAMING" in (m.arguments or {}) for m in system_msgs)

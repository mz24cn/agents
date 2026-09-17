"""Tests for tui/render.py.

The compact-badge section is a case-for-case port of
``web/src/lib/compact-tool.test.js`` (acceptance baseline, contract §5).
The Transcript section covers contract §4.2 frame semantics.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tui.render import (  # noqa: E402
    Block,
    Transcript,
    compact_tool_display,
    parse_tool_args,
)

# ---------------------------------------------------------------------------
# Icon constants (web/src/lib/compact-tool.js)
# ---------------------------------------------------------------------------

ICON_READ = "\U0001f440"        # 👁
ICON_WRITE = "\U0001f4be"       # ✉
ICON_EDIT = "\u270d\ufe0f"      # ✍️
ICON_SEARCH = "\U0001f50e"      # 🔎
ICON_SHELL = "\U0001f527"       # 🔧
ICON_CLI = "\U0001f4bb"         # 💻
ICON_DEFAULT = "\U0001f6e0\ufe0f"  # 🛠️


# ===========================================================================
# parseToolArgs (port of describe('parseToolArgs'))
# ===========================================================================

class TestParseToolArgs:
    def test_parses_json_string_arguments(self):
        assert parse_tool_args({"arguments": '{"path":"a/b.py"}'}) == {
            "path": "a/b.py",
        }

    def test_passes_object_arguments_through(self):
        assert parse_tool_args({"arguments": {"path": "a/b.py"}}) == {
            "path": "a/b.py",
        }

    def test_returns_empty_for_missing_or_invalid_arguments(self):
        assert parse_tool_args(None) == {}
        assert parse_tool_args({}) == {}
        assert parse_tool_args({"arguments": ""}) == {}
        assert parse_tool_args({"arguments": '{"path":'}) == {}
        assert parse_tool_args({"arguments": '"just a string"'}) == {}


# ===========================================================================
# compactToolDisplay: file tools (port of the file-tools describe block)
# ===========================================================================

class TestCompactFileTools:
    def test_read_file_shows_file_name(self):
        display = compact_tool_display({
            "name": "read_file",
            "arguments": '{"path":"web/src/lib/compact-tool.js"}',
        })
        assert display == (ICON_READ, "compact-tool.js")

    def test_write_file_and_edit_file_icons(self):
        assert compact_tool_display(
            {"name": "write_file", "arguments": {"path": "a/b.txt"}}
        ) == (ICON_WRITE, "b.txt")
        assert compact_tool_display(
            {"name": "edit_file", "arguments": {"path": "/abs/c.txt"}}
        ) == (ICON_EDIT, "c.txt")

    def test_windows_style_paths(self):
        assert compact_tool_display(
            {"name": "read_file", "arguments": {"path": "C:\\tmp\\notes.md"}}
        ) == (ICON_READ, "notes.md")
        assert compact_tool_display(
            {"name": "write_file", "arguments": {"path": "C:/scratch/tmp/x.log"}}
        ) == (ICON_WRITE, "x.log")

    def test_path_without_directory_separator(self):
        assert compact_tool_display(
            {"name": "edit_file", "arguments": {"path": "Makefile"}}
        ) == (ICON_EDIT, "Makefile")

    def test_falls_back_to_tool_name_when_path_missing(self):
        assert compact_tool_display(
            {"name": "read_file", "arguments": '{"path":'}
        ) == (ICON_READ, "read_file")
        assert compact_tool_display({"name": "write_file"}) == (
            ICON_WRITE,
            "write_file",
        )


# ===========================================================================
# compactToolDisplay: search_code
# ===========================================================================

class TestCompactSearchCode:
    def test_plain_pattern(self):
        assert compact_tool_display(
            {"name": "search_code", "arguments": {"query": "ToolCallCard"}}
        ) == (ICON_SEARCH, "ToolCallCard")

    def test_first_keyword_of_pipe_alternation(self):
        assert compact_tool_display(
            {"name": "search_code",
             "arguments": {"query": "read_file|write_file|edit_file"}}
        ) == (ICON_SEARCH, "read_file")

    def test_first_keyword_of_multi_word_pattern(self):
        assert compact_tool_display(
            {"name": "search_code", "arguments": {"query": "def main"}}
        ) == (ICON_SEARCH, "def")

    def test_skips_empty_leading_alternative(self):
        assert compact_tool_display(
            {"name": "search_code", "arguments": {"query": "|foo|bar"}}
        ) == (ICON_SEARCH, "foo")

    def test_falls_back_to_tool_name_when_query_missing(self):
        assert compact_tool_display({"name": "search_code"}) == (
            ICON_SEARCH,
            "search_code",
        )


# ===========================================================================
# compactToolDisplay: exec tools
# ===========================================================================

class TestCompactExecTools:
    def test_skips_cd_echo_and_summarizes_first_substantive(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "cd /tmp && echo hi && python run.py --x"},
        })
        assert display == (ICON_SHELL, "python run.py")

    def test_exec_cli_icon(self):
        assert compact_tool_display(
            {"name": "exec_cli", "arguments": {"command": "pytest -x"}}
        ) == (ICON_CLI, "pytest")

    def test_drops_switches_keeps_first_substantive_argument(self):
        # `grep -n "name=\|\"name\"\|'name'" runtime/builtin_tools_coding.py`
        # -> `grep name=\|\"name\"\|'name'`
        command = 'grep -n "name=\\|\\\"name\\\"\\|\'name\'" runtime/builtin_tools_coding.py'
        display = compact_tool_display(
            {"name": "exec_shell", "arguments": {"command": command}}
        )
        assert display == (ICON_SHELL, r"grep name=\|\"name\"\|'name'")

    def test_quoted_argument_with_spaces_stays_one_word(self):
        # command: grep -n "op=push\|op = \"push\"\|\"Unsu\"" runtime/handler_api.py
        # (backslash-pipe / backslash-quote exactly as in the JS fixture)
        command = r'grep -n "op=push\|op = \"push\"\|\"Unsu\"" runtime/handler_api.py'
        label = compact_tool_display(
            {"name": "exec_shell", "arguments": {"command": command}}
        )[1]
        assert label.startswith(r"grep op=push\|op = \"push\"\|")
        assert not label.endswith("runtime/handler_api.py")

    def test_strips_one_pair_of_surrounding_quotes(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "sed -i '1520,1600p' runtime/handler_api.py"},
        })
        assert display == (ICON_SHELL, "sed 1520,1600p")

    def test_keeps_plain_positional_drops_trailing_switches(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "git log --oneline -n 5"},
        })
        assert display == (ICON_SHELL, "git log")

    def test_noise_only_command_stays_visible(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "cd /var/log"},
        })
        assert display == (ICON_SHELL, "cd /var/log")

    def test_stops_at_shell_operators_within_statement(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "cd src\nls -la | grep py"},
        })
        assert display == (ICON_SHELL, "ls")

    def test_skips_bare_assignments_keeps_prefixed_command(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "export FOO=bar && FOO=1 npm test"},
        })
        assert display == (ICON_SHELL, "npm test")

    def test_falls_back_to_tool_name_for_empty_command(self):
        assert compact_tool_display(
            {"name": "exec_cli", "arguments": {"command": ""}}
        ) == (ICON_CLI, "exec_cli")


# ===========================================================================
# compactToolDisplay: fallback behavior
# ===========================================================================

class TestCompactFallback:
    def test_other_tools_untouched_with_default_icon(self):
        assert compact_tool_display(
            {"name": "fetch", "arguments": {"url": "https://example.com"}}
        ) == (ICON_DEFAULT, "fetch")

    def test_fallback_name_for_unknown_tools_without_name(self):
        assert compact_tool_display(None, "unknown") == (
            ICON_DEFAULT,
            "unknown",
        )
        assert compact_tool_display({}, "unknown") == (ICON_DEFAULT, "unknown")

    def test_truncates_overly_long_labels(self):
        display = compact_tool_display({
            "name": "exec_shell",
            "arguments": {"command": "python " + "a" * 80},
        })
        assert len(display[1]) == 40
        assert display[1].endswith("\u2026")


# ===========================================================================
# Transcript (contract §4.2)
# ===========================================================================

def _tool_tc(tc_id="call_1", name="read_file", path="a/b.py"):
    return {"id": tc_id, "name": name, "arguments": {"path": path}}


class TestTranscriptBasic:
    def test_init_records_session_and_title(self):
        t = Transcript()
        t.append_frame("init", {"session_id": "s-42", "title": "你好"})
        assert t.session_id() == "s-42"
        assert t.title() == "你好"
        assert t.blocks() == []

    def test_user_block_keeps_raw_lines(self):
        t = Transcript()
        t.append_frame("message", {"role": "user", "content": "第一行\n第二行"})
        (block,) = t.blocks()
        assert block.kind == "user"
        assert block.lines == ["第一行", "第二行"]
        assert block.full_lines is None

    def test_consecutive_assistant_frames_merge_into_one_block(self):
        t = Transcript()
        t.append_frame("message", {"role": "assistant", "content": "Hel"})
        t.append_frame("message", {"role": "assistant", "content": "lo 世界"})
        t.append_frame("message", {"role": "assistant", "content": "\n第二行"})
        (block,) = t.blocks()
        assert block.kind == "assistant"
        assert block.lines == ["Hello 世界", "第二行"]

    def test_assistant_blocks_split_by_tool_frame(self):
        t = Transcript()
        t.append_frame("message", {"role": "assistant", "content": "前"})
        t.append_frame("message", {
            "role": "assistant",
            "content": "",
            "tool_calls": [_tool_tc()],
        })
        t.append_frame("message", {
            "role": "tool", "name": "read_file",
            "content": "ok", "tool_use_id": "call_1",
        })
        t.append_frame("message", {"role": "assistant", "content": "后"})
        kinds = [b.kind for b in t.blocks()]
        assert kinds == ["assistant", "tool", "assistant"]
        assert t.blocks()[0].lines == ["前"]
        assert t.blocks()[2].lines == ["后"]

    def test_thinking_only_in_full_lines_with_prefix(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "assistant",
            "content": "答案",
            "thinking": "想一下\n再想",
        })
        (block,) = t.blocks()
        assert block.lines == ["答案"]
        assert block.full_lines == ["思考: 想一下", "思考: 再想"]

    def test_images_line(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "assistant",
            "content": "看图",
            "images": ["base64a", "base64b"],
        })
        (block,) = t.blocks()
        assert block.lines == [
            "看图",
            "[图片] 2 张（存于会话 artifact）",
        ]
        # a later metadata-only frame must not drop the images line
        t.append_frame("message", {"role": "assistant", "agent_id": "a1"})
        (block,) = t.blocks()
        assert block.lines[-1] == "[图片] 2 张（存于会话 artifact）"


class TestTranscriptTools:
    def test_tool_badge_pending_then_ok(self):
        t = Transcript()
        t.append_frame("message", {"role": "user", "content": "读文件"})
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [_tool_tc()],
        })
        blocks = t.blocks()
        tool = blocks[-1]
        assert tool.kind == "tool"
        assert tool.lines == [f"▸ {ICON_READ} b.py …"]
        assert tool.badge == tool.lines[0]
        assert tool.full_lines is None

        t.append_frame("message", {
            "role": "tool", "name": "read_file",
            "content": "line1\nline2\nline3", "tool_use_id": "call_1",
        })
        (tool,) = [b for b in t.blocks() if b.kind == "tool"]
        assert tool.lines == [f"▸ {ICON_READ} b.py ✓"]
        assert tool.full_lines == ["line1", "line2", "line3"]
        assert tool.detail == "3 行"

    def test_tool_error_content_marks_fail(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [_tool_tc(name="exec_shell")],
        })
        t.append_frame("message", {
            "role": "tool", "name": "exec_shell",
            "content": "Error: command failed", "tool_use_id": "call_1",
        })
        (tool,) = [b for b in t.blocks() if b.kind == "tool"]
        assert tool.lines == [f"▸ {ICON_SHELL} exec_shell ✗"]

    def test_tool_pairing_by_id_not_order(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"id": "a", "name": "read_file", "arguments": {"path": "x.py"}},
                {"id": "b", "name": "write_file", "arguments": {"path": "y.py"}},
            ],
        })
        # result for the SECOND call arrives first
        t.append_frame("message", {
            "role": "tool", "name": "write_file",
            "content": "wrote", "tool_use_id": "b",
        })
        tools = [b for b in t.blocks() if b.kind == "tool"]
        assert tools[0].lines == [f"▸ {ICON_READ} x.py …"]
        assert tools[1].lines == [f"▸ {ICON_WRITE} y.py ✓"]

    def test_tool_pairing_name_order_fallback(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"name": "read_file", "arguments": {"path": "x.py"}},
            ],
        })
        t.append_frame("message", {
            "role": "tool", "name": "read_file",
            "content": "data",  # no tool_use_id at all
        })
        (tool,) = [b for b in t.blocks() if b.kind == "tool"]
        assert tool.lines == [f"▸ {ICON_READ} x.py ✓"]

    def test_unpaired_tool_result_creates_synthetic_badge(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "tool", "name": "exec_shell",
            "content": "out",
        })
        (tool,) = [b for b in t.blocks() if b.kind == "tool"]
        assert tool.lines == [f"▸ {ICON_SHELL} exec_shell ✓"]


class TestTranscriptUsageError:
    def test_usage_keeps_only_last(self):
        t = Transcript()
        t.append_frame("usage", {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "overall_ms": 1200,
        })
        t.append_frame("message", {"role": "user", "content": "再来"})
        t.append_frame("usage", {
            "prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28,
            "overall_ms": 3400,
            "total_prompt_tokens": 30, "total_completion_tokens": 13,
        })
        usage_blocks = [b for b in t.blocks() if b.kind == "usage"]
        assert len(usage_blocks) == 1
        assert usage_blocks[0].lines == [
            "⌁ ↑20 ↓8 28 tok · 3.4s Σ↑30 Σ↓13",
        ]
        assert t.last_usage()["total_tokens"] == 28

    def test_usage_inplace_update_on_tail(self):
        t = Transcript()
        t.append_frame("usage", {"prompt_tokens": 1, "completion_tokens": 1,
                                 "total_tokens": 2, "overall_ms": 100})
        t.append_frame("usage", {"prompt_tokens": 2, "completion_tokens": 2,
                                 "total_tokens": 4, "overall_ms": 200})
        usage_blocks = [b for b in t.blocks() if b.kind == "usage"]
        assert len(usage_blocks) == 1
        assert usage_blocks[0].lines == ["⌁ ↑2 ↓2 4 tok · 0.2s"]

    def test_usage_format_without_totals(self):
        t = Transcript()
        t.append_frame("usage", {"prompt_tokens": 3, "completion_tokens": 4})
        (usage,) = [b for b in t.blocks() if b.kind == "usage"]
        assert usage.lines == ["⌁ ↑3 ↓4 7 tok · 0.0s"]

    def test_error_frame_block(self):
        t = Transcript()
        t.append_frame("error", {"message": "boom"})
        (block,) = t.blocks()
        assert block.kind == "error"
        assert block.lines == ["boom"]

    def test_error_prefixed_assistant_content(self):
        t = Transcript()
        t.append_frame("message", {
            "role": "assistant",
            "content": "\n\nError: user interrupted.",
        })
        (block,) = t.blocks()
        assert block.kind == "error"
        assert block.lines == ["user interrupted."]

    def test_done_is_noop(self):
        t = Transcript()
        t.append_frame("done", {})
        assert t.blocks() == []


class TestTranscriptLoadConversation:
    def test_envelope_meta_and_messages(self):
        t = Transcript()
        t.load_conversation({
            "meta": {
                "session_id": "s-7",
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:01:00",
                "turn_count": 4,
            },
            "messages": [
                {"role": "user", "content": "你好", "timestamp": "t1"},
                {"role": "assistant", "content": "hi",
                 "stat": {"prompt_tokens": 5, "completion_tokens": 6,
                          "total_tokens": 11, "overall_ms": 900}},
                {"role": "assistant", "content": "",
                 "tool_calls": [
                     {"id": "c1", "name": "read_file",
                      "arguments": {"path": "/tmp/a.log"}},
                 ]},
                {"role": "tool", "name": "read_file", "content": "L1\nL2",
                 "tool_id": "read_file", "tool_use_id": "c1"},
                {"role": "assistant", "content": "done",
                 "stat": {"prompt_tokens": 9, "completion_tokens": 2,
                          "total_tokens": 11, "overall_ms": 300}},
            ],
        })
        assert t.session_id() == "s-7"
        kinds = [b.kind for b in t.blocks()]
        # usage blocks collapse to the single last one, at the tail
        assert kinds == ["user", "assistant", "tool", "assistant", "usage"]
        (usage,) = [b for b in t.blocks() if b.kind == "usage"]
        assert usage.lines == ["⌁ ↑9 ↓2 11 tok · 0.3s"]
        tool = next(b for b in t.blocks() if b.kind == "tool")
        assert tool.lines == [f"▸ {ICON_READ} a.log ✓"]
        assert tool.full_lines == ["L1", "L2"]
        assert tool.detail == "2 行"
        assert t.last_usage()["overall_ms"] == 300

    def test_bare_message_list_tolerated(self):
        t = Transcript()
        t.load_conversation([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ])
        assert [b.kind for b in t.blocks()] == ["user", "assistant"]

    def test_load_resets_previous_state(self):
        t = Transcript()
        t.append_frame("init", {"session_id": "old"})
        t.append_frame("message", {"role": "user", "content": "旧"})
        t.load_conversation({"meta": {"session_id": "new"}, "messages": []})
        assert t.session_id() == "new"
        assert t.blocks() == []

    def test_raw_usage_role_message_tolerated(self):
        t = Transcript()
        t.load_conversation({"meta": {}, "messages": [
            {"role": "usage",
             "content": '{"prompt_tokens": 1, "completion_tokens": 2,'
                        ' "total_tokens": 3, "overall_ms": 50}'},
        ]})
        usage_blocks = [b for b in t.blocks() if b.kind == "usage"]
        assert len(usage_blocks) == 1
        assert usage_blocks[0].lines == ["⌁ ↑1 ↓2 3 tok · 0.1s"]


class TestTranscriptControl:
    def test_remove_trailing_assistant(self):
        t = Transcript()
        t.append_frame("message", {"role": "user", "content": "q"})
        t.append_frame("message", {"role": "assistant", "content": "old answer"})
        t.append_frame("message", {"type": "remove_trailing_assistant"})
        assert [b.kind for b in t.blocks()] == ["user"]

    def test_tool_call_deltas_merge_into_one_badge(self):
        t = Transcript()
        # streaming deltas for the same call (id arrives late, name streams)
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [{"_index": 0, "name": "read"}],
        })
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"_index": 0, "name": "_file",
                 "arguments": '{"pat'},
            ],
        })
        t.append_frame("message", {
            "role": "assistant", "content": "",
            "tool_calls": [
                {"_index": 0, "id": "call_9", "arguments": 'h":"a.txt"}'},
            ],
        })
        tools = [b for b in t.blocks() if b.kind == "tool"]
        assert len(tools) == 1
        assert tools[0].lines == [f"▸ {ICON_READ} a.txt …"]
        t.append_frame("message", {
            "role": "tool", "name": "read_file",
            "content": "ok", "tool_use_id": "call_9",
        })
        (tool,) = [b for b in t.blocks() if b.kind == "tool"]
        assert tool.lines == [f"▸ {ICON_READ} a.txt ✓"]


class TestBlockShape:
    def test_block_defaults(self):
        b = Block("user", ["x"])
        assert b.full_lines is None
        assert b.badge is None
        assert b.detail == ""
        assert b.lines == ["x"]

"""Tests for the TUI side of the client-side tool contract (``tui/client_tools.py``).

The service registers ``AskUser`` as JSON only and never runs it, and the local
shell tool is never registered at all: both are declared per request and both
are executed here, in the terminal.  These tests drive that whole loop offline
- question modal, shell execution, the local echo, and the resume that carries
the answers back into the same session - and they pin the contract against the
service catalogue so the three implementations cannot drift apart.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tui import client_tools as ct
from tui.app import App, TuiOptions
from tui.render import Transcript
from tui.ui import QuestionModal

# ── declarations ───────────────────────────────────────────────────────────


def test_ask_user_is_on_by_default_and_can_be_switched_off():
    ask = ct.client_tool_definitions(False)
    assert [tool["name"] for tool in ask] == ["AskUser"]
    assert ct.client_tool_names(False) == ["AskUser"]
    assert ct.supports("AskUser") is True
    assert ct.supports("local_shell") is False

    both = ct.client_tool_definitions(True)
    assert [tool["name"] for tool in both] == ["AskUser", "local_shell"]
    assert ct.supports("local_shell", include_shell=True) is True

    # --no-ask-user: a run that must not stop and wait for a human declares
    # nothing at all, so the model cannot even try.
    assert ct.client_tool_definitions(False, False) == []
    assert ct.client_tool_names(False, False) == []
    assert ct.supports("AskUser", include_ask_user=False) is False
    assert ct.client_tool_definitions(True, False)[0]["name"] == "local_shell"


def test_ask_user_declaration_carries_every_model_parameter():
    ask = ct.client_tool_definitions(False)[0]
    assert set(ask) == {"name", "description", "parameters"}
    schema = ask["parameters"]
    assert schema["type"] == "object"
    assert "question" in schema["properties"]
    assert "options" in schema["properties"]
    assert "timeout" in schema["properties"]
    assert "default_option" in schema["properties"]
    assert schema["required"] == ["question", "options"]   # the return value is an option
    assert schema["properties"]["timeout"].get("default") in (0, "0")


def test_declarations_match_the_service_catalogue():
    from runtime.client_tools import CATALOGUE

    catalogue = {entry["name"]: entry for entry in CATALOGUE}
    for tool in ct.client_tool_definitions(True):
        entry = catalogue[tool["name"]]
        assert tool["description"] == entry["description"]
        assert tool["parameters"] == json.loads(json.dumps(entry["parameters"]))
    assert set(catalogue) == {"AskUser", "local_shell"}


# ── argument handling ──────────────────────────────────────────────────────


def test_arguments_arrive_as_json_text_or_dict():
    assert ct.parse_arguments('{"command": "ls"}') == {"command": "ls"}
    assert ct.parse_arguments({"command": "ls"}) == {"command": "ls"}
    assert ct.parse_arguments("") == {}
    assert ct.parse_arguments(None) == {}
    # Not JSON at all: keep the text so the handler can still use it.
    assert ct.parse_arguments("git status") == {"_raw": "git status"}


def test_the_question_survives_the_models_loose_edges():
    question, options, timeout, default = ct.question_request({
        "question": " 用哪种部署方式？ ",
        "options": ["容器化", "容器化", " 虚拟机 ", None, 3],
        "timeout": "30",
        "default_option": " 虚拟机 ",
    })
    assert question == "用哪种部署方式？"
    assert options == ["容器化", "虚拟机", "3"]        # de-duplicated, never empty
    assert timeout == 30
    assert default == "虚拟机"

    # A question with nothing usable is still a question.
    assert ct.question_request({})[0] != ""
    # Options handed over as one newline-joined string still become a list.
    assert ct.question_request({"options": "容器化\n虚拟机"})[1] == ["容器化", "虚拟机"]
    # A nonsense timeout is not a reason to fail the call.
    assert ct.question_request({"timeout": "soon"})[2] == 0


def test_an_answer_is_mapped_back_onto_the_offered_options():
    options = ["容器化", "虚拟机", "回滚"]
    assert ct.normalize_answer(options, "容器化") == "容器化"
    assert ct.normalize_answer(options, "  虚拟机 ") == "虚拟机"
    assert ct.normalize_answer(options, 3) == "回滚"        # typed the number
    assert ct.normalize_answer(options, "1") == "容器化"
    assert ct.normalize_answer(options, "容") == "容器化"    # an unambiguous prefix
    assert ct.normalize_answer(options, "随便吧") is None    # nothing to map onto
    assert ct.normalize_answer(options, None) is None


# ── AskUser ────────────────────────────────────────────────────────────────


def test_ask_user_returns_the_chosen_option_verbatim():
    answer = ct.run_ask_user(
        {"question": "用哪种部署方式？", "options": ["容器化", "虚拟机"]},
        ask_user=lambda q, o, timeout, default, deadline: "容器化",
    )
    assert answer == "容器化"


def test_ask_user_hands_the_models_parameters_to_the_question():
    seen = {}

    def ask(question, options, timeout, default, deadline):
        seen.update({"question": question, "options": options, "timeout": timeout,
                     "default": default, "deadline": deadline})
        return "容器化"

    started = time.monotonic()
    ct.run_ask_user({"question": "q", "options": ["容器化", "虚拟机"],
                     "timeout": 30, "default_option": "虚拟机"}, ask_user=ask)
    assert seen["question"] == "q"
    assert seen["options"] == ["容器化", "虚拟机"]
    assert seen["timeout"] == 30
    assert seen["default"] == "虚拟机"
    assert started + 30 <= seen["deadline"] < started + 31   # a real deadline, not a flag


def test_no_timeout_means_wait_forever():
    seen = {}
    ct.run_ask_user({"question": "q", "options": ["a", "b"]},
                    ask_user=lambda q, o, timeout, default, deadline: seen.update(
                        {"timeout": timeout, "deadline": deadline}) or o[0])
    assert seen["timeout"] == 0                        # the documented default
    assert seen["deadline"] is None                    # ... and nothing to expire

    # A timeout of 0 explicitly asked for is passed through unchanged.
    seen.clear()
    ct.run_ask_user({"question": "q", "options": ["a", "b"], "timeout": 0},
                    ask_user=lambda q, o, timeout, default, deadline: seen.update(
                        {"timeout": timeout, "deadline": deadline}) or o[0])
    assert seen["timeout"] == 0 and seen["deadline"] is None


def test_a_timeout_that_runs_out_uses_the_default_option_and_says_so():
    answer = ct.run_ask_user({"question": "q", "options": ["容器化", "虚拟机"],
                              "timeout": 30, "default_option": "虚拟机"},
                             ask_user=lambda *a: None)
    assert answer.startswith("虚拟机")
    assert "30s" in answer                             # the model learns it was automatic

    # Without a default there is nothing to invent - report the gap instead.
    answer = ct.run_ask_user({"question": "q", "options": ["容器化"], "timeout": 5},
                             ask_user=lambda *a: None)
    assert answer.startswith("(no answer")


def test_a_closed_question_is_reported_honestly():
    answer = ct.run_ask_user({"question": "q", "options": ["a", "b"]},
                             ask_user=lambda *a: None)
    assert answer == ct.CANCELLED_ANSWER
    assert answer                                      # never an empty tool message


def test_an_answer_outside_the_list_is_passed_on_rather_than_dropped():
    # A client that offers free text (the web dialog does) must not be silenced.
    answer = ct.run_ask_user({"question": "q", "options": ["容器化", "虚拟机"]},
                             ask_user=lambda *a: "自己写一个 k8s 清单")
    assert answer == "自己写一个 k8s 清单"


# ── local_shell ────────────────────────────────────────────────────────────


def test_local_shell_reports_like_exec_shell():
    out = json.loads(ct.run_local_shell({"command": "echo hello"}))
    assert out["exit_code"] == 0
    assert "hello" in out["stdout"]
    assert set(out) >= {"exit_code", "stdout", "stderr", "truncated"}


def test_local_shell_keeps_the_exit_code_and_stderr_of_a_failing_command():
    out = json.loads(ct.run_local_shell({"command": "echo oops >&2; exit 3"}))
    assert out["exit_code"] == 3
    assert "oops" in out["stderr"]


def test_local_shell_works_in_the_terminals_own_directory():
    out = json.loads(ct.run_local_shell({"command": "pwd"}, workspace="/tmp"))
    assert out["exit_code"] == 0
    assert out["stdout"].strip() in ("/tmp", "/private/tmp")
    # No workspace: the current directory, like any shell would do.
    here = json.loads(ct.run_local_shell({"command": "pwd"}))
    assert here["stdout"].strip() == os.getcwd()


def test_local_shell_is_the_right_shell_per_platform(monkeypatch):
    assert ct.windows_are_posix_shells() is True
    monkeypatch.setattr(ct.os, "name", "nt")
    argv = ct.shell_argv("Get-Process")
    assert argv[0].lower().startswith("powershell")
    assert "-NoProfile" in argv and "-NonInteractive" in argv
    assert argv[-1] == "Get-Process"

    monkeypatch.setattr(ct.os, "name", "posix")
    monkeypatch.setattr(ct.shutil, "which", lambda name: None)
    assert ct.shell_argv("ls -l") == ["/bin/sh", "-c", "ls -l"]


def test_local_shell_kills_a_command_that_overstays_its_timeout():
    started = time.monotonic()
    out = json.loads(ct.run_local_shell({"command": "sleep 30"}, default_timeout=1))
    elapsed = time.monotonic() - started
    assert out["error"] == "Timeout"
    assert out["timed_out"] is True
    assert elapsed < 10, f"timeout was not enforced: {elapsed:.1f}s"


def test_local_shell_caps_what_it_sends_back():
    script = "yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx | head -n 400"
    out = json.loads(ct.run_local_shell({"command": script}, line_limit=50,
                                        column_limit=40))
    assert out["truncated"] is True
    assert out["omitted_lines"] == 350
    assert len(out["stdout"]) <= 50 * 40 + 60          # the marker line too


def test_local_shell_refuses_an_empty_command_without_spawning_anything():
    out = json.loads(ct.run_local_shell({"command": "   "}))
    assert out["error"] == "EmptyCommand"
    assert out["message"]


# ── dispatch ───────────────────────────────────────────────────────────────


def test_only_declared_tools_are_executed_here():
    result = json.loads(ct.run_client_tool("exec_shell", '{"command": "ls"}',
                                           ask_user=lambda *a: None))
    assert result["error"] == "UnknownClientTool"      # the service owns that one

    # local_shell is offered only by a terminal that opted in.
    refused = json.loads(ct.run_client_tool("local_shell", '{"command": "ls"}',
                                            ask_user=lambda *a: None))
    assert refused["error"] == "NotOffered"
    assert json.loads(ct.run_client_tool("local_shell", '{"command": "echo ok"}',
                                         ask_user=lambda *a: None,
                                         include_shell=True))["exit_code"] == 0


def test_the_result_is_a_tool_message_addressed_to_the_call():
    message = ct.tool_result_message({"id": "c1", "name": "AskUser"}, "容器化")
    assert message == {"role": "tool", "tool_use_id": "c1", "name": "AskUser",
                       "content": "容器化"}
    # The service matches answers on tool_use_id, so that key is the contract.
    assert ct.tool_result_message({"id": "c2", "name": "x"}, None)["content"] == ""


def test_unanswered_calls_are_found_in_the_transcript():
    frames = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "name": "AskUser",
                                              "arguments": '{"question": "q"}'},
                                             {"id": "s1", "name": "exec_shell",
                                              "function": {"arguments": "{}"}}]},
        {"role": "tool", "tool_use_id": "c1", "content": "容器化"},
    ]
    # The AskUser call was answered; only the shell call is the clients, and only
    # when this terminal offered it.
    assert ct.pending_client_calls_from_messages(frames, ["AskUser", "local_shell"]) == []
    frames.append({"role": "assistant", "tool_calls": [
        {"id": "c2", "name": "local_shell", "function": {"arguments": '{"command": "ls"}'}}]})
    pending = ct.pending_client_calls_from_messages(frames, ["AskUser", "local_shell"])
    assert [call["name"] for call in pending] == ["local_shell"]
    assert json.loads(pending[0]["arguments"]) == {"command": "ls"}
    # A server-side call is never picked up, named or not.
    assert ct.pending_client_calls_from_messages(frames, ["AskUser"]) == []


# ── the question modal (tui/ui.py) ─────────────────────────────────────────


def _modal(**kwargs):
    return QuestionModal("部署方式？", ["继续", "暂停", "取消"], **kwargs)


def test_modal_preselects_the_default_option():
    modal = _modal(timeout=10, default_option="暂停")
    assert modal.selected().value == "暂停"
    # A default that is not one of the options does not break the modal.
    assert _modal(default_option="重来").selected().value == "继续"


def test_modal_navigation_wraps_and_skips_nothing():
    # Option navigation is the /sessions selector: a ring, never an out-of-range
    # index, so a stuck arrow key cannot escape the question.
    modal = _modal()
    seen = []
    for _ in range(len(modal.options)):
        seen.append(modal.selected().value)
        modal.move(1)
    assert seen == ["继续", "暂停", "取消"]          # exactly one loop

    modal = _modal()
    for _ in range(9):                              # 3 options × 3 laps
        modal.move(-1)
        assert modal.selected().value in modal.options
    assert modal.selected().value == "继续"


def test_modal_without_timeout_never_expires():
    modal = _modal()
    assert modal.timeout == 0
    assert modal.expired(time.monotonic() + 10_000) is False   # waits forever
    assert "等待选择" in "\n".join(modal.render(70, 12))


def test_modal_expires_once_its_deadline_passes():
    # The App owns the clock and hands the modal its deadline.
    now = time.monotonic()
    modal = _modal(timeout=5, default_option="取消", deadline=now + 5)
    assert modal.expired(now) is False
    assert modal.expired(now + 5.5) is True
    assert 0 <= modal.remaining(now + 4) <= 1.1


def test_modal_renders_the_question_options_and_countdown():
    modal = _modal(timeout=8, default_option="取消", deadline=time.monotonic() + 8)
    text = "\n".join(modal.render(70, 12))
    assert "部署方式？" in text and "继续" in text and "取消" in text
    assert "8s" in text                              # the countdown is visible
    assert len(modal.render(70, 12)) == 12           # fits the box it was asked for


def test_a_typed_number_picks_that_option():
    picked = []
    modal = _modal(on_select=picked.append)
    modal.pick_index(2)
    assert picked == ["暂停"]
    modal.pick_index(99)                             # out of range: no-op
    assert picked == ["暂停"]


# ── the App loop: hand back, answer locally, resume ────────────────────────


class _FinishedHandle:
    """What infer_stream returns once the (simulated) stream is over."""

    def done(self) -> bool:
        return True

    def wait(self, timeout=None) -> bool:
        return True

    def abort(self) -> None:
        pass


class _StubClient:
    """Records the bodies the App posts; never touches the network."""

    def __init__(self):
        self.bodies = []

    def infer_stream(self, body, on_event=None, **kwargs):
        self.bodies.append(body)
        return _FinishedHandle()

    def list_agents(self, **kwargs):
        return []

    def list_models(self, **kwargs):
        return []


class _QuietPlainIO:
    lines = []

    def sync(self, provider):
        pass

    def readline(self, prompt):
        return self.lines.pop(0) if self.lines else None


def _app(local_shell=False, answer="金丝雀发布", question_lines=None, ask_user=True):
    app = App(_StubClient(), TuiOptions(local_shell=local_shell, ask_user=ask_user),
              term=None, transcript_factory=lambda: Transcript(),
              plain_io=_QuietPlainIO())
    app.plain = True                    # keep the loop off the real question I/O
    app.session_id = "261103_010203"
    app.model_id = "m1"
    app.plain_io.lines = list(question_lines or [])
    if answer is not None:
        app._ask_user_plain = lambda question, options, *a: answer
    return app


def test_handoff_frame_is_run_locally_and_answered_in_the_same_session():
    app = _app()
    app._observe_client_tool_frames({
        "role": "status", "status": "client_tool_calls",
        "client_tool_calls": [{"id": "c1", "name": "AskUser",
                               "arguments": '{"question": "用哪种部署方式？"}'}],
    })
    app._after_stream()

    body = app.client.bodies[-1]
    assert body["session_id"] == "261103_010203"     # same session, not a new one
    assert body["client_tools"][0]["name"] == "AskUser"
    assert body["messages"] == [{"role": "tool", "tool_use_id": "c1",
                                 "name": "AskUser", "content": "金丝雀发布"}]


def test_the_question_itself_reaches_the_user(monkeypatch):
    asked = []
    app = _app(question_lines=["2"])                 # plain mode answers by number
    monkeypatch.setattr(app.plain_io, "readline",
                        lambda prompt: asked.append(prompt) or "2")
    app._observe_client_tool_frames({
        "role": "status", "status": "client_tool_calls",
        "client_tool_calls": [{"id": "c1", "name": "AskUser",
                               "arguments": '{"question": "用哪种部署方式？",'
                                            ' "options": ["容器化", "金丝雀发布"]}'}],
    })
    app._after_stream()
    assert app.client.bodies[-1]["messages"][0]["content"] == "金丝雀发布"


def test_local_shell_result_is_echoed_locally_because_it_is_never_streamed():
    app = _app(local_shell=True)
    app._echo_client_result("local_shell", '{"command": "git status"}',
                            json.dumps({"exit_code": 0, "stdout": "clean",
                                        "stderr": "", "truncated": False}))
    notices = [line for block in app._system_blocks for line in block.lines]
    assert any("git status" in line for line in notices), notices
    assert any("exit_code=0" in line for line in notices), notices


def test_ask_user_needs_no_extra_echo_because_the_question_was_already_shown():
    # The modal / plain prompt already put the question and the answer on screen;
    # repeating it as a system line would double the same exchange.
    app = _app()
    app._echo_client_result("AskUser", '{"question": "用哪种部署方式？"}', "金丝雀发布")
    assert [block.lines for block in app._system_blocks] == []


def test_a_handoff_without_a_session_id_says_why_the_answer_cannot_be_sent():
    app = _app()
    app.session_id = None
    app._client_pending = [{"id": "c1", "name": "AskUser", "arguments": "{}"}]
    app._run_next_client_tool()
    assert app.client.bodies == [], app.client.bodies
    notices = [line for block in app._system_blocks for line in block.lines]
    assert any("session_id" in line for line in notices), notices


def test_handoff_is_still_detected_when_the_service_does_not_name_it():
    # Older builds end the stream without a status frame; the call itself is
    # the only trace, and it is enough.
    app = _app()
    app._handle_event_side("message", {"role": "assistant", "content": "",
                                       "tool_calls": [{"id": "c1", "name": "AskUser",
                                                       "function": {"name": "AskUser",
                                                                    "arguments": "{}"}}]})
    app._after_stream()
    body = app.client.bodies[-1]
    assert body["client_tools"][0]["name"] == "AskUser"
    assert body["messages"][0]["tool_use_id"] == "c1"


def test_server_side_calls_are_left_to_the_service():
    app = _app()
    app._handle_event_side("message", {"role": "assistant", "content": "",
                                       "tool_calls": [{"id": "s1", "name": "exec_shell",
                                                       "function": {"arguments": "{}"}}]})
    app._after_stream()
    assert app.client.bodies == [], app.client.bodies


def test_declarations_travel_on_every_request():
    # The service remembers a session's client tools, but a TUI that turned
    # local_shell on (or off) has to re-declare them on every single request.
    app = _app()
    app._send_user("部署一下")
    app._send_continue()
    assert len(app.client.bodies) == 2
    for body in app.client.bodies:
        assert [tool["name"] for tool in body["client_tools"]] == ["AskUser"]
        assert body["stream"] is True
    assert app.client.bodies[-1]["continue"] is True


def test_opt_in_adds_the_shell_tool_and_only_then():
    assert [t["name"] for t in _app().client_tools] == ["AskUser"]
    with_shell = _app(local_shell=True)
    assert [t["name"] for t in with_shell.client_tools] == ["AskUser", "local_shell"]
    assert ct.client_tool_names(bool(with_shell.local_shell)) == ["AskUser", "local_shell"]


def test_no_ask_user_declares_nothing_at_all():
    # --no-ask-user: the model is not merely refused when it asks, it is never
    # offered the tool, so an unattended run cannot stall on a question.
    app = _app(ask_user=False)
    assert app.client_tools == []
    app._send_user("部署一下")
    assert app.client.bodies[-1]["client_tools"] == []


# ── the on-screen question (TUI mode, not --plain) ─────────────────────────


def _terminal_app(keys, **opts):
    """An App wired to a headless screen with scripted keys."""
    from tui import term as term_mod
    from tui import ui as ui_mod

    source = term_mod.FakeInput(list(keys))
    terminal = term_mod.Terminal(screen=term_mod.HeadlessScreen(70, 16),
                                 input_source=source)
    app = App(_StubClient(), TuiOptions(**opts), term=terminal,
              transcript_factory=lambda: Transcript(), plain_io=_QuietPlainIO())
    app.plain = False                      # the modal path, not the plain prompt
    app.session_id = "261103_010203"
    app.model_id = "m1"
    app._ui = ui_mod.TuiUI(terminal.screen)
    app._ui.transcript.set_provider(app._provider)
    return app, source


def _handoff(app, arguments):
    app._observe_client_tool_frames({
        "role": "status", "status": "client_tool_calls",
        "client_tool_calls": [{"id": "c1", "name": "AskUser", "arguments": arguments}],
    })
    app._after_stream()


def test_pressing_enter_on_a_highlighted_option_answers_the_model():
    from tui import term as term_mod

    app, _keys = _terminal_app([term_mod.KEY_DOWN, term_mod.KEY_ENTER])
    _handoff(app, '{"question": "用哪种部署方式？", "options": ["容器化", "虚拟机"]}')

    body = app.client.bodies[-1]
    assert body["messages"][0]["content"] == "虚拟机"     # ↓ then Enter
    assert body["session_id"] == "261103_010203"
    assert app._ui.modal is None                         # the modal is taken down


def test_typing_the_number_picks_that_option_without_moving_the_cursor():
    app, _keys = _terminal_app(["2"])
    _handoff(app, '{"question": "用哪种部署方式？", "options": ["容器化", "虚拟机"]}')
    assert app.client.bodies[-1]["messages"][0]["content"] == "虚拟机"


def test_the_screen_question_is_visible_while_it_waits():
    app, _keys = _terminal_app(["1"])
    _handoff(app, '{"question": "用哪种部署方式？", "options": ["容器化", "虚拟机"]}')
    drawn = app._ui.screen.text()
    assert "用哪种部署方式？" in drawn and "容器化" in drawn
    assert any("已选择：容器化" in line for line in
               [line for block in app._system_blocks for line in block.lines])


def test_a_question_that_cannot_be_answered_falls_back_to_the_default(monkeypatch):
    # No key ever arrives (piped / headless input): the countdown's default
    # option is what the model gets, labelled as not the user's decision.
    app, _keys = _terminal_app([])
    _handoff(app, '{"question": "用哪种部署方式？", "options": ["容器化", "虚拟机"],'
                   ' "timeout": 1, "default_option": "虚拟机"}')
    content = app.client.bodies[-1]["messages"][0]["content"]
    assert content.startswith("虚拟机") and "auto-selected" in content


def test_a_question_without_options_is_reported_instead_of_guessed():
    app, _keys = _terminal_app(["1"])
    _handoff(app, '{"question": "接下来做什么？", "options": []}')
    assert app.client.bodies[-1]["messages"][0]["content"] == ct.CANCELLED_ANSWER
    notices = [line for block in app._system_blocks for line in block.lines]
    assert any("未给出选项" in line for line in notices), notices

"""The three AskUser implementations must be the same tool.

A client-side tool is only a promise: the agent service stores the JSON, shows
it to the model, and never runs it.  Whoever drives the session has to keep the
promise, so the contract exists three times - ``runtime/client_tools.py`` (the
specification, registered as a built-in tool), ``tui/client_tools.py``
(terminal), ``web/src/lib/client-tools.js`` (browser).  Nothing stops one of
them from drifting silently, and the failure is invisible until a model asks a
question that its own client cannot answer.  This test is what keeps them
honest.
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from runtime import client_tools as catalogue  # noqa: E402
from tui import client_tools as tui_tools  # noqa: E402

WEB_MODULE = REPO / "web" / "src" / "lib" / "client-tools.js"
I18N = REPO / "web" / "src" / "lib" / "i18n.svelte.js"
TOOL_FORM = REPO / "web" / "src" / "components" / "tools" / "ToolForm.svelte"


def _web_definitions(enabled=("AskUser",)):
    """The browser's client tool definitions, asked of the browser's own module.

    Node evaluates the real ES module, so this compares the shipped code rather
    than a copy of it.  ``enabled`` is what the user ticked in the tool picker:
    passing nothing asks the module what it would declare for a user who
    enabled nothing.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed; the web side cannot be evaluated")
    script = (
        "import(%r).then(m => console.log(JSON.stringify("
        "m.clientToolDefinitions(%r))))"
        % (WEB_MODULE.as_uri(), list(enabled))
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node could not load {WEB_MODULE}: {completed.stderr[:500]}")
    return json.loads(completed.stdout)


def test_catalogue_lists_askuser_and_local_shell():
    assert [entry["name"] for entry in catalogue.CATALOGUE] == ["AskUser", "local_shell"]


def test_askuser_parameters_are_the_promised_shape():
    parameters = catalogue.ASKUSER_CLIENT_TOOL["parameters"]
    assert parameters["type"] == "object"
    assert parameters["required"] == ["question", "options"]
    properties = parameters["properties"]
    assert set(properties) == {"question", "options", "timeout", "default_option"}
    assert properties["options"]["type"] == "array"
    assert properties["options"]["minItems"] == 2
    # timeout 0 is not "no timeout value", it is "wait forever": the default
    # that keeps the model from inventing a deadline it was never given.
    assert properties["timeout"]["default"] == 0
    assert catalogue.validate(catalogue.ASKUSER_CLIENT_TOOL) == ""


def test_terminal_definitions_match_the_catalogue():
    for entry in catalogue.CATALOGUE:
        terminal = tui_tools.client_tool_definitions(include_shell=True)
        match = [item for item in terminal if item["name"] == entry["name"]]
        assert len(match) == 1, f"terminal is missing {entry['name']}"
        assert match[0]["description"] == entry["description"]
        assert match[0]["parameters"] == entry["parameters"]


def test_terminal_does_not_offer_the_shell_it_was_not_asked_for():
    names = tui_tools.client_tool_names()
    assert names == ["AskUser"]
    assert tui_tools.supports("local_shell") is False
    assert tui_tools.supports("local_shell", include_shell=True) is True


def test_web_declares_nothing_until_the_user_enables_the_tool():
    # The tool picker is the contract: AskUser was once declared on every
    # request, and a model raised a question dialog in a conversation where the
    # user had never enabled it.
    assert _web_definitions(enabled=()) == []
    assert _web_definitions(enabled=("exec_shell",)) == []
    assert [entry["name"] for entry in _web_definitions(("AskUser",))] == ["AskUser"]


def test_web_only_declares_the_client_tools_it_can_run():
    if shutil.which("node") is None:
        pytest.skip("node is not installed; the web side cannot be evaluated")
    script = (
        "import(%r).then(m => console.log(JSON.stringify("
        "m.enabledClientToolNames(["
        "{tool_id:'client_side-AskUser',name:'AskUser',tool_type:'client_side'},"
        "{tool_id:'client_side-other',name:'other',tool_type:'client_side'},"
        "{tool_id:'function-exec_shell',name:'exec_shell',tool_type:'function'}"
        "]))))" % WEB_MODULE.as_uri()
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node could not load {WEB_MODULE}: {completed.stderr[:500]}")
    # Ticked, client-side, and implemented here: only that one is declared.
    assert json.loads(completed.stdout) == ["AskUser"]


def test_web_definitions_match_the_catalogue():
    web = _web_definitions()
    assert [entry["name"] for entry in web] == ["AskUser"], (
        "the browser ships only AskUser; local_shell belongs to the terminal, "
        "which is the one front end that may run commands on the caller's box"
    )
    match = [entry for entry in web if entry["name"] == "AskUser"][0]
    canonical = catalogue.ASKUSER_CLIENT_TOOL
    assert match["description"] == canonical["description"], (
        "the model is told one description; if the browser's differs, the tool "
        "the model reaches for is not the tool the user gets"
    )
    assert match["parameters"] == canonical["parameters"]


def test_the_form_placeholders_are_the_example():
    """The tool form teaches with its placeholders, not with a panel.

    A registration JSON the operator has to read beside the fields is worse
    than useless: it eats the form and it goes stale.  The empty fields
    themselves show the real AskUser registration, so the form needs no
    separate example block - and a second copy of the schema could drift from
    the one the browser actually sends.
    """
    source = I18N.read_text(encoding="utf-8")

    # Chinese and English both carry the schema, and both must say the same
    # tool the browser registers.  The wording is shortened for an empty field,
    # so compare the parts that must not drift, not the prose.
    canonical = catalogue.ASKUSER_CLIENT_TOOL["parameters"]
    schemas = re.findall(r"^\s*clientSideParamsPlaceholder: `([^`]*)`,", source, re.M)
    assert len(schemas) >= 2, "the parameters placeholder is missing a language"
    for text in schemas:
        placeholder = json.loads(text)
        assert placeholder["type"] == canonical["type"]
        assert placeholder["required"] == canonical["required"]
        assert set(placeholder["properties"]) == set(canonical["properties"])
        for name, spec in placeholder["properties"].items():
            assert spec["type"] == canonical["properties"][name]["type"], name
        assert placeholder["properties"]["options"]["minItems"] == \
            canonical["properties"]["options"]["minItems"]
        # "0 means wait forever" has to be visible where the operator reads it.
        assert placeholder["properties"]["timeout"]["default"] == 0

    # Name and description placeholders point at the same shipped tool.
    names = re.findall(r"^\s*clientSideNamePlaceholder: '([^']*)',", source, re.M)
    assert len(names) >= 2 and all("AskUser" in value for value in names)
    assert len(re.findall(r"^\s*clientSideDescPlaceholder: ", source, re.M)) >= 2

    # The form wires those placeholders into the three fields...
    form = TOOL_FORM.read_text(encoding="utf-8")
    for key in ("clientSideNamePlaceholder", "clientSideDescPlaceholder",
                "clientSideParamsPlaceholder"):
        assert f"t('{key}')" in form, f"the form no longer uses {key}"
    # ...and keeps no leftover example panel.
    for leftover in ("clientSideJsonExample", "clientSideTypeExample",
                     "applyExample", "example-json"):
        assert leftover not in form, f"the form grew an example panel again ({leftover})"
        assert leftover not in source, f"i18n kept the dead key {leftover}"


def test_clients_report_an_unanswered_question_in_the_same_words():
    """A non-answer must read the same whichever client produced it."""
    web_source = WEB_MODULE.read_text(encoding="utf-8")
    for phrase in (
        tui_tools.CANCELLED_ANSWER,
        "(auto-selected: the user did not answer within",
        "(no answer: the user did not respond within",
        "no default_option was given",
    ):
        assert phrase in web_source, f"the browser does not report {phrase!r}"
        assert phrase in (REPO / "tui" / "client_tools.py").read_text(encoding="utf-8")


def test_askuser_is_registered_as_a_builtin_tool():
    """AskUser ships the way ``write_file`` does: registered at start-up.

    A user must not have to register anything by hand to get the question tool,
    and the service must still be unable to run it.
    """
    from runtime.builtin_tools import register_builtin_tools
    from runtime.registry import ToolRegistry

    registry = ToolRegistry()
    ids = register_builtin_tools(registry)
    assert "AskUser" in ids

    config = registry.get("AskUser")
    assert config is not None
    assert config.tool_type == "client_side"
    assert config.builtin is True
    assert config.is_client_side()
    # The catalogue stays the single source of truth for what the model is told.
    assert config.description == catalogue.ASKUSER_CLIENT_TOOL["description"]
    assert config.parameters == catalogue.ASKUSER_CLIENT_TOOL["parameters"]
    # No callable on purpose: a client-side tool call is handed back, never run
    # here, so registering one would be the service breaking its own contract.
    assert registry.get_callable("AskUser") is None


def test_a_builtin_tool_never_lands_in_tools_json(tmp_path):
    """Built-ins are re-registered at every start-up; persisting them only lets
    a stale copy of the row come back and shadow the shipped one."""
    from runtime.registry import ToolRegistry

    registry = ToolRegistry()
    for config in catalogue.builtin_configs():
        registry.register(config)

    path = tmp_path / "tools.json"
    registry.save(str(path))
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_local_shell_is_not_a_builtin_tool():
    """Handing shell execution to whoever drives the session is opt-in; the
    service must not offer it on its own."""
    names = [config.name for config in catalogue.builtin_configs()]
    assert names == ["AskUser"]
    assert "local_shell" in [entry["name"] for entry in catalogue.CATALOGUE]


def test_terminal_answers_and_web_answers_agree_on_the_result_text():
    """Same question, same choice, same tool result - in either front end."""
    args = {
        "question": "部署方式？",
        "options": ["蓝绿发布", "金丝雀发布", "直接替换"],
        "timeout": 0,
    }
    terminal = tui_tools.run_ask_user(args, lambda *a, **k: "金丝雀")
    assert terminal == "金丝雀发布"

    web_source = WEB_MODULE.read_text(encoding="utf-8")
    script = (
        "import(%r).then(m => {"
        "const request = m.askUserRequest(%s);"
        "console.log(JSON.stringify(["
        "m.askUserResultText(request, '金丝雀'),"
        "m.askUserResultText(request, '2'),"
        "m.askUserResultText(request, null),"
        "m.askUserResultText({ ...request, timeout: 8, defaultOption: '直接替换' }, null, { timedOut: true })"
        "]));})"
        % (WEB_MODULE.as_uri(), json.dumps(args, ensure_ascii=False))
    )
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr[:500]
    web = json.loads(completed.stdout)
    assert web[0] == "金丝雀发布"          # fuzzy match, like the terminal's
    assert web[1] == "金丝雀发布"          # a number picks the option
    assert web[2] == tui_tools.CANCELLED_ANSWER
    assert web[3] == "直接替换 (auto-selected: the user did not answer within 8s)", (
        "an auto-selected default must be labelled as such, in both front ends"
    )

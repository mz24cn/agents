"""runtime/category_tree_builder.py 的单元测试。

覆盖：
  * 递归拆分规则：末级分类 <= max_leaf_size；有子分类时 >= 2 个子分类；
  * 顶层无包装层：全部会话拆开后顶层直接是若干并列分类；
  * 会话数 <= max_leaf 时顶层即唯一末级分类；
  * 大模型自底向上命名（末级用会话抽样、父级用子分类名）与失败回退；
  * 全量会话挂载；多挂载（一会话多分类）合法；
  * 输出 schema 与 tree.json 一致；
  * 会话数不足保护；命令行入口（python -m）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from runtime import category_tree_builder as builder

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_session(chats_dir: Path, sid: str, user_text: str, assistant_text: str = "") -> None:
    d = chats_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    conv = {
        "meta": {"session_id": sid},
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ],
    }
    (d / "conversation.json").write_text(json.dumps(conv, ensure_ascii=False), "utf-8")


def _make_topics(chats_dir: Path, topic_words: list[str], per_topic: int) -> list[str]:
    """为每个主题生成 per_topic 个会话，主题词互不重叠。"""
    sids = []
    for ti, word in enumerate(topic_words):
        filler = (word + " ") * 20
        for i in range(per_topic):
            sid = f"260101_{ti:03d}{i:03d}"
            user = f"please help with {word} task {i} {filler}"
            assistant = f"here is the {word} answer {i} {filler}"
            _make_session(chats_dir, sid, user, assistant)
            sids.append(sid)
    return sids


def _check_invariants(tree: list[dict], sids: set, max_leaf: int = 10) -> int:
    attached: set = set()
    terminals = 0

    def walk(node: dict) -> None:
        nonlocal terminals
        assert node.get("type") == "category"
        kids = node["children"]
        dict_kids = [k for k in kids if isinstance(k, dict)]
        str_kids = [k for k in kids if isinstance(k, str)]
        if dict_kids:
            assert len(dict_kids) >= 2, f"category {node.get('id')} has {len(dict_kids)} sub-category"
            assert not str_kids, "mixed dict/str children"
            for k in dict_kids:
                walk(k)
        else:
            terminals += 1
            assert node.get("category"), "terminal node missing category path"
            assert 1 <= len(str_kids) <= max_leaf, f"terminal size {len(str_kids)}"
            attached.update(str_kids)

    for top in tree:
        walk(top)
    assert attached == set(sids), (
        f"attached mismatch: missing={sorted(set(sids) - attached)[:5]} "
        f"extra={sorted(attached - set(sids))[:5]}"
    )
    return terminals


def _rows_from(chats_dir: Path):
    return builder.load_sessions(chats_dir)


def test_build_rules_sklearn(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(5)], per_topic=8)

    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    assert doc["version"] == 1
    assert doc["experimental"] is True
    for key in ("generated_at", "source", "schema", "stats", "tree"):
        assert key in doc
    assert doc["stats"]["conversation_count"] == len(sids)
    assert doc["stats"]["max_leaf_size"] == 10
    terminals = _check_invariants(doc["tree"], set(sids))
    assert doc["stats"]["terminal_category_count"] == terminals
    # 顶层无包装层：40 个会话必然拆出多个并列顶级分类
    assert len(doc["tree"]) >= 2
    assert not any(
        isinstance(top, dict) and len(top["children"]) == 1
        and any(isinstance(c, dict) for c in top["children"])
        for top in doc["tree"]
    )


def test_build_single_top_terminal_when_small(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, ["onlytopic"], per_topic=8)  # 8 <= 10

    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    assert len(doc["tree"]) == 1
    top = doc["tree"][0]
    assert all(not isinstance(c, dict) for c in top["children"])
    assert len(top["children"]) == 8
    assert top["category"] == "1"


def test_build_pure_python_fallback_rules(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"alpha{i}qqqq" for i in range(5)], per_topic=5)

    monkeypatch.setattr(builder, "_SKLEARN_OK", False)
    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    terminals = _check_invariants(doc["tree"], set(sids))
    assert doc["stats"]["terminal_category_count"] == terminals


def test_build_chats_dir_respects_min_sessions(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    _make_topics(chats, ["onlytopic"], per_topic=5)
    assert builder.build_chats_dir(chats) is None


def test_llm_names_bottom_up(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    # 48 个会话保证拆出"父分类 + 末级分类"的层级，覆盖父级命名路径
    _make_topics(chats, [f"topic{i}xyzabc" for i in range(4)], per_topic=12)
    rows = _rows_from(chats)

    calls = {"terminal": 0, "internal": 0}

    def fake_llm(prompt: str) -> str:
        if "子分类的名称" in prompt:
            calls["internal"] += 1
            return f"LLM父级{calls['internal']}"
        calls["terminal"] += 1
        return f"LLM末级{calls['terminal']}"

    doc = builder.build_document(rows, max_leaf=10, seed=137, llm=fake_llm)
    assert "LLM" in doc["source"]

    names = []

    def walk(node):
        names.append(node["name"])
        for c in node["children"]:
            if isinstance(c, dict):
                walk(c)

    for top in doc["tree"]:
        walk(top)
    assert all(name.startswith("LLM") for name in names)
    assert calls["terminal"] >= 2
    assert calls["internal"] >= 1
    # 父级名称来自子级命名（子级先命名）
    _check_invariants(doc["tree"], {r["sid"] for r in rows})


def test_llm_failure_falls_back_to_keywords(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    _make_topics(chats, [f"topic{i}xyzabc" for i in range(4)], per_topic=6)
    rows = _rows_from(chats)

    def broken_llm(prompt: str) -> str:
        raise RuntimeError("model offline")

    doc = builder.build_document(rows, max_leaf=10, seed=137, llm=broken_llm)
    _check_invariants(doc["tree"], {r["sid"] for r in rows})
    names = []

    def walk(node):
        names.append(node["name"])
        for c in node["children"]:
            if isinstance(c, dict):
                walk(c)

    for top in doc["tree"]:
        walk(top)
    assert not any(name.startswith("LLM") for name in names)


def test_llm_response_cleaning():
    assert builder._llm_name(lambda p: "  Agent 开发  ", "p") == "Agent 开发"
    assert builder._llm_name(lambda p: "名称：模型部署", "p") == "模型部署"
    assert builder._llm_name(lambda p: '```json\n"手机自动化"\n```', "p") == "手机自动化"
    assert builder._llm_name(lambda p: "叫工具执行吧，谢谢", "p") == "叫工具执行吧"
    assert builder._llm_name(lambda p: "", "p") == ""
    assert builder._llm_name(lambda p: (_ for _ in ()).throw(RuntimeError()), "p") == ""


def test_validate_rejects_single_wrapper():
    tree = [
        {
            "id": 1, "name": "Wrapper", "type": "category",
            "children": [
                {"id": 1, "name": "A", "type": "category", "children": ["s1"], "category": "1/1", "session_count": 1},
                {"id": 2, "name": "B", "type": "category", "children": ["s2"], "category": "1/2", "session_count": 1},
            ],
        }
    ]
    errors = builder.validate(tree, {"s1", "s2"}, max_leaf=10)
    assert any("包装" in e for e in errors)


def test_validate_allows_multi_membership():
    """同一会话出现在多个末级分类是合法的（自动+人工分类并存）。"""
    tree = [
        {
            "id": 1, "name": "Auto", "type": "category",
            "children": [
                {"id": 1, "name": "A1", "type": "category", "children": ["s1", "s2"], "category": "1/1", "session_count": 2},
                {"id": 2, "name": "A2", "type": "category", "children": ["s3"], "category": "1/2", "session_count": 1},
            ],
        },
        {
            "id": 2, "name": "Manual", "type": "category",
            "children": [
                {"id": 1, "name": "M1", "type": "category", "children": ["s1"], "category": "2/1", "session_count": 1},
                {"id": 2, "name": "M2", "type": "category", "children": ["s4"], "category": "2/2", "session_count": 1},
            ],
        },
    ]
    assert builder.validate(tree, {"s1", "s2", "s3", "s4"}, max_leaf=10) == []


def test_cli_module_entry_dry_run(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    _make_topics(chats, [f"topic{i}xyzabc" for i in range(3)], per_topic=4)
    proc = subprocess.run(
        [sys.executable, "-m", "runtime.category_tree_builder",
         "--chats-dir", str(chats), "--dry-run"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "terminal_category_count" in proc.stdout


def test_start_build_writes_tree_in_background(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(3)], per_topic=5)

    builder.reset_state_for_tests()
    assert builder.start_build(str(chats), llm=None) is True
    assert builder.build_state()["building"] is True

    import time
    deadline = time.time() + 120
    while time.time() < deadline and not (chats / "tree.json").exists():
        time.sleep(0.2)
    assert (chats / "tree.json").exists()
    doc = json.loads((chats / "tree.json").read_text("utf-8"))
    _check_invariants(doc["tree"], set(sids))

    deadline = time.time() + 30
    while time.time() < deadline and builder.build_state()["building"]:
        time.sleep(0.2)
    assert builder.build_state()["building"] is False
    assert builder.build_state()["last_error"] is None


def test_start_build_skips_when_tree_exists(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    (chats / "tree.json").write_text(json.dumps({"version": 1, "tree": []}), "utf-8")
    builder.reset_state_for_tests()
    assert builder.start_build(str(chats)) is False
    assert builder.build_state()["building"] is False


def test_workspace_file_context_basic():
    rows = [
        {"workspace": "/root/ws/agents-runtime/frontend",
         "files": ["/root/ws/agents-runtime/frontend/src/sidebar/Sidebar.svelte",
                   "/root/ws/agents-runtime/frontend/src/chat/ChatPage.svelte"]},
        {"workspace": "/root/ws/agents-runtime/frontend",
         "files": ["/root/ws/agents-runtime/frontend/src/sidebar/Sidebar.svelte"]},
        {"workspace": "/root/ws/agents-runtime/backend",
         "files": ["/root/ws/agents-runtime/backend/app.py"]},
        {"workspace": "", "files": ["lonely/only-once.txt"]},
    ]
    lines = builder._workspace_file_context(rows)
    assert len(lines) == 2
    assert "常见工作区" in lines[0]
    assert "agents-runtime/frontend (2/4 个会话)" in lines[0]
    assert "agents-runtime/backend" not in lines[0]  # 未达共享阈值
    assert "常见改动文件" in lines[1]
    assert "src/sidebar/Sidebar.svelte (2/4)" in lines[1]  # 已剥离 workspace 前缀
    assert "lonely/only-once.txt" not in lines[1]  # 单会话文件视为噪声
    assert builder._workspace_file_context([]) == []
    assert builder._short_workspace("/a/b/agents-runtime/frontend") == "agents-runtime/frontend"
    assert builder._short_workspace("") == ""


def _add_workspace_and_journal(chats: Path, sids: list[str], ws: str, rel: str) -> None:
    """给会话补 meta.workspace 与 file_journals manifest（共享同一工作区与文件）。"""
    for sid in sids:
        d = chats / sid
        path = d / "conversation.json"
        conv = json.loads(path.read_text("utf-8"))
        conv["meta"]["workspace"] = ws
        path.write_text(json.dumps(conv, ensure_ascii=False), "utf-8")
        fj = d / "file_journals" / "0"
        fj.mkdir(parents=True, exist_ok=True)
        (fj / "manifest.json").write_text(json.dumps({
            "workspace": ws,
            "files": {rel: {"path": rel}},
        }), "utf-8")


def test_naming_prompt_includes_workspace_and_files(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(4)], per_topic=12)
    _add_workspace_and_journal(chats, sids, "/root/ws/demo-proj/frontend", "src/App.svelte")
    rows = _rows_from(chats)
    assert all(r["workspace"] == "/root/ws/demo-proj/frontend" for r in rows)
    assert all("/root/ws/demo-proj/frontend/src/App.svelte" in r["files"] for r in rows)

    prompts: list[str] = []

    def capturing_llm(prompt: str) -> str:
        prompts.append(prompt)
        return "命名测试"

    doc = builder.build_document(rows, max_leaf=10, seed=137, llm=capturing_llm)
    _check_invariants(doc["tree"], {r["sid"] for r in rows})
    assert prompts
    for p in prompts:
        assert "常见工作区" in p
        assert "demo-proj/frontend" in p
        assert "src/App.svelte" in p

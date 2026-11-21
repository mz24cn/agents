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
from collections import Counter
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


def _cat_roots(doc: dict) -> list[dict]:
    """"主题"根的直接子树（v2 森林中聚类分类所在的位置）。"""
    root = doc["tree"][0]
    assert root["name"] == builder.CATEGORY_ROOT_NAME
    return root["children"]


def _forest_roots(doc: dict) -> dict:
    return {r["name"]: r for r in doc["tree"]}


def test_build_rules_sklearn(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(5)], per_topic=8)

    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    assert doc["version"] == builder.TREE_VERSION
    assert doc["experimental"] is True
    for key in ("generated_at", "source", "schema", "stats", "tree"):
        assert key in doc
    assert doc["stats"]["conversation_count"] == len(sids)
    assert doc["stats"]["max_leaf_size"] == 10
    terminals = _check_invariants(_cat_roots(doc), set(sids))
    assert doc["stats"]["terminal_category_count"] == terminals
    # 分类树顶层无包装层：40 个会话必然拆出多个并列顶级分类
    cats = _cat_roots(doc)
    assert len(cats) >= 2
    assert not any(
        isinstance(top, dict) and len(top["children"]) == 1
        and any(isinstance(c, dict) for c in top["children"])
        for top in cats
    )


def test_forest_roots_workspace_and_terminal(tmp_path):
    """v8 森林：五根齐全（最近会话根已取消）；工作区按事实分组且含未指定桶；历史根生效。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(3)], per_topic=4)
    _add_workspace_and_journal(chats, sids[:5], "/root/ws/alpha/proj", "src/a.py")
    _add_workspace_and_journal(chats, sids[5:8], "/root/ws/beta/proj", "src/b.py")

    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    roots = _forest_roots(doc)
    assert list(roots) == ["主题", "工作区", "标签", "本地终端", "会话历史"]
    assert roots["主题"]["id"] == 1 and roots["主题"]["type"] == "category"
    assert roots["工作区"]["id"] == 2 and roots["工作区"]["type"] == "workspace"
    # v8 标签根：无标签语料下是空的结构性根，但入口常在
    assert roots["标签"]["id"] == 6 and roots["标签"]["type"] == "labels"
    assert roots["标签"]["children"] == []
    assert roots["本地终端"]["id"] == 3 and roots["本地终端"]["type"] == "terminal"
    assert roots["本地终端"]["children"] == []
    assert roots["本地终端"]["session_count"] == 0
    history = roots["会话历史"]
    assert history["id"] == 5 and history["type"] == "history"
    assert sum(m["session_count"] for m in history["children"]) <= len(sids)
    # v7：不再有"最近会话"根，也不再有 recent_* stats
    assert "recent_session_count" not in doc["stats"]
    assert "recent_limit" not in doc["stats"]

    ws_children = roots["工作区"]["children"]
    names = [c["name"] for c in ws_children]
    assert "proj" in names and "未指定工作区" in names  # basename 同名 → 扩展一段去重
    full_paths = {c.get("workspace") for c in ws_children}
    assert {"/root/ws/alpha/proj", "/root/ws/beta/proj", ""} <= full_paths
    mounted = set()

    def _collect_ws(node):
        kids = node["children"]
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    _collect_ws(k)
        else:
            mounted.update(kids)

    for c in ws_children:
        assert c["type"] == "workspace"
        _collect_ws(c)
    assert mounted == set(sids)  # 全部会话在工作区根下都有归属
    # 叶子 category 路径已带上"主题"根前缀
    leaf = _cat_roots(doc)[0]
    while isinstance(leaf["children"][0], dict):
        leaf = leaf["children"][0]
    assert leaf["category"].startswith("1/")


def test_workspace_nodes_host_filtered_category_copies():
    """工作区目录节点挂主题分类的筛选副本：空分类剪除、层级名称保持、未挂载会话进"未分类"。"""
    category_roots = [
        {"id": 7, "name": "父分类", "type": "category", "session_count": 4,
         "category": "1/7",
         "children": [
             {"id": 1, "name": "子分类A", "type": "category", "session_count": 2,
              "category": "1/7/1", "children": ["s1", "s2"], "stats": {}},
             {"id": 2, "name": "子分类B", "type": "category", "session_count": 2,
              "category": "1/7/2", "children": ["s3", "s4"], "stats": {}},
         ], "stats": {}},
    ]
    rows = [
        {"sid": "s1", "workspace": "/w/one"}, {"sid": "s2", "workspace": "/w/one"},
        {"sid": "s3", "workspace": "/w/two"}, {"sid": "s4", "workspace": "/w/two"},
        {"sid": "s5", "workspace": "/w/one"},  # 不在主题树中 → 兜底"未分类"
    ]
    root = builder.build_workspace_root(rows, category_roots=category_roots)
    one = next(c for c in root["children"] if c["workspace"] == "/w/one")
    two = next(c for c in root["children"] if c["workspace"] == "/w/two")
    # one 只有 s1/s2 命中子分类A：B 被剪除，父分类保留；s5 进"未分类"
    assert [c["name"] for c in one["children"]] == ["父分类", builder.UNKNOWN_CATEGORY_NAME]
    parent = one["children"][0]
    assert [c["name"] for c in parent["children"]] == ["子分类A"]
    assert parent["children"][0]["children"] == ["s1", "s2"]
    assert parent["session_count"] == 2 and one["session_count"] == 3
    assert one["children"][1]["children"] == ["s5"]
    # two 只剩子分类B，且末级 children 全是会话 id（可按路径查询）
    assert [c["name"] for c in two["children"]] == ["父分类"]
    leaf = two["children"][0]["children"][0]
    assert leaf["children"] == ["s3", "s4"]
    # 副本独立：不带原主题树 category 路径，原主题树不被改动
    assert "category" not in parent and "category" not in leaf
    assert category_roots[0]["children"][1]["children"] == ["s3", "s4"]


def test_windows_workspace_paths_keep_original_style():
    """Windows 工作区路径：保留反斜杠原状，命名与去重和 Linux 同一策略
    （按 [/\\\\]+ 切分取尾段、冲突向左展开，分隔符跟随原风格）。"""
    assert builder._normalize_workspace("C:\\Users\\dev\\proj\\") == "C:\\Users\\dev\\proj"
    assert builder._normalize_workspace("\\\\server\\share\\data") == "\\\\server\\share\\data"
    assert builder._normalize_workspace("/cc/agents/") == "/cc/agents"
    assert builder._normalize_workspace("C:\\\\Users\\\\dev\\\\proj") == "C:\\Users\\dev\\proj"
    rows = [
        {"sid": "s1", "workspace": builder._normalize_workspace("C:\\Users\\dev\\proj")},
        {"sid": "s2", "workspace": builder._normalize_workspace("D:\\work\\proj")},
        {"sid": "s3", "workspace": builder._normalize_workspace("C:\\Users\\dev\\proj")},
    ]
    root = builder.build_workspace_root(rows)
    by_name = {c["name"]: c for c in root["children"]}
    # 两个 proj 冲突：会话多的保留裸名，另一个向左展开一段去重（用原分隔符）
    assert by_name["proj"]["children"] == ["s1", "s3"]
    assert by_name["work\\proj"]["children"] == ["s2"]
    assert by_name["work\\proj"]["workspace"] == "D:\\work\\proj"


def _set_session_meta(chats_dir: Path, sids: list[str], **meta) -> None:
    for sid in sids:
        path = chats_dir / sid / "conversation.json"
        data = json.loads(path.read_text("utf-8"))
        data.setdefault("meta", {}).update(meta)
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def _write_index(chats_dir: Path, entries: dict) -> None:
    (chats_dir / "index.json").write_text(json.dumps(entries, ensure_ascii=False), "utf-8")


def test_environment_and_month_normalization():
    assert builder.normalize_environment("") == builder.LOCAL_ENVIRONMENT_KEY
    assert builder.normalize_environment("local") == builder.LOCAL_ENVIRONMENT_KEY
    assert builder.normalize_environment("http://10.0.0.9:7988") == "10.0.0.9:7988"
    assert builder.normalize_environment("https://10.0.0.9:7988") == "10.0.0.9:7988"
    assert builder.normalize_environment("tunnel:3f85") == "tunnel:3f85"
    assert builder.environment_display_name(builder.LOCAL_ENVIRONMENT_KEY) == "本地"
    assert builder.environment_display_name("10.0.0.9:7988") == "10.0.0.9:7988"
    assert builder.month_key("2026-08-28 10:39:29") == "202608"
    assert builder.month_key("2026-11-14T11:17:45") == "202611"
    assert builder.month_key(None) == ""


def test_workspace_root_collapses_single_environment(tmp_path):
    """仅存在"本地"一个执行环境时，环境层在数据中直接省略。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, ["onlylocalxx"], per_topic=4)
    _set_session_meta(chats, sids, workspace="/ws/proja")
    root = builder.build_workspace_root(_rows_from(chats))
    assert root["stats"]["environment_count"] == 1
    assert all(c["type"] == "workspace" for c in root["children"])
    assert {c["workspace"] for c in root["children"]} == {"/ws/proja"}


def test_workspace_root_environment_layering(tmp_path):
    """remote_env 会话与本地会话分属不同环境节点；同一 URL 不同协议归并。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, ["localworkxx"], per_topic=6)
    remote = sids[:4]
    _set_session_meta(chats, remote, remote_env="http://10.0.0.9:7988", workspace="/remote/proj")
    _set_session_meta(chats, sids[4:5], remote_env="https://10.0.0.9:7988")  # 与上面归并
    _set_session_meta(chats, sids[5:], workspace="/ws/proja")

    root = builder.build_workspace_root(_rows_from(chats))
    assert root["stats"]["environment_count"] == 2
    envs = root["children"]
    assert [e["type"] for e in envs] == [builder.TYPE_ENVIRONMENT] * 2
    assert envs[0]["name"] == "本地" and envs[0]["environment"] == "local"
    assert envs[1]["name"] == "10.0.0.9:7988"
    assert envs[1]["session_count"] == 5  # 4 个 http + 1 个 https（协议不同仍归并）
    assert envs[0]["session_count"] == 1
    remote_ws = envs[1]["children"]
    assert all(w["type"] == "workspace" for w in remote_ws)
    mounted = {sid for e in envs for w in e["children"] for sid in w["children"]}
    assert mounted == set(sids)


def test_recent_root_limit_and_order_removed_in_v7():
    """v7 起最近会话根被取消：不再有 build_recent_root / RECENT_LIMIT_ENV。"""
    assert not hasattr(builder, "build_recent_root")
    assert not hasattr(builder, "recent_limit")
    assert not hasattr(builder, "RECENT_LIMIT_ENV")
    assert not hasattr(builder, "RECENT_ROOT_NAME")


def test_history_root_month_nodes(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, ["julyhistoryxx", "augusthistoryyy"], per_topic=2)
    july, august = sids[:2], sids[2:]
    _set_session_meta(chats, july, created_at="2026-07-05T08:00:00")
    _set_session_meta(chats, august, created_at="2026-08-20T08:00:00")
    _write_index(chats, {
        sid: {"session_id": sid, "title": sid,
              "last_inference_at": f"2026-08-{i + 1:02d}T10:00:00"}
        for i, sid in enumerate(july + august)
    })
    rows = _rows_from(chats)

    root = builder.build_history_root(rows)
    months = root["children"]
    assert [m["type"] for m in months] == [builder.TYPE_MONTH] * 2
    assert [m["name"] for m in months] == ["202608", "202607"]  # 时间倒序
    assert months[0]["session_count"] == 2
    assert months[0]["children"] == list(reversed(august))  # 月内按最近活动倒序
    assert root["stats"]["month_count"] == 2
    assert root["session_count"] == len(july) + len(august)


def test_build_single_top_terminal_when_small(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, ["onlytopic"], per_topic=8)  # 8 <= 10

    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    cats = _cat_roots(doc)
    assert len(cats) == 1
    top = cats[0]
    assert all(not isinstance(c, dict) for c in top["children"])
    assert len(top["children"]) == 8
    assert top["category"] == "1/1"


def test_build_pure_python_fallback_rules(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"alpha{i}qqqq" for i in range(5)], per_topic=5)

    monkeypatch.setattr(builder, "_SKLEARN_OK", False)
    doc = builder.build_document(_rows_from(chats), max_leaf=10, seed=137)
    terminals = _check_invariants(_cat_roots(doc), set(sids))
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

    for top in _cat_roots(doc):
        walk(top)
    assert all(name.startswith("LLM") for name in names)
    assert calls["terminal"] >= 2
    assert calls["internal"] >= 1
    # 父级名称来自子级命名（子级先命名）
    _check_invariants(_cat_roots(doc), {r["sid"] for r in rows})


def test_llm_failure_falls_back_to_keywords(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    _make_topics(chats, [f"topic{i}xyzabc" for i in range(4)], per_topic=6)
    rows = _rows_from(chats)

    def broken_llm(prompt: str) -> str:
        raise RuntimeError("model offline")

    doc = builder.build_document(rows, max_leaf=10, seed=137, llm=broken_llm)
    _check_invariants(_cat_roots(doc), {r["sid"] for r in rows})
    names = []

    def walk(node):
        names.append(node["name"])
        for c in node["children"]:
            if isinstance(c, dict):
                walk(c)

    for top in doc["tree"]:
        walk(top)
    assert not any(name.startswith("LLM") for name in names)
    assert doc["tree"][0]["name"] == builder.CATEGORY_ROOT_NAME


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


def test_validate_rejects_multi_membership():
    """v5 单分类语义：同一会话出现在多个末级分类属于数据错误。"""
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
    errors = builder.validate(tree, {"s1", "s2", "s3", "s4"}, max_leaf=10)
    assert any("s1" in e and "单分类" in e for e in errors)


def test_terminal_root_expands_registered_remote_envs():
    """"终端"根按登记配置展开：本地在前，远程环境带 env_id 路由标识。"""
    rows = [
        {"sid": "s1", "environment": "172.28.70.13:7988",
         "environment_id": "http://172.28.70.13:7988"},
        {"sid": "s2", "environment": "local", "environment_id": ""},
    ]
    remote_envs = [
        {"id": "http://172.28.70.13:7988"},
        {"id": "http://10.0.0.9:7988"},          # 已登记但没有会话使用
    ]
    root = builder.build_terminal_root(rows, remote_envs)
    assert root["name"] == builder.TERMINAL_MULTI_ROOT_NAME
    names = [c["name"] for c in root["children"]]
    env_ids = [c["env_id"] for c in root["children"]]
    assert names == ["本地", "172.28.70.13:7988", "10.0.0.9:7988"]
    assert env_ids == ["", "http://172.28.70.13:7988", "http://10.0.0.9:7988"]
    assert all(c["type"] == builder.TYPE_TERMINAL for c in root["children"])
    assert root["stats"]["environment_count"] == 3


def test_terminal_root_single_local_root():
    """没有登记远程环境时保持单根"本地终端"（与旧数据一致）。"""
    root = builder.build_terminal_root([{"sid": "s1", "environment": "local"}], [])
    assert root["name"] == builder.TERMINAL_ROOT_NAME
    assert root["children"] == []
    # 会话里出现过但已从登记中注销的远程环境不再成为终端节点：终端节点
    # 必须可连接，登记配置（remote_envs.json）是唯一权威来源。
    root = builder.build_terminal_root(
        [{"sid": "s1", "environment": "10.1.1.1:7988",
          "environment_id": "http://10.1.1.1:7988"}], [])
    assert root["name"] == builder.TERMINAL_ROOT_NAME
    assert root["children"] == []


def test_terminal_root_registered_env_ghost_removal():
    """登记的远程环境即使无会话也在位；未登记的即使有会话也不出现。"""
    rows = [
        {"sid": "s1", "environment": "ghost.io:7988", "environment_id": "https://ghost.io:7988"},
        {"sid": "s2", "environment": "local"},
    ]
    root = builder.build_terminal_root(
        rows, [{"id": "http://real.io:7988"}])
    assert root["name"] == builder.TERMINAL_MULTI_ROOT_NAME
    env_ids = [c["env_id"] for c in root["children"]]
    assert env_ids == ["", "http://real.io:7988"]


def test_terminal_layer_add_remove_and_chain():
    doc = {"tree": [builder.build_terminal_root(
        [{"sid": "s1", "environment": "local"}],
        [{"id": "http://win:7988"}])]}
    node = builder.add_terminal_layer(doc, "http://win:7988", "wsl")
    assert node["name"] == "WSL"
    assert node["type"] == builder.TYPE_TERMINAL_LAYER
    assert node["uid"]
    child = builder.add_terminal_layer(
        doc, "http://win:7988", "docker exec -it cc bash", parent_uid=node["uid"])
    assert child["name"] == "docker:cc"
    chain = builder.terminal_layer_chain(doc, "http://win:7988", child["uid"])
    # 链首为环境节点（command 为空），其后依次是各终端层的进入指令。
    assert [c.get("command") for c in chain] == [None, "wsl", "docker exec -it cc bash"]
    # 编辑进入指令：name 留空沿用原名；显式 name 才改名。
    updated = builder.update_terminal_layer(
        doc, "http://win:7988", child["uid"], "docker exec -it cc2 bash")
    assert updated["command"] == "docker exec -it cc2 bash"
    assert updated["name"] == "docker:cc"
    assert builder.update_terminal_layer(
        doc, "http://win:7988", child["uid"], "podman exec -it cc sh",
        name="cc容器")["name"] == "cc容器"
    assert [c.get("command") for c in builder.terminal_layer_chain(
        doc, "http://win:7988", child["uid"])] == [None, "wsl", "podman exec -it cc sh"]
    assert builder.update_terminal_layer(
        doc, "http://win:7988", "missing", "x") is None
    assert builder.update_terminal_layer(
        doc, "http://nope:1", child["uid"], "x") is None
    try:
        builder.update_terminal_layer(doc, "http://win:7988", child["uid"], "   ")
        raise AssertionError("blank command must raise")
    except ValueError:
        pass
    assert builder.remove_terminal_layer(doc, "http://win:7988", node["uid"])
    assert builder.terminal_layer_chain(doc, "http://win:7988", child["uid"]) == []
    assert not builder.remove_terminal_layer(doc, "http://win:7988", "missing")


def test_terminal_layer_on_local_only_root():
    """单环境形态（根即"本地"环境节点）下也能增删改嵌套终端层。

    没有登记任何远程环境时树里只有一个根"本地终端"，没有环境子节点；前端正是
    按 env_id='' 请求这个根。若环境节点定位只认环境子节点，最常见的"在本地终端
    下新建子终端"会 400 失败。
    """
    root = builder.build_terminal_root([{"sid": "s1", "environment": "local"}], [])
    # 根自身就是可连接的环境节点：前端据此显示右键菜单（新建/编辑/删除子终端）。
    assert root["env_id"] == "" and root["environment"] == builder.LOCAL_ENVIRONMENT_KEY
    doc = {"tree": [root]}

    node = builder.add_terminal_layer(doc, "", "ssh user@192.168.1.5")
    assert node["name"] == "ssh user@192.168.1.5"
    assert doc["tree"][0]["children"] == [node]      # 层直接挂在根下
    sub = builder.add_terminal_layer(doc, "", "docker exec -it cc bash",
                                     parent_uid=node["uid"])
    assert [c.get("command") for c in builder.terminal_layer_chain(doc, "", sub["uid"])] == [
        None, "ssh user@192.168.1.5", "docker exec -it cc bash"]

    assert builder.update_terminal_layer(
        doc, "", sub["uid"], "podman exec -it cc sh")["command"] == "podman exec -it cc sh"
    assert builder.remove_terminal_layer(doc, "", node["uid"])
    assert doc["tree"][0]["children"] == []
    # 未知环境仍然拒绝（不能把层挂到不存在的环上去）。
    try:
        builder.add_terminal_layer(doc, "http://nope:1", "wsl")
        raise AssertionError("unknown env_id must raise")
    except ValueError:
        pass


def test_terminal_layers_survive_local_only_root_promotion():
    """登记远程环境后，挂在单环境根下的层迁到"本地"子节点，而不是被丢弃。"""
    old = builder.build_terminal_root([{"sid": "s1", "environment": "local"}], [])
    doc = {"tree": [old]}
    layer = builder.add_terminal_layer(doc, "", "wsl", name="WSL(D)")
    builder.add_terminal_layer(doc, "", "su postgres", parent_uid=layer["uid"])

    new = builder.build_terminal_root(
        [{"sid": "s1", "environment": "local"}],
        [{"id": "http://win:7988"}], existing_root=old)
    assert new["name"] == builder.TERMINAL_MULTI_ROOT_NAME
    local = next(c for c in new["children"] if c["env_id"] == "")
    assert [(c["name"], c["command"]) for c in local["children"]] == [("WSL(D)", "wsl")]
    assert local["children"][0]["children"][0]["command"] == "su postgres"
    # 反向（多环境 -> 只剩本地）同样不丢：层回到根下。
    back = builder.build_terminal_root(
        [{"sid": "s1", "environment": "local"}], [], existing_root=new)
    assert [(c["name"], c["command"]) for c in back["children"]] == [("WSL(D)", "wsl")]


def test_terminal_layers_survive_terminal_root_rebuild():
    """事实刷新/重建按 env_id 移植用户自定义终端层（含子层与 command）。"""
    old = builder.build_terminal_root(
        [{"sid": "s1", "environment": "local"}], [{"id": "http://win:7988"}])
    doc = {"tree": [old]}
    layer = builder.add_terminal_layer(doc, "http://win:7988", "wsl", name="WSL(D)")
    sub = builder.add_terminal_layer(
        doc, "http://win:7988", "su postgres", parent_uid=layer["uid"])
    new = builder.build_terminal_root(
        [{"sid": "s1", "environment": "local"}],
        [{"id": "http://win:7988"}, {"id": "http://new:7988"}],
        existing_root=old)
    win = next(c for c in new["children"] if c["env_id"] == "http://win:7988")
    assert [(c["name"], c["command"]) for c in win["children"]] == [("WSL(D)", "wsl")]
    assert win["children"][0]["children"][0]["command"] == "su postgres"


def test_terminal_layer_path_ids_stable_under_session_removal(tmp_path):
    """remove_session_from_tree 的按 id 路径（Sidebar 双击链依赖）不受影响：
    terminal/terminal_layer 结构节点保留，id 稳定。"""
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from runtime.session_manager import SessionManager

    doc = {"version": 5, "stats": {}, "tree": [builder.build_terminal_root(
        [{"sid": "s1", "environment": "local"}], [{"id": "http://win:7988"}])]}
    layer = builder.add_terminal_layer(doc, "http://win:7988", "wsl")
    doc["tree"].append(builder.build_category_root([
        {"id": 1, "name": "cat", "type": "category", "session_count": 1,
         "children": ["s1"]},
        {"id": 2, "name": "cat2", "type": "category", "session_count": 1,
         "children": ["s2"]},
    ]))
    (tmp_path / "tree.json").write_text(json.dumps(doc), encoding="utf-8")
    sm = SessionManager(str(tmp_path))
    assert sm.remove_session_from_tree("s1") is True
    updated = json.loads((tmp_path / "tree.json").read_text(encoding="utf-8"))
    terminal = next(r for r in updated["tree"] if r.get("type") == "terminal")
    win = next(c for c in terminal["children"] if c.get("env_id") == "http://win:7988")
    assert win["children"][0]["uid"] == layer["uid"]
    assert win["children"][0]["id"] == layer["id"]


def test_derive_layer_name_heuristics():
    assert builder.derive_layer_name("ssh user@192.168.1.5 -p 2222") == "ssh user@192.168.1.5"
    assert builder.derive_layer_name("docker exec -it cc bash") == "docker:cc"
    assert builder.derive_layer_name("wsl") == "WSL"
    assert builder.derive_layer_name("su - postgres") == "su:postgres"
    assert builder.derive_layer_name("psql -h 127.0.0.1 -p 8932 -U postgres -d cc") == "psql"
    assert builder.derive_layer_name("kubectl exec -n ns pod-1 -- bash") == "kubectl:pod-1"
    assert builder.derive_layer_name("") == "嵌套终端"


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
    _check_invariants(_cat_roots(doc), set(sids),
                      max_leaf=builder.dynamic_max_leaf(len(sids)))

    deadline = time.time() + 30
    while time.time() < deadline and builder.build_state()["building"]:
        time.sleep(0.2)
    assert builder.build_state()["building"] is False
    assert builder.build_state()["last_error"] is None


def test_start_build_skips_when_tree_exists(tmp_path):
    chats = tmp_path / "chats"
    chats.mkdir()
    (chats / "tree.json").write_text(json.dumps(
        {"version": builder.TREE_VERSION, "tree": []}), "utf-8")
    builder.reset_state_for_tests()
    assert builder.start_build(str(chats)) is False
    assert builder.build_state()["building"] is False


def test_start_build_rebuilds_stale_version(tmp_path):
    """磁盘上是 v1 单树：start_build 允许后台重建为 v2 森林。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(3)], per_topic=5)
    (chats / "tree.json").write_text(json.dumps({"version": 1, "tree": []}), "utf-8")

    builder.reset_state_for_tests()
    assert builder.start_build(str(chats)) is True
    import time
    deadline = time.time() + 120
    while time.time() < deadline:
        doc = json.loads((chats / "tree.json").read_text("utf-8"))
        if doc.get("version") == builder.TREE_VERSION:
            break
        time.sleep(0.2)
    assert doc.get("version") == builder.TREE_VERSION
    _check_invariants(_cat_roots(doc), set(sids),
                      max_leaf=builder.dynamic_max_leaf(len(sids)))


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
    _check_invariants(_cat_roots(doc), {r["sid"] for r in rows})
    assert prompts
    for p in prompts:
        assert "常见工作区" in p
        assert "demo-proj/frontend" in p
        assert "src/App.svelte" in p


# ---------------------------------------------------------------------------
# v4：公共词抑制 (A) + 无效分裂闸门/兄弟合并 (B) + 对比式命名 (C)
# ---------------------------------------------------------------------------

def _cluster_purity(labels: list[int], truth: list[int]) -> float:
    """聚类标签对真实分组的纯度（每个标签簇取多数真类别占比之和）。"""
    from collections import Counter as _C
    by: dict = {}
    for lab, tru in zip(labels, truth):
        by.setdefault(lab, _C())[tru] += 1
    total = len(truth)
    return sum(max(c.values()) for c in by.values()) / total if total else 0.0


def test_mask_shared_columns_zeroes_dominant_terms():
    np, TfidfVectorizer, _km, _sil = builder._optional_sklearn()
    docs = (
        ["sharedword sharedword alphamarker" for _ in range(10)]
        + ["sharedword sharedword betamarker" for _ in range(10)]
        + ["neutralzone neutralword" for _ in range(10)]
    )
    vec = TfidfVectorizer(analyzer=builder._analyzer, min_df=1, max_df=0.95)
    X = vec.fit_transform(docs)
    terms = list(vec.get_feature_names_out())
    col = terms.index("sharedword")
    assert float(np.abs(X[:, col]).sum()) > 0
    Xc, masked = builder._mask_shared_columns(np, X, len(docs), 0.5, 1)
    assert masked >= 1
    assert float(np.abs(np.asarray(Xc[:, col].todense()).ravel()).sum()) == 0.0
    # 未被屏蔽的行仍保持非零且 L2 归一化
    kept_rows = np.asarray(np.abs(Xc).sum(axis=1)).ravel()
    assert (kept_rows > 0).sum() >= 20
    norms = np.sqrt(np.asarray(Xc.multiply(Xc).sum(axis=1)).ravel())
    assert abs(float(norms[0]) - 1.0) < 1e-9


def test_mask_shared_columns_keeps_split_signal():
    np, TfidfVectorizer, _km, _sil = builder._optional_sklearn()
    docs: list[str] = []
    truth: list[int] = []
    for i in range(12):
        docs.append(f"sharedword sharedword sharedword alphamarker alphamarker taskx{i:02d}qq")
        truth.append(0)
    for i in range(8):
        docs.append(f"sharedword sharedword sharedword betamarker betamarker tasky{i:02d}qq")
        truth.append(1)
    for i in range(4):
        docs.append(f"betamarker betamarker taskz{i:02d}qq")
        truth.append(1)
    vec = TfidfVectorizer(analyzer=builder._analyzer, min_df=2, max_df=0.9)
    X = vec.fit_transform(docs)
    assert "sharedword" in list(vec.get_feature_names_out())  # df=20/24=0.83 可存活
    result = builder._cluster_sklearn(docs, [2], seed=137)
    assert result is not None
    assert _cluster_purity(result[0], truth) >= 0.9


def test_naming_circuit_breaker_skips_after_failures():
    """端点连续 3 次传输失败后，后续命名不再发起调用（直接关键词回退）。"""
    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def __call__(self, prompt: str) -> str:
            self.calls += 1
            raise RuntimeError("endpoint down")

    children = [
        {"_name": f"k{i} term{i}", "_rows": [{}, {}],
         "_children_raw": [], "_path_prefix": []}
        for i in range(5)
    ]
    llm = FlakyLLM()
    for ch in children:
        builder._name_sibling_set([ch], llm, max_leaf=10)
    assert llm.calls == 3


def test_sibling_merge_gates_duplicates():
    """B2：centroid 余弦超阈值的孪生簇被合并。"""
    import importlib
    np = importlib.import_module("numpy")
    scipy_sparse = importlib.import_module("scipy.sparse")
    rows = (
        [[1.0, 1.0, 0.0, 0.0], [1.0, 0.8, 0.0, 0.1]]   # 簇0
        + [[0.9, 1.0, 0.1, 0.0], [1.0, 0.9, 0.0, 0.0]]  # 簇1（与簇0几乎相同）
        + [[0.0, 0.0, 1.0, 0.5], [0.0, 0.1, 1.0, 1.0]]  # 簇2（正交）
    )
    Xc = scipy_sparse.csr_matrix(rows)
    labels = [0, 0, 1, 1, 2, 2]
    terms = {0: ["aa"], 1: ["bb"], 2: ["cc"]}
    merged_labels, merged_terms = builder._merge_similar_clusters(np, Xc, labels, terms, 0.9)
    groups = set(merged_labels)
    assert len(groups) == 2
    assert merged_labels[0] == merged_labels[2] != merged_labels[4]
    assert set(merged_terms[merged_labels[0]]) == {"aa", "bb"} or \
        set(merged_terms[merged_labels[0]]) == {"cc"}


def test_contrastive_naming_names_and_merges():
    """C：同层兄弟一次调用互斥命名；末级小组可合并，超限合并被拒。"""
    def make_terminal(name, sids):
        rows = [{"sid": s, "title": s, "doc": "d", "user_first": ""} for s in sids]
        return {"_name": name, "_children_raw": list(sids), "_rows": rows}

    g1 = make_terminal("kw1", ["s1", "s2", "s3"])
    g2 = make_terminal("kw2", ["s4", "s5", "s6"])
    g3 = make_terminal("kw3", ["s7", "s8", "s9"])
    g4 = make_terminal("kw4", [f"x{i}" for i in range(10)])  # 10 个，合并将超上限
    parent = {
        "_name": "kw", "_rows": g1["_rows"] + g2["_rows"] + g3["_rows"] + g4["_rows"],
        "_children_raw": [g1, g2, g3, g4],
    }
    replies = {
        "4 组兄弟分类": (
            '```json\n{"names": {"1": "前端侧边栏", "2": "移动端设备", '
            '"3": "推理服务", "4": "工具执行"}, "merges": [["1","2"], ["3","4"]]}\n```'
        ),
        "1 组兄弟分类": '{"names": {"1": "父主题"}}',
    }

    def fake_llm(prompt: str) -> str:
        for key, value in replies.items():
            if key in prompt:
                return value
        raise AssertionError("unexpected naming prompt")

    builder.assign_names_llm([parent], fake_llm, max_leaf=10)
    kids = parent["_children_raw"]
    # 合并 [["3","4"]] 会话数 13 > 10 被拒；[["1","2"]] 6 ≤ 10 生效
    assert len(kids) == 3
    merged = kids[0]
    assert sorted(merged["_children_raw"]) == ["s1", "s2", "s3", "s4", "s5", "s6"]
    names = {k["_name"] for k in kids}
    assert {"前端侧边栏", "移动端设备", "工具执行"} <= names or \
        {"前端侧边栏", "推理服务", "工具执行"} <= names
    assert parent["_name"] == "父主题"


def test_contrastive_naming_line_format_fallback():
    def make_terminal(name, sids):
        rows = [{"sid": s, "title": s, "doc": "d", "user_first": ""} for s in sids]
        return {"_name": name, "_children_raw": list(sids), "_rows": rows}

    g1, g2 = make_terminal("kw1", ["s1"]), make_terminal("kw2", ["s2"])
    parent = {"_name": "kw", "_rows": g1["_rows"] + g2["_rows"], "_children_raw": [g1, g2]}

    def fake_llm(prompt: str) -> str:
        if "2 组兄弟分类" in prompt:
            return "1: 图像识别\n2: 语音转写\n(说明)"
        if "1 组兄弟分类" in prompt:
            return "1: 多模态"
        raise AssertionError("unexpected naming prompt")

    builder.assign_names_llm([parent], fake_llm, max_leaf=10)
    assert parent["_children_raw"][0]["_name"] == "图像识别"
    assert parent["_children_raw"][1]["_name"] == "语音转写"
    assert parent["_name"] == "多模态"


def test_contrastive_naming_failure_falls_back_to_legacy(tmp_path):
    """JSON 与行格式都解析不了 → 逐组旧协议命名。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(4)], per_topic=6)
    rows = _rows_from(chats)

    def prose_llm(prompt: str) -> str:
        if "组兄弟分类" in prompt:
            return "我觉得这些分类都很相似，没法起名。"  # 无法解析
        return "旧协议名"

    doc = builder.build_document(rows, max_leaf=10, seed=137, llm=prose_llm)
    _check_invariants(_cat_roots(doc), {r["sid"] for r in rows})
    names = []

    def walk(node):
        names.append(node["name"])
        for c in node["children"]:
            if isinstance(c, dict):
                walk(c)

    for top in _cat_roots(doc):
        walk(top)
    # 同名兄弟会被 assemble 去重为 "旧协议名·2" 之类，前缀断言即可
    assert all(n.startswith("旧协议名") for n in names)


def _leaf_names(category_root: dict) -> list[str]:
    names: list[str] = []

    def walk(node: dict) -> None:
        kids = node.get("children") or []
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    walk(k)
        else:
            names.append(node["name"])

    walk(category_root)
    return sorted(names)


def test_refresh_facts_keeps_categories_and_rebuilds_fact_roots(tmp_path):
    """刷新保留主题子树与命名，重建事实根，并升级到当前 TREE_VERSION。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, ["alphaworkxx", "betaworkyy"], per_topic=3)
    _set_session_meta(chats, sids[:2], remote_env="http://10.0.0.9:7988")
    rows = _rows_from(chats)

    doc = builder.build_document(rows, max_leaf=10, seed=137, remote_envs=[])
    # 模拟旧事实层：版本低一档 + 空"本地终端"根（无远程子节点）。
    doc["version"] = builder.TREE_VERSION - 1
    doc["tree"][2] = {"id": 3, "name": builder.TERMINAL_ROOT_NAME, "type": "terminal",
                      "children": [], "session_count": 0}
    before = _leaf_names(doc["tree"][0])
    assert doc["tree"][2]["children"] == []

    refreshed = builder.refresh_facts(
        doc, rows, [{"id": "http://10.0.0.9:7988", "display_name": "E9"}])
    assert refreshed["version"] == builder.TREE_VERSION

    cat_root = next(r for r in refreshed["tree"] if r.get("type") == "category")
    assert _leaf_names(cat_root) == before  # 主题子树原样保留

    term = next(r for r in refreshed["tree"] if r.get("type") == "terminal")
    assert term["name"] == builder.TERMINAL_MULTI_ROOT_NAME
    child_names = [c["name"] for c in term["children"]]
    assert child_names[0] == "本地" and "10.0.0.9:7988" in child_names
    env = next(c for c in term["children"] if c["name"] == "10.0.0.9:7988")
    assert env["env_id"] == "http://10.0.0.9:7988"

    st = refreshed["stats"]
    assert st["membership_edge_count"] == len(sids)
    assert st["attached_conversation_count"] == len(sids)
    assert st["multi_category_conversation_count"] == 0
    assert st["terminal_environment_count"] == len(term["children"])


def test_refresh_facts_rejects_multi_category_subtree(tmp_path):
    """主题子树存在重复挂载时拒绝刷新（v5 单分类语义），要求完整重建。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    _make_topics(chats, ["alphaworkxx", "betaworkyy"], per_topic=3)
    doc = builder.build_document(_rows_from(chats), max_leaf=3, seed=137)

    cat_root = doc["tree"][0]
    leaves: list[dict] = []

    def walk(node: dict) -> None:
        kids = node.get("children") or []
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    walk(k)
        else:
            leaves.append(node)

    walk(cat_root)
    assert len(leaves) >= 2
    leaves[1]["children"].append(leaves[0]["children"][0])  # 人为制造多分类

    with pytest.raises(ValueError, match="multiple categories"):
        builder.refresh_facts(doc, _rows_from(chats), [])


def test_dynamic_max_leaf_is_logarithmic():
    assert builder.dynamic_max_leaf(0) == 2
    assert builder.dynamic_max_leaf(1) == 2
    assert builder.dynamic_max_leaf(2) == 5       # 5*log2(2)=5
    assert builder.dynamic_max_leaf(4) == 10      # 5*log2(4)=10
    assert builder.dynamic_max_leaf(540) == 45    # 5*log2(540)=45.4


def test_default_max_leaf_scales_with_corpus(tmp_path):
    """不显式传 max_leaf 时按总会话数取动态上限，而不是固定 10。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    sids = _make_topics(chats, [f"topic{i}xyzabc" for i in range(6)],
                        per_topic=5)  # 30 会话 → cap=24（旧固定值为 10）
    doc = builder.build_document(_rows_from(chats))  # max_leaf/seed 全默认
    cap = builder.dynamic_max_leaf(len(sids))
    assert cap == 24
    assert doc["stats"]["max_leaf_size"] == cap
    _check_invariants(_cat_roots(doc), set(sids), max_leaf=cap)


def test_clean_terms_stitches_overlapping_cjk_grams():
    """CJK n-gram 碎片段（文件管+件管理）拼接回完整词，避免碎词名。"""
    assert builder._clean_terms(
        ["workspacefilemanager", "文件管", "件管理"]
    ) == ["workspacefilemanager", "文件管理"]
    # 重叠 1 字仅当拼接结果本身是候选词（爱奇艺）才拼接
    assert builder._clean_terms(["桌面", "爱奇", "奇艺", "爱奇艺"]) == ["桌面", "爱奇艺"]
    assert builder._clean_terms(["爱奇", "奇艺", "爱奇艺"]) == ["爱奇艺"]
    # 重叠不足 / 拼接结果不是候选词时不拼接，非 CJK 也不拼
    assert builder._clean_terms(["推理", "理由"]) == ["推理", "理由"]
    assert builder._clean_terms(["http", "psql"]) == ["http", "psql"]


def test_merge_small_clusters_merges_into_nearest():
    np = pytest.importorskip("numpy")
    X = np.zeros((20, 4))
    X[:10, 0] = 1.0          # 簇0：10 条
    X[10:18, 1] = 1.0        # 簇1：8 条
    X[18:20, 1] = 1.0        # 簇2：2 条，与簇1 最近
    X[18:20, 2] = 0.2
    labels = [0] * 10 + [1] * 8 + [2] * 2
    terms = {0: ["a"], 1: ["b"], 2: ["c"]}
    out_labels, out_terms = builder._merge_small_clusters(
        np, X, labels, terms, min_child=3, cap=45)
    assert Counter(out_labels) == {0: 10, 1: 10}
    assert "c" in out_terms[1]


def test_merge_small_clusters_respects_cap_and_keeps_orphans():
    np = pytest.importorskip("numpy")
    X = np.zeros((20, 4))
    X[:10, 0] = 1.0
    X[10:18, 1] = 1.0
    X[18:20, 1] = 1.0
    labels = [0] * 10 + [1] * 8 + [2] * 2
    # cap=9：簇1(8)+簇2(2) 超限，孤小簇保留而不是违反容量
    out_labels, _ = builder._merge_small_clusters(
        np, X, labels, {}, min_child=3, cap=9)
    assert Counter(out_labels) == {0: 10, 1: 8, 2: 2}


def test_build_prefers_coarser_splits(tmp_path):
    """最小容量约束下，末级数量向 总量/max_leaf 靠拢，不再出现成堆碎叶。"""
    chats = tmp_path / "chats"
    chats.mkdir()
    topics = [f"alpha{i}主题词" for i in range(3)] + [f"beta{i}调试项" for i in range(3)]
    sids = _make_topics(chats, topics, per_topic=25)  # 150 会话
    doc = builder.build_document(_rows_from(chats), max_leaf=45, seed=137)
    sizes = []

    def walk(node):
        kids = node["children"]
        dicts = [k for k in kids if isinstance(k, dict)]
        if dicts:
            for k in dicts:
                walk(k)
        else:
            sizes.append(len([k for k in kids if isinstance(k, str)]))

    for top in _cat_roots(doc):
        walk(top)
    assert all(s <= 45 for s in sizes)
    # 旧行为会按 8 簇递归切出 15+ 个碎叶；约束后应显著更少
    assert len(sizes) <= 10, sizes
    assert sum(sizes) == len(sids)


# ---------------------------------------------------------------------------
# 分类合并（collapse_small_category_subtrees / 工作区副本）
# ---------------------------------------------------------------------------

def _cat(nid: int, name: str, children: list) -> dict:
    return {"id": nid, "name": name, "type": "category",
            "session_count": 0, "children": children, "stats": {}}


def test_collapse_small_category_subtrees_rule():
    nodes = [
        _cat(1, "A", [
            _cat(1, "B", [
                _cat(1, "D", ["d1", "d2", "d3", "d4"]),
                _cat(2, "E", ["e1", "e2", "e3"]),
            ]),
            _cat(2, "F", [f"f{i}" for i in range(1, 10)]),
        ]),
        _cat(2, "G", [_cat(1, "H", ["h1", "h2", "h3"])]),
    ]
    assert builder.collapse_small_category_subtrees(nodes, 10) is True
    a = nodes[0]
    assert all(isinstance(c, dict) for c in a["children"])  # A 子树 16 > 10，保持内部
    b = a["children"][0]
    assert b["children"] == ["d1", "d2", "d3", "d4", "e1", "e2", "e3"]  # B(7) 收拢
    assert b["session_count"] == 7
    assert a["children"][1]["name"] == "F"  # 纯末级不动
    assert nodes[1]["children"] == ["h1", "h2", "h3"]  # G 在最高可容纳节点收拢
    assert nodes[1]["session_count"] == 3
    # 幂等
    assert builder.collapse_small_category_subtrees(nodes, 10) is False


def test_collapse_boundary_keeps_siblings_separate():
    nodes = [
        _cat(1, "X", [_cat(1, "X1", ["x1", "x2", "x3", "x4"])]),
        _cat(2, "Y", [_cat(1, "Y1", ["y1", "y2", "y3", "y4"])]),
    ]
    # 门槛恰等于子树会话数（<=）即合并；但绝不跨兄弟并池
    assert builder.collapse_small_category_subtrees(nodes, 4) is True
    assert nodes[0]["children"] == ["x1", "x2", "x3", "x4"]
    assert nodes[1]["children"] == ["y1", "y2", "y3", "y4"]
    assert builder.collapse_small_category_subtrees(nodes, 3) is False  # 已扁平


def test_collapse_tolerates_mixed_children_without_losing_sessions():
    nodes = [_cat(1, "M", ["s_direct", _cat(1, "N", ["s_n"])])]
    assert builder.collapse_small_category_subtrees(nodes, 5) is True
    assert sorted(nodes[0]["children"]) == ["s_direct", "s_n"]


def test_workspace_category_copy_collapses_small_subtrees():
    category_roots = [
        _cat(1, "L1", [_cat(1, "L2", [_cat(1, "L3", ["s1", "s2", "s3"])])]),
    ]
    flat = builder.workspace_category_children(category_roots, ["s1", "s2", "s3"], max_leaf=10)
    assert len(flat) == 1
    assert flat[0]["id"] == 1  # 沿用原顶层分类 id
    assert flat[0]["children"] == ["s1", "s2", "s3"]
    assert flat[0]["session_count"] == 3
    # 不给门槛：保持 v6 原层级（旧调用方语义）
    deep = builder.workspace_category_children(category_roots, ["s1", "s2", "s3"])
    assert deep[0]["children"][0]["children"][0]["children"] == ["s1", "s2", "s3"]

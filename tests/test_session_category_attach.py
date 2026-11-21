"""SessionManager 分类树辅助方法与"标题+分类"生成流程的单元测试。

覆盖：
  * terminal_categories 末级分类枚举（支持不同深度的末级节点）；
  * attach_session_to_category 挂载、幂等、非法路径、非末级路径、计数重算；
  * _parse_title_and_category 对纯标题/JSON/代码块包裹的解析；
  * _do_generate_title 在存在分类树时附带分类目录并完成挂载。
"""
from __future__ import annotations

import json

import pytest

from runtime.models import InferenceResult, Message
from runtime.session_manager import SessionManager, _parse_title_and_category


def _tree_fixture(pad: int = 0) -> dict:
    """Root 下：Group(1/1) 含两个末级 LeafA(1/1/1)/LeafB(1/1/2)，以及二级末级 LeafC(1/2)。

    pad>0 时 LeafA 里塞 pad 个填充会话：分类合并规则下，子树会话数不超过
    dynamic_max_leaf(总数) 的分类会被收拢；需要保留深层结构的测试必须让
    Group 子树超过门槛。"""
    pad_ids = [f"p{i:02d}" for i in range(pad)]
    return {
        "version": 1,
        "tree": [
            {
                "id": 1, "name": "Root", "type": "category",
                "children": [
                    {
                        "id": 1, "name": "Group", "type": "category",
                        "children": [
                            {"id": 1, "name": "LeafA", "type": "category", "category": "1/1/1",
                             "children": pad_ids + ["s_old"], "session_count": len(pad_ids) + 1},
                            {"id": 2, "name": "LeafB", "type": "category", "category": "1/1/2",
                             "children": [], "session_count": 0},
                        ],
                    },
                    {"id": 2, "name": "LeafC", "type": "category", "category": "1/2",
                     "children": ["s_old"], "session_count": 1},
                ],
            }
        ],
    }


def _read_tree(chats_dir) -> list:
    return json.loads((chats_dir / "tree.json").read_text("utf-8"))["tree"]


def _write_conversation(chats_dir, sid: str, user_text: str) -> None:
    d = chats_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    conv = {
        "meta": {"session_id": sid},
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": "好的，我来处理。"},
        ],
    }
    (d / "conversation.json").write_text(json.dumps(conv, ensure_ascii=False), "utf-8")


def _find_terminal(tree: list[dict], category: str) -> dict:
    parts = category.split("/")
    nodes = tree
    node = None
    for part in parts:
        node = next(n for n in nodes if isinstance(n, dict) and str(n.get("id")) == part)
        nodes = [c for c in node.get("children", []) if isinstance(c, dict)]
    return node


def test_terminal_categories_lists_all_paths(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")
    cats = sm.terminal_categories()
    assert cats == [
        {"path": "1/1/1", "label": "Root/Group/LeafA"},
        {"path": "1/1/2", "label": "Root/Group/LeafB"},
        {"path": "1/2", "label": "Root/LeafC"},
    ]


def test_terminal_categories_empty_without_tree(tmp_path):
    sm = SessionManager(str(tmp_path))
    assert sm.terminal_categories() == []


def test_attach_session_to_category_updates_counts(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture(pad=41)), "utf-8")

    assert sm.attach_session_to_category("s1", "1/1/2") is True
    tree = _read_tree(tmp_path)
    leaf_b = _find_terminal(tree, "1/1/2")
    assert leaf_b["children"] == ["s1"]
    assert leaf_b["session_count"] == 1
    group = _find_terminal(tree, "1/1")
    assert group["session_count"] == 43  # 41 填充 + s_old(LeafA) + s1(LeafB)
    root = tree[0]
    assert root["session_count"] == 43  # s_old 与 s1 去重（LeafC 复用 s_old）

    # 幂等：重复挂载不改变文件
    before = (tmp_path / "tree.json").read_text("utf-8")
    assert sm.attach_session_to_category("s1", "1/1/2") is False
    assert (tmp_path / "tree.json").read_text("utf-8") == before

    # 非法路径 / 不存在路径 / 非末级路径
    assert sm.attach_session_to_category("s1", "1/a") is False
    assert sm.attach_session_to_category("s1", "9/9") is False
    assert sm.attach_session_to_category("s1", "1/1") is False
    # tree.json 缺失
    os_remove = (tmp_path / "tree.json").unlink
    os_remove()
    assert sm.attach_session_to_category("s1", "1/1/1") is False


def test_parse_title_and_category_variants():
    assert _parse_title_and_category("") == ("", "")
    assert _parse_title_and_category("普通标题文本") == ("普通标题文本", "")
    assert _parse_title_and_category('{"title": "T", "category": "1/2/3"}') == ("T", "1/2/3")
    assert _parse_title_and_category('{"title": "T", "category": ""}') == ("T", "")
    fenced = '```json\n{"title": "围栏标题", "category": "1/2"}\n```'
    assert _parse_title_and_category(fenced) == ("围栏标题", "1/2")
    wrapped = '好的，结果是 {"title": "包裹标题", "category": "1/1"} 谢谢'
    assert _parse_title_and_category(wrapped) == ("包裹标题", "1/1")
    assert _parse_title_and_category("not json at all") == ("not json at all", "")


def _fake_infer(reply: str):
    calls = []

    def infer(request):
        calls.append(request)
        return InferenceResult(
            success=True,
            messages=[
                Message(role="user", content=request.messages[0].content),
                Message(role="assistant", content=reply),
            ],
        )

    return infer, calls


def test_do_generate_title_with_category_attaches_session(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")
    _write_conversation(tmp_path, "s1", "帮我排查推理超时问题")
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture(pad=41)), "utf-8")

    infer, calls = _fake_infer('{"title": "推理超时排查", "category": "1/1/2"}')
    sm._infer_fn = infer

    title = sm.generate_title_forced("s1")
    assert title == "推理超时排查"

    # 请求中附带了分类目录
    prompt = calls[0].messages[0].content
    assert "分类目录" in prompt
    assert "1/1/2" in prompt and "1/1/1" in prompt and "1/2" in prompt

    # index 已更新
    index = sm._read_index()
    assert index["s1"]["title"] == "推理超时排查"
    # 新格式：title_generated 记录模型生成的标题文本
    assert index["s1"]["title_generated"] == "推理超时排查"
    assert index["s1"]["title_given"] is False

    # tree.json 已挂载
    tree = _read_tree(tmp_path)
    assert "s1" in _find_terminal(tree, "1/1/2")["children"]


def test_do_generate_title_unknown_category_keeps_title(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s2", "第二条用户消息")
    _write_conversation(tmp_path, "s2", "帮我写一个脚本")
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")

    sm._infer_fn, _ = _fake_infer('{"title": "脚本生成", "category": "9/9"}')
    title = sm.generate_title_forced("s2")
    assert title == "脚本生成"

    tree = _read_tree(tmp_path)
    for cat in ("1/1/1", "1/1/2", "1/2"):
        assert "s2" not in _find_terminal(tree, cat)["children"]


def test_do_generate_title_legacy_plain_title_without_tree(tmp_path):
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s3", "第三条用户消息")
    _write_conversation(tmp_path, "s3", "随便聊聊")
    # 无 tree.json：退化为旧版纯标题请求
    sm._infer_fn, calls = _fake_infer("普通标题")
    title = sm.generate_title_forced("s3")
    assert title == "普通标题"
    assert "分类目录" not in calls[0].messages[0].content


def test_attach_is_exclusive_single_category(tmp_path):
    """v5 单分类语义：挂载到另一个末级分类 = 移动，旧挂载被解除。"""
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "会话一")
    fixture = _tree_fixture(pad=41)
    # 预先把 s1 同时挂在两个旧末级下（旧版多分类数据），挂载应顺带清理。
    for path in ("1/1/1", "1/1/2"):
        leaf = _find_terminal(fixture["tree"], path)
        if "s1" not in leaf["children"]:
            leaf["children"].append("s1")
    (tmp_path / "tree.json").write_text(json.dumps(fixture), "utf-8")

    assert sm.attach_session_to_category("s1", "1/2") is True
    tree = _read_tree(tmp_path)
    assert "s1" in _find_terminal(tree, "1/2")["children"]
    assert "s1" not in _find_terminal(tree, "1/1/1")["children"]
    # 被搬空的 LeafB(1/1/2) 已被剪除（整个节点不复存在）
    # （与 remove_session_from_tree 的运行时剪枝策略一致，单层包装只在
    # 完整重建时折叠）。
    child_names = [c["name"] for c in tree[0]["children"]]
    assert "Group" in child_names
    paths = [c["path"] for c in sm.terminal_categories()]
    assert "1/1/2" not in paths and "1/1/1" in paths and "1/2" in paths
    # 只有目标分类能查到该会话
    assert any(e["session_id"] == "s1" for e in sm.list_sessions_by_category("1/2"))
    assert not any(e["session_id"] == "s1" for e in sm.list_sessions_by_category("1/1/1"))
    # 重复挂载幂等：返回 False，不重复出现
    assert sm.attach_session_to_category("s1", "1/2") is False
    assert _find_terminal(_read_tree(tmp_path), "1/2")["children"].count("s1") == 1


def test_remove_session_from_tree_prunes_empty_categories(tmp_path):
    """删除会话后，空分类被剪掉；仍有会话的分类保留。"""
    sm = SessionManager(str(tmp_path))
    fixture = _tree_fixture(pad=41)
    # 先让 s1 挂到只有它自己的 LeafB，删掉 s1 后 LeafB 应被剪除
    (tmp_path / "tree.json").write_text(json.dumps(fixture), "utf-8")
    assert sm.attach_session_to_category("s1", "1/1/2") is True

    assert sm.remove_session_from_tree("s1") is True
    tree = _read_tree(tmp_path)
    # LeafA(1/1/1) 仍有 s_old（与填充会话一起），保留；LeafC(1/2) 仍有 s_old，保留
    assert "s_old" in _find_terminal(tree, "1/1/1")["children"]
    assert "s1" not in _find_terminal(tree, "1/1/1")["children"]
    assert _find_terminal(tree, "1/2")["children"] == ["s_old"]
    # LeafB(1/1/2) 已不存在
    paths = [c["path"] for c in sm.terminal_categories()]
    assert "1/1/2" not in paths
    # 计数重算：Group 只剩 LeafA（含填充）
    assert _find_terminal(tree, "1/1")["session_count"] == 42
    assert tree[0]["session_count"] == 42

    # 再次移除同一会话：无变化
    assert sm.remove_session_from_tree("s1") is False


def test_remove_session_from_tree_removes_all_memberships(tmp_path):
    """会话位于多个末级分类时，删除会移除其全部挂载。"""
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture(pad=41)), "utf-8")
    sm.attach_session_to_category("s1", "1/1/2")
    sm.attach_session_to_category("s1", "1/2")

    assert sm.remove_session_from_tree("s1") is True
    tree = _read_tree(tmp_path)
    # LeafC(1/2) 原本只有 s_old，s1 移除后仍保留（s_old 还在）
    assert "s1" not in _find_terminal(tree, "1/2")["children"]
    # LeafB(1/1/2) 只有 s1 → 被剪掉
    assert "1/1/2" not in [c["path"] for c in sm.terminal_categories()]


def test_remove_session_from_tree_deletes_file_when_empty(tmp_path):
    """树中最后一个会话被移除后，tree.json 被删除（可重新构建）。"""
    sm = SessionManager(str(tmp_path))
    fixture = _tree_fixture()
    # 只保留一个末级分类、一个会话
    fixture["tree"][0]["children"] = [
        {"id": 1, "name": "Group", "type": "category", "children": [
            {"id": 1, "name": "LeafA", "type": "category", "category": "1/1",
             "children": ["s_old"], "session_count": 1},
        ]},
    ]
    (tmp_path / "tree.json").write_text(json.dumps(fixture), "utf-8")

    assert sm.remove_session_from_tree("s_old") is True
    assert not (tmp_path / "tree.json").exists()
    assert sm.terminal_categories() == []


def test_remove_session_keeps_tree_when_terminal_layers_exist(tmp_path):
    """分类清空但森林里有用户自建嵌套终端层：tree.json 保留，链不丢。

    删掉最后几个会话会清空分类，但"终端"根下的 terminal_layer 是用户手工配的
    进入指令（ssh→wsl→docker 之类）；连带 tree.json 一起删等于丢用户配置。
    """
    sm = SessionManager(str(tmp_path))
    fixture = _tree_fixture()
    fixture["tree"][0]["children"] = [
        {"id": 1, "name": "LeafA", "type": "category", "category": "1/1",
         "children": ["s_old"], "session_count": 1},
    ]
    fixture["tree"].append({
        "id": 3, "name": "本地终端", "type": "terminal", "environment": "local",
        "env_id": "", "session_count": 0, "stats": {},
        "children": [
            {"id": 9, "uid": "u9", "name": "WSL", "type": "terminal_layer",
             "command": "wsl", "children": [
                 {"id": 10, "uid": "u10", "name": "cc", "type": "terminal_layer",
                  "command": "docker exec -it cc bash", "children": []},
             ]},
        ],
    })
    (tmp_path / "tree.json").write_text(json.dumps(fixture), "utf-8")

    assert sm.remove_session_from_tree("s_old") is True
    assert (tmp_path / "tree.json").exists()
    terminal = next(r for r in _read_tree(tmp_path) if r.get("type") == "terminal")
    assert terminal["children"][0]["uid"] == "u9"
    assert terminal["children"][0]["children"][0]["command"] == "docker exec -it cc bash"
    # 空分类照旧被剪掉：保留的只是用户自建的终端链
    assert sm.terminal_categories() == []


def test_delete_session_updates_tree(tmp_path):
    """DELETE 会话接口链路：delete_session 同步清理 tree.json 挂载。"""
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "第一条用户消息")
    session_dir = tmp_path / "s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "conversation.json").write_text(
        json.dumps({"meta": {"session_id": "s1"},
                    "messages": [{"role": "user", "content": "hi"}]}, ensure_ascii=False),
        "utf-8",
    )
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture(pad=41)), "utf-8")
    assert sm.attach_session_to_category("s1", "1/1/2") is True

    sm.delete_session("s1")
    tree = _read_tree(tmp_path)
    # LeafB(1/1/2) 只有 s1 → 剪除；其余分类不再包含 s1
    remaining = {c["path"] for c in sm.terminal_categories()}
    for path in remaining:
        assert "s1" not in _find_terminal(tree, path)["children"]
    assert "1/1/2" not in [c["path"] for c in sm.terminal_categories()]
    assert not (tmp_path / "s1").exists()


# ---------------------------------------------------------------------------
# v2 森林：主题 / 工作区 / 本地终端
# ---------------------------------------------------------------------------

def _forest_fixture() -> dict:
    return {
        "version": 2,
        "stats": {
            "conversation_count": 2,
            "attached_conversation_count": 2,
            "terminal_category_count": 1,
            "membership_edge_count": 2,
            "multi_category_conversation_count": 0,
            "max_categories_per_conversation": 1,
            "max_leaf_size": 10,
            "workspace_node_count": 2,
        },
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 2, "stats": {},
             "children": [
                 {"id": 1, "name": "Top", "type": "category", "session_count": 2, "children": [
                     {"id": 1, "name": "Leaf", "type": "category", "category": "1/1/1",
                      "children": ["s_old", "s_del"], "session_count": 2},
                 ]},
             ]},
            {"id": 2, "name": "工作区", "type": "workspace", "session_count": 2,
             "stats": {"workspace_count": 2}, "children": [
                 {"id": 1, "name": "agents", "type": "workspace", "workspace": "/cc/agents",
                  "session_count": 2, "children": ["s_old", "s_del"], "stats": {}},
                 {"id": 2, "name": "未指定工作区", "type": "workspace", "workspace": "",
                  "session_count": 0, "children": [], "stats": {}},
             ]},
            {"id": 3, "name": "本地终端", "type": "terminal", "session_count": 0,
             "children": [], "stats": {}},
        ],
    }


def test_forest_terminal_categories_excludes_non_category(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_fixture()), "utf-8")
    assert sm.terminal_categories() == [
        {"path": "1/1/1", "label": "主题/Top/Leaf"},
    ]


def test_forest_attach_rejects_non_category_targets(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_fixture()), "utf-8")
    assert sm.attach_session_to_category("s_new", "2/2") is False  # 工作区叶子
    assert sm.attach_session_to_category("s_new", "3") is False    # 终端根
    assert sm.attach_session_to_category("s_new", "1/1/1") is True  # 分类叶子正常
    # 挂载后 3 个会话仍在门槛（5*log2）之下：Top(1/1) 被分类合并成末级。
    # 继承/查询只看分类挂载：s_old 也挂在工作区叶子下，但不出现在分类路径中
    assert sm.session_categories("s_old") == ["1/1"]
    assert sm.session_categories("s_new") == ["1/1"]


def test_forest_remove_keeps_structural_terminal_root(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_fixture()), "utf-8")

    assert sm.remove_session_from_tree("s_del") is True
    tree = _read_tree(tmp_path)
    roots = {r["name"]: r for r in tree}
    assert "本地终端" in roots  # 结构性节点不因空被剪
    # 剩 1 个会话：Top(1/1/1) 链被分类合并，Top(1/1) 直接挂会话
    leaf = _find_terminal(tree, "1/1")
    assert leaf["children"] == ["s_old"]
    agents = _find_terminal(tree, "2/1")
    assert agents["children"] == ["s_old"]  # 工作区挂载同步清理
    stats = json.loads((tmp_path / "tree.json").read_text("utf-8"))["stats"]
    assert stats["attached_conversation_count"] == 1
    assert stats["terminal_category_count"] == 1


def test_forest_remove_all_sessions_deletes_tree_file(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_fixture()), "utf-8")
    assert sm.remove_session_from_tree("s_old") is True
    assert sm.remove_session_from_tree("s_del") is True
    # 分类子树全空 → 删除 tree.json（下次访问满足条件会重建整个森林）
    assert not (tmp_path / "tree.json").exists()


# ---------------------------------------------------------------------------
# v3 森林：最近会话根 / 会话历史月份根
# ---------------------------------------------------------------------------

def _forest_v3_fixture() -> dict:
    return {
        "version": 3,
        "stats": {
            "conversation_count": 2,
            "attached_conversation_count": 2,
            "terminal_category_count": 1,
            "membership_edge_count": 1,
            "multi_category_conversation_count": 0,
            "max_categories_per_conversation": 1,
            "max_leaf_size": 10,
            "workspace_node_count": 1,
            "environment_node_count": 1,
            "recent_session_count": 2,
            "recent_limit": 20,
            "month_node_count": 2,
        },
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 2, "stats": {},
             "children": [
                 {"id": 1, "name": "Top", "type": "category", "session_count": 2, "children": [
                     {"id": 1, "name": "Leaf", "type": "category", "category": "1/1/1",
                      "children": ["s_a", "s_b"], "session_count": 2},
                 ]},
             ]},
            {"id": 2, "name": "工作区", "type": "workspace", "session_count": 2,
             "stats": {"workspace_count": 1, "environment_count": 1}, "children": [
                 {"id": 1, "name": "agents", "type": "workspace", "workspace": "/cc/agents",
                  "session_count": 2, "children": ["s_a", "s_b"], "stats": {}},
             ]},
            {"id": 3, "name": "本地终端", "type": "terminal", "session_count": 0,
             "children": [], "stats": {}},
            {"id": 4, "name": "最近会话", "type": "recent", "session_count": 2,
             "stats": {"limit": 20}, "children": ["s_b", "s_a"]},
            {"id": 5, "name": "会话历史", "type": "history", "session_count": 2,
             "stats": {"month_count": 2}, "children": [
                 {"id": 1, "name": "202607", "type": "month", "session_count": 1,
                  "children": ["s_b"], "stats": {}},
                 {"id": 2, "name": "202608", "type": "month", "session_count": 1,
                  "children": ["s_a"], "stats": {}},
             ]},
        ],
    }


def _write_index_entries(tmp_path, entries: dict) -> None:
    (tmp_path / "index.json").write_text(json.dumps(entries, ensure_ascii=False), "utf-8")


def test_forest_v3_recent_and_month_queries_use_time_order(tmp_path):
    """recent/month 末级按时间倒序；category/workspace 末级仍按标题稳定序。"""
    _write_index_entries(tmp_path, {
        "s_a": {"session_id": "s_a", "title": "Zebra",
                "created_at": "2026-08-01T10:00:00", "last_inference_at": "2026-09-02T10:00:00"},
        "s_b": {"session_id": "s_b", "title": "Alpha",
                "created_at": "2026-07-01T10:00:00", "last_inference_at": "2026-09-01T10:00:00"},
    })
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_v3_fixture()), "utf-8")

    assert [s["session_id"] for s in sm.list_sessions_by_category("4")] == ["s_a", "s_b"]
    assert [s["session_id"] for s in sm.list_sessions_by_category("5/2")] == ["s_a"]
    assert [s["session_id"] for s in sm.list_sessions_by_category("5/1")] == ["s_b"]
    # 工作区叶子仍是浏览语义：标题稳定序（Alpha, Zebra），不随时间
    assert [s["session_id"] for s in sm.list_sessions_by_category("2/1")] == ["s_b", "s_a"]


def test_forest_v3_history_root_is_not_queryable_leaf(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_v3_fixture()), "utf-8")
    with pytest.raises(ValueError):
        sm.list_sessions_by_category("5")  # 历史根含子节点，不是末级
    with pytest.raises(ValueError):
        sm.list_sessions_by_category("2")  # 工作区根同理


def test_forest_v3_remove_prunes_empty_month_keeps_new_roots(tmp_path):
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_forest_v3_fixture()), "utf-8")

    assert sm.remove_session_from_tree("s_a") is True
    doc = json.loads((tmp_path / "tree.json").read_text("utf-8"))
    roots = {r["name"]: r for r in doc["tree"]}
    assert "最近会话" in roots and "会话历史" in roots  # 结构根不因空而消失
    assert roots["最近会话"]["children"] == ["s_b"]
    months = roots["会话历史"]["children"]
    assert [m["name"] for m in months] == ["202607"]  # 空月份被剪除
    assert roots["最近会话"]["session_count"] == 1
    # 删除后剩 1 会话，低于门槛：主题链合并，Top(1/1) 成为末级
    leaf = _find_terminal(doc["tree"], "1/1")
    assert leaf["children"] == ["s_b"]
    # 分类 membership 统计仍只覆盖分类子树
    assert doc["stats"]["attached_conversation_count"] == 1


def test_attach_collapses_small_topic_subtrees(tmp_path):
    """小树挂载后触发分类合并：Group(1/1) 的链收拢为直挂会话，LeafC 不动。"""
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")

    assert sm.attach_session_to_category("s1", "1/1/2") is True
    tree = _read_tree(tmp_path)
    group = _find_terminal(tree, "1/1")
    assert group["children"] == ["s_old", "s1"]
    assert group["session_count"] == 2
    assert _find_terminal(tree, "1/2")["children"] == ["s_old"]
    paths = [c["path"] for c in sm.terminal_categories()]
    assert sorted(paths) == ["1/1", "1/2"]  # 1/1/1、1/1/2 已被合并

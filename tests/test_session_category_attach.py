"""SessionManager 分类树辅助方法与"标题+分类"生成流程的单元测试。

覆盖：
  * terminal_categories 末级分类枚举（支持不同深度的末级节点）；
  * attach_session_to_category 挂载、幂等、非法路径、非末级路径、计数重算；
  * _parse_title_and_category 对纯标题/JSON/代码块包裹的解析；
  * _do_generate_title 在存在分类树时附带分类目录并完成挂载。
"""
from __future__ import annotations

import json

from runtime.models import InferenceResult, Message
from runtime.session_manager import SessionManager, _parse_title_and_category


def _tree_fixture() -> dict:
    """Root 下：Group(1/1) 含两个末级 LeafA(1/1/1)/LeafB(1/1/2)，以及二级末级 LeafC(1/2)。"""
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
                             "children": ["s_old"], "session_count": 1},
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
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")

    assert sm.attach_session_to_category("s1", "1/1/2") is True
    tree = _read_tree(tmp_path)
    leaf_b = _find_terminal(tree, "1/1/2")
    assert leaf_b["children"] == ["s1"]
    assert leaf_b["session_count"] == 1
    group = _find_terminal(tree, "1/1")
    assert group["session_count"] == 2  # s_old(LeafA) + s1(LeafB)
    root = tree[0]
    assert root["session_count"] == 2  # s_old 与 s1 去重

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
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")

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
    assert index["s1"]["title_generated"] is True

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


def test_multi_membership_attach_is_legal(tmp_path):
    """同一会话可同时挂载到多个末级分类（自动分类 + 人工分类并存）。"""
    sm = SessionManager(str(tmp_path))
    sm.on_session_created("s1", "会话一")
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")

    assert sm.attach_session_to_category("s1", "1/1/2") is True
    assert sm.attach_session_to_category("s1", "1/2") is True

    tree = _read_tree(tmp_path)
    assert "s1" in _find_terminal(tree, "1/1/2")["children"]
    assert "s1" in _find_terminal(tree, "1/2")["children"]
    # 祖先计数按唯一会话去重：Root = {s_old, s1}
    assert tree[0]["session_count"] == 2
    # 两个分类查询都能查到该会话
    assert any(e["session_id"] == "s1" for e in sm.list_sessions_by_category("1/1/2"))
    assert any(e["session_id"] == "s1" for e in sm.list_sessions_by_category("1/2"))


def test_remove_session_from_tree_prunes_empty_categories(tmp_path):
    """删除会话后，空分类被剪掉；仍有会话的分类保留。"""
    sm = SessionManager(str(tmp_path))
    fixture = _tree_fixture()
    # 先让 s1 挂到只有它自己的 LeafB，删掉 s1 后 LeafB 应被剪除
    (tmp_path / "tree.json").write_text(json.dumps(fixture), "utf-8")
    assert sm.attach_session_to_category("s1", "1/1/2") is True

    assert sm.remove_session_from_tree("s1") is True
    tree = _read_tree(tmp_path)
    # LeafA(1/1/1) 仍有 s_old，保留；LeafC(1/2) 仍有 s_old，保留
    assert _find_terminal(tree, "1/1/1")["children"] == ["s_old"]
    assert _find_terminal(tree, "1/2")["children"] == ["s_old"]
    # LeafB(1/1/2) 已不存在
    paths = [c["path"] for c in sm.terminal_categories()]
    assert "1/1/2" not in paths
    # 计数重算：Group 只剩 LeafA
    assert _find_terminal(tree, "1/1")["session_count"] == 1
    assert tree[0]["session_count"] == 1

    # 再次移除同一会话：无变化
    assert sm.remove_session_from_tree("s1") is False


def test_remove_session_from_tree_removes_all_memberships(tmp_path):
    """会话位于多个末级分类时，删除会移除其全部挂载。"""
    sm = SessionManager(str(tmp_path))
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")
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
    (tmp_path / "tree.json").write_text(json.dumps(_tree_fixture()), "utf-8")
    assert sm.attach_session_to_category("s1", "1/1/2") is True

    sm.delete_session("s1")
    tree = _read_tree(tmp_path)
    # LeafB(1/1/2) 只有 s1 → 剪除；其余分类不再包含 s1
    remaining = {c["path"] for c in sm.terminal_categories()}
    for path in remaining:
        assert "s1" not in _find_terminal(tree, path)["children"]
    assert "1/1/2" not in [c["path"] for c in sm.terminal_categories()]
    assert not (tmp_path / "s1").exists()

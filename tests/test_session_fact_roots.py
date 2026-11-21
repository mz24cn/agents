"""事实根增量维护与缺失分类补齐的专项测试。

覆盖：
  * ensure_session_facts：新会话补挂进工作区目录（"未分类"兜底）与月份历史；
  * attach_session_to_category：主题挂载后工作区分类副本从"未分类"迁移到镜像路径；
  * on_session_created 与事实根补挂的接线；
  * classify_missing_sessions：SUMMARY LLM 为缺席会话选择既有末级分类并补标题；
  * CLI --classify-missing 的参数门控。
"""
from __future__ import annotations

import json

import pytest

from runtime.category_tree_builder import classify_missing_sessions, main, refresh_facts
from runtime.session_manager import SessionManager


def _v7_tree() -> dict:
    """四根森林 v7 最小形：主题(1)、工作区(2, 带环境层)、终端(3)、历史(5)。"""
    return {
        "version": 7,
        "stats": {},
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 1, "children": [
                {"id": 2, "name": "运行时", "type": "category", "session_count": 1, "children": [
                    {"id": 1, "name": "网络与终端", "type": "category",
                     "session_count": 1, "children": ["s1"], "stats": {}},
                ]},
            ]},
            {"id": 2, "name": "工作区", "type": "workspace", "session_count": 1, "children": [
                {"id": 1, "name": "本地", "type": "environment", "environment": "local",
                 "session_count": 1, "children": [
                     {"id": 1, "name": "agents", "type": "workspace", "workspace": "/cc/agents",
                      "session_count": 1, "children": [
                          {"id": 2, "name": "运行时", "type": "category", "session_count": 1,
                           "children": [
                               {"id": 1, "name": "网络与终端", "type": "category",
                                "session_count": 1, "children": ["s1"], "stats": {}},
                           ]},
                      ], "stats": {}},
                 ], "stats": {}},
            ], "stats": {}},
            {"id": 3, "name": "终端", "type": "terminal", "session_count": 0, "children": []},
            {"id": 5, "name": "会话历史", "type": "history", "session_count": 1, "children": [
                {"id": 1, "name": "202611", "type": "month",
                 "session_count": 1, "children": ["s1"], "stats": {}},
            ], "stats": {}},
        ],
    }


def _write_conversation(chats_dir, sid: str, workspace: str = "/cc/CosyVoice",
                        remote_env: str = "", created_at: str = "2026-11-17T21:24:43") -> None:
    d = chats_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"session_id": sid, "workspace": workspace, "created_at": created_at}
    if remote_env:
        meta["remote_env"] = remote_env
    conv = {
        "meta": meta,
        "messages": [
            {"role": "user", "content": "帮我修一下 generate_audio.py 的 CUDA_HOME 问题"},
            {"role": "assistant", "content": "好的，先看一下脚本内容。"},
        ],
    }
    (d / "conversation.json").write_text(json.dumps(conv, ensure_ascii=False), "utf-8")


def _write_tree(chats_dir, tree: dict) -> None:
    (chats_dir / "tree.json").write_text(json.dumps(tree, ensure_ascii=False), "utf-8")


def _read_tree(chats_dir) -> dict:
    return json.loads((chats_dir / "tree.json").read_text("utf-8"))


def _find(node: dict, pred, path=()):
    if pred(node):
        return node, path
    for c in node.get("children") or []:
        if isinstance(c, dict):
            hit = _find(c, pred, path + (node,))
            if hit:
                return hit
    return None


def _leaf_ids(tree_doc: dict, sid: str) -> list:
    """返回 sid 挂载位置的节点 id 链（仅末级分类，跳过事实根）。"""
    out = []

    def walk(node, ids):
        kids = node.get("children") or []
        nid = str(node.get("id"))
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    walk(k, ids + [nid])
        elif sid in [str(x) for x in kids]:
            out.append(ids + [nid])

    for top in tree_doc["tree"]:
        if str(top.get("type") or "") == "category":
            walk(top, [])
    return out


def _ws_copy_leaf_ids(tree_doc: dict, sid: str) -> list:
    """工作区目录副本下 sid 所在叶子的节点 id 链（环境层不计，工作区节点起）。"""
    out = []
    ws_root = next(t for t in tree_doc["tree"] if t.get("type") == "workspace")

    def walk(node, ids):
        nid = str(node.get("id"))
        kids = node.get("children") or []
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    walk(k, ids + [nid])
        elif sid in [str(x) for x in kids]:
            out.append(ids + [nid])

    for child in ws_root.get("children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "environment":
            for g in child.get("children") or []:
                if isinstance(g, dict):
                    walk(g, [])
        else:
            walk(child, [])
    return out


@pytest.fixture()
def manager(tmp_path):
    return SessionManager(str(tmp_path))


def test_ensure_session_facts_adds_unclassified_and_month(tmp_path, manager):
    _write_tree(tmp_path, _v7_tree())
    _write_conversation(tmp_path, "s2")

    assert manager.ensure_session_facts("s2") is True
    doc = _read_tree(tmp_path)

    # 工作区：新的 CosyVoice 目录节点（本地环境层下），兜底进"未分类"
    hit = _find(doc["tree"][1], lambda n: n.get("name") == "CosyVoice")
    assert hit is not None
    wnode = hit[0]
    assert wnode["workspace"] == "/cc/CosyVoice"
    leaves = [c for c in wnode["children"] if isinstance(c, dict)]
    assert [c["name"] for c in leaves] == ["未分类"]
    assert leaves[0]["children"] == ["s2"]
    assert _ws_copy_leaf_ids(doc, "s2")

    # 会话历史：202611 月份头部
    month = doc["tree"][3]["children"][0]
    assert month["name"] == "202611"
    assert month["children"][0] == "s2"

    # 计数与统计
    assert doc["tree"][1]["session_count"] == 2
    assert doc["tree"][3]["session_count"] == 2
    assert doc["stats"]["workspace_node_count"] == 2

    # 幂等
    assert manager.ensure_session_facts("s2") is False


def test_ensure_session_facts_creates_environment_node(tmp_path, manager):
    _write_tree(tmp_path, _v7_tree())
    _write_conversation(tmp_path, "s9", workspace="/home/dev/app",
                       remote_env="http://10.0.0.1:8080")

    assert manager.ensure_session_facts("s9") is True
    doc = _read_tree(tmp_path)
    envs = [c for c in doc["tree"][1]["children"] if c.get("type") == "environment"]
    assert [e["environment"] for e in envs] == ["local", "10.0.0.1:8080"]
    env = envs[1]
    assert env["children"][0]["workspace"] == "/home/dev/app"
    assert doc["stats"]["environment_node_count"] == 2


def test_attach_moves_session_from_unclassified_to_mirror(tmp_path, manager):
    tree = _v7_tree()
    # s2 已经在工作区目录的"未分类"里，但还没有主题分类
    ws_node = tree["tree"][1]["children"][0]["children"][0]
    ws_node["children"].append(
        {"id": 2, "name": "未分类", "type": "category",
         "session_count": 1, "children": ["s2"], "stats": {}})
    _write_tree(tmp_path, tree)
    _write_conversation(tmp_path, "s2")

    assert manager.attach_session_to_category("s2", "1/2/1") is True
    doc = _read_tree(tmp_path)

    # 主题树：正常挂载；随后 2 个会话低于门槛(5*log2)，网络与终端并入运行时
    assert _leaf_ids(doc, "s2") == [["1", "2"]]
    # 工作区副本：从"未分类"迁移到镜像路径、空"未分类"被剪除，同样被合并
    assert _ws_copy_leaf_ids(doc, "s2") == [["1", "2"]]
    ws_node = doc["tree"][1]["children"][0]["children"][0]
    assert [c["name"] for c in ws_node["children"] if isinstance(c, dict)] == ["运行时"]
    # 统计
    assert doc["stats"]["membership_edge_count"] == 2
    assert doc["stats"]["attached_conversation_count"] == 2
    assert doc["tree"][1]["session_count"] == 2


def test_attach_backfills_facts_when_session_missing(tmp_path, manager):
    _write_tree(tmp_path, _v7_tree())
    _write_conversation(tmp_path, "s3")

    assert manager.attach_session_to_category("s3", "1/2/1") is True
    doc = _read_tree(tmp_path)
    # 合并语义：小链收拢到"运行时"，主题 1/2、CosyVoice 目录副本 2/2
    assert _leaf_ids(doc, "s3") == [["1", "2"]]
    # 事实根同时补齐：工作区副本镜像路径 + 月份历史
    assert _ws_copy_leaf_ids(doc, "s3") == [["2", "2"]]
    month = doc["tree"][3]["children"][0]
    assert "s3" in month["children"]


def test_on_session_created_plugs_into_fact_roots(tmp_path, manager):
    _write_tree(tmp_path, _v7_tree())
    _write_conversation(tmp_path, "s4")

    manager.on_session_created("s4", "新问题")
    doc = _read_tree(tmp_path)
    month = doc["tree"][3]["children"][0]
    assert "s4" in month["children"]
    assert _ws_copy_leaf_ids(doc, "s4")


def test_ensure_session_facts_without_tree_is_noop(tmp_path, manager):
    _write_conversation(tmp_path, "s2")
    assert manager.ensure_session_facts("s2") is False


def test_all_non_terminal_roots_hold_every_category_member(tmp_path, manager):
    """不变量：主题根里的每个会话必须同时存在于其它非终端根（工作区/历史…）。

    这条测试刻意按"根类型"泛化而不是点名工作区/历史：以后新增任何分类体系
    （新的森林根），主题挂载路径若忘了为该根补挂会话，这里会直接失败，
    从而结构性地保证自动与手动生成路径的"所属目录"始终一致。
    """
    _write_tree(tmp_path, _v7_tree())
    _write_conversation(tmp_path, "s3")
    assert manager.attach_session_to_category("s3", "1/2/1") is True
    doc = _read_tree(tmp_path)

    def collect_sids(node: dict) -> set:
        found: set = set()
        stack = [node]
        while stack:
            for k in stack.pop().get("children") or []:
                if isinstance(k, dict):
                    stack.append(k)
                elif isinstance(k, str):
                    found.add(k)
        return found

    cat_root = next(t for t in doc["tree"] if t.get("type") == "category")
    category_sids = collect_sids(cat_root)
    assert "s1" in category_sids and "s3" in category_sids

    for root in doc["tree"]:
        kind = str(root.get("type") or "")
        if kind in ("category", "terminal"):
            continue  # 主题根是来源；终端根按设计不承载会话列表
        missing = category_sids - collect_sids(root)
        assert not missing, f"根 {kind!r} 缺少会话挂载: {missing}"


def _stub_infer(payload: str):
    from types import SimpleNamespace

    def infer(request):  # noqa: ARG001 - 只关心返回
        return SimpleNamespace(
            messages=[SimpleNamespace(role="assistant", content=payload)])

    return infer


def test_auto_compression_and_manual_regeneration_mount_identically(tmp_path):
    """会话完成后的自动压缩与手动"重新生成概要和记忆"必须产生完全相同的挂载集合。

    两条路径都汇入 update_index(compression_updated=True) → 标题+分类 →
    attach_session_to_category（主题 + 事实根 + 镜像），此测试把该等价性钉死。
    """
    mgr = SessionManager(
        str(tmp_path),
        infer_fn=_stub_infer('{"title": "语音合成环境修复", "category": "1/2/1"}'),
    )
    # 主题叶子与其工作区镜像垫 41 个填充会话：两条路径挂载后子树仍在
    # 合并门槛之上，深链保持、断言与未合并形态一致。
    tree = _v7_tree()
    pad = [f"p{i:02d}" for i in range(41)]
    topic_leaf = tree["tree"][0]["children"][0]["children"][0]
    mirror_leaf = tree["tree"][1]["children"][0]["children"][0]["children"][0]["children"][0]
    for leaf in (topic_leaf, mirror_leaf):
        leaf["children"] = pad + list(leaf["children"])
        leaf["session_count"] = len(leaf["children"])
    _write_tree(tmp_path, tree)
    _write_conversation(tmp_path, "s2")
    _write_conversation(tmp_path, "s3")
    mgr.on_session_created("s2", "帮我修一下 generate_audio.py")
    mgr.on_session_created("s3", "帮我修一下 generate_audio.py")

    # 自动时机：post-turn 持久化发现 summary_version 变化（server_state.py）
    mgr.update_index("s2", generate_title=True, compression_updated=True)
    # 手动时机：regenerate-summary 端点调用同一入口（handler_api.py）
    mgr.update_index("s3", generate_title=True, compression_updated=True)

    doc = _read_tree(tmp_path)
    # 两条路径挂载后的叶子链必须完全一致（合并门槛之上，深链保持）
    assert _leaf_ids(doc, "s2") == _leaf_ids(doc, "s3") == [["1", "2", "1"]]
    assert _ws_copy_leaf_ids(doc, "s2") == _ws_copy_leaf_ids(doc, "s3")
    assert _ws_copy_leaf_ids(doc, "s2")  # 工作区副本非空
    months = doc["tree"][3]["children"][0]["children"]
    assert {"s2", "s3"} <= set(map(str, months))


# ---------------------------------------------------------------------------
# classify_missing_sessions（builder 层补齐）
# ---------------------------------------------------------------------------

def _fake_llm(payload: str):
    calls = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        return payload

    llm.calls = calls
    return llm


def _prepare_missing_corpus(tmp_path):
    _write_tree(tmp_path, _v7_tree())
    _write_conversation(tmp_path, "s2")
    index = {"s1": {"session_id": "s1", "title": "旧会话", "title_generated": "旧会话"},
             "s2": {"session_id": "s2", "title": "261117_212443", "title_generated": ""}}
    (tmp_path / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")


def test_classify_missing_attaches_and_fills_title(tmp_path):
    _prepare_missing_corpus(tmp_path)
    llm = _fake_llm('{"title": "语音合成环境修复", "category": "1/2/1"}')

    counts = classify_missing_sessions(_read_tree(tmp_path), str(tmp_path), llm)
    assert counts["classified"] == 1
    assert counts["title_filled"] == 1

    index = json.loads((tmp_path / "index.json").read_text("utf-8"))
    assert index["s2"]["title_generated"] == "语音合成环境修复"


def test_classify_missing_then_refresh_facts_mirrors_copy(tmp_path):
    _prepare_missing_corpus(tmp_path)
    doc = _read_tree(tmp_path)
    llm = _fake_llm('{"title": "语音合成环境修复", "category": "1/2/1"}')
    counts = classify_missing_sessions(doc, str(tmp_path), llm)
    assert counts["classified"] == 1

    from runtime.category_tree_builder import load_sessions
    doc = refresh_facts(doc, load_sessions(tmp_path), [])
    # refresh_facts 一并归一化：保留的主题子树与副本都执行分类合并
    assert _leaf_ids(doc, "s2") == [["1", "2"]]
    # 工作区副本重新按主题树筛选：出现在镜像叶子而不是"未分类"
    assert _ws_copy_leaf_ids(doc, "s2") == [["1", "2"]]


def test_classify_missing_unknown_path_counts_failed(tmp_path):
    _prepare_missing_corpus(tmp_path)
    doc = _read_tree(tmp_path)
    llm = _fake_llm('{"title": "随便", "category": "9/9/9"}')
    counts = classify_missing_sessions(doc, str(tmp_path), llm, update_index=False)
    assert counts["classified"] == 0
    assert counts["failed"] == 1
    assert _leaf_ids(doc, "s2") == []


def test_classify_missing_empty_category_keeps_unmounted(tmp_path):
    _prepare_missing_corpus(tmp_path)
    doc = _read_tree(tmp_path)
    llm = _fake_llm('{"title": "独特主题", "category": ""}')
    counts = classify_missing_sessions(doc, str(tmp_path), llm, update_index=False)
    assert counts["no_category"] == 1
    assert _leaf_ids(doc, "s2") == []


def test_classify_missing_requires_category_root(tmp_path):
    _prepare_missing_corpus(tmp_path)
    doc = _read_tree(tmp_path)
    doc["tree"] = [t for t in doc["tree"] if t.get("type") != "category"]
    with pytest.raises(ValueError):
        classify_missing_sessions(doc, str(tmp_path), _fake_llm("{}"))


def test_cli_classify_missing_requires_llm(tmp_path, capsys):
    _prepare_missing_corpus(tmp_path)
    code = main(["--chats-dir", str(tmp_path), "--classify-missing"])
    assert code == 2
    assert "--classify-missing requires --llm" in capsys.readouterr().err

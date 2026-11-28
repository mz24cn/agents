"""标签重命名传播、来源标签持久化、系统消息合并（第一阶段需求 1/2/4）。

覆盖：
  * SessionManager.rename_label：改名传播到全部原标签会话；index.json 与
    tree.json 同步（旧标签节点剪除、新标签节点承接）；目标标签已存在时按标签
    集合语义合并去重；入参校验；未知标签 404 语义（LabelNotFound）；同名幂等。
  * label_store.rename_label_doc：成员集合没变才改名搬迁；并入已有标签时**两边
    都删**（旧标签与目标标签，无论目标原先有没有文档），避免任一侧缺成员的陈旧
    摘要被"发起新会话"读到。
  * SessionManager.on_session_created(source_labels=...)：来源标签随建档落盘，
    而不只是前端 UI 上显示。
  * runtime.system_context.merge_first_turn_system_messages：AI 代理系统提示词
    与标签来源系统消息合并共存（代理在前、来源在后），不带来源标记的系统消息
    仍按原语义被替换；等价去重避免重复追加；群聊路径同样合并。
  * 协议层 _merge_leading_system_messages：多条前置系统消息最终并入同一条 system。
"""
from __future__ import annotations

import json
import os

import pytest

from runtime import label_store
from runtime.session_manager import (
    LabelNotFound,
    SessionManager,
    clean_label,
    clean_labels,
)
from runtime.system_context import (
    is_label_system_message,
    is_plain_system_message,
    label_origin_of,
    merge_first_turn_system_messages,
)


def _v8_tree_doc() -> dict:
    """五根 v8 森林最小形（主题/工作区/标签/终端/历史），主题与标签根为空。"""
    return {
        "version": 8,
        "stats": {},
        "tree": [
            {"id": 1, "name": "主题", "type": "category", "session_count": 0,
             "children": []},
            {"id": 2, "name": "工作区", "type": "workspace", "session_count": 0,
             "children": []},
            {"id": 6, "name": "标签", "type": "labels", "session_count": 0,
             "children": [], "stats": {}},
            {"id": 3, "name": "本地终端", "type": "terminal", "session_count": 0,
             "environment": "local", "env_id": "", "children": []},
            {"id": 5, "name": "会话历史", "type": "history", "session_count": 0,
             "children": [], "stats": {}},
        ],
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    chats = tmp_path / "chat_data"
    chats.mkdir()
    (chats / "tree.json").write_text(
        json.dumps(_v8_tree_doc(), ensure_ascii=False), "utf-8")
    mgr = SessionManager(str(chats))
    # 聚合文档落在 tmp_path 下，测试之间互不污染（也不污染真实 DATA_DIR）
    monkeypatch.setattr(label_store, "labels_dir",
                        lambda: str(tmp_path / "labels"))
    return mgr, chats


def _index_add(mgr, sid: str, labels=None) -> None:
    index = mgr._read_index()
    entry = {"session_id": sid, "title": f"t-{sid}",
             "created_at": "2026-11-20T10:00:00", "last_inference_at": ""}
    if labels:
        entry["labels"] = list(labels)
    index[sid] = entry
    mgr._write_index(index)


def _labels_root(mgr) -> dict:
    tree = mgr.get_category_tree()
    return next(t for t in tree["tree"] if t.get("type") == "labels")


def _label_node(mgr, name: str) -> dict | None:
    return next((c for c in _labels_root(mgr)["children"]
                 if isinstance(c, dict) and c.get("name") == name), None)


# ---------------------------------------------------------------------------
# 1. rename_label：传播 + index/tree 同步
# ---------------------------------------------------------------------------

def test_rename_label_propagates_to_every_session(env):
    mgr, _chats = env
    _index_add(mgr, "s1", ["TTS", "其他"])
    _index_add(mgr, "s2", ["TTS"])
    _index_add(mgr, "s3", ["其他"])
    mgr.set_session_labels("s1", ["TTS", "其他"])
    mgr.set_session_labels("s2", ["TTS"])
    mgr.set_session_labels("s3", ["其他"])

    result = mgr.rename_label("TTS", "语音合成")

    assert result["changed"] is True
    assert result["session_ids"] == ["s1", "s2"]
    assert result["session_count"] == 2
    assert result["labels"] == {"s1": ["语音合成", "其他"], "s2": ["语音合成"]}

    index = mgr._read_index()
    assert index["s1"]["labels"] == ["语音合成", "其他"]
    assert index["s2"]["labels"] == ["语音合成"]
    assert index["s3"]["labels"] == ["其他"]

    # tree.json：旧标签节点整体消失，新标签节点承接全部会话
    assert _label_node(mgr, "TTS") is None
    node = _label_node(mgr, "语音合成")
    assert node is not None and sorted(node["children"]) == ["s1", "s2"]
    assert _label_node(mgr, "其他")["children"] == ["s1", "s3"]


def test_rename_label_merges_when_target_label_exists(env):
    """目标标签已存在：按标签集合语义合并去重，不重复写、不丢其它标签。"""
    mgr, _chats = env
    _index_add(mgr, "s1")
    _index_add(mgr, "s2")
    mgr.set_session_labels("s1", ["旧", "新"])   # 本来就两个都有
    mgr.set_session_labels("s2", ["旧"])

    result = mgr.rename_label("旧", "新")

    assert result["changed"] is True
    index = mgr._read_index()
    assert index["s1"]["labels"] == ["新"], "已是目标标签的会话只去掉旧标签"
    assert index["s2"]["labels"] == ["新"]

    assert _label_node(mgr, "旧") is None
    node = _label_node(mgr, "新")
    assert sorted(node["children"]) == ["s1", "s2"]


def test_rename_label_validation_and_unknown_label(env):
    mgr, _chats = env
    _index_add(mgr, "s1", ["TTS"])
    mgr.set_session_labels("s1", ["TTS"])

    with pytest.raises(ValueError):
        mgr.rename_label("", "新名")
    with pytest.raises(ValueError):
        mgr.rename_label("TTS", "")
    # 含分隔符的"一个名字"会被语义拆成多个标签 → 明确拒绝而不是默默取第一段
    with pytest.raises(ValueError):
        mgr.rename_label("TTS", "a,b")
    with pytest.raises(ValueError):
        mgr.rename_label("TTS", "a b")
    with pytest.raises(LabelNotFound):
        mgr.rename_label("不存在", "新名")

    # 校验失败不留半改状态
    assert mgr._read_index()["s1"]["labels"] == ["TTS"]
    assert _label_node(mgr, "TTS")["children"] == ["s1"]

    # clean_label 自身的边界
    assert clean_label("  tts  ") == "tts"
    assert clean_labels(["a，a b", "c"]) == ["a", "b", "c"]


def test_rename_label_same_name_is_idempotent(env):
    mgr, _chats = env
    _index_add(mgr, "s1", ["TTS"])
    mgr.set_session_labels("s1", ["TTS"])
    before = mgr._read_index()

    result = mgr.rename_label("TTS", "TTS")

    assert result["changed"] is False
    assert result["session_ids"] == ["s1"]
    assert mgr._read_index() == before


def test_rename_label_migrates_aggregate_doc(env):
    """聚合文档跟改名走：旧标签不留缓存，新标签读到同一份概要/记忆。"""
    mgr, _chats = env
    _index_add(mgr, "s1", ["旧"])
    _index_add(mgr, "s2", ["旧"])
    mgr.set_session_labels("s1", ["旧"])
    mgr.set_session_labels("s2", ["旧"])
    label_store.save_label_doc({
        "label": "旧", "summary": "S", "memory": "M",
        "session_ids": ["s1", "s2"], "session_count": 2, "config": {},
    })

    result = mgr.rename_label("旧", "新")

    assert result["doc"]["moved"] is True
    assert result["doc"]["removed_old"] is True
    assert result["doc"]["membership_changed"] is False
    assert label_store.load_label_doc("旧") is None
    doc = label_store.load_label_doc("新")
    assert doc["summary"] == "S" and doc["memory"] == "M"
    assert doc["label"] == "新" and doc["renamed_from"] == "旧"
    assert sorted(doc["session_ids"]) == ["s1", "s2"]


def test_rename_label_into_label_without_doc_invalidates_doc(env):
    """目标标签已有会话但没做过聚合：搬过来的摘要覆盖不全，必须作废而非改名续用。

    「旧」有一份摘要（成员只有 s1），「新」已经挂着 s2 却没有文档。把「旧」改名
    成「新」之后，「新」的成员是 {s1, s2}：那份摘要说的还是 s1 一批，换名挂到
    「新」下面就会以偏概全地下一次"发起新会话"注入 system。只刷 session_ids 也
    一样错 —— 摘要正文没变。所以这次不搬家，直接把旧文档删掉，等重算。
    """
    mgr, _chats = env
    _index_add(mgr, "s1", ["旧"])
    _index_add(mgr, "s2", ["新"])
    mgr.set_session_labels("s1", ["旧"])
    mgr.set_session_labels("s2", ["新"])
    label_store.save_label_doc({
        "label": "旧", "summary": "只有 s1 的摘要", "memory": "M",
        "session_ids": ["s1"], "session_count": 1, "config": {},
    })
    assert label_store.load_label_doc("新") is None

    result = mgr.rename_label("旧", "新")

    # 标签本身照常传播：两个会话现在都挂在「新」下
    assert sorted(result["session_ids"]) == ["s1"]
    index = mgr._read_index()
    assert index["s1"]["labels"] == ["新"]
    assert index["s2"]["labels"] == ["新"]
    assert sorted(mgr.sessions_with_label("新")) == ["s1", "s2"]

    # 聚合文档：不迁移、不刷新，直接失效重算
    assert result["doc"]["membership_changed"] is True
    assert result["doc"]["moved"] is False
    assert label_store.load_label_doc("旧") is None
    assert label_store.load_label_doc("新") is None


def test_rename_label_merges_when_both_labels_have_docs(env):
    """SessionManager 层：双方都聚合过 → 两份摘要都盖不住全部成员，双双作废。

    「旧」的摘要只含 s1、「新」的摘要只含 s2，改名并入后「新」的成员是 {s1, s2}：
    留住任何一份，下一次"从标签发起新会话"都会把缺成员的空洞摘要当标签上下文注入
    system（只搬文件、只刷 session_ids 也救不了摘要正文）。这里断言的是**磁盘上两个
    文件真的没了**，不只是返回值里标了 invalidated_target —— 缓存文件留着，重启进程
    后一样会被读到。
    """
    mgr, _chats = env
    _index_add(mgr, "s1", ["旧"])
    _index_add(mgr, "s2", ["新"])
    mgr.set_session_labels("s1", ["旧"])
    mgr.set_session_labels("s2", ["新"])
    label_store.save_label_doc({"label": "旧", "summary": "只有 s1 的摘要",
                                "memory": "M1", "session_ids": ["s1"],
                                "session_count": 1, "config": {}})
    label_store.save_label_doc({"label": "新", "summary": "只有 s2 的摘要",
                                "memory": "M2", "session_ids": ["s2"],
                                "session_count": 1, "config": {}})

    result = mgr.rename_label("旧", "新")

    assert result["doc"] == {"old_existed": True, "target_existed": True,
                             "moved": False, "removed_old": True,
                             "invalidated_target": True,
                             "membership_changed": True}
    assert label_store.load_label_doc("旧") is None
    assert label_store.load_label_doc("新") is None
    assert os.listdir(label_store.labels_dir()) == []
    # 标签传播照常完成；下一次"发起新会话"读不到文档，按 {s1, s2} 重算
    assert sorted(mgr.sessions_with_label("新")) == ["s1", "s2"]


def test_rename_label_doc_target_exists_invalidates_both(tmp_path, monkeypatch):
    """同名合并：两份摘要都不再覆盖全部成员，必须双双作废而不是保留错误旧摘要。

    聚合摘要是「该标签下全部会话」的一次合成产物。把 s1 改名并入已有的「新」之后，
    「新」的旧摘要只描述 s2（缺成员），「旧」描述的名字已经不存在。留下任何一份，下次
    「发起新会话」都会把不完整的摘要当标签上下文注入 system —— 比没有摘要更危险。
    删除后读取落空会自动重算，「生成概要和记忆」也可手动重算。
    """
    monkeypatch.setattr(label_store, "labels_dir",
                        lambda: str(tmp_path / "labels"))
    label_store.save_label_doc({"label": "旧", "summary": "旧摘要",
                                "session_ids": ["s1"], "session_count": 1})
    label_store.save_label_doc({"label": "新", "summary": "新摘要",
                                "session_ids": ["s2"], "session_count": 1})

    result = label_store.rename_label_doc("旧", "新", session_ids=["s1", "s2"])

    assert result == {"old_existed": True, "target_existed": True,
                      "moved": False, "removed_old": True,
                      "invalidated_target": True,
                      "membership_changed": False}
    assert label_store.load_label_doc("旧") is None
    # 关键：目标标签也不能留着那份只描述 s2 的摘要
    assert label_store.load_label_doc("新") is None


def test_rename_label_doc_without_old_doc_invalidates_stale_target(tmp_path, monkeypatch):
    """旧标签没做过聚合：目标摘要现在漏掉了并进来的会话，同样必须作废。"""
    monkeypatch.setattr(label_store, "labels_dir",
                        lambda: str(tmp_path / "labels"))
    label_store.save_label_doc({"label": "新", "summary": "新摘要",
                                "session_ids": ["s2"], "session_count": 1})

    result = label_store.rename_label_doc("旧", "新", session_ids=["s1", "s2"])

    assert result["old_existed"] is False
    assert result["target_existed"] is True
    assert result["moved"] is False
    assert result["invalidated_target"] is True
    assert label_store.load_label_doc("新") is None


def test_rename_label_doc_membership_change_invalidates_even_without_target_doc(
        tmp_path, monkeypatch):
    """成员集合变了就不搬家：目标标签已有会话、只是没做过聚合，也一样作废。

    这条锁的是 store 的契约本身（不依赖 SessionManager 的判定）：换名搬迁会把
    "只有 s1" 的摘要挂到成员已经是 {s1, s2} 的标签下，下一次"发起新会话"就把
    以偏概全的摘要注入 system。刷新 session_ids 也救不了它 —— 摘要正文没变。
    """
    monkeypatch.setattr(label_store, "labels_dir",
                        lambda: str(tmp_path / "labels"))
    label_store.save_label_doc({"label": "旧", "summary": "只有 s1 的摘要",
                                "memory": "M",
                                "session_ids": ["s1"], "session_count": 1})

    result = label_store.rename_label_doc("旧", "新",
                                          session_ids=["s1", "s2"],
                                          membership_changed=True)

    assert result["moved"] is False
    assert result["removed_old"] is True
    assert result["membership_changed"] is True
    assert label_store.load_label_doc("旧") is None
    assert label_store.load_label_doc("新") is None


def test_recompute_after_merge_covers_every_member(env):
    """作废之后必须能重算，而且重算看到的是合并后的全部成员。

    只断言"旧摘要被删"是不够的：那可能顺手把标签上下文永久弄坏。这里在改名并入
    之后重新聚合，喂给模型的素材必须同时包含两个会话的要点，落盘的新文档成员为
    两人 —— 否则"失效"只是把错误摘要换成没有摘要。
    """
    mgr, chats = env
    _index_add(mgr, "s1", ["旧"])
    _index_add(mgr, "s2", ["新"])
    mgr.set_session_labels("s1", ["旧"])
    mgr.set_session_labels("s2", ["新"])
    for sid, text in (("s1", "甲会话的要点"), ("s2", "乙会话的要点")):
        session_dir = chats / sid
        session_dir.mkdir(exist_ok=True)
        (session_dir / "summary.md").write_text(text, encoding="utf-8")
    label_store.save_label_doc({
        "label": "旧", "summary": "只有甲的旧摘要", "memory": "M",
        "session_ids": ["s1"], "session_count": 1, "config": {},
    })

    result = mgr.rename_label("旧", "新")
    assert result["doc"]["moved"] is False
    assert label_store.load_label_doc("新") is None

    prompts: list = []

    def fake_llm(prompt: str) -> str:
        prompts.append(prompt)
        return '{"summary": "甲乙合并要点", "memory": "- 甲\\n- 乙"}'

    doc = label_store.summarize_label("新", mgr, llm=fake_llm)

    assert sorted(doc["session_ids"]) == ["s1", "s2"]
    assert doc["session_count"] == 2
    assert len(prompts) == 1
    assert "甲会话的要点" in prompts[0]
    assert "乙会话的要点" in prompts[0]
    saved = label_store.load_label_doc("新")
    assert saved["summary"] == "甲乙合并要点"
    assert sorted(saved["session_ids"]) == ["s1", "s2"]


# ---------------------------------------------------------------------------
# 2. 来源标签随创建会话的请求持久化
# ---------------------------------------------------------------------------

def test_on_session_created_persists_source_labels(env):
    mgr, _chats = env
    mgr.on_session_created("260101_120000", "首个问题",
                           source_labels=["TTS", "TTS", "", "部署"])

    entry = mgr._read_index()["260101_120000"]
    assert entry["labels"] == ["TTS", "部署"]
    assert _label_node(mgr, "TTS")["children"] == ["260101_120000"]
    assert _label_node(mgr, "部署")["children"] == ["260101_120000"]


def test_on_session_created_without_source_labels_keeps_shape(env):
    mgr, _chats = env
    mgr.on_session_created("260101_120001", "首个问题")
    assert "labels" not in mgr._read_index()["260101_120001"]
    assert _labels_root(mgr)["children"] == []


# ---------------------------------------------------------------------------
# 4. 标签系统消息不能被代理系统消息覆盖
# ---------------------------------------------------------------------------

LABEL_MSG = {"role": "system", "content": "【标签「TTS」聚合概要】\n旧会话要点",
             "label_origin": "TTS"}
AGENT_MSG = {"role": "system", "content": "你是 TTS 维护助手"}
USER_MSG = {"role": "user", "content": "修一下崩溃"}


def test_label_system_message_survives_agent_prompt():
    merged = merge_first_turn_system_messages([LABEL_MSG, USER_MSG], [AGENT_MSG])

    assert [m["role"] for m in merged[:2]] == ["system", "system"]
    assert merged[0]["content"] == "你是 TTS 维护助手", "代理提示词在前"
    assert merged[1]["content"].startswith("【标签「TTS」聚合概要】")
    assert merged[2] is USER_MSG, "非系统消息相对顺序不变"


def test_unmarked_system_message_is_still_replaced_by_agent_prompt():
    """用户自己贴的系统提示词（无来源标记）保持原语义：由代理提示词接管。"""
    merged = merge_first_turn_system_messages(
        [{"role": "system", "content": "用户的旧提示词"}, USER_MSG], [AGENT_MSG])

    assert [m["role"] for m in merged] == ["system", "user"]
    assert merged[0]["content"] == "你是 TTS 维护助手"
    assert "用户的旧提示词" not in json.dumps(merged, ensure_ascii=False)


def test_equivalent_system_messages_are_not_appended_twice():
    """同一份聚合内容重发/代理提示词与请求里残留副本并存时，只保留第一条。"""
    dup_text = merge_first_turn_system_messages(
        [dict(LABEL_MSG), USER_MSG],
        [AGENT_MSG, dict(LABEL_MSG)],
    )
    assert [m["role"] for m in dup_text] == ["system", "system", "user"]
    assert dup_text[1]["content"] == LABEL_MSG["content"]

    # 注入项与请求里残留的副本等价（同模板+同参数）→ 只留注入的那条
    dup_template = merge_first_turn_system_messages(
        [{"role": "system", "content": "", "prompt_template": "tpl",
          "arguments": {"a": 1}, "label_origin": "TTS"}, USER_MSG],
        [{"role": "system", "content": "", "prompt_template": "tpl",
          "arguments": {"a": 1}}],
    )
    systems = [m for m in dup_template if m["role"] == "system"]
    assert len(systems) == 1
    assert not systems[0].get("label_origin")  # 注入项在前，等价副本被丢弃


def test_group_chat_path_keeps_both_messages():
    """群聊：默认提示词与标签聚合内容同时进入 system（调用方已确保没有用户
    自己的系统提示词，否则根本不会注入默认提示词）。"""
    merged = merge_first_turn_system_messages(
        [LABEL_MSG, USER_MSG], [{"role": "system", "content": "群聊默认提示词"}])
    contents = [m.get("content") for m in merged if m["role"] == "system"]
    assert contents == ["群聊默认提示词", LABEL_MSG["content"]]


def test_plain_system_message_suppresses_group_default_prompt():
    """群聊沿用旧语义：已有"别的"系统提示词就不再补默认提示词；但标签来源那条
    不算"别的"，默认提示词仍会注入。"""
    assert not any(is_plain_system_message(m) for m in [LABEL_MSG])
    assert any(is_plain_system_message(m)
               for m in [{"role": "system", "content": "用户模板"}])


def test_origin_helpers():
    assert label_origin_of(LABEL_MSG) == "TTS"
    assert is_label_system_message(LABEL_MSG)
    assert label_origin_of({"role": "system", "content": "x",
                            "label_origin": True}) == "label"
    assert not is_label_system_message(AGENT_MSG)
    assert is_plain_system_message(AGENT_MSG)
    assert not is_plain_system_message({"role": "user", "content": "u"})
    assert not is_plain_system_message({"role": "system", "content": "s",
                                        "label_origin": "TTS"})


def test_protocol_layer_merges_into_single_system_message():
    """合并写入 system：协议层把多条前置 system 并成同一条，两者内容都在。"""
    from runtime.models import Message
    from runtime.protocols import _merge_leading_system_messages

    merged = merge_first_turn_system_messages([LABEL_MSG, USER_MSG], [AGENT_MSG])
    as_objects = [Message.from_dict(m) for m in merged]
    final = _merge_leading_system_messages(as_objects)

    assert [m.role for m in final] == ["system", "user"]
    assert "你是 TTS 维护助手" in final[0].content
    assert "【标签「TTS」聚合概要】" in final[0].content

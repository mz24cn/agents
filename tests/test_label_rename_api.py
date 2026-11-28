"""标签重命名 HTTP API（POST /v1/sessions/labels/rename）的行为与边界。

单元测试（tests/test_label_rename_and_system_merge.py）覆盖 SessionManager 的
语义；这里覆盖 HTTP 层：状态码、请求体别名、以及"改完立刻在 GET /v1/sessions
里看到新标签"的可观察结果。
"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from runtime import label_store
from tests.test_server import (  # noqa: F401
    _get,
    _post,
    registries,
    runtime,
    server,
)


@pytest.fixture()
def isolated_label_docs(tmp_path, monkeypatch):
    """标签聚合文档默认落在真实 DATA_DIR/labels —— 测试里必须挪走。"""
    monkeypatch.setattr(label_store, "labels_dir",
                        lambda: str(tmp_path / "labels"))
    return tmp_path / "labels"


def _seed(srv, sid: str, labels) -> None:
    index = srv._session_manager._read_index()
    entry = {"session_id": sid, "title": f"t-{sid}",
             "created_at": "2026-11-20T10:00:00", "last_inference_at": ""}
    if labels:
        entry["labels"] = list(labels)
    index[sid] = entry
    srv._session_manager._write_index(index)


def _labels_of(body: dict, sid: str) -> list:
    entry = next((s for s in body["sessions"] if s.get("session_id") == sid), {})
    return entry.get("labels") or []


def test_rename_endpoint_propagates_to_all_sessions(server):
    srv = server
    _seed(srv, "261120_100000", ["旧标签", "其它"])
    _seed(srv, "261120_100001", ["旧标签"])
    _seed(srv, "261120_100002", ["其它"])

    status, body = _post(srv, "/v1/sessions/labels/rename",
                         {"from": "旧标签", "to": "新标签"})
    assert status == 200, body
    assert body["changed"] is True
    assert body["session_count"] == 2
    assert sorted(body["session_ids"]) == ["261120_100000", "261120_100001"]

    _, listing = _get(srv, "/v1/sessions")
    assert _labels_of(listing, "261120_100000") == ["新标签", "其它"]
    assert _labels_of(listing, "261120_100001") == ["新标签"]
    assert _labels_of(listing, "261120_100002") == ["其它"]


def test_rename_endpoint_accepts_old_new_aliases(server):
    srv = server
    _seed(srv, "261120_100100", ["A"])
    status, body = _post(srv, "/v1/sessions/labels/rename",
                         {"old_label": "A", "new_label": "B"})
    assert status == 200, body
    assert body["from"] == "A" and body["to"] == "B"
    _, listing = _get(srv, "/v1/sessions")
    assert _labels_of(listing, "261120_100100") == ["B"]


def test_rename_endpoint_merges_when_target_exists(server, isolated_label_docs):
    srv = server
    _seed(srv, "261120_100200", ["旧", "新"])
    _seed(srv, "261120_100201", ["旧"])
    # 两个标签各有一份聚合概要。合并后「新」的成员集合变了，那份只覆盖单个
    # 成员的摘要不能再当权威用 —— 两边都必须作废重算。
    label_store.save_label_doc({"label": "旧", "summary": "旧摘要",
                                "session_ids": ["261120_100201"],
                                "session_count": 1})
    label_store.save_label_doc({"label": "新", "summary": "新摘要",
                                "session_ids": ["261120_100200"],
                                "session_count": 1})

    status, body = _post(srv, "/v1/sessions/labels/rename",
                         {"from": "旧", "to": "新"})
    assert status == 200, body
    assert body["doc"]["invalidated_target"] is True
    assert label_store.load_label_doc("旧") is None
    assert label_store.load_label_doc("新") is None

    _, listing = _get(srv, "/v1/sessions")
    assert _labels_of(listing, "261120_100200") == ["新"]
    assert _labels_of(listing, "261120_100201") == ["新"]

    # 列表里新标签下已挂着两个会话，但聚合文档为空：从新标签新建会话会先重新
    # 聚合，而不是把"新摘要"注入 system。
    status, doc = _get(srv, "/v1/sessions/labels?label="
                          + urllib.parse.quote("新"))
    assert status == 200, doc
    assert doc["doc"] is None


def test_rename_endpoint_into_label_without_doc_invalidates_doc(
        server, isolated_label_docs):
    """并入"有会话但从没聚合过"的标签：换名搬迁会以偏概全，必须作废。

    「旧」做过聚合（成员只有 261120_100400），「新」已经挂着另一个会话但没有
    文档 —— 目标侧没有文档不代表成员集合没变。把「旧」的摘要搬去「新」，下次
    "从标签发起会话"就会把只覆盖一批会话的摘要当标签上下文注入 system。
    """
    srv = server
    _seed(srv, "261120_100400", ["旧"])
    _seed(srv, "261120_100401", ["新"])
    label_store.save_label_doc({"label": "旧", "summary": "只有 100400 的摘要",
                                "session_ids": ["261120_100400"],
                                "session_count": 1})

    status, body = _post(srv, "/v1/sessions/labels/rename",
                         {"from": "旧", "to": "新"})
    assert status == 200, body
    assert body["doc"]["membership_changed"] is True
    assert body["doc"]["moved"] is False
    assert label_store.load_label_doc("旧") is None
    assert label_store.load_label_doc("新") is None

    # 作废的是缓存，标签传播本身照常完成
    _, listing = _get(srv, "/v1/sessions")
    assert _labels_of(listing, "261120_100400") == ["新"]
    assert _labels_of(listing, "261120_100401") == ["新"]


def test_rename_endpoint_validation_and_not_found(server):
    srv = server
    _seed(srv, "261120_100300", ["存在"])

    # 缺目标标签
    status, body = _post(srv, "/v1/sessions/labels/rename", {"from": "存在"})
    assert status == 400, body
    # 空标签名
    status, _ = _post(srv, "/v1/sessions/labels/rename", {"from": "", "to": "x"})
    assert status == 400
    # 一个名字里带分隔符 → 语义会被拆成多个标签，明确拒绝
    status, _ = _post(srv, "/v1/sessions/labels/rename",
                      {"from": "存在", "to": "x y"})
    assert status == 400
    # 原标签不存在
    status, body = _post(srv, "/v1/sessions/labels/rename",
                         {"from": "不存在", "to": "新"})
    assert status == 404, body

    # 失败的尝试都不留半改状态
    _, listing = _get(srv, "/v1/sessions")
    assert _labels_of(listing, "261120_100300") == ["存在"]


def test_rename_endpoint_moves_label_doc(server, isolated_label_docs):
    """改名后旧标签的聚合文档不再可读，新标签读到同一份概要/记忆。"""
    srv = server
    _seed(srv, "261120_100400", ["旧"])
    label_store.save_label_doc({"label": "旧", "summary": "S", "memory": "M",
                                "session_ids": ["261120_100400"],
                                "session_count": 1})

    status, body = _post(srv, "/v1/sessions/labels/rename",
                         {"from": "旧", "to": "新"})
    assert status == 200, body
    assert body["doc"]["moved"] is True
    assert label_store.load_label_doc("旧") is None

    status, doc = _get(srv, "/v1/sessions/labels?label="
                          + urllib.parse.quote("新"))
    assert status == 200, doc
    assert doc["doc"] is not None
    assert doc["doc"]["summary"] == "S" and doc["doc"]["memory"] == "M"

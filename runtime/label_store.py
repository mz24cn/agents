"""标签聚合层：一个标签下全部会话的合并概要、记忆与相关配置设定。

会话标签是事实数据：index.json 条目的 ``labels`` 列表（SessionManager.
set_session_labels 维护），tree.json 的标签根（type=labels）由它派生。
标签级的聚合产物（概要/记忆/配置）数据量较大，不放 tree.json，而是每个
标签一个 JSON 文件放在 ``DATA_DIR/labels/`` 下。

聚合语义（对应侧栏标签节点右键菜单）：
  * 合并该标签下每个会话自身的 summary.md 与 memory.md；未生成的会话以
    conversation.json 首尾摘录代替；交给 SUMMARY 模型合成一份标签级
    概要 + 记忆。
  * 统计这些会话的执行环境、工作区、模型、AI代理、工具集，各取出现次数
    最多的一份作为标签的"相关配置设定"（工具/代理按整组取值统计）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime

from runtime.common import DATA_DIR

logger = logging.getLogger(__name__)

#: 单会话 summary+memory 注入合并语料的字符上限。
SESSION_BLOCK_LIMIT = 6000
#: 未生成概要的会话，conversation 首尾各截取的字符数。
HEAD_TAIL_LIMIT = 1500
#: 合并语料总上限（超出后按块截断，保证大标签也能一次请求）。
MERGED_LIMIT = 60000


def labels_dir() -> str:
    return os.path.join(DATA_DIR, "labels")


def label_doc_path(label: str) -> str:
    """标签聚合文档路径：可读名 + 标签原文哈希，避免清洗后互相覆盖。"""
    raw = str(label or "").strip()
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", raw)[:60].strip("._") or "label"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return os.path.join(labels_dir(), f"{safe}_{digest}.json")


def load_label_doc(label: str) -> dict | None:
    try:
        with open(label_doc_path(label), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_label_doc(doc: dict) -> str:
    path = label_doc_path(str(doc.get("label") or ""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def delete_label_doc(label: str) -> bool:
    """删除某标签的聚合文档；不存在返回 False（幂等）。"""
    try:
        os.remove(label_doc_path(label))
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("delete_label_doc 失败 (label=%s): %s", label, exc)
        return False


def rename_label_doc(old_label: str, new_label: str, session_ids=None,
                     membership_changed: bool = False) -> dict:
    """标签改名后迁移/失效聚合文档，绝不让错误的旧摘要继续被读到。

    聚合文档（``summary`` / ``memory`` / ``config``）是对"该标签下全部会话"的
    一次 LLM 合成产物，它的**成员集合**就是它成立的前提。改名传播之后成员集合
    可能变化，前提变了的摘要就是错的摘要 —— 留着它比没有更危险（"发起新会话"
    会把它当标签上下文注入 system）。因此规则按成员集合是否改变来分：

      * 成员集合没变（目标标签原本没有别的会话，``membership_changed=False``）
        → 只是标签名换了：换名搬迁，并按改名后该标签的实际成员刷新
        ``session_ids``/``session_count``。
      * 成员集合变了（目标标签本来就有别的会话，无论它有没有文档）→ 改名的
        会话并进来后，哪一份摘要都覆盖不全：两边都**失效**（删除），下一次
        "发起新会话"读不到文档会自动重算，"生成概要和记忆"也可手动重算。
        合并两份 LLM 产物只会产出一份没人核对过的摘要，不可取；只搬文档、只刷
        id 列表同样不可取 —— 摘要说的还是原来那批会话。
      * 只有新文档（旧标签没做过聚合）→ 同样理由：新文档现在漏掉了改名并入的
        会话，必须失效。

    返回 ``{old_existed, target_existed, moved, removed_old,
    invalidated_target, membership_changed}``。
    """
    old = str(old_label or "").strip()
    new = str(new_label or "").strip()
    result = {"old_existed": False, "target_existed": False,
              "moved": False, "removed_old": False,
              "invalidated_target": False,
              "membership_changed": bool(membership_changed)}
    if not old or not new or old == new:
        return result
    old_doc = load_label_doc(old)
    result["old_existed"] = old_doc is not None
    target_doc = load_label_doc(new)
    result["target_existed"] = target_doc is not None
    if old_doc is None:
        # 旧标签没有聚合文档：改名并入的会话没有任何摘要支撑，目标文档却已经
        # 不再覆盖全部成员 —— 同样必须失效，不能继续端出缺成员的旧摘要。
        if result["target_existed"]:
            result["invalidated_target"] = delete_label_doc(new)
        return result
    if result["target_existed"] or membership_changed:
        # 成员集合变了：目标标签本来就有别的会话时，手心里这份摘要覆盖不全，
        # 换个名字挂着它同样是错误摘要（只刷 session_ids 也骗不过人：摘要说的
        # 还是原来那批会话）。删掉，让下一次读取触发重算。
        if result["target_existed"]:
            result["invalidated_target"] = delete_label_doc(new)
        result["removed_old"] = delete_label_doc(old)
        return result
    old_doc["label"] = new
    old_doc["renamed_from"] = old
    if session_ids is not None:
        sids = [str(s) for s in session_ids]
        old_doc["session_ids"] = sids
        old_doc["session_count"] = len(sids)
    old_doc["updated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds")
    save_label_doc(old_doc)
    result["moved"] = True
    result["removed_old"] = delete_label_doc(old)
    return result


def label_session_ids(session_manager, label: str) -> list[str]:
    """该标签在 tree.json 标签根下的会话；根缺失时回退扫描 index.json。"""
    for top in session_manager.get_category_tree().get("tree") or []:
        if not isinstance(top, dict) or str(top.get("type") or "") != "labels":
            continue
        for node in top.get("children") or []:
            if isinstance(node, dict) and str(node.get("name") or "") == label:
                return [str(c) for c in node.get("children") or []
                        if isinstance(c, str)]
    out: list[str] = []
    for sid, entry in (session_manager._read_index() or {}).items():
        if not isinstance(entry, dict):
            continue
        labels = [str(x).strip() for x in (entry.get("labels") or [])]
        if label in labels:
            out.append(str(sid))
    return out


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            x.get("text", "") for x in content
            if isinstance(x, dict) and isinstance(x.get("text"), str)
        )
    return ""


def _session_block(chats_dir: str, sid: str, title: str) -> str:
    """单会话素材：概要+记忆；未生成则截取 conversation 首尾。"""
    session_dir = os.path.join(chats_dir, sid)
    parts: list[str] = []
    for fname, key in (("summary.md", "概要"), ("memory.md", "记忆")):
        try:
            with open(os.path.join(session_dir, fname), "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
        if text:
            parts.append(f"{key}：\n{text}")
    body = "\n\n".join(parts)
    if not body:
        try:
            with open(os.path.join(session_dir, "conversation.json"),
                      "r", encoding="utf-8") as fh:
                conv = json.load(fh)
        except (OSError, json.JSONDecodeError):
            conv = {}
        texts = []
        for msg in conv.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            text = _message_text(msg.get("content")).strip()
            if text:
                texts.append(f"[{msg.get('role')}] {text}")
        joined = "\n".join(texts)
        if len(joined) > HEAD_TAIL_LIMIT * 2:
            joined = (joined[:HEAD_TAIL_LIMIT] + "\n……（中间省略）……\n"
                      + joined[-HEAD_TAIL_LIMIT:])
        body = "（该会话未生成概要/记忆，以下为对话首尾摘录）\n" + joined
    if len(body) > SESSION_BLOCK_LIMIT:
        body = body[:SESSION_BLOCK_LIMIT] + "……"
    return f"### 会话 {sid}（{title or sid}）\n{body}"


def aggregate_config(chats_dir: str, session_ids: list[str]) -> dict:
    """各维度取出现次数最多的一份设置（多数即"该标签的常用配置"）。

    工具与 AI 代理按整组统计（会话的选择是一组 id），最常见的一组胜出。
    """
    env_counter: Counter = Counter()
    ws_counter: Counter = Counter()
    model_counter: Counter = Counter()
    agent_counter: Counter = Counter()
    tool_counter: Counter = Counter()
    for sid in session_ids:
        try:
            with open(os.path.join(chats_dir, sid, "conversation.json"),
                      "r", encoding="utf-8") as fh:
                meta = (json.load(fh) or {}).get("meta") or {}
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            meta = {}
        env_counter[str(meta.get("remote_env") or "").strip()] += 1
        from runtime.category_tree_builder import _normalize_workspace
        ws = _normalize_workspace(meta.get("workspace"))
        if ws:
            ws_counter[ws] += 1
        model = str(meta.get("model_id") or "").strip()
        if model:
            model_counter[model] += 1
        agents = tuple(str(x) for x in (meta.get("agent_ids") or []) if str(x))
        if agents:
            agent_counter[agents] += 1
        tools = tuple(str(x) for x in (meta.get("tool_ids") or []) if str(x))
        if tools:
            tool_counter[tools] += 1

    def top(counter: Counter, default=""):
        return counter.most_common(1)[0][0] if counter else default

    return {
        "remote_env_id": top(env_counter),
        "workspace": top(ws_counter),
        "model_id": top(model_counter),
        "agent_ids": list(top(agent_counter, ())),
        "tool_ids": list(top(tool_counter, ())),
    }


def _parse_llm_pair(raw: str) -> tuple[str, str]:
    """宽松解析 {"summary":..., "memory":...}；失败时整段作为概要。"""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return (str(obj.get("summary") or "").strip(),
                    str(obj.get("memory") or "").strip())
    return text, ""


def summarize_label(label: str, session_manager, llm=None) -> dict:
    """基于标签下全部会话生成聚合概要/记忆与相关配置，写入 labels 目录。

    ``llm`` 缺省时取 SUMMARY 模型（与标题/分类命名同一模型）。返回完整
    聚合文档（前端"生成新会话"直接消费）。
    """
    label = str(label or "").strip()
    if not label:
        raise ValueError("label is required")
    sids = label_session_ids(session_manager, label)
    if not sids:
        raise ValueError(f"no sessions carry label: {label!r}")
    index = session_manager._read_index() or {}
    chats_dir = session_manager.chats_dir
    blocks = [
        _session_block(chats_dir, sid,
                       str((index.get(sid) or {}).get("title") or sid))
        for sid in sids
    ]
    merged = "\n\n".join(blocks)
    if len(merged) > MERGED_LIMIT:
        merged = merged[:MERGED_LIMIT] + "\n……（内容过长，已截断）……"

    summary, memory = "", ""
    if llm is None:
        from runtime.category_tree_builder import make_summary_llm
        llm = make_summary_llm(session_manager)
    if llm is not None:
        prompt = (
            f"以下是带标签「{label}」的 {len(sids)} 个会话各自的概要、记忆"
            "（未生成的会话以对话首尾摘录代替）。请把它们合并成一份该标签级别的"
            "总概要（覆盖共同主题、演进时间线、关键结论）和一份总记忆（要点条目，"
            "去重）。只输出一个 JSON 对象，"
            f'格式为 {{"summary": "...", "memory": "..."}}。\n\n{merged}'
        )
        try:
            summary, memory = _parse_llm_pair(llm(prompt))
        except Exception as exc:
            logger.warning("label summarize: LLM 失败 (label=%s): %s", label, exc)
    if not summary:
        # LLM 不可用/失败：退化为直接拼接，聚合仍可用。
        summary = merged

    doc = {
        "label": label,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_ids": sids,
        "session_count": len(sids),
        "summary": summary,
        "memory": memory,
        "config": aggregate_config(chats_dir, sids),
    }
    save_label_doc(doc)
    return doc

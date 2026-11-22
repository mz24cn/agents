"""SessionManager — 会话管理模块。

负责读取 chats_dir 目录下的历史会话列表，以及读取指定会话的 conversation.json 数据。
同时维护 index.json 索引文件，记录每个会话的元信息。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import Counter
from typing import Callable, Optional

from runtime.common import parse_search_query, search_files

logger = logging.getLogger("runtime.session_manager")

# 标题生成请求中附带的分类目录上限（避免 prompt 过长）
MAX_CATEGORY_LIST = 300


def _iter_string_children(node: dict):
    """深度优先产出 node 子树中所有字符串叶子（会话 ID）。"""
    stack = [node]
    while stack:
        for k in stack.pop().get("children") or []:
            if isinstance(k, dict):
                stack.append(k)
            else:
                yield k


def _parse_title_and_category(text: str) -> tuple[str, str]:
    """解析标题生成请求的模型回复，返回 (title, category)。

    兼容三种回复形态：
      * 纯标题文本（旧格式，category 为空）；
      * 严格 JSON 对象 {"title": ..., "category": ...}；
      * JSON 对象被代码块围栏或多余文字包裹（截取首个平衡的 {...}）。
    """
    if not text:
        return "", ""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()

    def _try(s: str):
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(data, dict) and ("title" in data or "category" in data):
            return (
                str(data.get("title") or "").strip(),
                str(data.get("category") or "").strip(),
            )
        return None

    parsed = _try(t)
    if parsed is None and "{" in t:
        start = t.index("{")
        for end in range(len(t) - 1, start, -1):
            if t[end] != "}":
                continue
            parsed = _try(t[start:end + 1])
            if parsed is not None:
                break
    if parsed is not None:
        title, category = parsed
        if not title:
            title = t
        return title, category
    # 旧格式：整个回复就是标题
    return t, ""


class SessionManager:
    """管理历史会话的列举与读取，以及 index.json 索引维护。

    Args:
        chats_dir: 存储历史会话的根目录路径，每个子目录对应一个会话。
        infer_fn: 可选的推理函数，用于生成会话标题。
    """

    def __init__(self, chats_dir: str, infer_fn: Optional[Callable] = None,
                 broadcast_fn: Optional[Callable] = None,
                 model_registry: Optional[object] = None) -> None:
        self._chats_dir = chats_dir
        self._infer_fn = infer_fn
        self._broadcast_fn = broadcast_fn
        # Optional model registry used to resolve SUMMARY_MODEL_ID by ID/label.
        self._model_registry = model_registry

    # ------------------------------------------------------------------
    # 内部属性
    # ------------------------------------------------------------------

    @property
    def _index_path(self) -> str:
        return os.path.join(self._chats_dir, "index.json")

    @property
    def _category_tree_path(self) -> str:
        return os.path.join(self._chats_dir, "tree.json")

    def get_category_tree(self) -> dict:
        """Return the experimental session category tree stored beside index.json."""
        try:
            with open(self._category_tree_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"version": 1, "experimental": True, "tree": []}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 tree.json 失败: %s", exc)
            return {"version": 1, "experimental": True, "tree": []}
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            logger.warning("tree.json 格式无效，返回空分类树")
            return {"version": 1, "experimental": True, "tree": []}
        return data

    def save_category_tree(self, data: dict) -> bool:
        """原子写回整个 tree.json 文档（终端层增删等就地编辑使用）。"""
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            return False
        try:
            fd, tmp = tempfile.mkstemp(prefix=".tree.", suffix=".tmp", dir=self._chats_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self._category_tree_path)
        except OSError as exc:
            logger.warning("save_category_tree: 写入 tree.json 失败: %s", exc)
            return False
        return True

    def list_sessions_by_category(self, category: str) -> list[dict]:
        """Return indexed sessions attached to a terminal category path.

        ``category`` is a slash-separated sequence of numeric node IDs, such as
        ``1/2`` or ``1/2/3``. Terminal categories may occur at different depths.
        Single-membership mode (tree.json v5): each session belongs to exactly
        one terminal category; older data with repeated mounts is still readable.

        Ordering is the same for every leaf kind（主题分类 / 工作区目录 / 历史
        月份 / 标签 / 遗留 recent 根）：newest final session time first, so each
        leaf's session list stays in sync with the Sidebar's default 最近会话 list.
        """
        parts = [part for part in str(category or "").strip("/").split("/") if part]
        if not parts or any(not part.isdigit() for part in parts):
            raise ValueError("category must be a slash-separated numeric path")

        nodes = self.get_category_tree().get("tree") or []
        node = None
        for part in parts:
            node = next(
                (item for item in nodes if isinstance(item, dict) and str(item.get("id")) == part),
                None,
            )
            if node is None:
                raise ValueError(f"category not found: {category}")
            nodes = [item for item in node.get("children", []) if isinstance(item, dict)]

        raw_children = node.get("children", []) if isinstance(node, dict) else []
        session_ids = [item for item in raw_children if isinstance(item, str)]
        if any(isinstance(item, dict) for item in raw_children):
            raise ValueError(f"category is not a terminal node: {category}")

        index = self._read_index()
        sessions = []
        for session_id in session_ids:
            entry = index.get(session_id)
            if isinstance(entry, dict):
                sessions.append(dict(entry))
            elif os.path.isfile(os.path.join(self._chats_dir, session_id, "conversation.json")):
                sessions.append({"session_id": session_id, "title": session_id})
        # 所有末级节点（主题分类叶子 / 工作区叶子 / 历史月份 / 标签 / 遗留 recent
        # 根）一律按「最终会话时间」倒序，与 list_sessions() 的 last_inference_at
        # 语义同序：主题、工作区、标签只是筛选维度，用户在每个类别里同样希望先看到
        # 最近活动的会话。时间缺失退回 created_at，两者都没有的落在末尾；排序是稳定
        # 的，同一时刻沿用 tree.json 里的挂载顺序（标签节点天然是新会话在前）。
        sessions.sort(
            key=lambda item: str(
                item.get("last_inference_at") or item.get("created_at") or ""
            ),
            reverse=True,
        )
        return sessions

    def category_tree_path(self) -> str:
        """Return the absolute tree.json path for diagnostics and tooling."""
        return self._category_tree_path

    @property
    def chats_dir(self) -> str:
        """会话数据目录（包含 index.json、tree.json 与各会话子目录）。"""
        return self._chats_dir

    @property
    def inference_callable(self):
        """推理函数（标题生成、分类命名等），未设置时为 None。"""
        return self._infer_fn

    @property
    def model_registry(self):
        """模型注册表（用于解析 SUMMARY_MODEL_ID label），未设置时为 None。"""
        return self._model_registry

    def category_tree_exists(self) -> bool:
        """tree.json 当前是否存在于磁盘。"""
        return os.path.isfile(self._category_tree_path)

    def terminal_categories(self) -> list[dict]:
        """列出 tree.json 的全部末级分类。

        Returns:
            [{"path": "1/2/3", "label": "A/B/C"}, ...]，
            path 为节点 ID 以 / 连接，label 为节点名称以 / 连接。
            tree.json 缺失或无效时返回空列表。
        """
        out: list[dict] = []

        def walk(children, path_ids: list[str], path_names: list[str]) -> None:
            for item in children:
                if not isinstance(item, dict):
                    continue
                # 森林中只有分类节点参与"末级分类"枚举（工作区/终端根不参与）。
                if str(item.get("type") or "category") != "category":
                    continue
                kids = item.get("children", [])
                node_id = str(item.get("id", ""))
                name = str(item.get("name") or "")
                if any(isinstance(k, dict) for k in kids):
                    walk(kids, path_ids + [node_id], path_names + [name])
                else:
                    out.append({
                        "path": "/".join(path_ids + [node_id]),
                        "label": "/".join(path_names + [name]),
                    })

        walk(self.get_category_tree().get("tree") or [], [], [])
        return out

    def attach_session_to_category(self, session_id: str, category_path: str) -> bool:
        """将会话挂载到 tree.json 中一个既有的末级分类（单分类：挂载即移动）。

        会话至多属于一个末级分类：挂载前先从其它末级分类移除该会话，再挂到
        目标分类；已在目标分类时不做任何修改并返回 False。挂载成功后沿祖先
        链重算 session_count 并原子写入 tree.json。
        文件缺失、路径非法或写入失败时记录日志并返回 False，
        绝不影响标题生成主流程。
        """
        try:
            with open(self._category_tree_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("attach_session_to_category: 读取 tree.json 失败: %s", exc)
            return False
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            return False

        parts = [p for p in str(category_path or "").strip("/").split("/") if p]
        if not parts or any(not p.isdigit() for p in parts):
            logger.warning("attach_session_to_category: 非法分类路径 %r", category_path)
            return False

        node = None
        nodes = data["tree"]
        for part in parts:
            node = next(
                (item for item in nodes if isinstance(item, dict) and str(item.get("id")) == part),
                None,
            )
            if node is None:
                logger.warning("attach_session_to_category: 分类路径不存在 %r", category_path)
                return False
            nodes = [item for item in node.get("children", []) if isinstance(item, dict)]

        if str(node.get("type") or "category") != "category":
            logger.warning("attach_session_to_category: %r 不是分类节点（type=%s）",
                           category_path, node.get("type"))
            return False
        children = node.get("children", [])
        if not isinstance(children, list) or any(isinstance(c, dict) for c in children):
            logger.warning("attach_session_to_category: %r 不是末级分类", category_path)
            return False
        if session_id in children:
            return False

        # 单分类模式：先挂到目标末级分类，再从其它末级分类解除该会话的
        # 挂载（旧多分类数据的重复挂载也会在这里被顺带清理）。先挂后摘还能
        # 保证目标分类在随后的空分类剪枝中始终非空。
        children.append(session_id)
        node["children"] = children
        moved = False

        def detach_category_leaf(n: dict) -> None:
            nonlocal moved
            if n is node or str(n.get("type") or "category") != "category":
                return
            kids = n.get("children", [])
            if any(isinstance(k, dict) for k in kids):
                for k in kids:
                    if isinstance(k, dict):
                        detach_category_leaf(k)
            elif session_id in kids:
                n["children"] = [c for c in kids if c != session_id]
                moved = True

        for top in data["tree"]:
            if isinstance(top, dict):
                detach_category_leaf(top)

        # 被搬空的末级分类（及其因此变空的父分类）一并剪除，避免目录里
        # 残留 0 会话的空分类；与 remove_session_from_tree 的策略一致。
        def prune_empty(n: dict) -> bool:
            kids = n.get("children", [])
            if any(isinstance(k, dict) for k in kids):
                kept = [k for k in kids if not isinstance(k, dict)]
                kept += [k for k in kids if isinstance(k, dict) and prune_empty(k)]
                n["children"] = kept
                return len(kept) > 0
            return len([c for c in kids if isinstance(c, str)]) > 0

        if moved:
            for top in data["tree"]:
                if isinstance(top, dict) and str(top.get("type") or "category") == "category":
                    prune_empty(top)

        # 主题挂载之后同步事实根：会话若还没出现在工作区目录/会话历史里就地补挂
        # （新会话的即时入口在 on_session_created → ensure_session_facts，这里兜底），
        # 再把工作区目录下的主题分类副本从"未分类"迁移到该分类的镜像路径。
        try:
            self._apply_session_facts(data, session_id)
            self._mirror_category_in_workspace(data, session_id, parts)
            # 搬移/剪空之后主题侧与工作区副本侧都可能变小——顺手做分类合并。
            self._collapse_category_copies(data)
        except Exception as exc:
            logger.warning(
                "attach_session_to_category: 同步工作区副本失败 (session=%s): %s",
                session_id, exc,
            )

        self._fill_and_stats(data)

        try:
            fd, tmp = tempfile.mkstemp(prefix=".tree.", suffix=".tmp", dir=self._chats_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self._category_tree_path)
        except OSError as exc:
            logger.warning("attach_session_to_category: 写入 tree.json 失败: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # 事实根（工作区目录 / 会话历史月份）的增量维护
    # ------------------------------------------------------------------

    def _fill_and_stats(self, data: dict) -> None:
        """重算全部根 subtree 的 session_count，并刷新工作区/历史/成员统计块。"""
        def fill(n: dict) -> set:
            kids = n.get("children", [])
            if any(isinstance(k, dict) for k in kids):
                found: set = set()
                for k in kids:
                    if isinstance(k, dict):
                        found |= fill(k)
                n["session_count"] = len(found)
                return found
            ids = {c for c in kids if isinstance(c, str)}
            n["session_count"] = len(ids)
            return ids

        roots = [r for r in data.get("tree") or [] if isinstance(r, dict)]
        for top in roots:
            fill(top)

        for top in roots:
            kind = str(top.get("type") or "")
            kids = [c for c in top.get("children") or [] if isinstance(c, dict)]
            if kind == "workspace":
                env_nodes = [c for c in kids if str(c.get("type") or "") == "environment"]
                ws_nodes = 0

                def count_ws(node: dict) -> None:
                    nonlocal ws_nodes
                    for c in node.get("children") or []:
                        if isinstance(c, dict):
                            if str(c.get("type") or "") == "workspace":
                                ws_nodes += 1
                            count_ws(c)

                count_ws(top)
                stats = top.setdefault("stats", {})
                stats["workspace_count"] = ws_nodes
                stats["environment_count"] = len(env_nodes) or 1
            elif kind == "history":
                stats = top.setdefault("stats", {})
                stats["month_count"] = len(
                    [c for c in kids if str(c.get("type") or "") == "month"])
            elif kind == "labels":
                stats = top.setdefault("stats", {})
                stats["label_count"] = len(
                    [c for c in kids if str(c.get("type") or "") == "label"])

        # 文档级统计（分类子树 membership；工作区/环境/月份节点数量）
        stats = data.setdefault("stats", {})
        membership: Counter = Counter()
        terminals = 0

        def walk_cat(node: dict) -> None:
            nonlocal terminals
            if str(node.get("type") or "category") != "category":
                return
            kids = node.get("children", [])
            if all(not isinstance(c, dict) for c in kids):
                terminals += 1
                for sid in kids:
                    membership[str(sid)] += 1
            else:
                for c in kids:
                    if isinstance(c, dict):
                        walk_cat(c)

        for top in roots:
            if str(top.get("type") or "category") == "category":
                walk_cat(top)
        stats["attached_conversation_count"] = len(membership)
        stats["terminal_category_count"] = terminals
        stats["membership_edge_count"] = sum(membership.values())
        stats["multi_category_conversation_count"] = sum(v > 1 for v in membership.values())
        stats["max_categories_per_conversation"] = (
            max(membership.values()) if membership else 0)

        def stat_of(kind: str, key: str, default=0):
            top = next((r for r in roots if str(r.get("type") or "") == kind), None)
            return (top.get("stats") or {}).get(key, default) if top else default

        stats["workspace_node_count"] = stat_of("workspace", "workspace_count")
        stats["environment_node_count"] = stat_of("workspace", "environment_count")
        stats["month_node_count"] = stat_of("history", "month_count")
        stats["label_node_count"] = stat_of("labels", "label_count")

    def _session_fact_fields(self, session_id: str) -> tuple:
        """读取会话事实字段：(归一化 workspace, 执行环境 key, 月份 YYYYMM)。"""
        from runtime.category_tree_builder import (
            _normalize_workspace, month_key, normalize_environment)
        meta: dict = {}
        conv_path = os.path.join(self._chats_dir, session_id, "conversation.json")
        try:
            with open(conv_path, "r", encoding="utf-8") as fh:
                meta = (json.load(fh) or {}).get("meta") or {}
        except (OSError, json.JSONDecodeError):
            meta = {}
        created = str(meta.get("created_at") or "")
        if not created:
            created = str((self._read_index().get(session_id) or {}).get("created_at") or "")
        return (
            _normalize_workspace(meta.get("workspace")),
            normalize_environment(meta.get("remote_env")),
            month_key(created),
        )

    def _apply_session_facts(self, data: dict, session_id: str,
                             fields: Optional[tuple] = None) -> bool:
        """把会话补挂进工作区目录（未分类兜底）、会话历史月份节点与标签根，
        就地平改 data。

        只处理 v6+ 森林（工作区目录挂分类副本、未分类兜底）；v5 及更老的
        workspace 节点 children 是会话 ID 列表，直接追加字符串。返回是否有修改。

        标签根（v8）是增量**补齐**语义：mount index.json 条目里已有的 labels；
        移除标签由 set_session_labels 显式同步（它同时改 index 与树）。

        扩展契约：这是所有挂载路径（创建 ensure_session_facts、主题 attach、
        自动压缩与手动重新生成共用的 update_index 漏斗）唯一的事实根入口。
        以后新增任何分类体系（新的森林根）时，必须在这里登记该根的增量补挂
        逻辑，并让 tests/test_session_fact_roots.py 的按根类型不变量测试覆盖
        它——保证自动与手动生成的"所属目录"始终一致。
        """
        from runtime.category_tree_builder import (
            TYPE_CATEGORY, TYPE_ENVIRONMENT, TYPE_HISTORY, TYPE_LABEL,
            TYPE_LABELS, TYPE_MONTH, TYPE_WORKSPACE, UNKNOWN_CATEGORY_NAME,
            UNKNOWN_WORKSPACE_NAME, _workspace_display_name,
            environment_display_name)

        sid = str(session_id)
        workspace, env_key, month = fields or self._session_fact_fields(sid)
        changed = False
        roots = [r for r in data.get("tree") or [] if isinstance(r, dict)]

        def subtree_has_sid(node: dict) -> bool:
            stack = [node]
            while stack:
                for k in stack.pop().get("children") or []:
                    if isinstance(k, dict):
                        stack.append(k)
                    elif str(k) == sid:
                        return True
            return False

        def next_id(nodes: list) -> int:
            return max([int(n.get("id") or 0) for n in nodes], default=0) + 1

        # ---- 会话历史：YYYYMM 月份节点（时间倒序），新会话最近活动 → 头部 ----
        hist = next((r for r in roots if str(r.get("type") or "") == TYPE_HISTORY), None)
        if hist is not None and month:
            months = [c for c in hist.get("children") or [] if isinstance(c, dict)]
            target = next((c for c in months if str(c.get("name")) == month), None)
            if target is None:
                node = {"id": next_id(months), "name": month, "type": TYPE_MONTH,
                        "session_count": 1, "children": [sid], "stats": {}}
                idx = next((i for i, c in enumerate(months)
                            if str(c.get("name") or "") < month), len(months))
                hist.setdefault("children", []).insert(idx, node)
                changed = True
            elif sid not in [str(c) for c in target.get("children") or []]:
                target.setdefault("children", []).insert(0, sid)
                changed = True

        # ---- 工作区目录：执行环境层（若存在）→ workspace 节点 → 未分类 ----
        ws_root = next((r for r in roots if str(r.get("type") or "") == TYPE_WORKSPACE),
                       None)
        if ws_root is not None and not subtree_has_sid(ws_root):
            kids = [c for c in ws_root.get("children") or [] if isinstance(c, dict)]
            if kids and all(str(c.get("type") or "") == TYPE_ENVIRONMENT for c in kids):
                container = next(
                    (c for c in kids if str(c.get("environment") or "") == env_key), None)
                if container is None:
                    container = {"id": next_id(kids),
                                 "name": environment_display_name(env_key),
                                 "type": TYPE_ENVIRONMENT, "environment": env_key,
                                 "session_count": 0, "children": [], "stats": {}}
                    ws_root.setdefault("children", []).append(container)
                    changed = True
            else:
                container = ws_root

            wkids = [c for c in container.get("children") or [] if isinstance(c, dict)]
            want_name = workspace or UNKNOWN_WORKSPACE_NAME
            wnode = next(
                (c for c in wkids
                 if str(c.get("workspace") or "") == workspace
                 or (not workspace and str(c.get("name") or "") == UNKNOWN_WORKSPACE_NAME)),
                None)
            if wnode is None:
                used = {str(c.get("name") or "") for c in wkids}
                wnode = {"id": next_id(wkids),
                         "name": _workspace_display_name(workspace, used)
                         if workspace else UNKNOWN_WORKSPACE_NAME,
                         "type": TYPE_WORKSPACE, "workspace": workspace,
                         "session_count": 0, "children": [], "stats": {}}
                container.setdefault("children", []).append(wnode)
                changed = True
            wkids_raw = wnode.get("children") or []
            if wkids_raw and all(not isinstance(k, dict) for k in wkids_raw):
                # v5 形态：children 直接是会话 ID 列表
                wnode.setdefault("children", []).append(sid)
            else:
                # v6+ 形态：分类副本 + 未分类兜底
                copy_nodes = [c for c in wkids_raw if isinstance(c, dict)]
                leaf = next((c for c in copy_nodes
                             if str(c.get("name") or "") == UNKNOWN_CATEGORY_NAME), None)
                if leaf is None:
                    leaf = {"id": next_id(copy_nodes), "name": UNKNOWN_CATEGORY_NAME,
                            "type": TYPE_CATEGORY, "session_count": 0,
                            "children": [], "stats": {}}
                    wnode.setdefault("children", []).append(leaf)
                leaf.setdefault("children", []).append(sid)
            changed = True

        # ---- 标签根（v8）：按 index.json 条目的 labels 补齐挂载 ----
        labels_root = next((r for r in roots if str(r.get("type") or "") == TYPE_LABELS),
                           None)
        if labels_root is not None:
            entry_labels = [str(x).strip()
                            for x in ((self._read_index().get(sid) or {})
                                      .get("labels") or [])
                            if str(x).strip()]
            lkids = [c for c in labels_root.get("children") or [] if isinstance(c, dict)]
            for label in entry_labels:
                target = next((c for c in lkids
                               if str(c.get("name") or "") == label), None)
                if target is None:
                    target = {"id": next_id(lkids), "name": label,
                              "type": TYPE_LABEL, "session_count": 1,
                              "children": [], "stats": {}}
                    labels_root.setdefault("children", []).append(target)
                    lkids.append(target)
                    changed = True
                if sid not in [str(c) for c in target.get("children") or []]:
                    target.setdefault("children", []).insert(0, sid)
                    changed = True

        return changed

    def _collapse_category_copies(self, data: dict) -> bool:
        """分类合并（收缩维护）：会话数不超过末级门槛的分类子树就地收拢。

        门槛与构建期同一语义：``dynamic_max_leaf(总分类会话数)``，即
        max(2, 5*log2(n))。主题根与工作区副本同时适用——删除/搬移会话后，
        原来靠拆分撑开的层级可能只剩几个会话，继续保留全深度只是让使用
        者白白穿透；收拢到子树会话数不超过门槛的最高分类节点为止。与
        构建期的 ``workspace_category_children`` 合并步同一函数，幂等。
        """
        from runtime.category_tree_builder import (
            TYPE_CATEGORY, TYPE_WORKSPACE, collapse_small_category_subtrees,
            dynamic_max_leaf, subtree_session_count)

        roots = [r for r in data.get("tree") or [] if isinstance(r, dict)]
        total = sum(subtree_session_count(r) for r in roots
                    if str(r.get("type") or "") == TYPE_CATEGORY)
        if total <= 0:
            return False
        threshold = dynamic_max_leaf(total)
        changed = False
        for r in roots:
            if str(r.get("type") or "") in (TYPE_CATEGORY, TYPE_WORKSPACE):
                if collapse_small_category_subtrees(r.get("children") or [], threshold):
                    changed = True
        return changed

    def _mirror_category_in_workspace(self, data: dict, session_id: str,
                                      parts: list) -> bool:
        """把会话从工作区分类副本的"未分类"迁移到该主题分类路径的镜像叶子。

        parts 是主题树的完整 ID 路径（含分类根，如 1/2/1/1）。副本沿用原节点
        ID 与名称（copy_category_subtree_for_sessions 的约定），因此镜像链按
        相同 ID 逐级查找/创建。v5 形态（children 为会话 ID 列表）不做镜像。
        """
        from runtime.category_tree_builder import (
            TYPE_CATEGORY, TYPE_WORKSPACE, UNKNOWN_CATEGORY_NAME)

        sid = str(session_id)
        roots = [r for r in data.get("tree") or [] if isinstance(r, dict)]
        # 收集路径各段的 (id, name)（跳过分类根段，副本里不含根）
        chain: list = []
        nodes = roots
        for offset, part in enumerate(parts):
            node = next((item for item in nodes
                         if isinstance(item, dict) and str(item.get("id")) == part), None)
            if node is None:
                return False
            if offset > 0:
                chain.append((str(node.get("id")), str(node.get("name") or "")))
            nodes = [item for item in node.get("children", []) if isinstance(item, dict)]
        if not chain:
            return False

        ws_root = next((r for r in roots
                        if str(r.get("type") or "") == TYPE_WORKSPACE), None)
        if ws_root is None:
            return False

        ws_nodes: list = []

        def collect(node: dict) -> None:
            for c in node.get("children") or []:
                if isinstance(c, dict):
                    if str(c.get("type") or "") == TYPE_WORKSPACE:
                        ws_nodes.append(c)
                    collect(c)

        collect(ws_root)

        def next_id(nodes: list) -> int:
            return max([int(n.get("id") or 0) for n in nodes], default=0) + 1

        changed = False
        for wnode in ws_nodes:
            if not any(
                str(k) == sid
                for k in _iter_string_children(wnode)
            ):
                continue
            raw = wnode.get("children") or []
            if raw and all(not isinstance(k, dict) for k in raw):
                continue  # v5 形态：字符串列表，_apply_session_facts 已处理
            # 从所有副本叶子移除该会话
            def detach(node: dict) -> None:
                kids = node.get("children", [])
                if any(isinstance(k, dict) for k in kids):
                    for k in kids:
                        if isinstance(k, dict):
                            detach(k)
                elif sid in kids:
                    node["children"] = [c for c in kids if str(c) != sid]

            detach(wnode)
            # 沿主题路径 ID 逐级查找/创建镜像节点，把会话挂到末级
            cursor_children = wnode.setdefault("children", [])
            for offset, (seg_id, seg_name) in enumerate(chain):
                nxt = next((c for c in cursor_children
                            if isinstance(c, dict) and str(c.get("id")) == seg_id
                            and str(c.get("name") or "") == seg_name), None)
                if nxt is None:
                    # 镜像节点已被分类合并成"直挂会话"形态时，先把字符串会话
                    # 包进"未分类"子分类再落新链——children 保持纯字典或纯字符
                    # 串，后续合并步会重新收拢，不会再碎。
                    direct = [c for c in cursor_children if not isinstance(c, dict)]
                    if direct:
                        rest = [c for c in cursor_children if isinstance(c, dict)]
                        bucket = next((c for c in rest
                                       if str(c.get("name") or "") == UNKNOWN_CATEGORY_NAME), None)
                        if bucket is None:
                            bucket = {"id": next_id(rest), "name": UNKNOWN_CATEGORY_NAME,
                                      "type": TYPE_CATEGORY, "session_count": len(direct),
                                      "children": [str(c) for c in direct], "stats": {}}
                            rest.append(bucket)
                        else:
                            bucket.setdefault("children", []).extend(str(c) for c in direct)
                            bucket["session_count"] = len(bucket["children"])
                        cursor_children[:] = rest
                    # 优先沿用原分类 ID（副本约定）；该层已被"未分类"等占位时
                    # 顺延取新 ID，查询路径按实际 id 仍可达。
                    used_ids = {str(c.get("id")) for c in cursor_children
                                if isinstance(c, dict)}
                    nid = (int(seg_id)
                           if seg_id.isdigit() and seg_id not in used_ids
                           else next_id(cursor_children))
                    nxt = {"id": nid, "name": seg_name, "type": TYPE_CATEGORY,
                           "session_count": 0, "children": [], "stats": {}}
                    cursor_children.append(nxt)
                if offset == len(chain) - 1:
                    nxt.setdefault("children", []).append(sid)
                else:
                    cursor_children = nxt.setdefault("children", [])
            # 移除变空的副本分类（含搬空的"未分类"）
            def prune(node: dict) -> bool:
                kids = node.get("children", [])
                if any(isinstance(k, dict) for k in kids):
                    kept = [k for k in kids if not isinstance(k, dict)]
                    kept += [k for k in kids if isinstance(k, dict) and prune(k)]
                    node["children"] = kept
                    return len(kept) > 0
                return len([c for c in kids if isinstance(c, str)]) > 0

            prune(wnode)
            changed = True
        return changed

    def ensure_session_facts(self, session_id: str) -> bool:
        """把新会话增量补挂进 tree.json 的工作区目录与会话历史根。

        主题分类由标题生成链路（generate_title → attach_session_to_category）
        负责；这里只保证纯事实根（工作区目录/月份历史）不依赖 LLM 就有入口，
        未分类会话在工作区目录副本下落到"未分类"节点。tree.json 不存在时
        静默跳过（首次构建由后台版本门控完成）。
        """
        try:
            with open(self._category_tree_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            return False
        try:
            changed = self._apply_session_facts(data, session_id)
        except Exception as exc:
            logger.warning("ensure_session_facts: 补挂失败 (session=%s): %s", session_id, exc)
            return False
        if not changed:
            return False
        self._fill_and_stats(data)
        return self.save_category_tree(data)

    def set_session_labels(self, session_id: str, labels) -> list[str]:
        """整组替换会话标签：写 index.json 条目的 ``labels``，并同步 tree.json
        标签根（添加/移除挂载、创建/剪除标签节点）。返回清洗后的标签列表。

        清洗规则与模型/工具/AI代理的标签输入一致：字符串化、去空白、去空、
        保序去重。会话必须已存在于 index.json（未入册视为 404 语义，抛
        FileNotFoundError）。tree.json 不存在或没有标签根时只更新索引——
        下次构建/事实刷新会按索引重建标签根。
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in labels or []:
            # 服务端兜底与前端 parseLabels 同规则的再切分：调用方即便没有
            # 预分割（比如脚本直接 POST 一整串），全角/半角逗号、分号、空白
            # 也能拆开。
            for item in re.split(r"[,，;；\s]+", str(raw)):
                item = item.strip()
                if item and item not in seen:
                    seen.add(item)
                    cleaned.append(item)
        index = self._read_index()
        entry = index.get(session_id)
        if not isinstance(entry, dict):
            raise FileNotFoundError(f"Session not in index: {session_id}")
        if cleaned:
            entry["labels"] = cleaned
        else:
            entry.pop("labels", None)
        self._write_index(index)
        self._sync_tree_labels(session_id, cleaned)
        return cleaned

    def _sync_tree_labels(self, session_id: str, labels: list[str]) -> bool:
        """把 tree.json 标签根与该会话的 labels 对齐（就地编辑后原子保存）。"""
        try:
            with open(self._category_tree_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            return False
        from runtime.category_tree_builder import TYPE_LABEL, TYPE_LABELS
        labels_root = next(
            (r for r in data["tree"]
             if isinstance(r, dict) and str(r.get("type") or "") == TYPE_LABELS),
            None)
        if labels_root is None:
            return False
        sid = str(session_id)
        kids = [c for c in labels_root.get("children") or [] if isinstance(c, dict)]
        changed = False
        # 先移除：该会话从所有标签节点退出，空标签节点随之剪除。
        kept: list = []
        for node in kids:
            children = node.get("children") or []
            if sid in [str(c) for c in children]:
                node["children"] = [c for c in children if str(c) != sid]
                changed = True
            if node["children"]:
                kept.append(node)
            else:
                changed = True
        labels_root["children"] = kept
        # 再挂载：缺失的标签节点按名称追加（下次完整构建时统一排序）。
        def next_id(nodes: list) -> int:
            return max([int(n.get("id") or 0) for n in nodes], default=0) + 1
        for label in labels:
            target = next((c for c in kept if str(c.get("name") or "") == label), None)
            if target is None:
                target = {"id": next_id(kept), "name": label, "type": TYPE_LABEL,
                          "session_count": 1, "children": [], "stats": {}}
                kept.append(target)  # kept 就是 labels_root["children"]，勿二次 append
                changed = True
            if sid not in [str(c) for c in target.get("children") or []]:
                target.setdefault("children", []).insert(0, sid)
                changed = True
        if not changed:
            return False
        self._fill_and_stats(data)
        return self.save_category_tree(data)

    def remove_session_from_tree(self, session_id: str) -> bool:
        """把会话从 tree.json 的全部末级分类中移除，并剪掉空分类。

        会话删除后调用：分类下没有会话时该分类一并移除；父分类因此失去
        全部子分类时同样移除；整棵树变空时删除 tree.json（之后满足条件
        可重新构建）。同一会话可能同时位于多个末级分类（自动+人工），
        这里移除的是该会话的全部挂载。返回 True 表示 tree.json 有变化。
        """
        try:
            with open(self._category_tree_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return False
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("remove_session_from_tree: 读取 tree.json 失败: %s", exc)
            return False
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            return False

        removed_any = False

        def prune(node: dict) -> bool:
            """移除会话后判断节点是否仍有内容（空分类返回 False）。

            结构性节点不因无会话而剪除：终端动作节点（本地终端及未来的
            ssh/docker 节点）、最近会话根与会话历史根；月份节点空了则照
            常剪除，分类与工作区节点同理。
            """
            nonlocal removed_any
            kids = node.get("children", [])
            structural = str(node.get("type") or "category") in (
                "terminal", "terminal_layer", "recent", "history", "labels")
            if any(isinstance(k, dict) for k in kids):
                # 容忍混合 children：字符串会话保留（并同样移除本会话）
                strs = [k for k in kids if not isinstance(k, dict) and k != session_id]
                if len(strs) != len([k for k in kids if not isinstance(k, dict)]):
                    removed_any = True
                kept = strs
                for k in kids:
                    if isinstance(k, dict) and prune(k):
                        kept.append(k)
                node["children"] = kept
                return structural or len(kept) > 0
            if session_id in kids:
                node["children"] = [c for c in kids if c != session_id]
                removed_any = True
            return structural or len(node["children"]) > 0

        kept_top = []
        for top in data["tree"]:
            if isinstance(top, dict) and prune(top):
                kept_top.append(top)
        if not removed_any:
            return False
        data["tree"] = kept_top

        # 森林判空只看分类子树：分类全空即视为树失效（删除 tree.json，
        # 之后满足条件会重建整个森林，包括工作区与终端根）。
        def count_category(node: dict) -> int:
            node_kind = str(node.get("type") or "category")
            if node_kind != "category":
                return 0
            kids = node.get("children", [])
            if any(isinstance(k, dict) for k in kids):
                return sum(count_category(k) for k in kids if isinstance(k, dict))
            return len({c for c in kids if isinstance(c, str)})

        def has_user_layer(node: dict) -> bool:
            """森林里是否还有用户自建的嵌套终端层（terminal_layer）。"""
            stack = [node]
            while stack:
                kids = stack.pop().get("children") or []
                for k in kids:
                    if not isinstance(k, dict):
                        continue
                    if str(k.get("type") or "") == "terminal_layer":
                        return True
                    stack.append(k)
            return False

        # 分类全空即视为树失效（删除 tree.json，之后满足条件会重建整个森林，
        # 包括工作区与终端根）——但用户手工搭的嵌套终端链不能跟着分类一起丢：
        # 删掉最后几个会话不该顺手清空"终端"目录里的自建环境。
        if (sum(count_category(top) for top in kept_top) == 0
                and not any(has_user_layer(top) for top in kept_top)):
            try:
                os.remove(self._category_tree_path)
            except OSError as exc:
                logger.warning("remove_session_from_tree: 删除 tree.json 失败: %s", exc)
            return True

        def fill(n: dict) -> set:
            kids = n.get("children", [])
            if any(isinstance(k, dict) for k in kids):
                found: set = set()
                for k in kids:
                    if isinstance(k, dict):
                        found |= fill(k)
                n["session_count"] = len(found)
                return found
            ids = {c for c in kids if isinstance(c, str)}
            n["session_count"] = len(ids)
            return ids

        # 删除后收缩维护：子树会话数不超过门槛的分类就地合并（主题根与
        # 工作区副本同一规则），避免留下深层链上挂着寥寥几个会话。
        self._collapse_category_copies(data)

        for top in kept_top:
            fill(top)

        # 重算统计块（若存在），保持 tree.json 自洽
        if isinstance(data.get("stats"), dict):
            membership = Counter()
            terminals = 0
            for top in kept_top:
                def walk(node: dict) -> None:
                    nonlocal terminals
                    # 统计只覆盖分类子树；工作区/终端挂载不计入分类 membership。
                    if str(node.get("type") or "category") != "category":
                        return
                    kids = node.get("children", [])
                    if all(not isinstance(c, dict) for c in kids):
                        terminals += 1
                        for sid in kids:
                            membership[str(sid)] += 1
                    else:
                        for c in kids:
                            if isinstance(c, dict):
                                walk(c)
                walk(top)
            stats = data["stats"]
            stats["attached_conversation_count"] = len(membership)
            stats["terminal_category_count"] = terminals
            stats["membership_edge_count"] = sum(membership.values())
            stats["multi_category_conversation_count"] = sum(v > 1 for v in membership.values())
            stats["max_categories_per_conversation"] = max(membership.values()) if membership else 0

        try:
            fd, tmp = tempfile.mkstemp(prefix=".tree.", suffix=".tmp", dir=self._chats_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self._category_tree_path)
        except OSError as exc:
            logger.warning("remove_session_from_tree: 写入 tree.json 失败: %s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # index.json 读写
    # ------------------------------------------------------------------

    def _read_index(self) -> dict:
        """读取 index.json，返回完整字典。

        Returns:
            index.json 内容字典。文件不存在时返回空字典，格式异常时记录日志并返回空字典。
        """
        if not os.path.isfile(self._index_path):
            return {}
        try:
            with open(self._index_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning("index.json 内容不是 JSON 对象，返回空字典")
                return {}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 index.json 失败: %s，返回空字典", exc)
            return {}

    def _write_index(self, index: dict) -> None:
        """原子写入 index.json（先写临时文件再 os.replace）。

        Args:
            index: 要写入的索引字典。

        Raises:
            OSError: 写入失败时抛出。
        """
        from runtime.common import atomic_write_json

        os.makedirs(self._chats_dir, exist_ok=True)
        atomic_write_json(self._index_path, index)

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def on_session_created(self, session_id: str, first_user_message: Optional[str] = None) -> None:
        """新会话创建后调用，在 index.json 中新增对应的 SessionIndexEntry。

        Args:
            session_id: 新创建的会话 ID。
            first_user_message: 用户的第一条消息文本，用作初始标题（可选）。
        """
        from runtime.common import now_iso
        now = now_iso()
        # 用第一条用户消息作为初始标题，超过 30 字符则截断
        if first_user_message and first_user_message.strip():
            initial_title = first_user_message.strip()
            if len(initial_title) > 30:
                initial_title = initial_title[:30]
        else:
            initial_title = session_id
        try:
            index = self._read_index()
            index[session_id] = {
                "session_id": session_id,
                "title": initial_title,
                "created_at": now,
                "last_inference_at": now,
                "turn_count": 0,
                "last_total_tokens": None,
                "title_generated": "",
                "title_given": False,
            }
            self._write_index(index)
        except Exception as exc:
            logger.warning("on_session_created: 写入 index.json 失败 (session=%s): %s", session_id, exc)
        # 新会话立即进入工作区目录与会话历史事实根（不依赖 LLM 标题/分类链路）
        try:
            self.ensure_session_facts(session_id)
        except Exception as exc:
            logger.warning("on_session_created: 补挂事实根失败 (session=%s): %s", session_id, exc)

    def on_session_forked(
        self,
        session_id: str,
        parent_session_id: str,
        title: Optional[str] = None,
        first_user_message: Optional[str] = None,
    ) -> str:
        """衍生会话建档：沿用父会话标题，并记录 ``forked_from`` 溯源字段。

        标题优先取显式传入值，其次父会话标题，最后退回首条用户消息
        （``on_session_created`` 的默认行为）。``forked_from`` 让侧边栏可以
        标出衍生会话，也保留了「这条会话从哪里来」的信息。

        Args:
            session_id: 新建的衍生会话 ID。
            parent_session_id: 被衍生的父会话 ID。
            title: 可选的显式标题（通常由调用方从父会话 index 条目取得）。
            first_user_message: 衍生历史里的首条用户消息，用于兜底标题。

        Returns:
            实际写入 index.json 的标题（失败时返回传入标题或空串）。
        """
        self.on_session_created(session_id, first_user_message)
        try:
            index = self._read_index()
            entry = index.get(session_id)
            if not isinstance(entry, dict):
                return str(title or "")
            parent_entry = index.get(parent_session_id)
            parent_entry = parent_entry if isinstance(parent_entry, dict) else {}

            inherited = str(title or "").strip() or str(parent_entry.get("title") or "").strip()
            if inherited:
                entry["title"] = inherited[:100]
            entry["forked_from"] = parent_session_id

            index[session_id] = entry
            self._write_index(index)
            return str(entry.get("title") or "")
        except Exception as exc:
            logger.warning(
                "on_session_forked: 写入 index.json 失败 (session=%s): %s", session_id, exc
            )
            return str(title or "")

    def session_categories(self, session_id: str) -> list[str]:
        """列出会话在 tree.json 中挂载的全部末级分类路径（无则空列表）。"""
        tree = self.get_category_tree().get("tree") or []
        found: list[str] = []

        def walk(children, path_ids: list[str]) -> None:
            for item in children:
                if not isinstance(item, dict):
                    continue
                # 继承只复制分类挂载；工作区/终端节点不参与。
                if str(item.get("type") or "category") != "category":
                    continue
                node_id = str(item.get("id", ""))
                kids = item.get("children", [])
                if any(isinstance(k, dict) for k in kids):
                    walk(kids, path_ids + [node_id])
                elif session_id in kids:
                    found.append("/".join(path_ids + [node_id]))

        walk(tree, [])
        return found

    def inherit_categories(self, session_id: str, parent_session_id: str) -> int:
        """把衍生会话挂到父会话所在的全部末级分类，返回成功挂载数。

        分类树是人工/模型共同维护的结构，衍生会话落在同一分类里才与
        目录视图一致；tree.json 缺失或分类被剪枝时自然什么都不做。
        """
        attached = 0
        try:
            for category_path in self.session_categories(parent_session_id):
                if self.attach_session_to_category(session_id, category_path):
                    attached += 1
        except Exception as exc:
            logger.warning(
                "inherit_categories: 继承分类失败 (session=%s): %s", session_id, exc
            )
        return attached

    def update_index(
        self,
        session_id: str,
        last_total_tokens: Optional[int] = None,
        *,
        generate_title: bool = True,
        compression_updated: bool = False,
    ) -> None:
        """推理完成后调用，更新 index.json 中对应条目的元信息。

        读取对应 conversation.json 的 meta.turn_count，更新 last_inference_at、
        turn_count、last_total_tokens。仅当 ``generate_title`` 为 True 时才
        检查标题生成：首次完整推理后生成一次；之后仅在上下文压缩实际更新
        summary/memory 时再生成一次。

        Args:
            session_id: 会话 ID。
            last_total_tokens: 本次推理的总 token 数（可选）。
        """
        from runtime.common import now_iso
        now = now_iso()
        try:
            index = self._read_index()
            entry = index.get(session_id, {
                "session_id": session_id,
                "title": session_id,
                "created_at": now,
                "last_inference_at": now,
                "turn_count": 0,
                "last_total_tokens": None,
                "title_generated": "",
                "title_given": False,
            })

            # 读取 conversation.json 的 meta.turn_count
            conv_path = os.path.join(self._chats_dir, session_id, "conversation.json")
            try:
                with open(conv_path, "r", encoding="utf-8") as fh:
                    conv_data = json.load(fh)
                turn_count = conv_data.get("meta", {}).get("turn_count", entry.get("turn_count", 0))
            except Exception:
                turn_count = entry.get("turn_count", 0)

            entry["last_inference_at"] = now
            entry["turn_count"] = turn_count
            if last_total_tokens is not None:
                entry["last_total_tokens"] = last_total_tokens

            index[session_id] = entry
            self._write_index(index)
        except Exception as exc:
            logger.warning("update_index: 写入 index.json 失败 (session=%s): %s", session_id, exc)
            return

        if generate_title:
            self.generate_title(
                session_id,
                last_total_tokens,
                compression_updated=compression_updated,
            )

    def set_session_status(self, session_id: str, status: Optional[str]) -> None:
        """Persist a resting session status (currently only ``"paused"``) to index.json.

        In-memory statuses (:mod:`runtime.server_state`) are lost on restart,
        so the sidebar list — which is built from index entries — would show a
        paused session as if it had simply finished. Writing the marker into
        the index keeps the pause visible across server restarts and page
        reloads, without any boot-time scan.

        Args:
            session_id: Target session.
            status: ``"paused"`` to mark; ``None`` / ``""`` clears the field.

        The write is a no-op when the index entry is absent (a session that
        never reached persistence has no resting state to mark).
        """
        try:
            index = self._read_index()
            entry = index.get(session_id)
            if entry is None:
                return
            if status:
                entry["status"] = status
            else:
                entry.pop("status", None)
            index[session_id] = entry
            self._write_index(index)
        except Exception as exc:
            logger.warning(
                "set_session_status: 写入 index.json 失败 (session=%s): %s",
                session_id, exc,
            )

    def generate_title(
        self,
        session_id: str,
        last_total_tokens: Optional[int],
        *,
        compression_updated: bool = False,
    ) -> None:
        """当满足条件时，使用推理函数为会话生成标题。

        自动生成时机：首次完整推理结束后一次；之后只有上下文压缩实际更新
        summary/memory 时一次。增量持久化不会调用这里。

        Args:
            session_id: 会话 ID。
            last_total_tokens: 本次推理的总 token 数。
        """
        entry = self._read_index().get(session_id) or {}
        # 人工设定的标题优先：自动生成（含压缩后刷新）不得覆盖它
        if entry.get("title_given"):
            return
        if self._generated_title_of(entry) and not compression_updated:
            return
        self._do_generate_title(session_id)

    def generate_title_forced(self, session_id: str) -> Optional[str]:
        """强制为会话生成标题（手动触发，跳过 token 阈值检查）。

        Args:
            session_id: 会话 ID。

        Returns:
            生成的标题，失败时返回 None。
        """
        return self._do_generate_title(session_id)

    @staticmethod
    def _generated_title_of(entry: dict) -> str:
        """返回 index 条目中记录的模型生成标题。

        新格式下 ``title_generated`` 直接存生成的标题字符串；旧格式存的是
        bool，``True`` 表示当前标题即模型生成。
        """
        value = entry.get("title_generated")
        if isinstance(value, str):
            return value
        if value is True:
            return str(entry.get("title") or "")
        return ""

    def set_title_manually(self, session_id: str, title: str) -> str:
        """人工设定会话标题并持久化到 index.json。

        给定标题成为当前标题（``title`` 字段）并标记 ``title_given: true``；
        原模型生成的标题记入 ``title_generated``（尚无生成标题时置空）。
        已标记 ``title_given`` 的会话再次设定时复用同一逻辑。

        Args:
            session_id: 会话 ID。
            title: 人工设定的标题（strip 后非空，超过 100 字符则截断）。

        Returns:
            实际保存的标题。

        Raises:
            ValueError: title 为空时抛出。
            FileNotFoundError: 会话不在 index.json 中时抛出。
        """
        title = str(title or "").strip()
        if not title:
            raise ValueError("title must not be empty")
        if len(title) > 100:
            title = title[:100]

        index = self._read_index()
        entry = index.get(session_id)
        if not isinstance(entry, dict):
            raise FileNotFoundError(f"Session not found: {session_id}")

        # 原模型生成的标题：新格式为字符串值，旧格式 bool True 表示
        # 当前标题即模型生成（必须在覆盖 title 字段之前取回）
        generated = self._generated_title_of(entry)
        entry["title"] = title
        entry["title_given"] = True
        entry["title_generated"] = generated
        index[session_id] = entry
        self._write_index(index)
        logger.info("set_title_manually: 人工设定标题 (session=%s): %s", session_id, title)
        if self._broadcast_fn:
            self._broadcast_fn(session_id, "title_update", {"title": title, "title_given": True})
        return title

    def _do_generate_title(self, session_id: str) -> Optional[str]:
        """实际执行标题生成的内部方法（顺带完成分类）。

        Args:
            session_id: 会话 ID。

        当 tree.json 中存在末级分类时，标题生成的推理请求会附带分类目录，
        要求模型同时返回一个既有末级分类路径；解析成功后将会话挂载到该分类。

        Returns:
            生成的标题，失败时返回 None。
        """
        summary_model_id = os.environ.get("SUMMARY_MODEL_ID", "summary")
        if not summary_model_id:
            return None
        if self._model_registry is not None:
            config = self._model_registry.get(summary_model_id)
            if config is None:
                logger.warning(
                    "generate_title: SUMMARY_MODEL_ID=%r not found in model registry",
                    summary_model_id,
                )
                return None
            summary_model_id = config.model_id
        if self._infer_fn is None:
            logger.warning("generate_title: 推理函数未设置")
            return None

        try:
            index = self._read_index()
            entry = index.get(session_id)
            if entry is None:
                logger.warning("generate_title: 会话不存在 (session=%s)", session_id)
                return None

            # 读取 conversation.json 前几条消息
            conv_path = os.path.join(self._chats_dir, session_id, "conversation.json")
            try:
                with open(conv_path, "r", encoding="utf-8") as fh:
                    conv_data = json.load(fh)
                messages = conv_data.get("messages", [])
            except Exception as e:
                logger.warning("generate_title: 读取会话文件失败 (session=%s): %s", session_id, e)
                return None

            # 取前 10 条 user/assistant 消息
            excerpt_parts = []
            count = 0
            for msg in messages:
                if msg.get("role") in ("user", "assistant") and count < 10:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        excerpt_parts.append(f"{role}: {content[:200]}")
                        count += 1
            if not excerpt_parts:
                logger.warning("generate_title: 会话无有效消息 (session=%s)", session_id)
                return None

            conversation_excerpt = "\n".join(excerpt_parts)

            # 分类目录：tree.json 存在末级分类时，同一次推理请求顺带完成分类
            categories = self.terminal_categories()
            if categories:
                category_lines = "\n".join(
                    f"{item['path']} {item['label']}" for item in categories[:MAX_CATEGORY_LIST]
                )
                prompt = (
                    "请分析以下对话，完成两件事：\n"
                    "1. 生成一个简短的标题（不超过20个字）。\n"
                    "2. 从下面的分类目录中选择该会话最贴切的一个末级分类，返回其完整路径（形如 1/2/3）；"
                    "若没有合适的分类，category 返回空字符串。只能选择目录中已有的路径，不得新增或改写。\n\n"
                    "分类目录（路径 名称）：\n"
                    + category_lines
                    + "\n\n只输出一个 JSON 对象，格式为 {\"title\": \"标题\", \"category\": \"路径\"}，"
                      "不要输出任何其他文字或代码块标记。\n\n"
                    + conversation_excerpt
                )
            else:
                prompt = (
                    "请为以下对话生成一个简短的标题（不超过20个字，直接输出标题文字，不要加引号或其他格式）：\n\n"
                    + conversation_excerpt
                )

            from runtime.models import InferenceRequest, Message
            request = InferenceRequest(
                model_id=summary_model_id,
                messages=[Message(role="user", content=prompt)],
                stream=False,
            )
            result = self._infer_fn(request)
            # 提取模型回复（纯标题，或 标题+分类 的 JSON）
            raw = ""
            for msg in reversed(result.messages):
                if msg.role == "assistant" and msg.content:
                    raw = msg.content.strip()
                    break
            title, category = _parse_title_and_category(raw)
            if not title:
                logger.warning("generate_title: 模型未返回有效标题 (session=%s)", session_id)
                return None

            # 截断至 100 字符
            title = title[:100]

            # 写入 index
            index = self._read_index()
            if session_id in index:
                index[session_id]["title"] = title
                # 记录模型生成的标题；人工设定标记复位
                index[session_id]["title_generated"] = title
                index[session_id]["title_given"] = False
                self._write_index(index)
                logger.info("generate_title: 成功生成标题 (session=%s): %s", session_id, title)
                # 广播标题更新事件
                if self._broadcast_fn:
                    self._broadcast_fn(session_id, "title_update", {"title": title, "title_given": False})
                # 将会话归入模型选择的既有末级分类（失败不影响标题）
                if category:
                    valid_paths = {item["path"] for item in categories}
                    if category in valid_paths:
                        if self.attach_session_to_category(session_id, category):
                            logger.info(
                                "generate_title: 已归入分类 (session=%s, category=%s)",
                                session_id, category,
                            )
                    else:
                        logger.warning(
                            "generate_title: 模型返回未知分类路径 (session=%s): %r",
                            session_id, category,
                        )
                return title
            return None
        except Exception as exc:
            logger.warning("generate_title: 生成标题失败 (session=%s): %s", session_id, exc)
            return None

    def remove_from_index(self, session_id: str) -> None:
        """从 index.json 中移除指定会话条目（不删除目录）。

        用于 conversation.json 不存在时的清理（人为删除或磁盘故障）。
        失败时静默忽略。

        Args:
            session_id: 要移除的会话 ID。
        """
        try:
            index = self._read_index()
            if session_id in index:
                del index[session_id]
                self._write_index(index)
        except Exception as exc:
            logger.warning("remove_from_index: 更新 index.json 失败 (session=%s): %s", session_id, exc)

    def set_session_inferring(self, session_id: str, inferring: bool) -> None:
        """把「这个会话正在推理中」记进 index.json（``inferring`` / ``inferring_since``）。

        推理的生命周期属于会话本身，与浏览器连接无关，所以"是否正在推理"必须在磁盘上
        有一份凭证：正常收尾（完成 / 停止 / 失败 / 关闭时强停）时清除；实例被强杀时它会
        残留，下一个实例据此把该会话判为异常中断（见 ``fail_inferring_sessions``）。

        写失败只记日志：标记缺失最多让一次崩溃看起来像正常停止，绝不能反过来影响推理。
        """
        from runtime.common import now_iso
        try:
            index = self._read_index()
            entry = index.get(session_id)
            if not isinstance(entry, dict):
                if not inferring:
                    return            # 会话已不存在：不需要为它补写"已结束"
                now = now_iso()
                entry = {
                    "session_id": session_id,
                    "title": session_id,
                    "created_at": now,
                    "last_inference_at": now,
                    "turn_count": 0,
                    "last_total_tokens": None,
                    "title_generated": "",
                    "title_given": False,
                }
            if inferring:
                if not entry.get("inferring"):
                    entry["inferring"] = True
                    entry["inferring_since"] = now_iso()
            else:
                entry.pop("inferring", None)
                entry.pop("inferring_since", None)
            index[session_id] = entry
            self._write_index(index)
        except Exception as exc:
            logger.warning(
                "set_session_inferring: 写入 index.json 失败 (session=%s): %s",
                session_id, exc,
            )

    def inferring_sessions(self) -> list[str]:
        """列出 index.json 标记为推理中的会话 ID（升序）。"""
        index = self._read_index()
        return sorted(
            session_id
            for session_id, entry in index.items()
            if isinstance(entry, dict) and entry.get("inferring")
        )

    def fail_inferring_sessions(self) -> list[str]:
        """启动时清掉残留的推理中标记，返回这些会话 ID（调用方落成失败完成）。

        本实例刚初始化，不可能有任何会话真的在推理：标记还在，就说明上一个实例没有走到
        收尾（进程被强杀 / 崩溃 / 断电）。调用方把这些会话标记为 ``done_error_unread``，
        侧栏据此显示失败完成，而不是永远显示"推理中"。
        """
        session_ids = self.inferring_sessions()
        if not session_ids:
            return []
        for session_id in session_ids:
            self.set_session_inferring(session_id, False)
        return session_ids

    def list_sessions(self, session_ids: Optional[set[str]] = None) -> list[dict]:
        """读取 index.json，返回所有 SessionIndexEntry 列表，按 last_inference_at 降序排列。

        Args:
            session_ids: 可选的父 session_id 集合。传入时只返回该集合命中的会话，
                用于历史会话全文搜索过滤。

        Returns:
            SessionIndexEntry 字典列表（降序排列）。index.json 不存在时返回空列表。
        """
        index = self._read_index()
        entries = list(index.values())
        if session_ids is not None:
            entries = [e for e in entries if e.get("session_id") in session_ids]
        entries.sort(
            key=lambda e: e.get("last_inference_at") or "",
            reverse=True,
        )
        return entries

    @staticmethod
    def _parse_search_query(query: str):
        """Parse a search query — delegates to :func:`runtime.common.parse_search_query`."""
        return parse_search_query(query)

    def search_sessions(self, query: str) -> list[dict]:
        """全文搜索 chats_dir 下所有 conversation.json，并返回命中的父会话列表。

        优先使用 ripgrep (rg)，不存在时 fallback 到 grep。搜索范围包含子目录，
        因此会命中子 session 的 conversation.json；命中路径最终会归并到父
        session_id（即 chats_dir 下的第一层目录名），再从 index.json 中取回会话
        元数据并按 list_sessions 的排序规则返回。

        搜索语法：
        - 空格分隔 = AND（同时包含所有关键词）
        - | 分隔 = OR（包含任一关键词）
        不支持混合使用。
        """
        if not query:
            return self.list_sessions()

        _, keywords = parse_search_query(query)
        if not keywords:
            return self.list_sessions()

        root = os.path.realpath(self._chats_dir)
        if not os.path.isdir(root):
            return []

        matched_files = search_files(
            root,
            query,
            include="**/conversation.json",
        )

        if not matched_files:
            return []

        session_ids: set[str] = set()
        for path in matched_files:
            path = path.strip()
            if not path:
                continue
            real_path = os.path.realpath(path)
            try:
                rel = os.path.relpath(real_path, root)
            except ValueError:
                continue
            parts = rel.split(os.sep)
            if len(parts) >= 2 and parts[-1] == "conversation.json" and parts[0] not in ("", ".", ".."):
                # 子 session 路径形如 <parent>/sub_xxx/conversation.json，仍归并为 <parent>。
                session_ids.add(parts[0])

        return self.list_sessions(session_ids)

    def delete_session(self, session_id: str) -> None:
        """删除指定会话目录及其所有内容，并从 index.json 中移除对应条目。

        Args:
            session_id: 会话标识符（对应 chats_dir 下的子目录名）。

        Raises:
            FileNotFoundError: 会话目录不存在时抛出。
            ValueError: session_id 包含路径分隔符（防止路径穿越）时抛出。
        """
        # 防止路径穿越攻击
        if os.sep in session_id or (os.altsep and os.altsep in session_id) or ".." in session_id:
            raise ValueError(f"非法的 session_id: {session_id}")

        session_dir = os.path.join(self._chats_dir, session_id)
        if not os.path.isdir(session_dir):
            raise FileNotFoundError(f"会话目录不存在: {session_dir}")

        import shutil
        shutil.rmtree(session_dir)

        # 从 index.json 中移除对应条目
        try:
            index = self._read_index()
            if session_id in index:
                del index[session_id]
                self._write_index(index)
        except Exception as exc:
            logger.warning("delete_session: 更新 index.json 失败 (session=%s): %s", session_id, exc)

        # 从分类树中移除会话，剪掉因此变空的分类
        try:
            self.remove_session_from_tree(session_id)
        except Exception as exc:
            logger.warning("delete_session: 更新 tree.json 失败 (session=%s): %s", session_id, exc)

    def session_dir(self, session_id: str) -> str:
        """返回指定会话的目录路径（``DATA_DIR/chat_data/{session_id}``）。

        Args:
            session_id: 会话标识符（对应 chats_dir 下的子目录名）。

        Returns:
            该会话目录的绝对路径。

        Raises:
            ValueError: session_id 包含路径分隔符（防止路径穿越）时抛出。
            FileNotFoundError: 会话目录不存在时抛出。
        """
        # 防止路径穿越攻击
        if os.sep in session_id or (os.altsep and os.altsep in session_id) or ".." in session_id:
            raise ValueError(f"非法的 session_id: {session_id}")

        session_path = os.path.join(self._chats_dir, session_id)
        if not os.path.isdir(session_path):
            raise FileNotFoundError(f"会话目录不存在: {session_path}")
        return session_path

    def get_session(self, session_id: str) -> dict:
        """读取指定会话的 conversation.json，返回完整数据。

        Args:
            session_id: 会话标识符（对应 chats_dir 下的子目录名）。

        Returns:
            conversation.json 的完整内容（dict）。

        Raises:
            FileNotFoundError: conversation.json 文件不存在时抛出。
            ValueError: 文件内容不是合法 JSON 或格式异常时抛出。
        """
        conv_path = os.path.join(self._chats_dir, session_id, "conversation.json")

        if not os.path.isfile(conv_path):
            raise FileNotFoundError(
                f"会话文件不存在: {conv_path}"
            )

        try:
            with open(conv_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"conversation.json 格式异常: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"无法读取 conversation.json: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"conversation.json 内容必须是 JSON 对象，实际类型: {type(data).__name__}"
            )

        return data

    def get_title_info(self, session_id: str) -> Optional[dict]:
        """返回会话在 index.json 中的标题信息 ``{"title": ..., "title_given": ...}``。

        供终态 done_* 状态广播附带权威标题：广播 done 状态时最终持久化
        （含自动标题生成）已经完成，index 中的标题即最终标题，前端可仅凭
        事件恢复侧边栏标题，无需再请求会话列表。

        Args:
            session_id: 会话标识符。

        Returns:
            含 ``title`` / ``title_given`` 的 dict；会话不在 index 中或无
            标题时返回 None。
        """
        entry = self._read_index().get(session_id)
        if not isinstance(entry, dict):
            return None
        title = str(entry.get("title") or "").strip()
        if not title:
            return None
        return {"title": title, "title_given": bool(entry.get("title_given"))}

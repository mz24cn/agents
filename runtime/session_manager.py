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

    def list_sessions_by_category(self, category: str) -> list[dict]:
        """Return indexed sessions attached to a terminal category path.

        ``category`` is a slash-separated sequence of numeric node IDs, such as
        ``1/2`` or ``1/2/3``. Terminal categories may occur at different depths.
        Category nodes remain a tree, while session ID leaves may be repeated in
        multiple terminal nodes to form cross-links.
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
        # Directory browsing is a structural view, not the reverse-
        # chronological recent-sessions view returned by list_sessions(). Use a
        # stable title order so category results are easy to scan and do not
        # inherit the time ordering used by the Sidebar's default list.
        sessions.sort(
            key=lambda item: (
                str(item.get("title") or item.get("session_id") or "").casefold(),
                str(item.get("session_id") or ""),
            )
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
        """将会话挂载到 tree.json 中一个既有的末级分类。

        末级分类允许挂载多个会话（图状连接）；会话已挂载时不做任何修改。
        挂载成功后沿祖先链重算 session_count 并原子写入 tree.json。
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

        children = node.get("children", [])
        if not isinstance(children, list) or any(isinstance(c, dict) for c in children):
            logger.warning("attach_session_to_category: %r 不是末级分类", category_path)
            return False
        if session_id in children:
            return False

        children.append(session_id)
        node["children"] = children

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

        for top in data["tree"]:
            if isinstance(top, dict):
                fill(top)

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
            """移除会话后判断节点是否仍有内容（空分类返回 False）。"""
            nonlocal removed_any
            kids = node.get("children", [])
            if any(isinstance(k, dict) for k in kids):
                kept = []
                for k in kids:
                    if isinstance(k, dict) and prune(k):
                        kept.append(k)
                node["children"] = kept
                return len(kept) > 0
            if session_id in kids:
                node["children"] = [c for c in kids if c != session_id]
                removed_any = True
            return len(node["children"]) > 0

        kept_top = []
        for top in data["tree"]:
            if isinstance(top, dict) and prune(top):
                kept_top.append(top)
        if not removed_any:
            return False
        data["tree"] = kept_top

        if not kept_top:
            # 全部分类已空：删除 tree.json，之后可重新构建
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

        for top in kept_top:
            fill(top)

        # 重算统计块（若存在），保持 tree.json 自洽
        if isinstance(data.get("stats"), dict):
            membership = Counter()
            terminals = 0
            for top in kept_top:
                def walk(node: dict) -> None:
                    nonlocal terminals
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
                "title_generated": False,
            }
            self._write_index(index)
        except Exception as exc:
            logger.warning("on_session_created: 写入 index.json 失败 (session=%s): %s", session_id, exc)

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
                "title_generated": False,
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
        if entry.get("title_generated") and not compression_updated:
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
                index[session_id]["title_generated"] = True
                self._write_index(index)
                logger.info("generate_title: 成功生成标题 (session=%s): %s", session_id, title)
                # 广播标题更新事件
                if self._broadcast_fn:
                    self._broadcast_fn(session_id, "title_update", {"title": title})
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

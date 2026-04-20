"""SessionManager — 会话管理模块。

负责读取 chats_dir 目录下的历史会话列表，以及读取指定会话的 conversation.json 数据。
同时维护 index.json 索引文件，记录每个会话的元信息。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger("runtime.session_manager")


class SessionManager:
    """管理历史会话的列举与读取，以及 index.json 索引维护。

    Args:
        chats_dir: 存储历史会话的根目录路径，每个子目录对应一个会话。
        infer_fn: 可选的推理函数，用于生成会话标题。
    """

    def __init__(self, chats_dir: str, infer_fn: Optional[Callable] = None) -> None:
        self._chats_dir = chats_dir
        self._infer_fn = infer_fn

    # ------------------------------------------------------------------
    # 内部属性
    # ------------------------------------------------------------------

    @property
    def _index_path(self) -> str:
        return os.path.join(self._chats_dir, "index.json")

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
        os.makedirs(self._chats_dir, exist_ok=True)
        tmp_path = self._index_path + ".tmp"
        content = json.dumps(index, ensure_ascii=False, indent=2)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, self._index_path)

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def on_session_created(self, session_id: str) -> None:
        """新会话创建后调用，在 index.json 中新增对应的 SessionIndexEntry。

        Args:
            session_id: 新创建的会话 ID。
        """
        import datetime as _dt
        now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            index = self._read_index()
            index[session_id] = {
                "session_id": session_id,
                "title": session_id,
                "created_at": now,
                "last_inference_at": now,
                "turn_count": 0,
                "last_total_tokens": None,
            }
            self._write_index(index)
        except Exception as exc:
            logger.warning("on_session_created: 写入 index.json 失败 (session=%s): %s", session_id, exc)

    def update_index(self, session_id: str, last_total_tokens: Optional[int] = None) -> None:
        """推理完成后调用，更新 index.json 中对应条目的元信息。

        读取对应 conversation.json 的 meta.turn_count，更新 last_inference_at、
        turn_count、last_total_tokens，写入成功后调用 generate_title()。

        Args:
            session_id: 会话 ID。
            last_total_tokens: 本次推理的总 token 数（可选）。
        """
        import datetime as _dt
        now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            index = self._read_index()
            entry = index.get(session_id, {
                "session_id": session_id,
                "title": session_id,
                "created_at": now,
                "last_inference_at": now,
                "turn_count": 0,
                "last_total_tokens": None,
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

        # 写入成功后调用 generate_title
        self.generate_title(session_id, last_total_tokens)

    def generate_title(self, session_id: str, last_total_tokens: Optional[int]) -> None:
        """当满足条件时，使用推理函数为会话生成标题。

        条件：
        - last_total_tokens > 1000
        - SUMMARY_MODEL_ID 环境变量已配置
        - self._infer_fn 不为 None
        - index 中该会话的 title 为空（避免重复生成）

        Args:
            session_id: 会话 ID。
            last_total_tokens: 本次推理的总 token 数。
        """
        if last_total_tokens is None or last_total_tokens <= 10000:
            return
        summary_model_id = os.environ.get("SUMMARY_MODEL_ID", "")
        if not summary_model_id:
            return
        if self._infer_fn is None:
            return

        try:
            index = self._read_index()
            entry = index.get(session_id)
            if entry is None:
                return
            if entry.get("title", "") and entry.get("title") != session_id:
                # 已有真实标题（非 session_id 占位），不重复生成
                return

            # 读取 conversation.json 前几条消息
            conv_path = os.path.join(self._chats_dir, session_id, "conversation.json")
            try:
                with open(conv_path, "r", encoding="utf-8") as fh:
                    conv_data = json.load(fh)
                messages = conv_data.get("messages", [])
            except Exception:
                return

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
                return

            conversation_excerpt = "\n".join(excerpt_parts)
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
            # 提取生成的标题文本
            title = ""
            for msg in reversed(result.messages):
                if msg.role == "assistant" and msg.content:
                    title = msg.content.strip()
                    break
            if not title:
                return

            # 截断至 100 字符
            title = title[:100]

            # 写入 index
            index = self._read_index()
            if session_id in index:
                index[session_id]["title"] = title
                self._write_index(index)
        except Exception as exc:
            logger.warning("generate_title: 生成标题失败 (session=%s): %s", session_id, exc)

    def list_sessions(self) -> list[dict]:
        """读取 index.json，返回所有 SessionIndexEntry 列表，按 last_inference_at 降序排列。

        Returns:
            SessionIndexEntry 字典列表（降序排列）。index.json 不存在时返回空列表。
        """
        index = self._read_index()
        entries = list(index.values())
        # 按 last_inference_at 降序排列，缺失时排在最后
        entries.sort(
            key=lambda e: e.get("last_inference_at") or "",
            reverse=True,
        )
        return entries

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

"""首轮推理的 system 上下文装配：代理提示词与"来源系统消息"必须共存。

标签节点"发起新会话"会把该标签的聚合摘要/记忆作为一条系统消息随首轮请求发
出（前端在消息上打 ``label_origin`` 标记）。此前的实现只要选中了 AI 代理，就
会把请求里已有的系统消息整条清掉再写入代理自己的系统提示词 —— 标签带来的上
下文因此被覆盖，新会话虽然继承了标签，却拿不到该标签的聚合记忆。

这里的规则（对应需求"标签系统消息不能被 AI 代理系统消息覆盖，必须合并写入
system"）：

  * 代理 / 群聊默认提示词排在最前：它定义"这一轮是谁在说话"；
  * 带来源标记（标签聚合等）的系统消息紧随其后，不被删除；
  * 不带标记的既有系统消息仍按原语义由代理提示词替换（单代理路径），群聊路径
    则由调用方自行判断是否注入默认提示词（保持旧行为）；
  * 等价判定：同模板 + 同参数，或同文本 —— 只保留第一条，避免重复追加；
  * 多条前置系统消息在装配/协议层被合并成同一条 ``system``
    （``ContextManager._merge_system_parts`` 与
    ``protocols._merge_leading_system_messages``），所以"合并写入 system"这一
    要求在这两条路上都成立，同时按消息保存还能让模板占位符继续可渲染。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import json
from typing import Optional

#: 系统消息上的来源标记键。目前只有标签聚合会写它（值为标签名）。
LABEL_ORIGIN_KEY = "label_origin"


def label_origin_of(message) -> str:
    """返回系统消息的来源标记（无标记返回空串）。

    ``True`` 归一化为 ``"label"``，便于调用方只关心"是不是来源消息"。
    """
    if not isinstance(message, dict):
        return ""
    value = message.get(LABEL_ORIGIN_KEY)
    if isinstance(value, bool):
        return "label" if value else ""
    return str(value or "").strip()


def is_label_system_message(message) -> bool:
    """是否为带来源标记（标签聚合）的系统消息。"""
    return bool(label_origin_of(message))


def is_plain_system_message(message) -> bool:
    """是否为不带来源标记的系统消息（例如用户自选的提示词模板）。"""
    return isinstance(message, dict) \
        and str(message.get("role") or "") == "system" \
        and not is_label_system_message(message)


def system_message_identity(message: dict) -> tuple:
    """两条系统消息是否等价：同模板 + 同参数，或同文本。

    用于去重：同一份标签聚合摘要被重发、或代理提示词与请求里残留的副本同时存
    在时，只保留第一条。
    """
    template = str(message.get("prompt_template") or "")
    if template:
        try:
            args = json.dumps(message.get("arguments") or {},
                              sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = ""
        return ("template", template, args)
    return ("text", str(message.get("content") or "").strip())


def merge_first_turn_system_messages(messages, injected: Optional[list]) -> list:
    """把注入的系统提示词与请求里的系统消息合并成一段 system 头。

    Args:
        messages: 请求里的原始消息字典列表（未经 ``Message.from_dict``）。
        injected: 要注入到最前面的系统消息（代理提示词 / 群聊默认提示词）。

    Returns:
        新列表：合并后的系统消息在前，其余非系统消息相对顺序不变。

    语义：
      * 带来源标记（标签聚合）的系统消息一律保留，排在注入项之后 —— 需求
        "标签系统消息不能被 AI 代理系统消息覆盖，必须合并写入 system"；
      * 不带标记的既有系统消息由注入项替换（单代理路径的旧语义）；群聊路径
        由调用方先用 :func:`is_plain_system_message` 判断要不要注入默认提示词，
        因此不会误伤用户自己贴的系统提示词；
      * 等价（同模板+同参数，或同文本）只保留第一条，避免重复追加。
    """
    original = list(messages or [])
    merged_system: list = []
    seen: set = set()

    def remember(msg: dict) -> None:
        key = system_message_identity(msg)
        if key in seen:
            return
        seen.add(key)
        merged_system.append(msg)

    for msg in injected or []:
        if isinstance(msg, dict):
            remember(msg)
    for msg in original:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "system":
            continue
        if is_label_system_message(msg):
            remember(msg)

    rest = [m for m in original
            if not (isinstance(m, dict)
                    and str(m.get("role") or "") == "system")]
    return merged_system + rest

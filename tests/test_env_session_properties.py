"""属性测试：EnvManager 正确性属性验证。

使用 Hypothesis 库对 EnvManager 的核心行为进行属性测试，
覆盖设计文档中定义的属性 1 ~ 6。

每个属性测试使用临时目录，避免污染真实文件系统。
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from runtime.env_manager import EnvManager


# ---------------------------------------------------------------------------
# 辅助策略
# ---------------------------------------------------------------------------

# 合法的环境变量 key 策略：非空字符串，不含 NUL 字节（os.environ 不允许）
_env_key_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Pc"),
        blacklist_characters="\x00",
    ),
    min_size=1,
    max_size=64,
)

# 合法的环境变量 value 策略：任意字符串，不含 NUL 字节和代理字符（UTF-8 不允许代理字符）
_env_value_strategy = st.text(
    alphabet=st.characters(
        blacklist_characters="\x00",
        blacklist_categories=("Cs",),  # 排除代理字符（surrogate）
    ),
    max_size=256,
)

# 合法的 Python 标识符策略（\w+ 且以字母或下划线开头）
_identifier_strategy = st.from_regex(r"[A-Za-z_]\w{0,31}", fullmatch=True)


# ---------------------------------------------------------------------------
# 属性 1：环境变量读写 round-trip
# Validates: Requirements 1.1, 2.1, 2.2
# ---------------------------------------------------------------------------

@given(
    env_dict=st.dictionaries(
        keys=_env_key_strategy,
        values=_env_value_strategy,
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=100)
def test_env_read_write_roundtrip(env_dict: dict) -> None:
    """属性 1：对于任意非空字符串键值对字典，写入后读取应得到相同内容。

    **Validates: Requirements 1.1, 2.1, 2.2**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")
        mgr = EnvManager(env_path=env_path)

        # 逐个写入所有键值对
        for key, value in env_dict.items():
            mgr.set(key, value)

        # 读取并验证内容一致
        result = mgr.read()
        assert result == env_dict, (
            f"读写 round-trip 失败：写入 {env_dict!r}，读取到 {result!r}"
        )


# ---------------------------------------------------------------------------
# 属性 2：写入后 os.environ 同步
# Validates: Requirements 2.1, 2.2, 2.6
# ---------------------------------------------------------------------------

@given(
    key=_env_key_strategy,
    value=_env_value_strategy,
)
@settings(max_examples=100)
def test_set_syncs_to_os_environ(key: str, value: str) -> None:
    """属性 2：调用 set() 后，os.environ 中应包含该键值对。

    **Validates: Requirements 2.1, 2.2, 2.6**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")
        mgr = EnvManager(env_path=env_path)

        mgr.set(key, value)

        assert key in os.environ, (
            f"set() 后 os.environ 中未找到 key={key!r}"
        )
        assert os.environ[key] == value, (
            f"os.environ[{key!r}] = {os.environ[key]!r}，期望 {value!r}"
        )


# ---------------------------------------------------------------------------
# 属性 3：删除后键不存在
# Validates: Requirements 2.3
# ---------------------------------------------------------------------------

@given(
    env_dict=st.dictionaries(
        keys=_env_key_strategy,
        values=_env_value_strategy,
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=100)
def test_delete_removes_key(env_dict: dict) -> None:
    """属性 3：删除任意一个 key 后，该 key 不在读取结果中，且其余键值对不变。

    **Validates: Requirements 2.3**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")
        mgr = EnvManager(env_path=env_path)

        # 写入所有键值对
        for key, value in env_dict.items():
            mgr.set(key, value)

        # 选取第一个 key 进行删除
        key_to_delete = next(iter(env_dict))
        mgr.delete(key_to_delete)

        result = mgr.read()

        # 验证被删除的 key 不存在
        assert key_to_delete not in result, (
            f"删除 key={key_to_delete!r} 后，该 key 仍存在于读取结果中"
        )

        # 验证其余键值对不变
        expected_remaining = {k: v for k, v in env_dict.items() if k != key_to_delete}
        assert result == expected_remaining, (
            f"删除后剩余键值对不一致：期望 {expected_remaining!r}，实际 {result!r}"
        )


# ---------------------------------------------------------------------------
# 属性 4：无效 JSON 内容返回错误
# Validates: Requirements 1.3
# ---------------------------------------------------------------------------

# 生成非 JSON 对象内容的策略
_invalid_json_object_strategy = st.one_of(
    # JSON 数组
    st.lists(st.integers(), min_size=0, max_size=5).map(json.dumps),
    # 纯字符串（JSON 字符串，不是对象）
    st.text(min_size=1, max_size=50).map(json.dumps),
    # 纯整数
    st.integers().map(str),
    # 纯布尔值
    st.sampled_from(["true", "false"]),
    # null
    st.just("null"),
    # 格式错误的 JSON（在合法 JSON 后追加垃圾字符）
    st.text(min_size=1, max_size=20).map(lambda s: "{" + s),
    # 空文件
    st.just(""),
)


@given(invalid_content=_invalid_json_object_strategy)
@settings(max_examples=100)
def test_read_raises_value_error_for_invalid_json(invalid_content: str) -> None:
    """属性 4：非 JSON 对象内容应使 read() 抛出 ValueError。

    **Validates: Requirements 1.3**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")

        # 直接写入无效内容（绕过 EnvManager.set()）
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(invalid_content)

        mgr = EnvManager(env_path=env_path)

        with pytest.raises(ValueError):
            mgr.read()


# ---------------------------------------------------------------------------
# 属性 5：环境变量检测 round-trip
# Validates: Requirements 3.1, 3.2
# ---------------------------------------------------------------------------

@given(identifier=_identifier_strategy)
@settings(max_examples=100)
def test_detect_used_keys_roundtrip(identifier: str) -> None:
    """属性 5：将标识符嵌入 os.environ.get("KEY") 格式写入 .py 文件后，
    detect_used_keys() 应返回包含该标识符的列表。

    **Validates: Requirements 3.1, 3.2**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")
        mgr = EnvManager(env_path=env_path)

        # 写入包含 os.environ.get("KEY") 格式的 .py 文件
        py_content = f'value = os.environ.get("{identifier}")\n'
        py_file = os.path.join(tmpdir, "test_script.py")
        with open(py_file, "w", encoding="utf-8") as fh:
            fh.write(py_content)

        result = mgr.detect_used_keys(tmpdir)

        assert identifier in result, (
            f"detect_used_keys() 未检测到标识符 {identifier!r}，返回结果: {result!r}"
        )


# ---------------------------------------------------------------------------
# 属性 6：检测结果去重
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

@given(
    identifier=_identifier_strategy,
    repeat_count=st.integers(min_value=2, max_value=10),
    file_count=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_detect_used_keys_deduplication(
    identifier: str, repeat_count: int, file_count: int
) -> None:
    """属性 6：包含重复 os.environ.get("KEY") 引用的文件集合，
    detect_used_keys() 返回列表中每个 key 只出现一次。

    **Validates: Requirements 3.3**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")
        mgr = EnvManager(env_path=env_path)

        # 在多个文件中写入重复的 os.environ.get("KEY") 引用
        for i in range(file_count):
            py_content = "\n".join(
                [f'v{j} = os.environ.get("{identifier}")' for j in range(repeat_count)]
            ) + "\n"
            py_file = os.path.join(tmpdir, f"script_{i}.py")
            with open(py_file, "w", encoding="utf-8") as fh:
                fh.write(py_content)

        result = mgr.detect_used_keys(tmpdir)

        # 验证结果中每个 key 只出现一次（列表无重复）
        assert len(result) == len(set(result)), (
            f"detect_used_keys() 返回了重复的 key：{result!r}"
        )

        # 验证目标标识符在结果中只出现一次
        count = result.count(identifier)
        assert count == 1, (
            f"标识符 {identifier!r} 在结果中出现了 {count} 次，期望 1 次"
        )


# ---------------------------------------------------------------------------
# 属性 7：会话列表降序排列
# Validates: Requirements 4.1, 4.4
# ---------------------------------------------------------------------------

@given(
    session_names=st.lists(
        # 生成类似时间戳格式的会话目录名，确保可排序且互不相同
        st.from_regex(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}", fullmatch=True),
        min_size=2,
        max_size=20,
        unique=True,
    )
)
@settings(max_examples=100)
def test_list_sessions_descending_order(session_names: list) -> None:
    """属性 7：对于任意数量（≥2）的会话目录，list_sessions() 返回结果应严格降序排列。

    **Validates: Requirements 4.1, 4.4**
    """
    from runtime.session_manager import SessionManager

    with tempfile.TemporaryDirectory() as tmpdir:
        # 在临时目录下创建各会话子目录，并写入 index 条目
        mgr = SessionManager(chats_dir=tmpdir)
        for name in session_names:
            os.makedirs(os.path.join(tmpdir, name), exist_ok=True)
            mgr.on_session_created(name)

        result = mgr.list_sessions()

        # 验证返回数量与创建数量一致
        assert len(result) == len(session_names), (
            f"list_sessions() 返回 {len(result)} 个会话，期望 {len(session_names)} 个"
        )

        # 验证严格降序：每个元素的 last_inference_at 都大于等于其后一个元素
        # （由于 on_session_created 在同一秒内调用，时间戳可能相同，用 session_id 作为次要排序）
        result_ids = [e["session_id"] for e in result]
        for i in range(len(result_ids) - 1):
            assert result[i].get("last_inference_at", "") >= result[i + 1].get("last_inference_at", ""), (
                f"list_sessions() 未按 last_inference_at 降序排列：result[{i}]={result[i]!r} "
                f"不大于等于 result[{i+1}]={result[i+1]!r}"
            )


# ---------------------------------------------------------------------------
# 属性 8：会话数据读取 round-trip
# Validates: Requirements 5.1
# ---------------------------------------------------------------------------

# 合法 role 策略
_role_strategy = st.sampled_from(["user", "assistant", "tool"])

# 单条消息策略（排除 NUL 字节和 Unicode 代理字符，确保 JSON 序列化兼容）
_message_strategy = st.fixed_dictionaries({
    "role": _role_strategy,
    "content": st.text(
        alphabet=st.characters(
            blacklist_characters="\x00",
            blacklist_categories=("Cs",),  # 排除代理字符（surrogate）
        ),
        max_size=200,
    ),
})


@given(
    messages=st.lists(
        _message_strategy,
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=100)
def test_get_session_roundtrip(messages: list) -> None:
    """属性 8：通过 ContextManager.save_conversation() 保存的会话，
    调用 SessionManager.get_session() 读取后，messages 数组长度及每条消息的
    role、content 应与保存时一致。

    **Validates: Requirements 5.1**
    """
    from runtime.context_manager import ContextManager, ConversationTurn
    from runtime.session_manager import SessionManager

    with tempfile.TemporaryDirectory() as tmpdir:
        # 构造 ContextManager，使用临时目录作为 chats_dir
        ctx_mgr = ContextManager(
            infer_fn=lambda req: None,
            chats_dir=tmpdir,
        )

        # 创建会话目录
        session_id = ctx_mgr.create_session()

        # 将测试消息转换为 ConversationTurn 列表
        turns = [
            ConversationTurn(
                role=msg["role"],
                content=msg["content"],
                timestamp="2026-01-01T00:00:00",
            )
            for msg in messages
        ]

        # 通过 ContextManager 保存会话
        ctx_mgr.save_conversation(session_id, turns)

        # 通过 SessionManager 读取会话
        session_mgr = SessionManager(chats_dir=tmpdir)
        data = session_mgr.get_session(session_id)

        # 验证 messages 数组长度一致
        loaded_messages = data.get("messages", [])
        assert len(loaded_messages) == len(messages), (
            f"messages 数组长度不一致：保存 {len(messages)} 条，读取到 {len(loaded_messages)} 条"
        )

        # 验证每条消息的 role 和 content 一致
        for i, (original, loaded) in enumerate(zip(messages, loaded_messages)):
            assert loaded["role"] == original["role"], (
                f"第 {i} 条消息 role 不一致：保存 {original['role']!r}，读取 {loaded['role']!r}"
            )
            assert loaded["content"] == original["content"], (
                f"第 {i} 条消息 content 不一致：保存 {original['content']!r}，读取 {loaded['content']!r}"
            )


# ---------------------------------------------------------------------------
# 属性 9：写操作响应包含完整键值对列表
# Validates: Requirements 6.5
# ---------------------------------------------------------------------------

import json as _json
import urllib.request
import urllib.error
from unittest.mock import patch

from runtime.server import RuntimeHTTPServer
from runtime.runtime import Runtime
from runtime.registry import ModelRegistry, ToolRegistry


def _post_env(port: int, key: str, value: str) -> tuple:
    """向测试服务器发送 POST /v1/env 请求，返回 (status, body)。"""
    payload = _json.dumps({"key": key, "value": value}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/env",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json.loads(exc.read())


def _get_env(port: int) -> tuple:
    """向测试服务器发送 GET /v1/env 请求，返回 (status, body)。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/env")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json.loads(exc.read())


@given(
    operations=st.lists(
        st.tuples(
            _env_key_strategy,   # key
            _env_value_strategy, # value
        ),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=50, deadline=None)
def test_post_env_response_matches_file_content(operations: list) -> None:
    """属性 9：对于任意有效的 POST /v1/env 请求序列，
    每次响应体中的 env 对象应与 env.json 实际内容完全一致（不多不少）。

    **Validates: Requirements 6.5**
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = os.path.join(tmpdir, "env.json")
        models_path = os.path.join(tmpdir, "models.json")
        tools_path = os.path.join(tmpdir, "tools.json")
        prompt_templates_path = os.path.join(tmpdir, "prompt_templates.json")
        chats_dir = os.path.join(tmpdir, "chats")

        runtime = Runtime(ModelRegistry(), ToolRegistry())

        with patch("runtime.server._MODELS_PATH", models_path), \
             patch("runtime.server._TOOLS_PATH", tools_path), \
             patch("runtime.server._PROMPT_TEMPLATES_PATH", prompt_templates_path), \
             patch("runtime.server._DATA_DIR", tmpdir), \
             patch("runtime.server._ENV_PATH", env_path):
            srv = RuntimeHTTPServer(runtime, host="127.0.0.1", port=0, chats_dir=chats_dir)
            srv.start_background()
            try:
                port = srv.port
                for key, value in operations:
                    status, body = _post_env(port, key, value)
                    assert status == 200, f"POST /v1/env 返回非 200 状态码: {status}"
                    assert "env" in body, "响应体中缺少 env 字段"

                    # 读取 env.json 实际内容
                    if os.path.isfile(env_path):
                        with open(env_path, "r", encoding="utf-8") as fh:
                            actual_file_content = _json.load(fh)
                    else:
                        actual_file_content = {}

                    # 验证响应体中的 env 与文件内容完全一致
                    assert body["env"] == actual_file_content, (
                        f"响应体 env={body['env']!r} 与文件内容 {actual_file_content!r} 不一致"
                    )
            finally:
                srv.stop()

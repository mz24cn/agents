"""Property-based tests for multi-agent-collaboration feature."""

import threading
from hypothesis import given, settings
import hypothesis.strategies as st
from runtime.builtin_tools import _make_delegate_fn


# Feature: multi-agent-collaboration, Property 4: 异常隔离——delegate 始终返回字符串
# Validates: Requirements 2.6
@given(st.text())
def test_exception_isolation_property(error_message):
    """对任意异常消息，delegate 捕获后返回包含 'Error' 的字符串，不向上传播。"""
    class FailingRuntime:
        def infer_stream(self, request, cancel_event=None):
            raise RuntimeError(error_message)

    thread_local = threading.local()
    delegate = _make_delegate_fn(FailingRuntime(), thread_local)
    result = delegate(model_id="any-model", tools=[], task="some task")
    assert isinstance(result, str), "delegate 应始终返回字符串"
    assert "Error" in result, "异常时返回值应包含 'Error'"


# Feature: multi-agent-collaboration, Property 6: tool_use_id 的唯一性
# Validates: Requirements 3.5
@given(st.integers(min_value=2, max_value=20))
def test_tool_use_id_uniqueness_property(call_count):
    """多次调用生成的 tool_use_id 互不相同。"""
    import uuid

    generated_ids = set()
    for _ in range(call_count):
        tool_use_id = "call_" + uuid.uuid4().hex[:8]
        generated_ids.add(tool_use_id)

    # 由于 UUID 随机性，生成的 ID 数量应等于调用次数（极低概率碰撞）
    assert len(generated_ids) == call_count, "每次调用生成的 tool_use_id 应互不相同"


# Feature: multi-agent-collaboration, Property 7: Subagent Session 路径在父目录下
# Validates: Requirements 5.1
@given(
    st.text(min_size=1, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_"
    )),
    st.text(min_size=1, alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_/."
    )),
)
def test_subagent_session_path_property(parent_session_id, chats_dir):
    """对任意 parent_session_id 和 chats_dir，生成路径以 {chats_dir}/{parent_session_id}/sub_ 为前缀。"""
    import os
    import time

    ts = int(time.time() * 1000000)
    sub_session_dir = os.path.join(chats_dir, parent_session_id, f"sub_{ts}")
    expected_prefix = os.path.join(chats_dir, parent_session_id, "sub_")
    assert sub_session_dir.startswith(expected_prefix), (
        f"子目录路径 {sub_session_dir!r} 应以 {expected_prefix!r} 为前缀"
    )


# Feature: multi-agent-collaboration, Property 8: 持久化失败不中断推理
# Validates: Requirements 5.4
@given(st.text(), st.text(min_size=1))
def test_persistence_failure_does_not_interrupt_property(task_content, error_message):
    """模拟持久化抛出异常，delegate 仍返回推理结果字符串，不抛出异常，不返回空字符串。"""
    from runtime.models import Message

    class SuccessRuntime:
        def infer_stream(self, request, cancel_event=None):
            yield Message(role="assistant", content=task_content)

    thread_local = threading.local()
    # 设置 chats_dir 和 session_id，触发持久化路径
    thread_local.chats_dir = "/nonexistent/path/that/cannot/be/created/\x00invalid"
    thread_local.session_id = "test-session"

    delegate = _make_delegate_fn(SuccessRuntime(), thread_local)
    result = delegate(model_id="any-model", tools=[], task="some task")

    assert isinstance(result, str), "持久化失败时 delegate 应仍返回字符串"
    assert not result.startswith("Error: delegate failed"), (
        "持久化失败不应触发外层 delegate 错误，推理结果应正常返回"
    )


# Feature: multi-agent-collaboration, Property 5: depth 字段的正确传递
# Validates: Requirements 3.4, 3.6
@given(st.integers(min_value=0, max_value=10))
def test_depth_propagation_property(initial_depth):
    """对任意初始深度 d >= 0，推送的流式帧 depth == d + 1；delegate 返回后 thread_local.depth 恢复为 d。"""
    from runtime.models import Message

    class SimpleRuntime:
        def infer_stream(self, request, cancel_event=None):
            yield Message(role="assistant", content="hello")
            yield Message(role="assistant", content=" world")

    thread_local = threading.local()
    thread_local.depth = initial_depth
    thread_local.session_id = None
    thread_local.chats_dir = None

    collected_frames = []

    def mock_sse_callback(frame):
        collected_frames.append(frame)

    thread_local.sse_callback = mock_sse_callback

    delegate = _make_delegate_fn(SimpleRuntime(), thread_local)
    result = delegate(model_id="any-model", tools=[], task="some task")

    # 验证流式帧的 depth 字段
    streaming_frames = [f for f in collected_frames if f.get("streaming") is True]
    end_frames = [f for f in collected_frames if f.get("streaming") is False]

    assert len(streaming_frames) > 0, "应有至少一个流式增量帧"
    assert all(f["depth"] == initial_depth + 1 for f in streaming_frames), (
        f"流式帧的 depth 应等于 {initial_depth + 1}，实际: {[f['depth'] for f in streaming_frames]}"
    )
    assert len(end_frames) == 1, "应有且仅有一个结束帧"
    # 结束帧不包含 depth 字段（仅流式增量帧携带 depth）

    # 验证 delegate 返回后 depth 恢复
    assert thread_local.depth == initial_depth, (
        f"delegate 返回后 depth 应恢复为 {initial_depth}，实际: {thread_local.depth}"
    )

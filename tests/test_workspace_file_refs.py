"""``<file>`` 引用展开：本地工作区与远程（子端）执行。

会话持久化的是**展开后**的形态（``[Image file attached: …]`` /
``[Text file attached: …]`` + 代码块），前端与推理都按这份现成内容工作：

* 本地会话：按母端工作区解析路径，图片把**路径**记到 ``message.images``，
  字节留给协议层按需读取（conversation.json 不塞 base64）。
* 远程会话：实体在**子端**文件系统上，母端没有这些字节，必须经子端的
  ``GET /v1/workspace/content`` 回拉到母环境才能进上下文——文本在展开时内联，
  图片在请求构造时经 ``InferenceRequest.image_resolver`` 取一次。
"""

import base64
import urllib.parse

import pytest

from runtime.handler_infer import _MAX_REMOTE_REF_BYTES, _make_remote_ref_hooks
from runtime.models import Message
from runtime.workspace_manager import expand_workspace_file_refs


# ---------------------------------------------------------------------------
# 本地会话
# ---------------------------------------------------------------------------


class TestLocalExpansion:
    def test_image_reference_records_the_path_not_base64(self, tmp_path):
        image = tmp_path / "bobo.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
        msg = Message(role="user", content="<file>bobo.jpg</file>用几句古诗描述图片内容")

        expanded = expand_workspace_file_refs([msg], str(tmp_path))[0]

        assert expanded.content == (
            f"[Image file attached: {image}]用几句古诗描述图片内容"
        )
        # 持久化里是路径；转 base64 只发生在构造模型请求时
        assert expanded.images == [str(image)]

    def test_text_reference_inlines_a_code_block_before_the_prompt(self, tmp_path):
        source = tmp_path / "a.py"
        source.write_text("print(1)", encoding="utf-8")
        msg = Message(role="user", content="看 <file>a.py</file> 有什么问题")

        expanded = expand_workspace_file_refs([msg], str(tmp_path))[0]

        assert expanded.content == (
            f"[Text file attached: {source}]\n```\nprint(1)\n```\n\n"
            f"看 [Text file attached: {source}] 有什么问题"
        )
        assert expanded.images is None


# ---------------------------------------------------------------------------
# 远程会话：母端经 /v1/workspace/content 回拉子端文件
# ---------------------------------------------------------------------------


class _FakeProxy:
    """最小远程代理：只实现 http_bytes，按 query 里的 path 返回字节。"""

    def __init__(self, payloads: dict, status: int = 200):
        self.payloads = payloads
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def http_bytes(self, url, timeout=None):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        path = query.get("path", [""])[0]
        self.calls.append((path, query.get("restrict", [""])[0]))
        if self.status != 200:
            return self.status, b"no such file"
        data = self.payloads.get(path)
        if data is None:
            return 404, b"File does not exist"
        return 200, data


class TestRemoteRefHooks:
    def test_text_is_pulled_from_the_child_unrestricted(self):
        # 粘贴目录（子端 /tmp）与工作区外的路径也要能读：引用本身已被用户授权。
        proxy = _FakeProxy({"/tmp/paste/a.py": b"print('child')"})
        read, _ = _make_remote_ref_hooks(proxy)

        assert read("/tmp/paste/a.py") == b"print('child')"
        assert proxy.calls == [("/tmp/paste/a.py", "0")]

    def test_image_reference_becomes_a_data_uri_for_the_request(self):
        proxy = _FakeProxy({"/child/work/pic.png": b"\x89PNG\r\n\x1a\n"})
        _, resolve_image = _make_remote_ref_hooks(proxy)

        resolved = resolve_image("/child/work/pic.png")

        assert resolved.startswith("data:image/png;base64,")
        assert base64.b64decode(resolved.split(",", 1)[1]) == b"\x89PNG\r\n\x1a\n"
        assert proxy.calls == [("/child/work/pic.png", "0")]

    def test_bytes_are_fetched_once_per_reference_per_request(self):
        proxy = _FakeProxy({"/child/a.py": b"A"})
        read, resolve_image = _make_remote_ref_hooks(proxy)

        read("/child/a.py")
        read("/child/a.py")

        assert len(proxy.calls) == 1

    def test_already_encodable_sources_pass_through(self):
        proxy = _FakeProxy({})
        _, resolve_image = _make_remote_ref_hooks(proxy)

        assert resolve_image("data:image/png;base64,AAA") == "data:image/png;base64,AAA"
        assert resolve_image("https://x.test/a.png") == "https://x.test/a.png"
        assert proxy.calls == []

    def test_missing_child_file_raises_instead_of_silently_dropping(self):
        proxy = _FakeProxy({})
        read, _ = _make_remote_ref_hooks(proxy)

        with pytest.raises(ValueError, match="not readable on the remote environment"):
            read("/child/gone.png")

    def test_oversized_reference_raises(self):
        proxy = _FakeProxy({"/child/big.png": b"x" * (_MAX_REMOTE_REF_BYTES + 1)})
        read, _ = _make_remote_ref_hooks(proxy)

        with pytest.raises(ValueError, match="too large"):
            read("/child/big.png")


class TestRemoteExpansion:
    """``remote_reader`` 在场即远程会话：占位符与 images 都保留用户写的子端引用。"""

    def test_text_content_is_inlined_from_the_child(self):
        proxy = _FakeProxy({"/tmp/paste/a.py": b"print('child')"})
        read, _ = _make_remote_ref_hooks(proxy)
        msg = Message(role="user", content="看 <file>/tmp/paste/a.py</file> 有什么问题")

        expanded = expand_workspace_file_refs([msg], "/parent/ws", remote_reader=read)[0]

        assert expanded.content == (
            "[Text file attached: /tmp/paste/a.py]\n```\nprint('child')\n```\n\n"
            "看 [Text file attached: /tmp/paste/a.py] 有什么问题"
        )

    def test_image_reference_is_kept_verbatim_for_request_time(self):
        """不在母端落盘：conversation.json 记的是子端引用，字节由 resolver 取。"""
        proxy = _FakeProxy({"/tmp/paste/shot.png": b"\x89PNG"})
        read, resolve_image = _make_remote_ref_hooks(proxy)
        msg = Message(role="user", content="<file>/tmp/paste/shot.png</file>描述这张图")

        expanded = expand_workspace_file_refs([msg], "/parent/ws", remote_reader=read)[0]

        assert expanded.content == "[Image file attached: /tmp/paste/shot.png]描述这张图"
        assert expanded.images == ["/tmp/paste/shot.png"]
        # 推理真正要用时才回拉，且拿到的是子端字节
        assert resolve_image(expanded.images[0]).startswith("data:image/png;base64,")

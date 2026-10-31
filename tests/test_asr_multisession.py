#coding=utf-8
"""
ASR 多路并发（max_sessions > 1）测试

回归的 bug：流式解码状态（FunASR 的 cache / pending / acc_text）挂在 engine 实例上，
两路会话共用一份——A 的 segment_start 调 stream_reset() 会把 B 正在解码的状态清空，
两路文字互相污染。修法是 StreamState 按路隔离（asr_engines.StreamState）。

跑法：
    conda run -n moneyprinter python -m pytest tests/test_asr_multisession.py -v
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import numpy as np
import pytest

websockets = pytest.importorskip("websockets")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASR_DIR = os.path.join(REPO, "accessories", "ASR")
if ASR_DIR not in sys.path:
    sys.path.insert(0, ASR_DIR)

import asr_engines as E                     # noqa: E402

WS_PATH = "/asr/ws"


def speech(seconds=1.0, level=12000, sr=16000) -> np.ndarray:
    n = int(seconds * sr)
    t = np.arange(n) / sr
    carrier = np.sin(2 * np.pi * 220 * t) + 0.5 * np.sin(2 * np.pi * 660 * t)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    return (carrier * env * level).astype("<i2")


def silence(seconds=1.0, sr=16000, noise=30) -> np.ndarray:
    rng = np.random.default_rng(1234)
    return rng.normal(0, noise, int(seconds * sr)).astype("<i2")


# ---------------------------------------------------------------------------
# 单元：StreamState 隔离
# ---------------------------------------------------------------------------

def _mock():
    eng = E.build_engine("mock", device="cpu")
    eng.load()
    return eng


def _step(eng) -> int:
    """mock 引擎每 600ms 出一个词。"""
    return int(0.6 * eng.spec.sample_rate)


def _feed(eng, state, chunks: int) -> str:
    text = ""
    for _ in range(chunks):
        text = eng.stream_feed(np.zeros(_step(eng), dtype=np.float32), state)
    return text


def test_stream_states_are_isolated():
    """两路各自累计，互不叠加、互不截断。"""
    eng = _mock()
    a, b = eng.new_stream("A"), eng.new_stream("B")

    assert _feed(eng, a, 3) == "今天天气不错"
    assert _feed(eng, b, 1) == "今天"
    assert eng.stream_flush(a) == "今天天气不错。"
    assert eng.stream_flush(b) == "今天。"


def test_reset_one_stream_leaves_others_alone():
    """A 复位（segment_start 会做）不能影响正在解码的 B。"""
    eng = _mock()
    a, b = eng.new_stream("A"), eng.new_stream("B")
    _feed(eng, a, 3)
    _feed(eng, b, 2)

    eng.stream_reset(a)
    assert _feed(eng, a, 1) == "今天"          # A 从零开始
    assert _feed(eng, b, 1) == "今天天气不错"   # B 继续，没被清空


def test_default_stream_is_its_own_lane():
    """不传 state 的旧调用（warmup / 整段转写）走兜底那一路，也不被会话影响。"""
    eng = _mock()
    assert _feed(eng, None, 3) == "今天天气不错"
    a = eng.new_stream("A")
    assert _feed(eng, a, 1) == "今天"
    assert eng.stream_flush() == "今天天气不错。"


def test_stream_state_reset_clears_everything():
    eng = _mock()
    st = eng.new_stream("X")
    _feed(eng, st, 3)
    st.reset()
    assert st.acc_text == "" and st.pending.size == 0 and st.cache == {} and st.extra == {}
    assert _feed(eng, st, 1) == "今天"


class _FakeFunasrModel:
    """假 funasr AutoModel：把喂过的 chunk 数记在 cache 里——cache 就是状态隔离的探针。"""

    def generate(self, input=None, cache=None, chunk_size=None, is_final=False):
        cache["n"] = int(cache.get("n", 0)) + 1
        return [{"text": f"字{cache['n']}"}]


def _fake_streaming_engine():
    eng = E.build_engine("paraformer-streaming", device="cpu", punc=False)
    eng._new_model = lambda extra=None: _FakeFunasrModel()
    eng.load()
    return eng


def test_funasr_streaming_cache_is_per_state():
    """真流式引擎类：cache 必须按路走，不能落在 engine 实例上。"""
    eng = _fake_streaming_engine()
    a, b = eng.new_stream("A"), eng.new_stream("B")
    step = eng._step()

    ta = tb = ""
    for _ in range(3):
        ta = eng.stream_feed(np.zeros(step, dtype=np.float32), a)
    tb = eng.stream_feed(np.zeros(step, dtype=np.float32), b)

    assert ta == "字1字2字3", ta
    assert tb == "字1", tb
    assert a.cache["n"] == 3 and b.cache["n"] == 1

    eng.stream_reset(a)                      # A 开口（segment_start）
    assert a.cache == {} and b.cache["n"] == 1
    assert eng.stream_flush(b) == "字1字2"   # B 的段还在（flush 再走一个 chunk）
    assert b.cache == {}                     # flush 后 B 自己复位，与 A 无关


# ---------------------------------------------------------------------------
# 集成：起一个 max_sessions=2 的真服务
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, extra=None):
        self.port = _free_port()
        cmd = [sys.executable, os.path.join(ASR_DIR, "ASR_server.py"),
               "--engine", "mock", "--device", "cpu",
               "--port", str(self.port),
               "--lock-file", f"/tmp/asr_ms_{self.port}.lock",
               "--spool-dir", f"/tmp/asr_ms_spool_{self.port}",
               "--no-warmup", "--max-sessions", "2"]
        cmd += extra or []
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, cwd=ASR_DIR)

    def wait_ready(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"服务启动失败:\n{out[-3000:]}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health",
                                            timeout=2) as r:
                    if json.loads(r.read()).get("engine"):
                        return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("服务启动超时")

    def health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
            return json.loads(r.read())

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture(scope="module")
def server():
    s = Server()
    s.wait_ready()
    yield s
    s.stop()


def _url(port, sid):
    return f"ws://127.0.0.1:{port}{WS_PATH}?session_id={sid}"


async def _collect(ws, out, until="flush_ack", timeout=20):
    while True:
        d = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        out.append(d)
        if d.get("type") == until:
            return


async def _run_two_sessions(port):
    """两路同时说话：交替喂同一份音频，制造 A 的 segment_start 撞上 B 的段中。"""
    got = {"A": [], "B": []}
    async with websockets.connect(_url(port, "msA"), max_size=4 << 20) as wa, \
            websockets.connect(_url(port, "msB"), max_size=4 << 20) as wb:
        for w in (wa, wb):
            await w.send(json.dumps({"type": "config", "sample_rate": 16000,
                                     "vad": {"max_end_silence_ms": 400}}))
        await asyncio.wait_for(wa.recv(), 15)      # ready
        await asyncio.wait_for(wb.recv(), 15)

        pcm = np.concatenate([speech(2.0), silence(0.8), speech(2.0), silence(0.8)])
        frame = 640
        for i in range(0, len(pcm), frame):
            await wa.send(pcm[i:i + frame].tobytes())
            await wb.send(pcm[i:i + frame].tobytes())
            await asyncio.sleep(0.002)
        for w in (wa, wb):
            await w.send(json.dumps({"type": "flush"}))
        await asyncio.wait_for(_collect(wa, got["A"]), 25)
        await asyncio.wait_for(_collect(wb, got["B"]), 25)
    return got


def _finals(msgs):
    return [m["text"] for m in msgs if m.get("type") == "final"]


def _partials_by_seg(msgs):
    by = {}
    for m in msgs:
        if m.get("type") == "partial":
            by.setdefault(m["seg_id"], []).append(m["text"])
    return by


def test_two_sessions_accepted_and_counted(server):
    assert server.health()["max_sessions"] == 2
    got = asyncio.run(_run_two_sessions(server.port))
    assert got["A"] and got["B"]


def test_two_sessions_do_not_pollute_each_other(server):
    """核心回归：两路各自拿到完整、单调增长、彼此一致的转写。"""
    got = asyncio.run(_run_two_sessions(server.port))

    for tag in ("A", "B"):
        finals = _finals(got[tag])
        assert len(finals) >= 2, (tag, finals)
        assert all(t.strip() for t in finals), (tag, finals)
        for sid, texts in _partials_by_seg(got[tag]).items():
            assert all(b.startswith(a) for a, b in zip(texts, texts[1:])), \
                f"{tag}/{sid} partial 被另一路污染: {texts}"

    # 两路喂的是同一份音频：状态隔离时结果必须一致
    assert _finals(got["A"]) == _finals(got["B"]), (_finals(got["A"]), _finals(got["B"]))


def test_third_session_still_rejected(server):
    """上限仍然生效：第三路被 busy 拒绝。"""
    async def _hold_then_third():
        async with websockets.connect(_url(server.port, "hold1")) as w1, \
                websockets.connect(_url(server.port, "hold2")) as w2:
            await asyncio.wait_for(w1.recv(), 15)
            await asyncio.wait_for(w2.recv(), 15)
            try:
                async with websockets.connect(_url(server.port, "hold3")) as w3:
                    msgs = []
                    for _ in range(4):
                        msgs.append(json.loads(await asyncio.wait_for(w3.recv(), 10)))
                    return msgs
            except websockets.exceptions.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                return [("closed", code)]

    res = asyncio.run(_hold_then_third())
    codes = [m.get("code") for m in res if isinstance(m, dict)]
    closed = [c for kind, c in res if kind == "closed"] if res and isinstance(res[0], tuple) else []
    assert "busy" in codes or any(c in (4403, 1008) for c in closed), res

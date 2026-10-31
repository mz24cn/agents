#coding=utf-8
"""
ASR 流式服务测试（不需要 GPU / 不需要模型）

- 单元：VAD 分段与判停、音频工具、引擎文本合并、引擎注册表
- 集成：用 --engine mock 起真实服务端，跑完整 WebSocket 协议 + HTTP 控制面

跑法：
    conda run -n moneyprinter python -m pytest tests/test_asr_streaming.py -v
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

websockets = pytest.importorskip("websockets")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASR_DIR = os.path.join(REPO, "accessories", "ASR")
if ASR_DIR not in sys.path:
    sys.path.insert(0, ASR_DIR)

import asr_audio as A                     # noqa: E402
import asr_engines as E                   # noqa: E402
import asr_vad as V                       # noqa: E402

WS_PATH = "/asr/ws"                        # 与 ASR_server.WS_PATH 一致


def speech(seconds=1.0, level=12000, sr=16000) -> np.ndarray:
    """带包络的类语音信号（能量远高于噪声门）。"""
    n = int(seconds * sr)
    t = np.arange(n) / sr
    carrier = np.sin(2 * np.pi * 220 * t) + 0.5 * np.sin(2 * np.pi * 660 * t)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    return (carrier * env * level).astype("<i2")


def silence(seconds=1.0, sr=16000, noise=30) -> np.ndarray:
    rng = np.random.default_rng(1234)
    return rng.normal(0, noise, int(seconds * sr)).astype("<i2")


# ---------------------------------------------------------------------------
# VAD
# ---------------------------------------------------------------------------

def test_vad_emits_start_and_end():
    vad = V.StreamVad(max_end_silence_ms=500, min_speech_ms=200)
    events = vad.feed(speech(1.5)) + vad.feed(silence(1.0))
    kinds = [e.kind for e in events]
    assert kinds == [V.START, V.END], kinds
    start, end = events
    assert start.start_ms <= 100
    assert 1400 <= end.end_ms <= 1700
    assert end.reason == "vad"


def test_vad_endpoints_multiple_sentences():
    vad = V.StreamVad(max_end_silence_ms=400, min_speech_ms=200)
    events = []
    for _ in range(3):
        events += vad.feed(speech(1.0)) + vad.feed(silence(0.8))
    ends = [e for e in events if e.kind == V.END]
    assert len(ends) == 3
    assert ends[0].end_ms < ends[1].start_ms < ends[1].end_ms < ends[2].start_ms


def test_vad_rejects_short_blip():
    """短促噪声：START 已经发出，但必须用 CANCEL 撤销，不能留一个没有 final 的段。"""
    vad = V.StreamVad(min_speech_ms=300, max_end_silence_ms=300)
    events = vad.feed(speech(0.1)) + vad.feed(silence(1.0))
    assert [e.kind for e in events] == [V.START, V.CANCEL]
    assert events[1].reason == "short"
    assert vad.stats["rejected_short"] == 1


def test_vad_force_cuts_long_segment():
    vad = V.StreamVad(max_segment_ms=2000, min_speech_ms=200)
    events = vad.feed(speech(6.0))
    ends = [e for e in events if e.kind == V.END]
    assert len(ends) >= 2
    assert all(e.reason == "max_len" for e in ends)
    assert ends[0].end_ms - ends[0].start_ms <= 2100


def test_vad_force_end_closes_open_segment():
    vad = V.StreamVad(min_speech_ms=200)
    vad.feed(speech(1.0))
    events = vad.force_end()
    assert [e.kind for e in events] == [V.END]
    assert events[0].reason == "flush"


def test_vad_ignores_background_noise():
    vad = V.StreamVad(min_speech_ms=200)
    events = vad.feed(silence(3.0))
    assert events == []
    assert vad.stats["segments"] == 0


def test_vad_adaptive_floor_tracks_noise():
    """稳态噪声（风扇/电流声）：底噪自适应抬升，误开的段会自己判停且不再重开。"""
    vad = V.StreamVad(min_speech_ms=200, max_end_silence_ms=800)
    vad.feed(silence(3.0, noise=3000))
    assert vad.threshold_db() > -42.0                 # 底噪已自适应抬升
    events = vad.feed(silence(2.0, noise=3200))
    assert all(e.kind != V.START for e in events), events
    assert not vad.in_speech()                        # 误判的段自己判停了
    # 底噪不能无限抬：正常音量的语音仍要能识别
    assert any(e.kind == V.START for e in vad.feed(speech(1.0, level=24000)))


def test_vad_handles_odd_and_tiny_writes():
    vad = V.StreamVad(min_speech_ms=100)
    data = speech(0.5).tobytes() + b"\x01"      # 奇数字节
    events = vad.feed(np.frombuffer(data, dtype="<u1"))
    assert isinstance(events, list)


# ---------------------------------------------------------------------------
# 音频工具
# ---------------------------------------------------------------------------

def test_pcm_bytes_roundtrip_and_odd_truncation():
    pcm = speech(0.2)
    back = A.pcm_bytes_to_int16(pcm.tobytes())
    assert np.array_equal(pcm, back)
    assert len(A.pcm_bytes_to_int16(pcm.tobytes() + b"\x00")) == len(pcm)


def test_resample_preserves_duration():
    pcm = speech(2.0)
    out = A.pcm16_resample(pcm, 16000, 8000)
    assert abs(len(out) - 8000 * 2) <= 80
    out2 = A.pcm16_resample(out, 8000, 16000)
    assert abs(len(out2) - 16000 * 2) <= 160


def test_wav_roundtrip(tmp_path):
    pcm = speech(0.5)
    p = str(tmp_path / "a.wav")
    A.write_wav(p, pcm, 16000)
    back, sr = A.read_wav(p)
    assert sr == 16000
    assert np.array_equal(pcm, back)


def test_decode_mp3_uses_ffmpeg(tmp_path):
    if not A.ffmpeg_available():
        pytest.skip("ffmpeg 不可用")
    src = str(tmp_path / "s.wav")
    A.write_wav(src, speech(1.0), 16000)
    mp3 = str(tmp_path / "s.mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, mp3], check=True)
    pcm = A.decode_to_pcm16(mp3, 16000)
    assert abs(len(pcm) - 16000) < 3200


def test_normalize_gain_bounded():
    """轻音频会被放大，但受 max_gain_db 限制且不削波。"""
    quiet = speech(0.5, level=300)
    out = A.normalize_gain(quiet)
    assert np.abs(out.astype(np.int64)).max() > np.abs(quiet.astype(np.int64)).max() * 3
    assert np.abs(out.astype(np.int64)).max() <= 32767
    # 已经够响的音频不应被继续放大
    loud = speech(0.5, level=12000)
    assert np.abs(A.normalize_gain(loud).astype(np.int64)).max() <= \
        np.abs(loud.astype(np.int64)).max()
    # 全静音原样返回
    zeros = np.zeros(1600, dtype="<i2")
    assert np.array_equal(A.normalize_gain(zeros), zeros)


# ---------------------------------------------------------------------------
# 引擎层（不加载真模型）
# ---------------------------------------------------------------------------

def test_merge_stream_text_variants():
    assert E._merge_stream_text("", "你好") == "你好"
    assert E._merge_stream_text("你好", "你好，世界") == "你好，世界"   # 累计式
    assert E._merge_stream_text("你好", "你好") == "你好"              # 重复块
    assert E._merge_stream_text("你好", "世界") == "你好世界"          # 增量式


def test_extract_text_handles_funasr_shapes():
    assert E._extract_text([{"text": "你好"}]) == "你好"
    assert E._extract_text({"text": "你好"}) == "你好"
    assert E._extract_text(None) == ""
    assert E._extract_text([{"text": "<|zh|>你好"}]) == "你好"


def test_engine_aliases_and_registry():
    assert E.resolve_engine_name("paraformer-zh-streaming") == "paraformer-streaming"
    assert E.resolve_engine_name("streaming") == "paraformer-streaming"
    assert E.resolve_engine_name("sensevoice-small") == "sensevoice"
    with pytest.raises(E.EngineError):
        E.resolve_engine_name("nope")
    for name in ("paraformer-streaming", "sensevoice", "qwen3-asr-1.7b", "mock"):
        assert name in E.ENGINE_SPECS
        assert E.ENGINE_SPECS[name].min_vram_mb >= 0


def test_engine_streaming_flag_follows_spec():
    """ready 事件里的 streaming 决定客户端等不等 partial，必须与注册表一致。"""
    for name, spec in E.ENGINE_SPECS.items():
        eng = E.build_engine(name, punc=False)          # 只构造，不加载模型
        assert eng.supports_streaming == spec.streaming, (name, eng.supports_streaming)


def test_qwen_engines_are_cross_env():
    for name in ("qwen3-asr-0.6b", "qwen3-asr-1.7b"):
        spec = E.ENGINE_SPECS[name]
        assert spec.python_bin, "Qwen 引擎必须走独立 env 子进程"
        assert spec.dtype == "float16", "Pascal 无 bf16"


def test_mock_engine_streaming_accumulates():
    eng = E.build_engine("mock", punc=False)
    eng.load()
    eng.stream_reset()
    step = 960 * 10
    text = ""
    for _ in range(6):
        text = eng.stream_feed(np.zeros(step, dtype=np.float32))
    assert len(text) > 0
    final = eng.stream_flush()
    assert final.startswith(text) or final == text
    eng.stream_reset()
    assert eng.stream_feed(np.zeros(step, dtype=np.float32)) != ""


def test_offline_engine_rejects_stream_call():
    eng = E.build_engine("sensevoice", punc=False)     # 未加载
    with pytest.raises(Exception):
        eng.transcribe(np.zeros(16000, dtype=np.float32))


def test_vram_guard_blocks_small_gpu(monkeypatch):
    monkeypatch.setattr(E, "gpu_free_mb", lambda device="": (500.0, 11000.0, "GTX 1080Ti"))
    eng = E.build_engine("paraformer-streaming", device="cuda:0", punc=False)
    with pytest.raises(E.EngineUnavailable):
        eng.load()


# ---------------------------------------------------------------------------
# 集成：真实服务端 + WebSocket 协议（mock 引擎，无模型）
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, extra=None):
        self.ws_port = _free_port()
        self.http_port = self.ws_port        # WS 与 HTTP 同端口，按路径区分
        cmd = [sys.executable, os.path.join(ASR_DIR, "ASR_server.py"),
               "--engine", "mock", "--device", "cpu",
               "--port", str(self.ws_port),
               "--lock-file", f"/tmp/asr_test_{self.ws_port}.lock",
               "--spool-dir", f"/tmp/asr_test_spool_{self.ws_port}",
               "--no-warmup", "--max-sessions", "1"]
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
                with urllib.request.urlopen(f"http://127.0.0.1:{self.http_port}/health",
                                            timeout=2) as r:
                    if json.loads(r.read()).get("engine"):
                        return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("服务启动超时")

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


def http_json(port, path, payload=None, timeout=30):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def test_health_reports_engine_and_vad(server):
    h = http_json(server.http_port, "/health")
    assert h["status"] in ("ready", "warming")
    assert h["engine"] == "mock"
    assert h["max_sessions"] == 1
    assert h["vad"]["type"] == "energy"
    # punc 报的是「实际生效」而不是命令行原值：mock 不带标点 → 自动档应该加
    assert h["punc"] is True, h


def test_no_punc_flag_shows_up_in_health():
    """--no-punc 是显式覆盖，/health 里必须看得见（排查显存时靠这个判断）。"""
    s = Server(extra=["--engine", "mock", "--no-punc"])
    s.wait_ready()
    try:
        h = http_json(s.http_port, "/health")
        assert h["punc"] is False, h
        assert h["engine_detail"]["punc"] is False, h
    finally:
        s.stop()


def test_engines_endpoint_lists_all_backends(server):
    d = http_json(server.http_port, "/engines")
    names = {e["name"]: e for e in d["engines"]}
    for n in ("paraformer-streaming", "paraformer", "sensevoice", "whisper",
              "qwen3-asr-0.6b", "qwen3-asr-1.7b", "mock"):
        assert n in names
    assert names["qwen3-asr-0.6b"]["cross_env"] is True


def test_single_port_routes_ws_and_http(server):
    """WS 与 HTTP 共用一个端口：/asr/ws 走 WS，其它路径走 HTTP。"""
    assert server.http_port == server.ws_port

    async def _ws_on_shared_port():
        url = f"ws://127.0.0.1:{server.ws_port}{WS_PATH}?session_id=route1"
        async with websockets.connect(url) as ws:
            return json.loads(await asyncio.wait_for(ws.recv(), 15))

    ready = asyncio.run(_ws_on_shared_port())
    assert ready["type"] == "ready" and ready["engine"] == "mock"
    # 同一端口的 HTTP 照常可用
    assert http_json(server.ws_port, "/health")["engine"] == "mock"
    assert WS_PATH in http_json(server.ws_port, "/")["ws"]


def test_unknown_http_path_returns_json_error(server):
    try:
        http_json(server.ws_port, "/definitely/not/here")
        assert False, "应返回 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert json.loads(e.read())["ok"] is False


def test_ws_protocol_stream_and_final(server):
    """不用 pytest-asyncio：测试自己 asyncio.run。"""
    asyncio.run(_ws_protocol_stream_and_final(server))


async def _ws_protocol_stream_and_final(server):
    url = f"ws://127.0.0.1:{server.ws_port}/?session_id=t1"
    got = {"ready": None, "partials": [], "finals": [], "starts": [], "seq": []}
    async with websockets.connect(url, max_size=4 << 20) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                  "vad": {"max_end_silence_ms": 400}}))
        got["ready"] = json.loads(await ws.recv())

        async def pump():
            pcm = np.concatenate([speech(2.0), silence(1.0), speech(2.0)])
            frame = 640
            for i in range(0, len(pcm), frame):
                await ws.send(pcm[i:i + frame].tobytes())
                await asyncio.sleep(0.002)

        await pump()
        await ws.send(json.dumps({"type": "flush"}))

        async def reader():
            while True:
                d = json.loads(await ws.recv())
                t = d.get("type")
                got["seq"].append((t, d.get("seg_id")))
                if t == "partial":
                    got["partials"].append(d)
                elif t == "final":
                    got["finals"].append(d)
                elif t == "segment_start":
                    got["starts"].append(d)
                elif t == "flush_ack":
                    return

        await asyncio.wait_for(reader(), timeout=20)

    assert got["ready"]["type"] == "ready"
    assert got["ready"]["engine"] == "mock"
    assert got["starts"], "应有 segment_start"
    assert got["partials"], "应有 partial"
    assert got["finals"], "应有 final"
    f = got["finals"][0]
    assert f["text"]
    assert f["end_ms"] >= f["start_ms"]
    assert f["seg_id"] == got["starts"][0]["seg_id"]
    # partial 文本在同一 seg 内应单调增长（累计语义）
    by_seg = {}
    for p in got["partials"]:
        by_seg.setdefault(p["seg_id"], []).append(p["text"])
    for sid, texts in by_seg.items():
        assert all(b.startswith(a) for a, b in zip(texts, texts[1:])), (sid, texts)

    # 事件顺序不变式：partial 必须先有 segment_start；final 之后不能再有该段的陈旧 partial
    started, closed = set(), set()
    for t, sid in got["seq"]:
        if t == "segment_start":
            started.add(sid)
        elif t == "partial":
            assert sid in started, (sid, got["seq"])
            assert sid not in closed, (sid, got["seq"])
        elif t == "final":
            closed.add(sid)


def test_offline_partials_auto_by_engine():
    """换到非流式引擎时自动开滚动 partial，间隔按引擎 RTF 分档。"""
    want = {"sensevoice": 600, "paraformer": 700, "whisper": 1200,
            "qwen3-asr-0.6b": 1500, "qwen3-asr-1.7b": 0,      # 太慢，自动也不开
            "paraformer-streaming": 0, "mock": 0}             # 流式用不上
    for name, ms in want.items():
        eng = E.build_engine(name, punc=False)                # 只构造，不加载
        assert E.effective_offline_partials_ms(eng, -1) == ms, name
        assert E.effective_offline_partials_ms(eng, 0) == 0, name      # 显式关
        if not eng.supports_streaming:
            assert E.effective_offline_partials_ms(eng, 900) == 900, name  # 显式指定


def test_offline_engine_auto_emits_rolling_partials():
    """非流式引擎不传 --offline-partials-ms 也该边说边出（按整段重扫，带 rolling）。"""
    s = Server(extra=["--engine", "mock-offline"])
    s.wait_ready()
    try:
        h = http_json(s.http_port, "/health")
        assert h["engine"] == "mock-offline"
        assert h["offline_partials_ms"] == 600, h       # 自动档生效且可观测
        asyncio.run(_rolling_partials(s))
    finally:
        s.stop()


def test_offline_partials_zero_really_disables_them():
    s = Server(extra=["--engine", "mock-offline", "--offline-partials-ms", "0"])
    s.wait_ready()
    try:
        assert http_json(s.http_port, "/health")["offline_partials_ms"] == 0
        got = {"partials": [], "finals": []}

        async def _run():
            url = f"ws://127.0.0.1:{s.ws_port}/?session_id=roll0"
            async with websockets.connect(url, max_size=4 << 20) as ws:
                json.loads(await ws.recv())
                pcm = np.concatenate([speech(3.0), silence(1.0)])
                for i in range(0, len(pcm), 640):
                    await ws.send(pcm[i:i + 640].tobytes())
                    await asyncio.sleep(0.002)
                await ws.send(json.dumps({"type": "flush"}))
                while True:
                    d = json.loads(await ws.recv())
                    if d.get("type") == "partial":
                        got["partials"].append(d)
                    elif d.get("type") == "final":
                        got["finals"].append(d)
                    elif d.get("type") == "flush_ack":
                        break
        asyncio.run(_run())
        assert not got["partials"], got["partials"]
        assert got["finals"], "final 还是要有的"
    finally:
        s.stop()


def test_rolling_partial_interval_grows_with_segment():
    """固定间隔会让一段 T 秒音频重扫 T²/2N 秒；间隔随段长放大后总解码量才是 O(T)。"""
    s = Server(extra=["--engine", "mock-offline"])
    s.wait_ready()
    try:
        by_seg, starts = {}, {}

        async def _run():
            url = f"ws://127.0.0.1:{s.ws_port}/?session_id=rollgrow"
            async with websockets.connect(url, max_size=4 << 20) as ws:
                await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                          "vad": {"max_end_silence_ms": 900}}))
                json.loads(await ws.recv())
                t = np.arange(int(14.0 * 16000)) / 16000
                voice = (np.sin(2 * np.pi * 180 * t) + 0.6 * np.sin(2 * np.pi * 360 * t)
                         + 0.3 * np.sin(2 * np.pi * 720 * t)) * (0.85 + 0.15 * np.sin(2 * np.pi * 2.5 * t))
                pcm = (voice * 12000).astype("<i2")
                for i in range(0, len(pcm), 640):
                    await ws.send(pcm[i:i + 640].tobytes())
                    await asyncio.sleep(0.001)
                await ws.send(json.dumps({"type": "flush"}))
                while True:
                    d = json.loads(await ws.recv())
                    t2 = d.get("type")
                    if t2 == "segment_start":
                        starts[d["seg_id"]] = d["start_ms"]
                    elif t2 == "partial":
                        by_seg.setdefault(d["seg_id"], []).append(d["end_ms"])
                    elif t2 == "flush_ack":
                        break
        asyncio.run(_run())

        seg_id = max(by_seg, key=lambda k: len(by_seg[k]))
        longest = by_seg[seg_id]
        assert len(longest) >= 4, by_seg
        gaps = [b - a for a, b in zip(longest, longest[1:])]
        assert gaps[-1] > gaps[0], gaps            # 间隔确实在变大
        start = starts[seg_id]
        for end, gap in zip(longest, gaps):        # 且始终守住 max(600, 段已长/4)
            want = max(600, (end - start) // 4)
            assert gap >= want - 60, (end, gap, want)
    finally:
        s.stop()


async def _rolling_partials(s):
    url = f"ws://127.0.0.1:{s.ws_port}/?session_id=roll1"
    partials, finals = [], []
    async with websockets.connect(url, max_size=4 << 20) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                  "vad": {"max_end_silence_ms": 400}}))
        ready = json.loads(await ws.recv())
        # spec.streaming=False 必须真的落到引擎上，否则走的是流式分支
        assert ready["type"] == "ready" and ready["streaming"] is False, ready

        pcm = np.concatenate([speech(3.0), silence(1.0)])
        frame = 640
        for i in range(0, len(pcm), frame):
            await ws.send(pcm[i:i + frame].tobytes())
            await asyncio.sleep(0.002)
        await ws.send(json.dumps({"type": "flush"}))

        while True:
            d = json.loads(await ws.recv())
            t = d.get("type")
            if t == "partial":
                partials.append(d)
            elif t == "final":
                finals.append(d)
            elif t == "flush_ack":
                break

    assert partials, "非流式引擎开了 --offline-partials-ms 就该出 partial"
    assert all(p.get("rolling") for p in partials), partials
    assert finals and finals[0]["text"], finals


def test_max_len_split_keeps_whole_segment():
    """连续长句被 max_segment_ms 强制切段时，final 必须覆盖整段而不是只剩尾巴。"""
    s = Server(extra=["--max-segment-ms", "3000", "--max-end-silence-ms", "800"])
    s.wait_ready()
    try:
        asyncio.run(_max_len_split(s))
    finally:
        s.stop()


async def _max_len_split(s):
    url = f"ws://127.0.0.1:{s.ws_port}/?session_id=maxlen"
    finals, starts = [], []
    async with websockets.connect(url, max_size=4 << 20) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000}))
        assert json.loads(await ws.recv())["type"] == "ready"

        pcm = speech(10.0)                       # 全程有声，不会自然判停
        frame = 640
        for i in range(0, len(pcm), frame):
            await ws.send(pcm[i:i + frame].tobytes())
            await asyncio.sleep(0.002)
        await ws.send(json.dumps({"type": "flush"}))

        while True:
            d = json.loads(await ws.recv())
            if d["type"] == "final":
                finals.append(d)
            elif d["type"] == "segment_start":
                starts.append(d)
            elif d["type"] == "flush_ack":
                break

    assert len(finals) >= 3, finals              # 10s / 3s
    for f in finals[:-1]:
        assert f["end_ms"] - f["start_ms"] >= 2500, f
        assert len(f["text"]) >= 6, f            # mock 每 600ms 出一个词
    # 段与段之间必须首尾相接（强制切段不能把音频丢掉）
    for a, b in zip(finals, finals[1:]):
        assert b["start_ms"] <= a["end_ms"] + 400, (a, b)


def test_http_transcribe_raw_body(server, tmp_path):
    """POST /transcribe 直接 POST 音频字节（curl --data-binary @a.wav）。"""
    p = str(tmp_path / "in2.wav")
    A.write_wav(p, np.concatenate([speech(2.0), silence(0.5)]), 16000)
    with open(p, "rb") as f:
        raw = f.read()
    req = urllib.request.Request(f"http://127.0.0.1:{server.http_port}/transcribe?format=wav",
                                 data=raw, headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    assert out["ok"] is True
    assert out["text"]


def test_refine_final_uses_offline_engine():
    """--refine：partial 仍来自流式引擎，final 由离线引擎按整段重扫（更长）。"""
    s = Server(extra=["--refine", "mock-offline"])
    s.wait_ready()
    try:
        assert http_json(s.http_port, "/health")["refiner"] == "mock-offline"
        asyncio.run(_refine_check(s))
    finally:
        s.stop()


async def _refine_check(s):
    url = f"ws://127.0.0.1:{s.ws_port}/?session_id=refine"
    partials, finals = [], []
    async with websockets.connect(url, max_size=4 << 20) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                  "vad": {"max_end_silence_ms": 400}}))
        await ws.recv()                                   # ready
        pcm = np.concatenate([speech(1.0), silence(0.6)])
        for i in range(0, len(pcm), 640):
            await ws.send(pcm[i:i + 640].tobytes())
            await asyncio.sleep(0.002)
        await ws.send(json.dumps({"type": "flush"}))
        while True:
            d = json.loads(await ws.recv())
            t = d.get("type")
            if t == "partial":
                partials.append(d)
            elif t == "final":
                finals.append(d)
            elif t == "flush_ack":
                break
    assert partials and finals, (partials, finals)
    rough, fine = partials[-1]["text"], finals[-1]["text"]
    assert fine.startswith(rough) and len(fine) > len(rough), (rough, fine)


def test_ws_busy_rejection_second_session(server):
    asyncio.run(_ws_busy(server))


async def _ws_busy(server):
    async with websockets.connect(f"ws://127.0.0.1:{server.ws_port}/?session_id=a"):
        try:
            async with websockets.connect(f"ws://127.0.0.1:{server.ws_port}/?session_id=b") as ws2:
                msgs = []
                for _ in range(3):
                    try:
                        msgs.append(json.loads(await asyncio.wait_for(ws2.recv(), timeout=3)))
                    except asyncio.TimeoutError:
                        break
                assert any(m.get("code") == "busy" or m.get("type") == "error" for m in msgs), msgs
        except websockets.exceptions.ConnectionClosed as exc:
            assert exc.code in (4403, 1008) or "busy" in str(exc)


def test_ws_segment_cancel_for_short_blip(server):
    """咳嗽/杂音：客户端应收到 segment_cancel，而不是等一条永远不来的 final。"""
    asyncio.run(_cancel(server))


async def _cancel(server):
    url = f"ws://127.0.0.1:{server.ws_port}/?session_id=cancel1"
    seen = []
    async with websockets.connect(url) as ws:
        await ws.recv()                                   # ready
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                  "vad": {"min_speech_ms": 400,
                                          "max_end_silence_ms": 300}}))
        pcm = np.concatenate([speech(0.12), silence(0.6), speech(1.5), silence(0.6)])
        for i in range(0, len(pcm), 640):
            await ws.send(pcm[i:i + 640].tobytes())
        await ws.send(json.dumps({"type": "flush"}))
        while True:
            try:
                d = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                break
            seen.append(d.get("type"))
            if d.get("type") == "flush_ack":
                break
    assert "segment_cancel" in seen, seen
    assert "final" in seen, seen


def test_http_transcribe_wav(server, tmp_path):
    p = str(tmp_path / "in.wav")
    A.write_wav(p, np.concatenate([speech(2.0), silence(0.6), speech(1.5)]), 16000)
    out = http_json(server.http_port, "/transcribe", {"path": p})
    assert out["ok"] is True
    assert out["text"]
    assert out["audio_ms"] > 3000
    assert len(out["segments"]) >= 1


def test_http_transcribe_rejects_missing_file(server):
    try:
        http_json(server.http_port, "/transcribe", {"path": "/nope/x.wav"})
        assert False, "应返回错误"
    except urllib.error.HTTPError as e:
        assert e.code == 500


def test_session_resume_replays_missed_finals(server):
    """断线重连：带 last_seg_id 应补发漏掉的 final。"""
    asyncio.run(_resume(server))


async def _resume(server):
    url = f"ws://127.0.0.1:{server.ws_port}/?session_id=resume1"
    finals = []
    async with websockets.connect(url) as ws:
        pcm = np.concatenate([speech(2.0), silence(1.0)])
        for i in range(0, len(pcm), 640):
            await ws.send(pcm[i:i + 640].tobytes())
        await ws.send(json.dumps({"type": "flush"}))
        while True:
            d = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if d.get("type") == "final":
                finals.append(d)
            if d.get("type") == "flush_ack":
                break
        last = finals[-1]["seg_id"] if finals else 0

    async with websockets.connect(url + f"&last_seg_id={max(0, last - 1)}") as ws:
        replayed = []
        for _ in range(4):
            try:
                d = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                break
            if d.get("type") == "replay":
                replayed.append(d)
        assert replayed, "应补发漏掉的 final"


def test_silence_heartbeat_and_segment_lines(tmp_path):
    """「说完一段、停顿久了再说就没字」得能在日志里分开三种故障：
    段根本没切开 / 段切开了但引擎返回空 / 客户端不再送音频。
    静音心跳顺带把底噪和阈值打出来，AGC 抬高底噪那种一眼就能看出来。"""
    log = tmp_path / "server.log"
    s = Server(extra=["--log-file", str(log)])
    s.wait_ready()
    try:
        asyncio.run(_stream_then_long_silence(s))
        text, deadline = "", time.time() + 6
        while time.time() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if "静音心跳" in text:
                break
            time.sleep(0.3)
    finally:
        s.stop()
    assert "段开始" in text, text[-1500:]
    assert "段结束" in text, text[-1500:]
    assert "静音心跳" in text and "阈值=" in text, text[-1500:]


async def _stream_then_long_silence(server):
    url = f"ws://127.0.0.1:{server.ws_port}/asr/ws"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                  "vad": {"max_end_silence_ms": 400}}))
        await ws.recv()                                   # ready
        pcm = np.concatenate([speech(2.0), silence(9.0)])
        for i in range(0, len(pcm), 640):
            await ws.send(pcm[i:i + 640].tobytes())
            await asyncio.sleep(0.002)
        await ws.send(json.dumps({"type": "flush"}))

#coding=utf-8
"""
说话人分离（--spk）测试：不需要 GPU、不需要下载模型。

- 单元：SpeakerRegistry 在线聚类（同人合并 / 异人分开 / 人数上限 / 复位）
- 单元：SpeakerTagger 的 mock 指纹链路（短段沿用、开关关闭）
- 集成：--engine mock --spk mock 起真服务，断言 final 带 spk 标签、reset 会重数人

跑法：
    conda run -n moneyprinter python -m pytest tests/test_asr_speaker.py -v
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

import asr_engines as E                    # noqa: E402

WS_PATH = "/asr/ws"


def tone(freq=220.0, seconds=1.2, sr=16000, level=12000) -> np.ndarray:
    n = int(seconds * sr)
    t = np.arange(n) / sr
    return (np.sin(2 * np.pi * freq * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
            * level).astype("<i2")


def silence(seconds=0.8, sr=16000, noise=30) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.normal(0, noise, int(seconds * sr)).astype("<i2")


def unit(v):
    a = np.asarray(v, dtype=np.float64)
    return a / np.linalg.norm(a)


# ---------------------------------------------------------------------------
# 单元：聚类
# ---------------------------------------------------------------------------

def test_resolve_spk_model_aliases():
    assert E.resolve_spk_model("") == ""
    assert E.resolve_spk_model("campplus") == "iic/speech_campplus_sv_zh-cn_16k-common"
    assert E.resolve_spk_model("ERes2NetV2") == "iic/speech_eres2netv2_sv_zh-cn_16k-common"
    assert E.resolve_spk_model("mock") == "mock"
    # 未知名当作 modelscope id 透传
    assert E.resolve_spk_model("iic/whatever") == "iic/whatever"


def test_registry_merges_same_speaker_and_splits_others():
    reg = E.SpeakerRegistry(threshold=0.45)
    a = unit([1.0, 0.2, 0.0])
    a2 = unit([1.0, 0.25, 0.05])          # 同一个人，略有抖动
    b = unit([0.0, 0.1, 1.0])             # 另一个人
    assert reg.assign(a)[0] == 0
    i, sim = reg.assign(a2)
    assert i == 0 and sim > 0.9, (i, sim)
    i, sim = reg.assign(b)
    assert i == 1 and sim is not None and sim < 0.45, (i, sim)
    assert reg.count == 2


def test_registry_threshold_is_the_boundary():
    reg = E.SpeakerRegistry(threshold=0.5)
    reg.assign(unit([1.0, 0.0]))
    near = unit([1.0, 0.55])              # cos ≈ 0.877 → 同人
    far = unit([1.0, 1.6])                # cos ≈ 0.53 → 也过阈值（>0.5）
    assert reg.assign(near)[0] == 0
    assert reg.assign(far)[0] == 0
    below = E.SpeakerRegistry(threshold=0.9)
    below.assign(unit([1.0, 0.0]))
    assert below.assign(unit([1.0, 0.55]))[0] == 1     # 0.877 < 0.9 → 新说话人


def test_registry_caps_speaker_count():
    """人数超过上限后并入最像的，不能无限涨（否则 embedding 噪声会造出一堆人）。"""
    reg = E.SpeakerRegistry(threshold=0.99, max_speakers=3)
    labels = [reg.assign(unit([1.0, i * 0.9]))[0] for i in range(8)]
    assert reg.count == 3, reg.count
    assert set(labels) <= {0, 1, 2}


def test_registry_reset_forgets_speakers():
    reg = E.SpeakerRegistry()
    reg.assign(unit([1.0, 0.0]))
    reg.assign(unit([0.0, 1.0]))
    assert reg.count == 2 and reg.last == 1
    reg.reset()
    assert reg.count == 0 and reg.last is None
    assert reg.assign(unit([0.0, 1.0]))[0] == 0        # 重新从 S1 开始数


def test_registry_rejects_empty_embedding():
    reg = E.SpeakerRegistry()
    assert reg.assign(np.zeros(8)) == (None, None)


# ---------------------------------------------------------------------------
# 单元：mock 声纹链路（不下载模型）
# ---------------------------------------------------------------------------

def test_mock_tagger_separates_different_voices():
    tag = E.SpeakerTagger("mock")
    reg = tag.new_registry()
    got = [tag.label(x, 16000, reg)["spk"]
           for x in (tone(220), tone(220, 1.4), tone(1500), tone(1500, 1.1), tone(4000))]
    assert got == ["S1", "S1", "S2", "S2", "S3"], got
    assert reg.count == 3


def test_mock_tagger_short_segment_inherits_previous():
    """短段 embedding 不可靠：沿用上一位，并标 spk_short，不新造一个人。"""
    tag = E.SpeakerTagger("mock", min_ms=800)
    reg = tag.new_registry()
    assert tag.label(tone(1500), 16000, reg)["spk"] == "S1"
    out = tag.label(tone(4000, 0.3), 16000, reg)
    assert out["spk"] == "S1" and out.get("spk_short") is True, out
    assert reg.count == 1


def test_mock_tagger_short_first_segment_has_no_label():
    tag = E.SpeakerTagger("mock", min_ms=800)
    assert tag.label(tone(300, 0.3), 16000, tag.new_registry()) is None


def test_tagger_disabled_when_name_empty():
    tag = E.SpeakerTagger("")
    assert not tag.enabled
    assert tag.label(tone(220), 16000) is None


def test_tagger_label_carries_cosine_similarity():
    tag = E.SpeakerTagger("mock")
    reg = tag.new_registry()
    tag.label(tone(220), 16000, reg)
    out = tag.label(tone(220, 1.3), 16000, reg)
    assert out["spk"] == "S1" and out["sim"] is not None and out["sim"] > 0.9, out


# ---------------------------------------------------------------------------
# 集成：真服务端 --engine mock --spk mock
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
               "--lock-file", f"/tmp/asr_spk_{self.port}.lock",
               "--spool-dir", f"/tmp/asr_spk_spool_{self.port}",
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
    s = Server(["--spk", "mock"])
    s.wait_ready()
    yield s
    s.stop()


def _url(port, sid):
    return f"ws://127.0.0.1:{port}{WS_PATH}?session_id={sid}"


async def _collect(ws, out, until="flush_ack", timeout=25):
    while True:
        d = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        out.append(d)
        if d.get("type") == until:
            return


async def _speak(port, sid, pcm, extra_control=None):
    """连一路会话，按 20ms 帧推音频，最后 flush；返回收到的全部事件。"""
    got = []
    async with websockets.connect(_url(port, sid), max_size=4 << 20) as ws:
        got.append(json.loads(await asyncio.wait_for(ws.recv(), 15)))     # ready
        await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                  "vad": {"max_end_silence_ms": 400}}))
        await asyncio.wait_for(_collect(ws, got, "config_ack"), 15)
        frame = 640
        for i in range(0, len(pcm), frame):
            await ws.send(pcm[i:i + frame].tobytes())
            await asyncio.sleep(0.002)
        if extra_control:
            await ws.send(json.dumps(extra_control))
        await ws.send(json.dumps({"type": "flush"}))
        await asyncio.wait_for(_collect(ws, got), 25)
    return got


def _finals(msgs):
    return [m for m in msgs if m.get("type") == "final"]


def test_health_reports_spk(server):
    spk = server.health().get("spk")
    assert spk and spk["model"] == "mock" and spk["loaded"] is True, spk
    assert spk["threshold"] == pytest.approx(0.45)


def test_ready_event_announces_spk(server):
    got = asyncio.run(_speak(server.port, "spkReady", silence(0.3)))
    assert got[0]["type"] == "ready" and got[0].get("spk") == "mock", got[0]


def test_finals_carry_speaker_labels(server):
    """两个人轮流说话：两段 final 必须拿到不同的 spk。"""
    pcm = np.concatenate([tone(220), silence(), tone(1500), silence()])
    got = asyncio.run(_speak(server.port, "spkTwo", pcm))
    fin = _finals(got)
    assert len(fin) >= 2, [m.get("type") for m in got]
    labels = [f.get("spk") for f in fin]
    assert labels[0] == "S1" and labels[1] == "S2", labels
    assert all(f.get("spk_ms") is not None for f in fin)


def test_same_voice_across_segments_keeps_one_label(server):
    """同一个人说三句：不能每句都当成新人。"""
    pcm = np.concatenate([tone(660), silence(), tone(660, 1.4), silence(),
                          tone(660, 1.1), silence()])
    got = asyncio.run(_speak(server.port, "spkSame", pcm))
    labels = [f.get("spk") for f in _finals(got)]
    assert len(labels) >= 3, labels
    assert set(labels) == {"S1"}, labels


def test_reset_restarts_speaker_numbering(server):
    """reset 之后重新数人：换人的那段应回到 S1，而不是接着数成 S2。"""
    async def _go():
        got = []
        async with websockets.connect(_url(server.port, "spkReset")) as ws:
            got.append(json.loads(await asyncio.wait_for(ws.recv(), 15)))       # ready
            await ws.send(json.dumps({"type": "config", "sample_rate": 16000,
                                      "vad": {"max_end_silence_ms": 400}}))
            await asyncio.wait_for(_collect(ws, got, "config_ack"), 15)
            for idx, pcm in enumerate((np.concatenate([tone(220), silence()]),
                                       np.concatenate([tone(1500), silence()]))):
                for i in range(0, len(pcm), 640):
                    await ws.send(pcm[i:i + 640].tobytes())
                    await asyncio.sleep(0.002)
                await asyncio.wait_for(_collect(ws, got, "final"), 25)
                if idx == 0:                        # 第一段说完后复位
                    await ws.send(json.dumps({"type": "reset"}))
                    await asyncio.wait_for(_collect(ws, got, "reset_ack"), 15)
            await ws.send(json.dumps({"type": "flush"}))
            await asyncio.wait_for(_collect(ws, got), 25)
        return got

    labels = [f.get("spk") for f in _finals(asyncio.run(_go()))]
    assert labels == ["S1", "S1"], labels


def test_stats_report_speaker_count(server):
    async def _go():
        async with websockets.connect(_url(server.port, "spkStats")) as ws:
            await asyncio.wait_for(ws.recv(), 15)                        # ready
            pcm = np.concatenate([tone(220), silence(), tone(1500), silence()])
            for i in range(0, len(pcm), 640):
                await ws.send(pcm[i:i + 640].tobytes())
                await asyncio.sleep(0.002)
            await ws.send(json.dumps({"type": "stats"}))
            got = []
            await _collect(ws, got, "stats")
            return got[-1]
    st = asyncio.run(_go())
    assert st.get("speakers") == 2, st


def test_no_spk_field_when_disabled():
    """没开 --spk 时 final 不该出现 spk 字段（老客户端不受影响）。"""
    s = Server()                       # 不带 --spk
    s.wait_ready()
    try:
        assert s.health().get("spk") is None
        got = asyncio.run(_speak(s.port, "spkOff", np.concatenate([tone(220), silence()])))
        fin = _finals(got)
        assert fin and all("spk" not in f for f in fin), fin
    finally:
        s.stop()

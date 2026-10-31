#coding=utf-8
"""
段首预滚（--pre-roll-ms）测试

回归的 bug：preroll 用的是 deque(maxlen=64)，数的是“客户端帧数”而不是时间。
浏览器 20ms 一帧 → 存了 1.28s，40ms 一帧 → 2.56s，远超配置的 200ms。段首会把
整段 preroll 补喂给引擎并拼进 seg_buf，等于把上一段的话重新听一遍：第二段 final
会重复上一段结尾的词，声纹也会被上一段污染。修法是按毫秒数裁剪 preroll。

跑法：
    conda run -n moneyprinter python -m pytest tests/test_asr_preroll.py -v
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

SR = 16000
WS_PATH = "/asr/ws"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, extra=None):
        self.port = _free_port()
        cmd = [sys.executable, os.path.join(ASR_DIR, "ASR_server.py"),
               "--engine", "mock", "--device", "cpu",
               "--port", str(self.port),
               "--lock-file", f"/tmp/asr_pre_{self.port}.lock",
               "--spool-dir", f"/tmp/asr_pre_spool_{self.port}",
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

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture()
def server():
    s = Server()
    try:
        s.wait_ready()
        yield s
    finally:
        s.stop()


def tone(freq=440.0, dur=1.2, amp=12000):
    t = np.arange(int(dur * SR)) / SR
    return (np.sin(2 * np.pi * freq * t) * amp).astype("<i2")


def silence(dur=0.6):
    rng = np.random.default_rng(11)
    return np.clip(rng.normal(0, 25, int(dur * SR)), -300, 300).astype("<i2")


async def _two_segments(port, pre_roll_ms=200):
    """两段各 1.2s 的“说话”，中间 0.6s 静音。返回两个 final。"""
    pcm = np.concatenate([tone(220), silence(), tone(1500), silence()])
    finals = []
    async with websockets.connect(f"ws://127.0.0.1:{port}{WS_PATH}") as ws:
        await asyncio.wait_for(ws.recv(), 15)                       # ready
        await ws.send(json.dumps({"type": "config", "sample_rate": SR,
                                  "pre_roll_ms": pre_roll_ms,
                                  "vad": {"max_end_silence_ms": 400}}))
        while True:
            d = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if d.get("type") == "config_ack":
                break
        for i in range(0, len(pcm), 640):                           # 40ms 一帧
            await ws.send(pcm[i:i + 640].tobytes())
            await asyncio.sleep(0.002)
        await ws.send(json.dumps({"type": "flush"}))
        deadline = time.time() + 25
        while len(finals) < 2 and time.time() < deadline:
            try:
                d = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.time())))
            except websockets.exceptions.ConnectionClosed:
                break
            if d.get("type") == "final":
                finals.append(d)
    return finals


def test_second_segment_does_not_replay_the_previous_one(server):
    """第二段 final 的词数只该对应第二段自己的时长，不能把上一段重新听一遍。

    mock 引擎每 600ms 音频吐一个两字词。第二段自身 ≈1.6s + 200ms 预滚 ≈ 3 个词；
    预滚按帧数存的话会多喂 2.4s，词数直接翻倍。
    """
    finals = asyncio.run(_two_segments(server.port))
    assert len(finals) == 2, finals
    first, second = finals[0]["text"], finals[1]["text"]
    dur2_ms = finals[1]["end_ms"] - finals[1]["start_ms"]
    budget = int((dur2_ms + 400) / 600) + 1          # 允许 1 个词的余量
    assert len(second) <= budget * 2, (first, second, dur2_ms, budget)
    assert len(second) <= len(first) + 4, (first, second)


def test_pre_roll_ms_is_actually_honoured():
    """把 --pre-roll-ms 调大，段首补的历史音频就该相应变长（同一个音频对比）。"""
    small, big = Server(), Server(["--pre-roll-ms", "1200"])
    try:
        small.wait_ready()
        big.wait_ready()
        a = asyncio.run(_two_segments(small.port, 200))
        b = asyncio.run(_two_segments(big.port, 1200))
        assert len(a) == 2 and len(b) == 2, (a, b)
        # 200ms 预滚：第二段词数受控；1200ms 预滚：明显更多（把上一段尾巴又听了一遍）
        assert len(b[1]["text"]) > len(a[1]["text"]), (a[1]["text"], b[1]["text"])
    finally:
        small.stop()
        big.stop()

#coding=utf-8
"""
ASR 流式识别服务端（accessories 附件服务）

功能
----
1. **流式输入 / 实时输出**：WebSocket 推 PCM16 裸流，服务端边收边出 partial，
   VAD 判停后出 final（带标点、带时间戳）。
2. **VAD**：内置自适应能量 VAD（滞回 + 自适应底噪 + pre_roll + 判停 + 强制切段），
   无模型依赖、几乎不占 CPU；8k 电话音频也能用。--vad none 时按 max_segment_ms
   定时切段。
3. **常驻 + warmup**：启动即（必要时）自动下载模型 → 加载到 GPU → 跑一次真实
   音频 warmup（触发 cuDNN autotune），之后常驻，首请求无冷启动。
4. **多引擎对比**：--engine 支持 paraformer-streaming / paraformer / sensevoice /
   whisper / qwen3-asr-0.6b / qwen3-asr-1.7b / mock；运行期可 POST /engine 热切换
   （无活动会话时）。加载前有显存守卫，避免 OOM 连累同卡的 OCR / CosyVoice。
5. **一次性转写**：POST /transcribe 支持 wav / mp3 / m4a（ffmpeg 解码）、base64、
   URL，用于录音文件转写与评测。

用法
----
    # 生产（WS 与 HTTP 同端口，按路径区分）
    python ASR_server.py --gpu 1 --port 7996

    # 自检：加载真实引擎 + warmup + 合成音频跑通链路，打印显存/延迟后退出
    python ASR_server.py --gpu 1 --self-test

    # 换引擎对比
    curl -X POST localhost:7996/engine -d '{"name":"sensevoice"}'
    curl -X POST localhost:7996/transcribe -d '{"path":"/root/test.mp3"}'

单端口路由（uvicorn + starlette）
--------------------------------
    一个端口同时提供 WebSocket 与 HTTP，以后挂 /mcp 之类的路径也不用改端口：
        ws   /asr/ws          流式识别（旧客户端连 "/" 仍然可用）
        http /health /stats /engines /engine /warmup /transcribe /shutdown
        http /webui           浏览器麦克风推流页面
        http /                端点索引（JSON）

WebSocket 协议（文本帧=控制/结果，二进制帧=PCM16LE 单声道裸流）
--------------------------------------------------------------
    C -> S  {"type":"config","sample_rate":16000,"language":"zh",
             "vad":{"max_end_silence_ms":800,"enabled":true}}
    S -> C  {"type":"ready","engine":"paraformer-streaming","device":"cuda:0",...}
    S -> C  {"type":"segment_start","seg_id":1,"start_ms":1200}
    S -> C  {"type":"partial","seg_id":1,"text":"今天天气","start_ms":1200,...}
    S -> C  {"type":"final","seg_id":1,"text":"今天天气不错。","start_ms":1200,
             "end_ms":3100,"took_ms":210,"rtf":0.08,"reason":"vad"}
    S -> C  {"type":"segment_cancel","seg_id":2,"reason":"short"}  # 咳嗽/杂音：撤掉该段 partial
    S -> C  {"type":"replay",...}                                  # 重连时补发的 final
    S -> C  {"type":"stats",...} {"type":"pong"} {"type":"error",...}
    C -> S  {"type":"flush"|"reset"|"ping"|"stats"}
    连接参数： ?session_id=xxx&sample_rate=16000&last_seg_id=12
              带 last_seg_id（含 0）会补发该 seg 之后的 final，断线重连不丢字

HTTP 控制面（与 WS 同端口，路径区分）
-------------------------------------
    GET  /health     状态 / 引擎 / 显存 / 会话数
    GET  /engines    引擎清单与可用性
    POST /engine     {"name":"sensevoice"}  热切换（需无活动会话）
    POST /warmup     重新 warmup
    POST /transcribe {"path"|"audio_b64"|"url", "language", "with_segments"}
                     也可直接 POST 音频字节：--data-binary @a.mp3 "?format=mp3"
    GET  /stats      累计计数
    GET  /webui      浏览器推流页面
"""

import argparse
import asyncio
import base64
import collections
import fcntl
import glob
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asr_audio as A                                     # noqa: E402
import asr_engines as E                                   # noqa: E402
from asr_vad import CANCEL, END, START, StreamVad, VadEvent   # noqa: E402

try:                    # WS 与 HTTP 共用一个端口：ASGI（uvicorn + starlette）
    import uvicorn
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.responses import Response
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect
except ImportError as exc:                              # pragma: no cover
    raise SystemExit(
        f"缺少 ASGI 依赖（{exc}）：pip install uvicorn starlette")

logger = logging.getLogger("asr.server")
VERSION = "1.0"
SUPPORTED_RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)
# 静音多久打一行「底噪/阈值」心跳日志（排「停顿久了再说就没字」用）
HEARTBEAT_MS = 8000


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 7996                 # WS 与 HTTP 同端口（按路径区分）
    http_port: int = 0               # 已废弃：非 0 且 != port 时仅告警
    engine: str = "paraformer-streaming"
    gpu: str = ""                    # 写入 CUDA_VISIBLE_DEVICES
    device: str = "auto"             # auto | cuda:0 | cpu
    model_dir: str = ""
    language: str = "zh"
    punc: Optional[bool] = None      # None=自动（自带标点的引擎不加）/ True 强制加 / False 强制不加
    chunk_size: list = field(default_factory=lambda: [0, 10, 5])
    refine: str = ""                 # 流式 final 用该离线引擎整段重扫（如 paraformer）

    vad: str = "energy"              # energy | none
    max_end_silence_ms: int = 800
    max_segment_ms: int = 15000
    min_speech_ms: int = 250
    pre_roll_ms: int = 200
    start_margin_db: float = 7.0
    min_level_db: float = -42.0

    sample_rate: int = 16000         # 客户端默认采样率（config 帧可覆盖）
    max_sessions: int = 1
    partial_interval_ms: int = 300   # 流式 partial 最小间隔
    offline_partials_ms: int = -1    # 非流式引擎滚动 partial：-1=按引擎自动，0=强制关，>0=指定间隔

    spk: str = ""                    # 说话人分离：campplus | eres2netv2 | mock（空=关）
    spk_device: str = "cpu"          # 声纹跑哪：cpu 零显存(RTF≈0.03)；cuda:0 约 +50MB
    spk_threshold: float = 0.45      # 余弦 ≥ 判同一人（CAM++ 实测同人 0.69 / 异人 <0.25）
    spk_max_speakers: int = 8        # 每路会话最多分几个人，超了就并入最像的
    spk_min_ms: int = 800            # 短于此不抽声纹，沿用上一位

    token: str = ""
    warmup: bool = True
    warmup_audio: str = ""
    idle_unload_min: int = 0         # 0 = 常驻
    lock_file: str = "/tmp/asr_server.lock"
    spool_dir: str = "/tmp/asr_spool"
    max_body_mb: int = 64
    force_vram: bool = False
    log_file: str = ""
    access_log: bool = False         # uvicorn 访问日志（默认关，避免刷屏）
    self_test: bool = False

    @staticmethod
    def from_args(a) -> "Config":
        c = Config()
        for k, v in vars(a).items():
            if hasattr(c, k) and v is not None:
                setattr(c, k, v)
        if c.http_port and c.http_port != c.port:
            print("--http-port 已废弃：HTTP 与 WebSocket 同端口 "
                  f"（{c.port}），按路径区分，忽略 {c.http_port}", file=sys.stderr)
        c.http_port = c.port
        if isinstance(c.chunk_size, str):
            parts = [x for x in c.chunk_size.replace(" ", "").split(",") if x != ""]
            c.chunk_size = [int(x) for x in parts]
        if len(c.chunk_size) != 3:
            raise SystemExit("--chunk-size 需要三个整数，例如 0,10,5")
        if c.max_sessions < 1:
            raise SystemExit("--max-sessions 至少为 1")
        if c.max_sessions > 4:
            print(f"--max-sessions {c.max_sessions} 偏大：推理是全局串行的（engine_lock），"
                  "路数再多也只是排队，建议 ≤4", file=sys.stderr)
        if c.spk:
            if not 0.05 <= c.spk_threshold <= 0.95:
                raise SystemExit("--spk-threshold 需要在 0.05~0.95（CAM++ 实测同人≈0.69、异人<0.25）")
            if c.spk_max_speakers < 1:
                raise SystemExit("--spk-max-speakers 至少为 1")
        return c


def ev(**kw) -> str:
    """统一的事件 JSON。"""
    kw.setdefault("ts", round(time.time(), 3))
    return json.dumps(kw, ensure_ascii=False)


WS_PATH = "/asr/ws"                  # 流式识别路径（"/" 作为旧客户端兼容）


def _ws_impl() -> str:
    """不同 uvicorn 版本的 WS 实现名不一样，挑一个可用的。"""
    try:
        from uvicorn.config import WS_PROTOCOLS
    except Exception:                                    # pragma: no cover
        return "auto"
    for name in ("websockets-sansio", "websockets", "auto"):
        if name in WS_PROTOCOLS:
            return name
    return "auto"


class WSClosed(Exception):
    """客户端已断开（对齐原先 websockets.ConnectionClosed 的用法）。"""


class WsConn:
    """把 starlette 的 WebSocket 适配成 Session 用的最小接口。

    异步迭代产出 bytes（音频）或 str（控制帧）；send 只发文本帧。
    """

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self.closed = False
        url = ws.url
        self.path = url.path + (("?" + url.query) if url.query else "")
        self.request_headers = ws.headers

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            try:
                msg = await self._ws.receive()
            except WebSocketDisconnect:
                self.closed = True
                return
            if msg["type"] == "websocket.disconnect":
                self.closed = True
                return
            data = msg.get("bytes")
            if data is None:
                data = msg.get("text")
            if data is not None:
                yield data

    async def send(self, payload: str):
        if self.closed:
            raise WSClosed("connection closed")
        try:
            await self._ws.send_text(payload)
        except (RuntimeError, WebSocketDisconnect):
            self.closed = True
            raise WSClosed("connection closed") from None

    async def close(self, code: int = 1000, reason: str = ""):
        if not self.closed:
            try:
                await self._ws.close(code=code, reason=reason)
            except Exception:
                pass
        self.closed = True


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class Session:
    """一条 WebSocket 连接的识别会话（并发 1 路，内部串行处理）。"""

    def __init__(self, app: "AsrApp", ws, sid: str):
        self.app = app
        self.ws = ws
        self.sid = sid
        self.cfg = replace(app.cfg)      # 每会话独立副本：客户端 config 不污染全局默认
        self.sr = app.cfg.sample_rate
        self.language = app.cfg.language
        self.engine = None
        self.vad = None
        self.seg_id = 0
        self.seg_open = False
        self.seg_start_ms = 0
        self.seg_text = ""
        self.seg_last_emit = 0.0
        self.seg_buf = np.zeros(0, dtype="<i2")
        self.seg_next_partial = 0
        self.offline_ms = 0          # 本次会话生效的滚动 partial 间隔（连接时按引擎定）
        self.preroll = collections.deque(maxlen=64)
        self.preroll_ms = 0           # preroll 里实际存了多少毫秒音频
        self.closed = False
        self.t_start = time.time()
        self.audio_ms = 0
        self.first_partial_ms = None
        self.n_partial = 0
        self.n_final = 0
        self.last_rtf = None
        self.last_seg_id_req = None      # None=客户端没要求补发；0=要求补发全部
        self._nv_started = False
        self._nv_ms = 0
        self._stream_state = None       # 本路会话独立的流式解码状态
        self._stream_for = None         # 该状态属于哪个 engine 实例
        self.spk_reg = None             # 本路会话的说话人聚类（按需创建）
        # 「停顿一会儿再说就没字」这类问题只有两种可能：客户端不再送音频，或者
        # 浏览器 AGC 把静音段底噪抬起来、VAD 阈值跟着涨。静音心跳把两者分开。
        self._hb_ms = 0                 # 上次打心跳时的 audio_ms
        self._hb_peak_db = -120.0       # 心跳窗口里最响的一段（RMS dBFS）

    # -- 生命周期 ---------------------------------------------------------

    async def run(self):
        if self.app.engine is None:
            await asyncio.to_thread(self.app.load_engine)
        self.engine = self.app.engine
        self.vad = self._make_vad()
        self.offline_ms = E.effective_offline_partials_ms(self.engine,
                                                          self.cfg.offline_partials_ms)
        await self._send(ev(type="ready", engine=self.engine.name, device=self.app.device,
                            streaming=self.engine.supports_streaming, sample_rate=self.sr,
                            vad=self.cfg.vad, session_id=self.sid,
                            spk=self.app.spk.name if self.app.spk else None,
                            warmup=self.app.warmup_done.is_set(), version=VERSION))
        await self._replay_missed()

        async for msg in self.ws:
            if isinstance(msg, (bytes, bytearray)):
                await self._on_audio(bytes(msg))
            else:
                await self._on_control(msg)

    def _make_vad(self):
        if self.cfg.vad == "none":
            return None
        return StreamVad(sample_rate=self.app.engine.spec.sample_rate,
                         max_end_silence_ms=self.cfg.max_end_silence_ms,
                         max_segment_ms=self.cfg.max_segment_ms,
                         min_speech_ms=self.cfg.min_speech_ms,
                         pre_roll_ms=self.cfg.pre_roll_ms,
                         start_margin_db=self.cfg.start_margin_db,
                         min_level_db=self.cfg.min_level_db)

    def _stream(self):
        """本路会话自己的流式解码状态。

        多路并发时每路一份：FunASR 的 cache 是有状态的，共用会让两路语音互相
        污染（A 开口时 segment_start 的 reset 会把 B 正在解码的状态清空）。
        引擎被切换/重载过（engine 实例换了）就重建，旧 cache 对新模型无意义。
        """
        eng = self.engine
        if self._stream_for is not eng:
            self._stream_state = eng.new_stream(self.sid)
            self._stream_for = eng
        return self._stream_state

    async def close(self, code: int = 1000, reason: str = ""):
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass

    # -- 输入 -------------------------------------------------------------

    async def _on_audio(self, data: bytes):
        if not data:
            return
        if not self.app.warmup_done.is_set():
            # warmup 未完成时不丢音频：等它完成再处理，首包不丢字
            await self.app.warmup_done.wait()

        sr_engine = self.engine.spec.sample_rate
        pcm = A.pcm_bytes_to_int16(data)
        if self.sr != sr_engine:
            pcm = A.pcm16_resample(pcm, self.sr, sr_engine)
        if pcm.size == 0:
            return
        frame_ms = int(pcm.size * 1000 / sr_engine)
        self.audio_ms += frame_ms

        if self.vad is not None:
            events = self.vad.feed(pcm)
        else:
            events = self._no_vad_events(frame_ms)

        # 事件顺序很重要：max_len 强制切段时 VAD 会一次给出 [END, START]。
        # 必须先关旧段（此时 seg_buf 才是整段），再开新段，最后把当前帧喂给新段；
        # 反过来会先 reset 掉引擎和缓冲，导致 final 只剩最后几十毫秒。
        for e in events:
            if e.kind == END:
                await self._end_segment(e.end_ms or self.audio_ms, e.reason)
            elif e.kind == CANCEL:
                await self._cancel_segment(e.reason)

        for e in events:
            if e.kind == START:
                await self._begin_segment(e.start_ms)

        await self._feed_engine(pcm, sr_engine)
        self._heartbeat(pcm)

    def _no_vad_events(self, frame_ms: int) -> list:
        """--vad none：整条流当一段，按 max_segment_ms 定时切。"""
        events = []
        if not self._nv_started:
            self._nv_started = True
            self._nv_ms = 0
            events.append(VadEvent(START, 0, 0, "no_vad"))
        else:
            self._nv_ms += frame_ms
            if self._nv_ms >= self.cfg.max_segment_ms:
                events.append(VadEvent(END, self.seg_start_ms, self.audio_ms, "max_len"))
                self._nv_ms = 0
                events.append(VadEvent(START, self.audio_ms, 0, "max_len"))
        return events

    async def _on_control(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            await self._send(ev(type="error", code="bad_json", message="控制帧不是 JSON"))
            return
        t = (msg.get("type") or "").lower()
        if t == "config":
            sr = int(msg.get("sample_rate") or self.sr)
            if sr not in SUPPORTED_RATES:
                await self._send(ev(type="error", code="bad_rate",
                                    message=f"不支持采样率 {sr}，可选 {SUPPORTED_RATES}"))
                return
            self.sr = sr
            vad_cfg = msg.get("vad") or {}
            if vad_cfg.get("enabled") is False:
                self.cfg.vad = "none"
            else:
                self.cfg.vad = "energy"
            for k in ("max_end_silence_ms", "max_segment_ms", "min_speech_ms",
                      "pre_roll_ms", "start_margin_db", "min_level_db"):
                if k in vad_cfg:
                    setattr(self.cfg, k, vad_cfg[k])
            if msg.get("language"):
                self.language = msg["language"]
            self.vad = self._make_vad()
            await self._send(ev(type="config_ack", sample_rate=self.sr, vad=self.cfg.vad,
                                language=self.language))
        elif t == "flush":
            await self._flush("client_flush")
        elif t == "reset":
            await self._reset_stream()
            await self._send(ev(type="reset_ack"))
        elif t == "ping":
            await self._send(ev(type="pong", client_ts=msg.get("ts")))
        elif t == "stats":
            await self._send_stats()
        else:
            await self._send(ev(type="error", code="bad_type", message=f"未知控制帧 '{t}'"))

    # -- 分段状态机 -------------------------------------------------------

    async def _begin_segment(self, start_ms: int):
        self.seg_id += 1
        self.seg_open = True
        self.seg_start_ms = int(start_ms)
        self.seg_text = ""
        self.seg_last_emit = time.time()
        # partial 节奏按“音频时间”而不是墙上时间：客户端一次性推文件也不会漏 partial
        step = (self.cfg.partial_interval_ms if self.engine.supports_streaming
                else self.offline_ms)
        self.seg_next_partial = self.audio_ms + max(0, int(step))
        self.seg_buf = np.zeros(0, dtype="<i2")
        if not self.engine.supports_streaming and self.preroll:
            # 把 pre_roll 的音频补进段缓冲，避免吃掉首字
            self.seg_buf = np.concatenate(list(self.preroll))
        try:
            with self.app.engine_lock:
                self.engine.stream_reset(self._stream())
                if self.engine.supports_streaming and self.preroll:
                    # 把 pre_roll 音频补喂给流式引擎，否则首字会被吃掉
                    pre = A.int16_to_float(np.concatenate(list(self.preroll)))
                    self.engine.stream_feed(pre, self._stream())
                    self.seg_buf = np.concatenate(list(self.preroll))
        except Exception as exc:
            logger.debug("stream_reset: %s", exc)
        if self.vad is not None:
            # 没有这行就只能看到「没字」，看不到是没切开段还是引擎返回空
            logger.info("段开始 sid=%s seg=%d @%dms 底噪=%.1fdB 阈值=%.1fdB",
                        self.sid, self.seg_id, self.seg_start_ms,
                        self.vad.floor, self.vad.threshold_db())
        await self._send(ev(type="segment_start", seg_id=self.seg_id,
                            start_ms=self.seg_start_ms))

    def _trim_preroll(self, sr_engine: int):
        """preroll 只留 --pre-roll-ms 那么长。

        deque(maxlen=64) 数的是“客户端帧数”，不是时间：浏览器 20ms 一帧 = 1.28s，
        40ms 一帧 = 2.56s，都远超配置的 200ms。段首会把整段 preroll 补喂给引擎并
        拼进 seg_buf，等于把上一段的话重新听一遍——final 会重复上一段结尾的词，
        声纹也会被上一段污染（实测第二段里混进了第一段的全部音频）。
        """
        limit = max(0, int(self.cfg.pre_roll_ms))
        while self.preroll and self.preroll_ms > limit:
            dropped = self.preroll.popleft()
            self.preroll_ms -= int(dropped.size * 1000 / max(1, sr_engine))
        if not self.preroll:
            self.preroll_ms = 0

    def _heartbeat(self, pcm):
        """静音超过 8s 就打一行：底噪 / 阈值 / 这段时间里最响的电平。

        排「说完一段、停顿十几秒、再说就没字」这种问题时先要分清两件事：
        audio_ms 还在涨 = 客户端还在送音频（不是浏览器掉线）；
        底噪/阈值在涨   = 浏览器 AGC 把静音段抬亮了，VAD 门限被顶到人声之上。
        """
        if self.vad is None:
            return
        db = A.frame_rms_db(pcm)
        if db > self._hb_peak_db:
            self._hb_peak_db = db
        if self.vad.in_speech() or self.seg_open:
            self._hb_ms = self.audio_ms
            self._hb_peak_db = db
            return
        if self.audio_ms - self._hb_ms < HEARTBEAT_MS:
            return
        self._hb_ms = self.audio_ms
        logger.info("静音心跳 sid=%s audio=%dms 底噪=%.1fdB 阈值=%.1fdB 最近峰值=%.1fdBFS",
                    self.sid, self.audio_ms, self.vad.floor, self.vad.threshold_db(),
                    self._hb_peak_db)
        self._hb_peak_db = -120.0

    def _offline_step(self) -> int:
        """滚动 partial 的下一次间隔：段越长间隔越大。

        固定间隔时一段 T 秒要重扫 T²/2N 秒音频（15s 段 + 600ms = 187s 音频，
        慢引擎直接追不上实时）。间隔取 max(base, 段已长/4) 后，每多 1s 音频
        只多解约 4s，总解码量与段长无关（O(T)），代价是后半段 partial 变稀。
        """
        return max(self.offline_ms, (self.audio_ms - self.seg_start_ms) // 4)

    async def _feed_engine(self, pcm, sr_engine):
        """把音频喂给引擎：流式引擎出 partial，非流式引擎攒段。"""
        in_speech = self.vad.in_speech() if self.vad is not None else True
        if self.vad is not None:
            self.preroll.append(pcm)
            self.preroll_ms += int(pcm.size * 1000 / max(1, sr_engine))
            self._trim_preroll(sr_engine)

        if self.engine.supports_streaming:
            if not in_speech and not self.seg_open:
                return            # 段外（还没 START 或已 END）：不喂静音，也不发陈旧 partial
            audio = A.int16_to_float(pcm)
            self.seg_buf = np.concatenate((self.seg_buf, pcm))

            st = self._stream()          # 本次喂音频固定用这一路状态

            def _feed():
                with self.app.engine_lock:
                    return self.engine.stream_feed(audio, st)

            try:
                text = await asyncio.to_thread(_feed)
            except Exception as exc:
                logger.exception("流式识别失败")
                self.app.last_error = str(exc)
                await self._send(ev(type="error", code="engine", message=str(exc)))
                return
            if text and text != self.seg_text:
                if self.audio_ms >= self.seg_next_partial:
                    self.seg_next_partial = self.audio_ms + self.cfg.partial_interval_ms
                    self.seg_text = text
                    self.seg_last_emit = time.time()
                    self.n_partial += 1
                    if self.first_partial_ms is None:
                        self.first_partial_ms = self.audio_ms
                    await self._send(ev(type="partial", seg_id=self.seg_id, text=text,
                                        start_ms=self.seg_start_ms, end_ms=self.audio_ms))
        else:
            if not in_speech:
                return
            self.seg_buf = np.concatenate((self.seg_buf, pcm))
            if (self.offline_ms > 0 and self.seg_id
                    and self.audio_ms >= self.seg_next_partial):
                self.seg_next_partial = self.audio_ms + self._offline_step()
                text = await self._transcribe_buffer(self.seg_buf)
                if text:
                    self.n_partial += 1
                    if self.first_partial_ms is None:
                        self.first_partial_ms = self.audio_ms
                    await self._send(ev(type="partial", seg_id=self.seg_id, text=text,
                                        start_ms=self.seg_start_ms, end_ms=self.audio_ms,
                                        rolling=True))

    async def _end_segment(self, end_ms: int, reason: str):
        t0 = time.monotonic()          # 耗时一律用单调时钟：NTP 校时不会把 took/rtf 带偏
        seg_pcm = self.seg_buf              # 声纹要用整段，下面会清空 seg_buf
        if self.engine.supports_streaming:
            st = self._stream()

            def _flush():
                with self.app.engine_lock:
                    return self.engine.stream_flush(st)
            try:
                text = await asyncio.to_thread(_flush)
            except Exception as exc:
                logger.exception("flush 失败")
                self.app.last_error = str(exc)
                await self._send(ev(type="error", code="engine", message=str(exc)))
                return
            text = await self._apply_refine(text)
        else:
            text = await self._transcribe_buffer(self.seg_buf)

        took = int((time.monotonic() - t0) * 1000)
        dur_ms = max(1, int(end_ms) - self.seg_start_ms)
        self.last_rtf = round(took / float(dur_ms), 4)
        text = (text or "").strip()
        self.seg_open = False
        self.seg_text = ""
        self.seg_buf = np.zeros(0, dtype="<i2")
        if not text:
            # 「段切开了、音频也到了，但引擎给了空串」——和「根本没切开段」是两种
            # 完全不同的故障，日志必须能分开它们
            logger.warning("段结束 sid=%s seg=%d %d→%dms reason=%s took=%dms：空结果",
                           self.sid, self.seg_id, self.seg_start_ms, int(end_ms),
                           reason, took)
            return
        logger.info("段结束 sid=%s seg=%d %d→%dms reason=%s took=%dms rtf=%.3f %r",
                    self.sid, self.seg_id, self.seg_start_ms, int(end_ms), reason,
                    took, self.last_rtf or 0.0, text[:40])
        self.n_final += 1
        payload = {"type": "final", "seg_id": self.seg_id, "text": text,
                   "start_ms": self.seg_start_ms, "end_ms": int(end_ms),
                   "reason": reason, "took_ms": took, "rtf": self.last_rtf}
        spk = await self._label_speaker(seg_pcm)
        if spk:
            payload.update(spk)
        self.app.remember_final(self.sid, payload)
        await self._send(ev(**payload))

    async def _label_speaker(self, pcm) -> dict:
        """给这段音频打说话人标签（--spk 未开则返回 {}）。

        声纹默认跑 CPU：不占显存、也不和 ASR 抢 engine_lock。放 GPU 时必须串行，
        否则两个模型同时吃同一张卡容易 OOM。
        """
        tag = self.app.spk
        if tag is None or pcm is None or len(pcm) == 0:
            return {}
        if self.spk_reg is None:
            self.spk_reg = tag.new_registry()
        audio = A.int16_to_float(np.asarray(pcm, dtype="<i2"))
        sr = self.engine.spec.sample_rate

        def _run():
            if tag.on_gpu:
                with self.app.engine_lock:
                    return tag.label(audio, sr, self.spk_reg)
            return tag.label(audio, sr, self.spk_reg)

        t0 = time.monotonic()
        try:
            out = await asyncio.to_thread(_run)
        except Exception as exc:                # 分不出人不能影响出字
            logger.warning("说话人分离失败（忽略）: %s", exc)
            return {}
        if not out:
            return {}
        out["spk_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info("说话人: seg=%d %s sim=%s (%dms)", self.seg_id, out.get("spk"),
                    out.get("sim"), out["spk_ms"])
        return out

    async def _cancel_segment(self, reason: str):
        """VAD 判定这段太短（咳嗽、关门声）：丢掉已发的 partial，通知客户端撤销。"""
        if not self.seg_open:
            return
        self.seg_open = False
        self.seg_text = ""
        self.seg_buf = np.zeros(0, dtype="<i2")
        try:
            with self.app.engine_lock:
                self.engine.stream_reset(self._stream())
        except Exception as exc:            # pragma: no cover
            logger.debug("cancel stream_reset: %s", exc)
        self.app.counters["cancelled"] = self.app.counters.get("cancelled", 0) + 1
        logger.info("段取消 sid=%s seg=%d @%dms reason=%s（太短，客户端要撤 partial）",
                    self.sid, self.seg_id, self.seg_start_ms, reason)
        await self._send(ev(type="segment_cancel", seg_id=self.seg_id,
                            reason=reason, start_ms=self.seg_start_ms))

    async def _transcribe_buffer(self, pcm) -> str:
        if pcm is None or len(pcm) == 0:
            return ""
        audio = A.int16_to_float(np.asarray(pcm, dtype="<i2"))

        def _run():
            with self.app.engine_lock:
                return self.engine.transcribe(audio, self.engine.spec.sample_rate,
                                              self.language)
        try:
            res = await asyncio.to_thread(_run)
        except Exception as exc:
            logger.exception("整段识别失败")
            self.app.last_error = str(exc)
            await self._send(ev(type="error", code="engine", message=str(exc)))
            return ""
        return (res.text or "").strip()

    async def _apply_refine(self, rough: str) -> str:
        """流式 final 用离线引擎把整段音频重扫一遍：partial 保延迟，final 保精度。"""
        ref = self.app.refiner
        if ref is None or len(self.seg_buf) == 0:
            return rough
        audio = A.int16_to_float(np.asarray(self.seg_buf, dtype="<i2"))

        def _run():
            with self.app.engine_lock:
                return ref.transcribe(audio, ref.spec.sample_rate, self.language)
        try:
            fine = ((await asyncio.to_thread(_run)).text or "").strip()
        except Exception as exc:
            logger.warning("精修失败，沿用流式结果: %s", exc)
            return rough
        if not fine:
            return rough
        if rough and len(fine) * 2 < len(rough):      # 明显变短多半是整段识别崩了
            logger.info("精修结果过短(%d<%d)，沿用流式结果", len(fine), len(rough))
            return rough
        if fine != rough:
            logger.info("精修: %r -> %r", rough[:40], fine[:60])
        # 精修引擎默认不挂标点模型（省一份 1.1GB），这里复用主引擎的标点模型；
        # 但 Qwen3-ASR 这类**自带标点**的引擎不能再叠 ct-punc，否则会出现「，，」
        if getattr(ref.spec, "self_punctuating", False):
            return fine
        return self.engine.apply_punc(fine)

    async def _flush(self, reason: str):
        events = self.vad.force_end() if self.vad is not None else []
        for e in events:
            if e.kind == END:
                await self._end_segment(e.end_ms or self.audio_ms, reason)
            elif e.kind == CANCEL:
                await self._cancel_segment(e.reason)
        if self.vad is None and self.seg_id:
            await self._end_segment(self.audio_ms, reason)
        await self._send(ev(type="flush_ack", seg_id=self.seg_id))

    async def _reset_stream(self):
        if self.vad is not None:
            self.vad.reset()
        try:
            with self.app.engine_lock:
                self.engine.stream_reset(self._stream())
        except Exception:
            pass
        self.seg_buf = np.zeros(0, dtype="<i2")
        self.seg_text = ""
        self.seg_open = False
        self.seg_id = 0
        self._nv_started = False
        self._nv_ms = 0
        self.preroll.clear()
        self.preroll_ms = 0
        if self.spk_reg is not None:      # 换人/重来：说话人从零开始数
            self.spk_reg.reset()

    # -- 输出 -------------------------------------------------------------

    async def _send(self, payload: str):
        if self.closed:
            return
        try:
            await self.ws.send(payload)
        except WSClosed:
            self.closed = True
        except Exception as exc:
            logger.warning("发送失败: %s", exc)
            self.closed = True

    async def _send_stats(self):
        await self._send(ev(type="stats", audio_ms=self.audio_ms,
                            first_partial_ms=self.first_partial_ms,
                            partials=self.n_partial, finals=self.n_final,
                            speakers=(self.spk_reg.count if self.spk_reg else 0),
                            rtf=self.last_rtf,
                            vad=self.vad.stats if self.vad else {},
                            mem_mb=self.app.mem_mb(),
                            uptime_s=int(time.time() - self.t_start)))

    async def _replay_missed(self):
        """断线重连：客户端带 last_seg_id（含 0=全部），把漏掉的 final 补发。"""
        if self.last_seg_id_req is None:
            return
        for item in self.app.missed_finals(self.sid, self.last_seg_id_req):
            payload = dict(item)
            payload.pop("type", None)          # payload 里带的是 final，换成 replay 重发
            await self._send(ev(type="replay", **payload))


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

class AsrApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine_lock = threading.Lock()   # 引擎只允许一个线程同时用（跨线程）
        self.engine = None
        self.refiner = None                   # 可选：流式 final 的离线精修引擎
        self.spk = None                       # 可选：声纹分离（final 带 spk=S1/S2）
        self.device = "cpu"
        self.sessions = []
        self.warmup_done = asyncio.Event()
        self.state = "starting"               # starting|warming|ready|loading|idle_unloaded
        self.started_at = time.time()
        self.final_ring = collections.deque(maxlen=100)
        self.final_by_sid = {}
        self.counters = collections.Counter({"ws_connections": 0, "rejected_busy": 0,
                                             "finals": 0, "http_transcribes": 0,
                                             "engine_switches": 0, "audio_ms": 0})
        self.last_error = ""
        self._uv = None
        self._loop = None

    # -- 引擎 -------------------------------------------------------------

    def resolve_device(self) -> str:
        if self.cfg.device == "cpu":
            return "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                p = torch.cuda.get_device_properties(0)
                logger.info("GPU: %s (%.1fGB, sm_%d%d)", p.name, p.total_memory / 2 ** 30,
                            p.major, p.minor)
                if p.major < 6:
                    logger.warning("GPU 计算能力过低(sm_%d%d)，多数 ASR 引擎不支持", p.major, p.minor)
                return "cuda:0"
        except Exception as exc:
            logger.info("CUDA 不可用（%s），使用 CPU", exc)
        if str(self.cfg.device).startswith("cuda"):
            raise SystemExit("指定了 --device cuda 但当前环境 CUDA 不可用")
        return "cpu"

    def load_engine(self, name: str = ""):
        name = name or self.cfg.engine
        self.state = "loading"
        prev, prev_ref = self.engine, self.refiner
        # 换引擎是「换」不是「加」：先把旧的卸掉，再让新引擎跑显存守卫。
        # 守卫读的是驱动级空闲，旧链（流式 840 + punc 1.1G + 精修 840 ≈ 2.8GB）
        # 不让位的话，「切到 qwen3-asr-0.6b（需 2600MB）」会被误判成显存不足 ——
        # 实测空闲只剩 1312MB，必然报错。
        switching = prev is not None
        if switching:
            self._drop_engines(f"切换到 {name}" if name != prev.name else f"重载 {name}")
        t0 = time.time()
        try:
            eng = E.build_engine(name, device=self.device, model_dir=self.cfg.model_dir,
                                 language=self.cfg.language, punc=self.cfg.punc,
                                 chunk_size=self.cfg.chunk_size, force_vram=self.cfg.force_vram)
            if eng.punc_requested is None:
                logger.info("标点模型(ct-punc ~1.1GB)：自动判定 %s（引擎自带标点=%s，家族=%s）",
                            "不加" if not eng.punc_enabled else "加",
                            eng.spec.self_punctuating, eng.spec.family)
            eng.load()
        except Exception:
            self.state = "ready"
            if switching:
                self._restore_engines(prev, prev_ref)   # 别让服务裸奔
            raise
        with self.engine_lock:
            old, self.engine = self.engine, eng
            self.cfg.engine = eng.name
        if old is not None and old is not eng:
            try:
                old.unload()
            except Exception:
                pass
        self._load_refiner()
        self.state = "ready"
        logger.info("引擎 %s 加载完成，用时 %.1fs，显存 %s", eng.name, time.time() - t0,
                    self.mem_mb())

    def _drop_engines(self, why: str = ""):
        """卸载当前引擎 + 精修，并把显存真正还给驱动（unload 里含 empty_cache）。"""
        with self.engine_lock:
            eng, self.engine = self.engine, None
            ref, self.refiner = self.refiner, None
        for e in (eng, ref):
            if e is not None:
                try:
                    e.unload()
                except Exception:
                    pass
        logger.info("已卸载引擎 %s（%s），显存 %s",
                    " + ".join(e.name for e in (eng, ref) if e is not None) or "-",
                    why or "-", self.mem_mb())

    def _restore_engines(self, eng, ref):
        """新引擎装不上时，尽力把原来那套装回去。"""
        try:
            eng.load()
            with self.engine_lock:
                self.engine, self.cfg.engine = eng, eng.name
            if ref is not None:
                try:
                    ref.load()
                except Exception:
                    ref = None
                with self.engine_lock:
                    self.refiner = ref
            logger.info("已回滚到原引擎 %s%s", eng.name, " + " + ref.name if ref else "")
        except Exception as exc:
            logger.warning("回滚原引擎 %s 失败: %s", eng.name, exc)

    def _load_refiner(self):
        """可选的离线精修引擎：流式 partial 保持低延迟，final 用整段音频重扫换取精度。"""
        name = (self.cfg.refine or "").strip()
        ref = None
        if name and name != self.engine.name:
            try:
                ref = E.build_engine(name, device=self.device, model_dir=self.cfg.model_dir,
                                     language=self.cfg.language, punc=False,
                                     chunk_size=self.cfg.chunk_size,
                                     force_vram=self.cfg.force_vram)
                ref.load()
                logger.info("精修引擎 %s 已加载（final 整段重扫）", ref.name)
            except Exception as exc:
                # 主引擎已经起来了，精修装不上就降级跑：final 少一次重写，
                # 但比整个切换失败、或者把刚装好的引擎拖没要好。
                ref = None
                self.last_error = f"refine: {exc}"
                logger.warning("精修引擎 %s 加载失败，本次不带精修继续: %s", name, exc)
        elif name:
            logger.info("refine=%s 与主引擎同名，忽略", name)
        with self.engine_lock:
            old, self.refiner = self.refiner, ref
        if old is not None:
            try:
                old.unload()
            except Exception:
                pass


    def load_spk(self):
        """可选声纹：加载失败不影响识别，只是 final 不带 spk 标签。"""
        name = (self.cfg.spk or "").strip()
        if not name:
            self.spk = None
            return None
        tag = E.SpeakerTagger(name, device=self.cfg.spk_device,
                              threshold=self.cfg.spk_threshold,
                              max_speakers=self.cfg.spk_max_speakers,
                              min_ms=self.cfg.spk_min_ms)
        try:
            tag.load()
        except Exception as exc:
            self.last_error = f"spk: {exc}"
            logger.warning("声纹模型加载失败（final 不带说话人标签）: %s", exc)
            self.spk = None
            return None
        self.spk = tag
        logger.info("说话人分离已启用: %s device=%s 阈值=%.2f 每会话最多 %d 人",
                    tag.model_id, tag.device, tag.threshold, tag.max_speakers)
        return tag

    def switch_engine(self, name: str) -> dict:
        if self.sessions:
            raise E.EngineError("有活动会话，不能切换引擎（先断开客户端）")
        self.counters["engine_switches"] += 1
        self.warmup_done.clear()
        try:
            self.load_engine(name)
            if self.cfg.warmup:
                self.run_warmup()
        except E.EngineError as exc:
            self.last_error = str(exc)
            # 把「当时卡上到底还剩多少」一起带回去，否则只看到「空闲 1312MB < 2600MB」
            # 分不清是自己没让位还是同卡别的进程占着
            raise type(exc)(f"{exc}｜此刻显存 {self.mem_mb()}") from None
        finally:
            self.warmup_done.set()
            self.state = "ready" if self.engine is not None else "error"
        return self.engine.describe()

    def unload_engine(self):
        self._drop_engines("空闲卸载")
        if self.spk is not None:          # 声纹按需重载（load 是惰性的）
            try:
                self.spk.unload()
            except Exception:
                pass

    def mem_mb(self) -> dict:
        free, total, name = E.gpu_free_mb(self.device)
        used = E.torch_mem_mb()
        return {"free_mb": None if free is None else int(free),
                "total_mb": None if total is None else int(total),
                "gpu": name,
                "torch_alloc_mb": None if used is None else used[0],
                "torch_reserved_mb": None if used is None else used[1],
                "device": self.device}

    # -- warmup -----------------------------------------------------------

    def find_warmup_audio(self):
        cands = []
        if self.cfg.warmup_audio:
            cands.append(self.cfg.warmup_audio)
        here = os.path.dirname(os.path.abspath(__file__))
        cands += [os.path.join(here, "resources", "asr_warmup.wav"),
                  os.path.join(here, "..", "resources", "asr_warmup.wav")]
        # 已下载模型自带的 example 音频（fsmn-vad / paraformer 都带真实语音）
        for root in (os.path.expanduser("~/.cache/modelscope"),
                     os.path.expanduser("~/.modelscope")):
            for pat in ("*/example/*.wav", "*/*/example/*.wav", "*/*/*/example/*.wav",
                        "models/*/snapshots/*/example/*.wav",
                        "hub/models/*/example/*.wav", "hub/models/*/*/example/*.wav"):
                cands += sorted(glob.glob(os.path.join(root, pat)))
        for p in cands:
            if p and os.path.isfile(p):
                try:
                    pcm, sr = A.read_wav(p)
                    if len(pcm) <= sr:                     # 至少 1s
                        continue
                    max_len = int(12 * sr)                 # 太长会拖慢启动
                    if len(pcm) > max_len:
                        pcm = pcm[:max_len]
                    logger.info("warmup 音频: %s (%.1fs)", p, len(pcm) / float(sr))
                    return pcm, sr
                except Exception as exc:
                    logger.warning("warmup 音频不可用 %s: %s", p, exc)
        logger.info("未找到 warmup 音频，使用合成音频（仅预热内核）")
        return A.synthetic_speech_like(3.0), 16000

    def run_warmup(self) -> dict:
        pcm, sr = self.find_warmup_audio()
        info = {}
        for i in range(2):        # 第一次编译内核 / cuDNN autotune，第二次测稳定耗时
            with self.engine_lock:
                info = self.engine.warmup(pcm, sr)
            logger.info("warmup #%d: %s", i + 1, info)
        info["mem_mb"] = self.mem_mb()
        return info

    # -- 会话管理 ---------------------------------------------------------

    def add_session(self, s: Session) -> bool:
        if len(self.sessions) >= self.cfg.max_sessions:
            self.counters["rejected_busy"] += 1
            return False
        self.sessions.append(s)
        return True

    def drop_session(self, s: Session):
        if s in self.sessions:
            self.sessions.remove(s)

    def remember_final(self, sid: str, payload: dict):
        self.counters["finals"] += 1
        self.counters["audio_ms"] += max(0, payload.get("end_ms", 0) - payload.get("start_ms", 0))
        self.final_ring.append(dict(payload))
        self.final_by_sid.setdefault(sid, collections.deque(maxlen=50)).append(dict(payload))
        while len(self.final_by_sid) > 32:                # 防长期运行内存增长
            self.final_by_sid.pop(next(iter(self.final_by_sid)), None)

    def missed_finals(self, sid: str, last_seg_id: int) -> list:
        dq = self.final_by_sid.get(sid)
        if not dq:
            return []
        return [x for x in dq if x.get("seg_id", 0) > last_seg_id]

    # -- 健康 -------------------------------------------------------------

    def health(self) -> dict:
        return {"status": self.state, "version": VERSION,
                "listen": f"{self.cfg.host}:{self.cfg.port}",
                "ws_path": WS_PATH,
                "warmup": self.warmup_done.is_set(),
                "engine": self.engine.name if self.engine else None,
                "refiner": self.refiner.name if self.refiner else None,
                # 标点模型是否真的生效（自动判定后的结果，不是命令行那个开关）
                "punc": bool(self.engine and self.engine.punc_enabled),
                "engine_detail": self.engine.describe() if self.engine else None,
                "sessions": len(self.sessions), "max_sessions": self.cfg.max_sessions,
                "offline_partials_ms": E.effective_offline_partials_ms(
                    self.engine, self.cfg.offline_partials_ms),
                "spk": (None if self.spk is None else
                        {"model": self.spk.model_id, "device": self.spk.device,
                         "threshold": self.spk.threshold, "loaded": self.spk.loaded,
                         "max_speakers": self.spk.max_speakers}),
                "uptime_s": int(time.time() - self.started_at),
                "mem": self.mem_mb(), "counters": dict(self.counters),
                "last_error": self.last_error,
                "vad": {"type": self.cfg.vad,
                        "max_end_silence_ms": self.cfg.max_end_silence_ms,
                        "max_segment_ms": self.cfg.max_segment_ms,
                        "min_speech_ms": self.cfg.min_speech_ms}}

    # -- 一次性转写 -------------------------------------------------------

    def _transcribe_segment(self, seg, sr_engine: int, language: str):
        """整段转写：优先离线引擎；主引擎只支持流式时用 refiner，再退化成流式喂整段。"""
        eng = self.engine
        audio = A.int16_to_float(seg)
        if getattr(eng, "supports_offline", True):
            with self.engine_lock:
                return eng.transcribe(audio, sr_engine, language)
        ref = self.refiner
        if ref is not None:
            with self.engine_lock:
                res = ref.transcribe(audio, sr_engine, language)
            if not getattr(ref.spec, "self_punctuating", False):
                try:                   # 精修引擎不挂标点模型，复用主引擎的
                    res.text = self.engine.apply_punc(res.text)
                except Exception:
                    pass
            return res
        t0 = time.monotonic()
        st = eng.new_stream("http")     # 整段转写自己一路状态，不碰会话的
        with self.engine_lock:
            eng.stream_reset(st)
            eng.stream_feed(audio, st)
            text = eng.stream_flush(st)
        return E.Transcription(text=text or "", segments=[], engine=eng.name,
                               elapsed_ms=int((time.monotonic() - t0) * 1000))

    def transcribe_source(self, source, language: str = "", with_segments: bool = True,
                          gain: bool = True) -> dict:
        if self.engine is None:
            self.load_engine()
        t0 = time.monotonic()
        pcm, sr = self._load_audio_source(source)
        if gain:
            pcm = A.normalize_gain(pcm)
        sr_engine = self.engine.spec.sample_rate
        if sr != sr_engine:
            pcm = A.pcm16_resample(pcm, sr, sr_engine)
        texts, segments, used = [], [], []
        for st, en, reason in self._segment_offline(pcm, sr_engine):
            seg = pcm[int(st * sr_engine / 1000):int(en * sr_engine / 1000)]
            if len(seg) < int(0.2 * sr_engine):
                continue
            res = self._transcribe_segment(seg, sr_engine, language or self.cfg.language)
            used.append(getattr(res, "engine", "") or self.engine.name)
            txt = (res.text or "").strip()
            if not txt:
                continue
            texts.append(txt)
            if with_segments:
                segments.append({"text": txt, "start_ms": st, "end_ms": en,
                                 "reason": reason, "took_ms": res.elapsed_ms})
        self.counters["http_transcribes"] += 1
        return {"ok": True, "text": "".join(texts), "segments": segments,
                "audio_ms": int(len(pcm) * 1000 / sr_engine),
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "engine": self.engine.name, "device": self.device,
                "engine_used": ",".join([u for u in dict.fromkeys(used) if u]) or None,
                "refiner": self.refiner.name if self.refiner else None}

    def _segment_offline(self, pcm, sr) -> list:
        """离线文件用同一套 VAD 切段，保证与流式行为一致。"""
        if self.cfg.vad == "none":
            return [(0, int(len(pcm) * 1000 / sr), "no_vad")]
        vad = StreamVad(sample_rate=sr, max_end_silence_ms=self.cfg.max_end_silence_ms,
                        max_segment_ms=self.cfg.max_segment_ms,
                        min_speech_ms=self.cfg.min_speech_ms,
                        pre_roll_ms=self.cfg.pre_roll_ms,
                        start_margin_db=self.cfg.start_margin_db,
                        min_level_db=self.cfg.min_level_db)
        out, start = [], 0
        step = max(1, sr // 10)                     # 100ms 一块
        for i in range(0, len(pcm), step):
            for e in vad.feed(pcm[i:i + step]):
                if e.kind == START:
                    start = e.start_ms
                elif e.kind == END:
                    out.append((start, e.end_ms, e.reason))
        for e in vad.force_end():
            if e.kind == END:
                out.append((start, e.end_ms, e.reason))
        return out

    def _load_audio_source(self, source) -> tuple:
        if source.get("path"):
            p = source["path"]
            if not os.path.isfile(p):
                raise E.EngineError(f"文件不存在: {p}")
            return A.decode_to_pcm16(p, A.ENGINE_SAMPLE_RATE), A.ENGINE_SAMPLE_RATE
        if source.get("audio_b64"):
            raw = base64.b64decode(source["audio_b64"])
            if len(raw) > self.cfg.max_body_mb * 2 ** 20:
                raise E.EngineError("音频过大")
            ext = (source.get("format") or "bin").lower()
            os.makedirs(self.cfg.spool_dir, exist_ok=True)
            tmp = os.path.join(self.cfg.spool_dir, f"up_{int(time.time() * 1000)}.{ext}")
            with open(tmp, "wb") as f:
                f.write(raw)
            try:
                return A.decode_to_pcm16(tmp, A.ENGINE_SAMPLE_RATE), A.ENGINE_SAMPLE_RATE
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        if source.get("url"):
            return self._download(source["url"])
        raise E.EngineError("需要 path / audio_b64 / url 之一")

    def _download(self, url: str) -> tuple:
        import urllib.request
        if not url.lower().startswith(("http://", "https://")):
            raise E.EngineError("只支持 http/https URL")
        os.makedirs(self.cfg.spool_dir, exist_ok=True)
        tmp = os.path.join(self.cfg.spool_dir, f"dl_{int(time.time() * 1000)}.audio")
        try:
            with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as f:
                got = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > self.cfg.max_body_mb * 2 ** 20:
                        raise E.EngineError("下载超过大小上限")
                    f.write(chunk)
            return A.decode_to_pcm16(tmp, A.ENGINE_SAMPLE_RATE), A.ENGINE_SAMPLE_RATE
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    # -- 服务循环 ---------------------------------------------------------

    async def ws_handler(self, ws):
        self.counters["ws_connections"] += 1
        path = getattr(ws, "path", None) or getattr(getattr(ws, "request", None), "path", "")
        qs = parse_qs(urlparse(path or "").query)
        sid = (qs.get("session_id") or [""])[0] or f"s{int(time.time() * 1000)}"
        headers = getattr(ws, "request_headers", {}) or {}
        token = ((qs.get("token") or [""])[0]
                 or str(headers.get("Authorization", "")).replace("Bearer ", "")
                 or str(headers.get("X-ASR-Token", "")))
        if self.cfg.token and token != self.cfg.token:
            logger.warning("拒绝未授权连接 sid=%s", sid)
            try:
                await ws.close(code=4401, reason="unauthorized")
            except Exception:
                pass
            return

        sess = Session(self, ws, sid)
        try:
            sess.sr = int((qs.get("sample_rate") or [self.cfg.sample_rate])[0])
        except ValueError:
            sess.sr = self.cfg.sample_rate
        raw_last = (qs.get("last_seg_id") or [None])[0]
        if raw_last is not None and str(raw_last).isdigit():
            sess.last_seg_id_req = int(raw_last)

        if not self.add_session(sess):
            logger.warning("并发已满（%d），拒绝 sid=%s", self.cfg.max_sessions, sid)
            try:
                await ws.send(ev(type="error", code="busy",
                                 message=f"并发已满（max_sessions={self.cfg.max_sessions}）"))
                await ws.close(code=4403, reason="busy")
            except Exception:
                pass
            return

        logger.info("会话开始 sid=%s sr=%d", sid, sess.sr)
        try:
            await sess.run()
        except WSClosed:
            pass
        except Exception as exc:
            logger.exception("会话异常 sid=%s", sid)
            self.last_error = str(exc)
        finally:
            self.drop_session(sess)
            if sess.audio_ms == 0:
                # 「按钮有反应但一个字都没有」：连接活着、客户端却一帧音频都没送
                logger.warning("会话结束 sid=%s：整场没收到音频（浏览器没送？AudioContext 被挂起？）",
                               sid)
            else:
                logger.info("会话结束 sid=%s audio_ms=%d finals=%d", sid, sess.audio_ms, sess.n_final)

    async def _idle_watchdog(self):
        if self.cfg.idle_unload_min <= 0:
            return
        last_active = time.time()
        while True:
            await asyncio.sleep(30)
            if self.sessions:
                last_active = time.time()
                continue
            if self.engine is not None and \
                    time.time() - last_active > self.cfg.idle_unload_min * 60:
                logger.info("空闲超过 %d 分钟，卸载引擎释放显存", self.cfg.idle_unload_min)
                self.unload_engine()
                self.state = "idle_unloaded"

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self.device = self.resolve_device()
        self.load_engine()
        self.load_spk()
        self.state = "warming"
        if self.cfg.warmup:
            asyncio.ensure_future(asyncio.to_thread(self._warmup_task))
        else:
            self.warmup_done.set()
            self.state = "ready"

        uv_cfg = uvicorn.Config(
            self._build_app(), host=self.cfg.host, port=self.cfg.port,
            ws=_ws_impl(), ws_max_size=4 << 20,
            ws_ping_interval=20, ws_ping_timeout=20,
            lifespan="off", log_config=None, access_log=self.cfg.access_log,
            timeout_graceful_shutdown=5, backlog=64)
        self._uv = uvicorn.Server(uv_cfg)
        asyncio.ensure_future(self._idle_watchdog())
        logger.info("ASR 服务就绪  WS=ws://%s:%d%s  HTTP=http://%s:%d/health  "
                    "engine=%s device=%s", self.cfg.host, self.cfg.port, WS_PATH,
                    self.cfg.host, self.cfg.port, self.cfg.engine, self.device)
        try:
            await self._uv.serve()      # SIGINT / SIGTERM 由 uvicorn 优雅收尾
        finally:
            self._uv = None
            self.unload_engine()

    def _warmup_task(self):
        try:
            info = self.run_warmup()
            logger.info("warmup 完成: %s", info)
        except Exception as exc:
            logger.exception("warmup 失败（服务仍可用，但首请求会慢）")
            self.last_error = f"warmup: {exc}"
        finally:
            self.warmup_done.set()
            self.state = "ready"

    # -- ASGI：WS 与 HTTP 同端口，按路径区分 ---------------------------------
    # 以后要加 MCP，只需在这里多挂一条 /mcp 路由，端口不用动。

    def _build_app(self) -> Starlette:
        svc = self
        webui_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "webui_asr.html")

        def _json(obj, status: int = 200) -> Response:
            return Response(json.dumps(obj, ensure_ascii=False), status_code=status,
                            media_type="application/json; charset=utf-8")

        def _fail(exc) -> Response:
            logger.exception("HTTP 请求失败")
            svc.last_error = str(exc)
            return _json({"ok": False, "error": str(exc)}, 500)

        def _authed(request) -> bool:
            if not svc.cfg.token:
                return True
            hdr = (request.headers.get("X-ASR-Token")
                   or str(request.headers.get("Authorization", "")).replace("Bearer ", ""))
            tok = request.query_params.get("token", "")
            return hdr == svc.cfg.token or tok == svc.cfg.token

        def _guard(handler):
            async def wrapped(request):
                if not _authed(request):
                    return _json({"ok": False, "error": "unauthorized"}, 401)
                return await handler(request)
            return wrapped

        async def _raw(request) -> bytes:
            limit = svc.cfg.max_body_mb * 2 ** 20
            if int(request.headers.get("content-length") or 0) > limit:
                raise E.EngineError(f"body 超过 {svc.cfg.max_body_mb}MB 上限")
            raw = await request.body()
            if len(raw) > limit:
                raise E.EngineError(f"body 超过 {svc.cfg.max_body_mb}MB 上限")
            return raw

        async def _json_body(request) -> dict:
            raw = await _raw(request)
            try:
                obj = json.loads(raw or b"{}")
            except Exception:
                raise ValueError("bad json") from None
            return obj if isinstance(obj, dict) else {}

        def _as_audio(raw: bytes, q) -> dict:
            # 直接 POST 音频字节：curl --data-binary @a.mp3 ".../transcribe?format=mp3"
            return {"audio_b64": base64.b64encode(raw).decode(),
                    "format": q.get("format", "bin"),
                    "language": q.get("language", ""),
                    "with_segments": q.get("segments", "1") != "0"}

        async def _audio_body(request) -> dict:
            raw = await _raw(request)
            q = request.query_params
            ctype = (request.headers.get("content-type") or "").lower()
            if raw and "json" not in ctype and raw.lstrip()[:1] not in (b"{", b"["):
                return _as_audio(raw, q)
            try:
                obj = json.loads(raw or b"{}")
            except Exception:
                return _as_audio(raw, q)   # curl -d @file 之类没带 Content-Type 的音频
            return obj if isinstance(obj, dict) else {}

        # ---- GET ---------------------------------------------------------
        async def r_health(request):
            return _json(svc.health())

        async def r_stats(request):
            return _json({"ok": True, "counters": dict(svc.counters),
                          "mem": svc.mem_mb(), "sessions": len(svc.sessions)})

        async def r_engines(request):
            return _json({"ok": True, "current": svc.cfg.engine,
                          "engines": [E.engine_availability(n)
                                      for n in sorted(E.ENGINE_SPECS)]})

        async def r_index(request):
            return _json({"ok": True, "service": "asr", "version": VERSION,
                          "ws": f"ws://<host>:{svc.cfg.port}{WS_PATH}",
                          "http": ["GET /health", "GET /stats", "GET /engines",
                                   "GET /webui", "POST /engine", "POST /warmup",
                                   "POST /transcribe", "POST /shutdown"]})

        async def r_webui(request):
            if not os.path.exists(webui_file):
                return _json({"ok": False, "error": "webui_asr.html 不存在"}, 404)
            with open(webui_file, "rb") as f:
                return Response(f.read(), media_type="text/html; charset=utf-8")

        # ---- POST（引擎调用丢线程池，不阻塞事件循环）--------------------
        async def r_engine(request):
            try:
                body = await _json_body(request)
                eng = await asyncio.to_thread(svc.switch_engine, body.get("name", ""))
                return _json({"ok": True, "engine": eng})
            except ValueError:
                return _json({"ok": False, "error": "bad json"}, 400)
            except Exception as exc:
                return _fail(exc)

        async def r_warmup(request):
            try:
                return _json({"ok": True,
                              "warmup": await asyncio.to_thread(svc.run_warmup)})
            except Exception as exc:
                return _fail(exc)

        async def r_transcribe(request):
            try:
                body = await _audio_body(request)
                out = await asyncio.to_thread(
                    svc.transcribe_source, body, body.get("language", ""),
                    bool(body.get("with_segments", True)),
                    bool(body.get("gain", True)))
                return _json(out)
            except Exception as exc:
                return _fail(exc)

        async def r_shutdown(request):
            # 端口对外开着，关服务得是本机或带 token
            peer = getattr(request.client, "host", "") or ""
            if peer not in ("127.0.0.1", "::1", "localhost") and not svc.cfg.token:
                return _json({"ok": False,
                              "error": "远程关闭需先配 --token"}, 403)
            threading.Thread(target=lambda: os._exit(0), daemon=True).start()
            return _json({"ok": True})

        # ---- WebSocket ---------------------------------------------------
        async def r_ws(ws):
            await ws.accept()                 # 鉴权失败在 ws_handler 里用 4401 关闭
            await svc.ws_handler(WsConn(ws))

        routes = [
            WebSocketRoute(WS_PATH, r_ws),
            WebSocketRoute("/", r_ws),        # 兼容连根路径的旧客户端
            Route("/health", _guard(r_health), methods=["GET"]),
            Route("/stats", _guard(r_stats), methods=["GET"]),
            Route("/engines", _guard(r_engines), methods=["GET"]),
            Route("/webui", _guard(r_webui), methods=["GET"]),
            Route("/engine", _guard(r_engine), methods=["POST"]),
            Route("/warmup", _guard(r_warmup), methods=["POST"]),
            Route("/transcribe", _guard(r_transcribe), methods=["POST"]),
            Route("/shutdown", _guard(r_shutdown), methods=["POST"]),
            Route("/", r_index, methods=["GET"]),
        ]

        async def on_http_exc(request, exc):
            return _json({"ok": False, "error": str(exc.detail)}, exc.status_code)

        return Starlette(routes=routes,
                         exception_handlers={HTTPException: on_http_exc})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def acquire_lock(cfg: Config):
    """防止 monitor 60s 轮询期间重复拉起（模型下载慢时尤其重要）。"""
    fd = open(cfg.lock_file, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"另一个 ASR 服务已持有锁 {cfg.lock_file}，本进程退出", file=sys.stderr)
        sys.exit(1)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd                      # 不关闭：进程退出时自动释放


def setup_logging(cfg: Config):
    handlers = [logging.StreamHandler(sys.stdout)]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        handlers=handlers)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ASR 流式识别服务（WebSocket + HTTP 控制面）")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7996, help="WebSocket 端口")
    p.add_argument("--http-port", type=int, default=0,
                   help="已废弃：HTTP 与 WS 同端口，按路径区分")
    p.add_argument("--access-log", action="store_true", help="打印 HTTP 访问日志")
    p.add_argument("--engine", default="paraformer-streaming",
                   help=" / ".join(sorted(E.ENGINE_SPECS)))
    p.add_argument("--gpu", default="", help="写入 CUDA_VISIBLE_DEVICES")
    p.add_argument("--device", default="auto", help="auto|cuda:0|cpu")
    p.add_argument("--model-dir", default="", help="本地模型目录（离线加载）")
    p.add_argument("--language", default="zh")
    p.add_argument("--punc", dest="punc", action="store_true", default=None,
                   help="强制加载标点模型 ct-punc（默认自动：引擎自带标点就不加）")
    p.add_argument("--no-punc", dest="punc", action="store_false",
                   help="强制不加标点模型（省 ~1.1GB 显存）；不带这两个开关时按引擎自动判断")
    p.add_argument("--chunk-size", default="0,10,5", help="流式块 [左,当前,右]，单位 60ms")
    p.add_argument("--refine", default="",
                   help="流式 final 用该离线引擎整段重扫（如 paraformer）；实测不一定更好，"
                        "常把口语重复改写成没说过的话，默认关")
    p.add_argument("--vad", default="energy", choices=["energy", "none"])
    p.add_argument("--max-end-silence-ms", type=int, default=800)
    p.add_argument("--max-segment-ms", type=int, default=15000)
    p.add_argument("--min-speech-ms", type=int, default=250)
    p.add_argument("--pre-roll-ms", type=int, default=200)
    p.add_argument("--start-margin-db", type=float, default=7.0)
    p.add_argument("--min-level-db", type=float, default=-42.0)
    p.add_argument("--sample-rate", type=int, default=16000, help="客户端默认采样率")
    p.add_argument("--max-sessions", type=int, default=1,
                   help="并发会话数：每路有独立流式解码状态（约 3~5MB 显存/路），"
                        "推理仍由 engine_lock 全局串行")
    p.add_argument("--partial-interval-ms", type=int, default=300)
    p.add_argument("--offline-partials-ms", type=int, default=-1,
                   help="非流式引擎滚动 partial 间隔：-1=按引擎自动（sensevoice 600 / "
                        "paraformer 700 / whisper 1200 / qwen3-0.6b 1500 / qwen3-1.7b 不开），"
                        "0=强制关，>0=指定间隔")
    p.add_argument("--spk", default="",
                   help="说话人分离：campplus(28MB) | eres2netv2(68MB) | mock；空=关。"
                        "只分轮流说话的人，final 多带 spk=S1/S2")
    p.add_argument("--spk-device", default="cpu",
                   help="声纹推理设备：默认 cpu（零显存，3~5s 段约 130ms）；cuda:0 约 +50MB")
    p.add_argument("--spk-threshold", type=float, default=0.45,
                   help="判同一人的余弦阈值（CAM++ 实测同人≈0.69、异人<0.25）")
    p.add_argument("--spk-max-speakers", type=int, default=8,
                   help="每路会话最多分几个人（每路会话独立计数）")
    p.add_argument("--spk-min-ms", type=int, default=800,
                   help="短于此的段不抽声纹，沿用上一位（短 embedding 不可靠）")
    p.add_argument("--token", default="", help="访问令牌（空=不校验）")
    p.add_argument("--no-warmup", dest="warmup", action="store_false", default=True)
    p.add_argument("--warmup-audio", default="")
    p.add_argument("--idle-unload-min", type=int, default=0, help="0=常驻")
    p.add_argument("--lock-file", default="/tmp/asr_server.lock")
    p.add_argument("--spool-dir", default="/tmp/asr_spool")
    p.add_argument("--max-body-mb", type=int, default=64)
    p.add_argument("--force-vram", action="store_true", help="忽略显存守卫")
    p.add_argument("--log-file", default="")
    p.add_argument("--self-test", action="store_true",
                   help="加载引擎 + warmup + 合成音频跑通链路，打印显存/延迟后退出")
    p.add_argument("--print-engines", action="store_true")
    return p.parse_args(argv)


def self_test(cfg: Config) -> int:
    """在目标机器上验证：模型能下载/加载、显存够、VAD + 引擎 + 标点链路通。"""
    app = AsrApp(cfg)
    app.device = app.resolve_device()
    print(f"device={app.device}  engine={cfg.engine}")
    print(f"显存(加载前): {app.mem_mb()}")
    app.load_engine()
    print(f"warmup: {app.run_warmup()}")

    pcm = A.synthetic_speech_like(4.0)
    segs = app._segment_offline(pcm, 16000)
    print(f"VAD 切段: {segs}")
    n_out = 0
    for st, en, reason in segs:
        seg = pcm[int(st * 16):int(en * 16)]
        with app.engine_lock:
            r = app.engine.transcribe(A.int16_to_float(seg), 16000, cfg.language)
        n_out += 1
        print(f"  [{st}-{en}ms {reason}] {r.text!r}  ({r.elapsed_ms}ms)")

    if app.engine.supports_streaming:
        app.engine.stream_reset()
        audio = A.int16_to_float(pcm)
        step = 960 * max(1, cfg.chunk_size[1])
        for i in range(0, len(audio), step):
            t = app.engine.stream_feed(audio[i:i + step])
            if t:
                n_out += 1
                print(f"  partial@{i * 1000 // 16000}ms: {t!r}")
        fin = app.engine.stream_flush()
        n_out += 1 if fin else 0
        print(f"  final: {fin!r}")

    print(f"显存(加载后): {app.mem_mb()}")
    print("SELF-TEST OK" if n_out else "SELF-TEST WARN: 没有产出任何文本（合成音频无语音属正常，"
                                       "请用 --warmup-audio 指定真实语音再判）")
    app.unload_engine()
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = Config.from_args(args)
    setup_logging(cfg)

    if args.print_engines:
        for n in sorted(E.ENGINE_SPECS):
            d = E.engine_availability(n)
            print(f"{d['name']:22s} streaming={str(d['streaming']):5s} "
                  f"min_vram={d['min_vram_mb']:5d}MB avail={str(d['available']):5s} "
                  f"{d.get('reason', ''):30s} {d['note']}")
        return 0

    # 必须在导入 torch 之前设置
    if cfg.gpu != "":
        os.environ["CUDA_VISIBLE_DEVICES"] = str(cfg.gpu)
        logger.info("CUDA_VISIBLE_DEVICES=%s", cfg.gpu)

    os.makedirs(cfg.spool_dir, exist_ok=True)
    if cfg.self_test:
        cfg.lock_file = cfg.lock_file + ".selftest"   # 自检不阻塞正式服务
    lock = acquire_lock(cfg)

    if cfg.self_test:
        return self_test(cfg)

    app = AsrApp(cfg)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

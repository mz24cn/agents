#coding=utf-8
"""
ASR 引擎层：统一接口 + 引擎注册表 + 显存守卫 + 标点模型。

设计要点
--------
1. 两类能力，一套协议：
   - streaming=True  的引擎（paraformer-zh-streaming）：真流式，边喂边出 partial
   - streaming=False 的引擎（paraformer / sensevoice / whisper / qwen3-asr）：
     由服务端用 VAD 分段后整段识别（"准流式"），协议输出完全一致
2. 引擎按需加载，一次只加载一个（显存有限，且并发只有 1 路）；
   换引擎走 /engine 热切换（unload -> load -> warmup），方便对比效果。
3. Qwen3-ASR 依赖 transformers==4.57 + qwen-asr，依赖树很重，且它的真流式接口只在
   vLLM 后端可用（多数部署用不上）—— 因此它跑在**子进程 worker** 里，通过 JSON-Lines
   协议通信（见 qwen_asr_worker.py）；默认与主服务同一个解释器，需要隔离时设
   ASR_QWEN_PYTHON=/path/to/python。
4. 加载前做显存守卫：引擎声明 min_vram_mb，空闲显存不足直接报错并给出建议，
   而不是让 CUDA OOM 把进程干掉（同一张卡上往往还跑着别的服务）。
"""

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

import asr_audio as A

logger = logging.getLogger("asr.engines")

# Qwen3-ASR 走子进程 worker（它的 transformers 版本和流式接口跟 FunASR 不在一个节奏上）。
# 默认用当前解释器，即所有引擎共用同一个环境；需要隔离时设 ASR_QWEN_PYTHON=/path/to/python。
QWEN_PY = os.environ.get("ASR_QWEN_PYTHON") or sys.executable


# ---------------------------------------------------------------------------
# 引擎注册表
# ---------------------------------------------------------------------------

@dataclass
class EngineSpec:
    name: str
    family: str                 # funasr | whisper | qwen | mock
    model_id: str               # modelscope 模型 id 或本地目录
    streaming: bool
    min_vram_mb: int            # 加载所需的最小空闲显存（含 CUDA context 余量）
    dtype: str = "float32"
    sample_rate: int = 16000
    language: str = "zh"
    python_bin: str = ""        # 非空 => 用该解释器起子进程（跨 env 引擎）
    self_punctuating: bool = False   # 引擎输出自带标点（Qwen3-ASR），不能再叠 ct-punc
    # 非流式引擎的「滚动 partial」自动间隔（ms）：换到该引擎时若用户没显式指定
    # --offline-partials-ms，就用这个值，否则中间一个字都不出。0 = 不自动开。
    # 数值按引擎 RTF 分档：越慢的引擎间隔越大，太慢的直接不开。
    offline_partial_ms: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "family": self.family, "model_id": self.model_id,
                "streaming": self.streaming, "min_vram_mb": self.min_vram_mb,
                "dtype": self.dtype, "sample_rate": self.sample_rate,
                "language": self.language, "cross_env": bool(self.python_bin),
                "self_punctuating": self.self_punctuating,
                "offline_partial_ms": self.offline_partial_ms, "note": self.note}


ENGINE_SPECS = {
    "paraformer-streaming": EngineSpec(
        "paraformer-streaming", "funasr",
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
        streaming=True, min_vram_mb=1900,
        note="默认引擎：中文流式，partial 实时；chunk_size 可调延迟/精度"),
    "paraformer": EngineSpec(
        "paraformer", "funasr",
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        streaming=False, min_vram_mb=2200, offline_partial_ms=700,
        note="离线高精度（VAD 分段后整段识别），配 ct-punc"),
    "sensevoice": EngineSpec(
        "sensevoice", "funasr", "iic/SenseVoiceSmall",
        streaming=False, min_vram_mb=1800, offline_partial_ms=600,
        note="多语种+情感，短音频强；非流式，靠服务端 VAD 分段"),
    "whisper": EngineSpec(
        "whisper", "whisper", "Systran/faster-whisper-large-v3",
        streaming=False, min_vram_mb=2000, dtype="int8", offline_partial_ms=1200,
        note="faster-whisper(CTranslate2)；老显卡用 int8/float32"),
    "qwen3-asr-0.6b": EngineSpec(
        "qwen3-asr-0.6b", "qwen", "Qwen/Qwen3-ASR-0.6B",
        streaming=False, min_vram_mb=2600, dtype="float16",
        python_bin=QWEN_PY, self_punctuating=True, offline_partial_ms=1500,
        note="子进程 worker；transformers 后端（vLLM 后端不可用时）；输出自带标点"),
    "qwen3-asr-1.7b": EngineSpec(
        "qwen3-asr-1.7b", "qwen", "Qwen/Qwen3-ASR-1.7B",
        streaming=False, min_vram_mb=4600, dtype="float16",
        python_bin=QWEN_PY, self_punctuating=True, offline_partial_ms=0,   # 太慢，自动开也开不起
        note="精度最高但最重；显存紧张时建议 --no-punc"),
    "mock": EngineSpec(
        "mock", "mock", "-", streaming=True, min_vram_mb=0,
        note="无依赖假引擎：用于协议/单测/压测，不加载任何模型"),
    "mock-offline": EngineSpec(
        "mock-offline", "mock", "-", streaming=False, min_vram_mb=0, offline_partial_ms=600,
        note="假离线引擎：给 --refine 做单测，按整段时长出词"),
}

# 中文别名，命令行里好写
ENGINE_ALIASES = {
    "paraformer-zh-streaming": "paraformer-streaming",
    "streaming": "paraformer-streaming",
    "paraformer-zh": "paraformer",
    "sensevoice-small": "sensevoice",
    "faster-whisper": "whisper",
    "qwen3-asr": "qwen3-asr-0.6b",
}


def resolve_engine_name(name: str) -> str:
    n = (name or "").strip().lower()
    n = ENGINE_ALIASES.get(n, n)
    if n not in ENGINE_SPECS:
        raise EngineError(f"未知引擎 '{name}'，可选: {', '.join(sorted(ENGINE_SPECS))}")
    return n


# ---------------------------------------------------------------------------
# 异常与结果
# ---------------------------------------------------------------------------

class EngineError(Exception):
    pass


class EngineUnavailable(EngineError):
    """依赖缺失 / 显存不足 / 模型下载失败"""


@dataclass
class Transcription:
    text: str = ""
    segments: list = field(default_factory=list)   # [{text,start_ms,end_ms}]
    elapsed_ms: int = 0
    engine: str = ""
    language: str = ""

    def as_dict(self) -> dict:
        return {"text": self.text, "segments": self.segments,
                "elapsed_ms": self.elapsed_ms, "engine": self.engine,
                "language": self.language}


# ---------------------------------------------------------------------------
# 显存守卫
# ---------------------------------------------------------------------------

def _device_index(device: str = "") -> int:
    idx = 0
    if device and ":" in device:
        try:
            idx = int(device.split(":")[1])
        except ValueError:
            idx = 0
    return idx


def gpu_free_mb(device: str = ""):
    """返回 (free_mb, total_mb, name)；拿不到信息返回 (None, None, None)。

    注意：pynvml 按**物理**卡号枚举，不认 CUDA_VISIBLE_DEVICES；torch 认。
    所以优先用 torch，退回 pynvml 时要把 CVD 映射回物理卡号（本机 --gpu 1）。
    """
    idx = _device_index(device)
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info(min(idx, torch.cuda.device_count() - 1))
            return free / 2**20, total / 2**20, torch.cuda.get_device_name(
                min(idx, torch.cuda.device_count() - 1))
    except Exception:
        pass
    try:
        import pynvml
        pynvml.nvmlInit()
        phys = idx
        cvd = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
        if cvd and cvd != "-1":
            order = [x.strip() for x in cvd.split(",") if x.strip() != ""]
            if len(order) > idx:
                try:
                    phys = int(order[idx])
                except ValueError:      # CVD 里可能是 UUID
                    phys = idx
        h = pynvml.nvmlDeviceGetHandleByIndex(phys)
        mi = pynvml.nvmlDeviceGetMemoryInfo(h)
        name = pynvml.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode("utf-8", "ignore")
        pynvml.nvmlShutdown()
        return mi.free / 2**20, mi.total / 2**20, f"{name}#{phys}"
    except Exception:
        return None, None, None


def torch_mem_mb():
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return int(torch.cuda.memory_allocated() / 2**20), int(torch.cuda.memory_reserved() / 2**20)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 标点模型（流式引擎输出无标点，final 后补）
# ---------------------------------------------------------------------------

class PuncModel:
    """FunASR ct-punc 封装：只处理文本，显存 ~1.2GB，可 --no-punc 关掉。"""

    def __init__(self, model_id="ct-punc", device="cpu", enabled=True):
        self.model_id = model_id
        self.device = device
        self.enabled = bool(enabled)
        self._m = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._m is not None

    def load(self):
        if not self.enabled or self._m is not None:
            return
        with self._lock:
            if self._m is not None:
                return
            from funasr import AutoModel
            kwargs = dict(model=self.model_id, device=self.device,
                          disable_update=True, disable_pbar=True)
            try:
                self._m = AutoModel(disable_log=True, **kwargs)
            except TypeError:
                self._m = AutoModel(**kwargs)
            logger.info("标点模型已加载: %s (%s)", self.model_id, self.device)

    def apply(self, text: str) -> str:
        if not self.enabled or not text or not text.strip():
            return text
        try:
            self.load()
            res = self._m.generate(input=text)
            out = _extract_text(res)
            return out or text
        except Exception as exc:  # 标点失败不能影响识别结果
            logger.warning("标点模型失败（返回原文）: %s", exc)
            return text

    def unload(self):
        self._m = None
        _free_gpu_memory()


# ---------------------------------------------------------------------------
# 说话人分离（可选：给每段 final 打 S1/S2 标签）
# ---------------------------------------------------------------------------

SPK_MODELS = {
    "campplus": {"model_id": "iic/speech_campplus_sv_zh-cn_16k-common",
                 "note": "CAM++ 声纹 28MB；实测同人 cos≈0.69、异人 -0.17~0.23"},
    "eres2netv2": {"model_id": "iic/speech_eres2netv2_sv_zh-cn_16k-common",
                   "note": "ERes2NetV2 声纹 68MB，区分度略高"},
    "mock": {"model_id": "",
             "note": "频谱指纹假声纹：不下载模型，用于打通链路与跑测试"},
}


def resolve_spk_model(name: str) -> str:
    """--spk 的别名 → modelscope id；未知名当作 id 直接用；空 → ''（关闭）。"""
    n = (name or "").strip().lower()
    if not n:
        return ""
    spec = SPK_MODELS.get(n)
    if spec is not None:
        return spec["model_id"] or n         # mock 没有 model_id，保留原名
    return n                                 # 未知名：当成 modelscope id 用


def _spectral_fingerprint(x: np.ndarray, sr: int) -> np.ndarray:
    """64 个对数频段的能量指纹（单位向量）——mock 声纹，够测试区分两个音高。

    只保留 ≥ 峰值 10% 的频段：静音段的宽带噪声底会把所有频段都填上，两段静音
    之间余弦接近 1，会把两个不同的"说话人"错判成同一个人。
    """
    n = int(x.size)
    if n < 64:
        return np.zeros(0)
    m = min(n, sr * 4)
    spec = np.abs(np.fft.rfft(x[:m] * np.hanning(m)))
    freqs = np.fft.rfftfreq(m, 1.0 / sr)
    bins = np.zeros(64)
    lo, hi = math.log(60.0), math.log(max(200.0, sr / 2.0))
    for f, e in zip(freqs, spec):
        if f <= 0:
            continue
        i = int((math.log(f) - lo) / (hi - lo) * 63.999)
        if 0 <= i < 64:
            bins[i] = max(bins[i], float(e))
    peak = float(bins.max())
    if peak <= 1e-9:
        return np.zeros(0)
    bins[bins < 0.1 * peak] = 0.0
    norm = float(np.linalg.norm(bins))
    return bins / norm if norm > 1e-9 else np.zeros(0)


def _embedding_of(res) -> np.ndarray:
    """funasr 声纹返回 [{'spk_embedding': Tensor(1,192)}]；兼容 list/dict/tensor。"""
    import torch
    obj = res[0] if isinstance(res, (list, tuple)) and res else res
    if isinstance(obj, dict):
        for k in ("spk_embedding", "embedding", "spk_emb", "feat"):
            if k in obj:
                obj = obj[k]
                break
    if isinstance(obj, (list, tuple)) and obj:
        obj = obj[0]
    if isinstance(obj, torch.Tensor):
        obj = obj.detach().cpu().numpy()
    v = np.asarray(obj, dtype=np.float64).reshape(-1)
    return v


class SpeakerRegistry:
    """一路会话的说话人聚类：在线质心 + 余弦阈值。

    只分"轮流说话"的人：能量 VAD 按停顿切段，每段抽一个 embedding 和已有质心比。
    重叠语音（两人同时开口）分不出来，那要语音分离/端到端 diarization 模型。
    """

    def __init__(self, threshold: float = 0.45, max_speakers: int = 8):
        self.threshold = float(threshold)
        self.max_speakers = max(1, int(max_speakers))
        self._c: list = []          # [[单位质心, 计数], ...]
        self.last = None            # 上一段判定的编号，短段沿用

    def reset(self):
        self._c = []
        self.last = None

    @property
    def count(self) -> int:
        return len(self._c)

    def _merge(self, i: int, v: np.ndarray):
        c, n = self._c[i]
        c = c * n + v
        m = float(np.linalg.norm(c))
        self._c[i] = [(c / m) if m > 1e-12 else c, n + 1]

    def assign(self, emb):
        """返回 (编号, 与该质心的余弦)；embedding 全零返回 (None, None)。"""
        v = np.asarray(emb, dtype=np.float64).reshape(-1)
        if v.size == 0:
            return None, None
        n = float(np.linalg.norm(v))
        if n <= 1e-12:
            return None, None
        v = v / n
        sims = [float(c[0] @ v) for c in self._c]
        best = max(range(len(sims)), key=lambda i: sims[i]) if sims else -1
        if best >= 0 and sims[best] >= self.threshold:
            self._merge(best, v)
            self.last = best
            return best, sims[best]
        if len(self._c) < self.max_speakers:
            self._c.append([v.copy(), 1])
            self.last = len(self._c) - 1
            return self.last, (sims[best] if best >= 0 else None)
        self._merge(best, v)        # 人数超上限：并入最像的那个，不再无限涨
        self.last = best
        return best, sims[best]


class SpeakerTagger:
    """声纹 embedding + 聚类。加载失败不影响识别，只是 final 不带 spk 标签。"""

    def __init__(self, name: str = "campplus", device: str = "cpu", threshold: float = 0.45,
                 max_speakers: int = 8, min_ms: int = 800):
        self.name = (name or "").strip().lower()
        self.model_id = resolve_spk_model(self.name)
        self.device = device or "cpu"
        self.threshold = float(threshold)
        self.max_speakers = int(max_speakers)
        self.min_ms = int(min_ms)
        self._m = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.name)

    @property
    def loaded(self) -> bool:
        return self.name == "mock" or self._m is not None

    @property
    def on_gpu(self) -> bool:
        return str(self.device).startswith("cuda")

    def load(self):
        if self.name == "mock" or self._m is not None:
            return
        with self._lock:
            if self._m is not None:
                return
            if self.on_gpu:
                free, _, _ = gpu_free_mb(self.device)
                if free is not None and free < 300:
                    raise EngineError(f"声纹需要 ~300MB 显存，当前空闲 {int(free)}MB")
            from funasr import AutoModel
            kwargs = dict(model=self.model_id, device=self.device,
                          disable_update=True, disable_pbar=True)
            try:
                self._m = AutoModel(disable_log=True, **kwargs)
            except TypeError:
                self._m = AutoModel(**kwargs)
            logger.info("声纹模型已加载: %s (%s)", self.model_id, self.device)

    def embed(self, audio, sr: int) -> np.ndarray:
        """float 单声道 → 单位化 embedding；太短/失败返回空数组。"""
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size < max(64, int(sr * 0.1)):
            return np.zeros(0)
        if self.name == "mock":
            return _spectral_fingerprint(x, sr)
        self.load()
        with self._lock:
            res = self._m.generate(input=x, fs=int(sr))
        v = _embedding_of(res)
        n = float(np.linalg.norm(v))
        return v / n if n > 1e-12 else np.zeros(0)

    def new_registry(self) -> SpeakerRegistry:
        return SpeakerRegistry(self.threshold, self.max_speakers)

    def label(self, audio, sr: int, reg: SpeakerRegistry = None):
        """→ {"spk": "S1", "sim": 0.62}；没开/抽不出特征返回 None。"""
        if not self.enabled:
            return None
        reg = reg if reg is not None else self.new_registry()
        dur_ms = len(np.asarray(audio).reshape(-1)) * 1000.0 / max(1, int(sr))
        if dur_ms < self.min_ms:
            # 太短的段 embedding 不可靠；同一人接着说的概率大，沿用上一位
            if reg.last is None:
                return None
            return {"spk": f"S{reg.last + 1}", "sim": None, "spk_short": True}
        v = self.embed(audio, sr)
        idx, sim = reg.assign(v)
        if idx is None:
            return None
        return {"spk": f"S{idx + 1}", "sim": None if sim is None else round(sim, 3)}

    def unload(self):
        self._m = None
        _free_gpu_memory()


# ---------------------------------------------------------------------------
# 流式状态（按会话隔离）
# ---------------------------------------------------------------------------

class StreamState:
    """一路会话的流式解码状态。

    FunASR 的 cache 字典是有状态的（encoder 各层 SANM 的 chunk 注意力/卷积缓存 +
    decoder 的 token 历史），挂在 engine 实例上会让多路会话互相污染——A 的
    segment_start 调 stream_reset() 就把 B 正在解码的状态清空。所以按路隔离，
    一路一份（实测每路约 3~5MB 显存）。
    """

    __slots__ = ("sid", "cache", "pending", "acc_text", "extra")

    def __init__(self, sid: str = ""):
        self.sid = sid
        self.extra = {}
        self.reset()

    def reset(self):
        self.cache = {}
        self.pending = np.zeros(0, dtype=np.float32)
        self.acc_text = ""
        self.extra = {}
        return self


# ---------------------------------------------------------------------------
# 引擎基类
# ---------------------------------------------------------------------------

class BaseEngine:
    supports_streaming = False
    supports_offline = True

    def __init__(self, spec: EngineSpec, device: str = "cpu", model_dir: str = "",
                 language: str = "", punc: Optional[bool] = None, chunk_size=None):
        self.spec = spec
        self.device = device
        self.model_dir = model_dir
        self.language = language or spec.language
        # 三态：None = 自动（自带标点的引擎不加），True = 强制加，False = 强制不加。
        # punc_enabled 记的是**最终生效值**，不是传进来的原始值 —— 以前记的是原始值，
        # 于是 /health 对 qwen 报 punc:true 而模型其实压根没加载，排查显存会被假信号带偏。
        self.punc_requested = punc
        self.punc_enabled = effective_punc(spec, punc)
        self.chunk_size = list(chunk_size) if chunk_size else [0, 10, 5]
        self._loaded = False
        self._lock = threading.Lock()
        self.punc = PuncModel(device=device, enabled=self.punc_enabled)

    # -- 生命周期 ---------------------------------------------------------

    @property
    def name(self) -> str:
        return self.spec.name

    def is_loaded(self) -> bool:
        return self._loaded

    def guard_vram(self, force: bool = False):
        if self.spec.min_vram_mb <= 0 or self.device == "cpu":
            return
        free, total, name = gpu_free_mb(self.device)
        if free is None:
            logger.warning("无法读取显存信息，跳过显存守卫")
            return
        if free + 1 < self.spec.min_vram_mb:
            msg = (f"显存不足：{name} 空闲 {free:.0f}MB < 引擎 {self.name} 需要 "
                   f"{self.spec.min_vram_mb}MB（总 {total:.0f}MB）。"
                   f"建议：--no-punc 释放标点模型显存 / 换更小引擎 / 释放同卡上的其它进程")
            if not force:
                raise EngineUnavailable(msg)
            logger.warning(msg + "  [--force 已忽略]")
        else:
            logger.info("显存守卫通过：空闲 %.0fMB / 需要 %dMB (%s)", free, self.spec.min_vram_mb, name)

    def load(self):
        raise NotImplementedError

    def apply_punc(self, text: str) -> str:
        """给别处产出的文本补标点（例如精修引擎的结果）。没启用/没加载就原样返回。"""
        if not text or not self.punc_enabled or not self.punc.loaded:
            return text
        try:
            return self.punc.apply(text)
        except Exception as exc:
            logger.warning("标点模型处理失败，返回原文: %s", exc)
            return text

    def unload(self):
        self._loaded = False
        self.punc.unload()
        _free_gpu_memory()

    def warmup(self, audio_pcm16: np.ndarray, sample_rate: int = 16000) -> dict:
        """跑一次真实音频，触发模型前向 + cuDNN autotune。"""
        t0 = time.time()
        info = {"engine": self.name, "warmup_ms": 0, "text_len": 0}
        audio = A.int16_to_float(np.asarray(audio_pcm16, dtype="<i2"))
        audio = A.resample(audio, sample_rate, self.spec.sample_rate)
        if self.supports_streaming:
            self.stream_reset()
            step = 960 * max(1, self.chunk_size[1])
            text = ""
            for i in range(0, max(1, len(audio)), step):
                got = self.stream_feed(audio[i:i + step])
                if got:
                    text = got
            text = self.stream_flush() or text
            info["text_len"] = len(text or "")
        else:
            res = self.transcribe(audio, self.spec.sample_rate)
            info["text_len"] = len(res.text or "")
        info["warmup_ms"] = int((time.time() - t0) * 1000)
        return info

    # -- 离线识别 ---------------------------------------------------------

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000,
                   language: str = "") -> Transcription:
        raise EngineError(f"引擎 {self.name} 不支持整段识别")

    # -- 流式识别 ---------------------------------------------------------

    def new_stream(self, sid: str = "") -> StreamState:
        """新建一路独立的流式状态；多路并发时每路一份，互不干扰。"""
        return StreamState(sid)

    def _default_stream(self) -> StreamState:
        """没显式传 state 时用的兜底那一路（warmup / 整段转写等单线程路径）。"""
        st = getattr(self, "_stream_default", None)
        if st is None:
            st = self._stream_default = self.new_stream("default")
        return st

    def _stream_or(self, state) -> StreamState:
        return state if state is not None else self._default_stream()

    def stream_reset(self, state: StreamState = None):
        pass

    def stream_feed(self, audio: np.ndarray, state: StreamState = None) -> str:
        raise EngineError(f"引擎 {self.name} 不支持流式")

    def stream_flush(self, state: StreamState = None) -> str:
        return ""

    def describe(self) -> dict:
        d = self.spec.as_dict()
        d["loaded"] = self._loaded
        d["device"] = self.device
        d["punc"] = self.punc_enabled
        d["chunk_size"] = self.chunk_size
        return d


def _merge_stream_text(acc: str, piece: str) -> str:
    """把 chunk 增量文本并入整段文本。

    funasr 流式各版本语义不完全一致（有的返回增量、有的返回累计），这里三种情况
    都兼容：累计式（piece 以 acc 开头）直接替换；重复块丢弃；否则拼接。
    """
    piece = (piece or "").strip()
    if not piece:
        return acc
    if not acc:
        return piece
    if piece.startswith(acc):
        return piece
    if acc.endswith(piece) or acc == piece:
        return acc
    return acc + piece


def _extract_text(res) -> str:
    """FunASR generate/generate_chunk 返回值 -> 文本。"""
    if res is None:
        return ""
    if isinstance(res, dict):
        items = [res]
    elif isinstance(res, (list, tuple)):
        items = list(res)
    else:
        return str(res)
    parts = []
    for it in items:
        if isinstance(it, dict):
            t = it.get("text") or it.get("sentence") or ""
            if t:
                parts.append(str(t))
        elif it:
            parts.append(str(it))
    text = " ".join(p.strip() for p in parts).strip()
    return _postprocess_sensevoice(text)


def _postprocess_sensevoice(text: str) -> str:
    """去掉 SenseVoice 的 <|zh|><|NEUTRAL|> 之类标签。"""
    if not text or "<|" not in text:
        return text
    try:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        return rich_transcription_postprocess(text)
    except Exception:
        out, depth, buf = [], 0, []
        i = 0
        while i < len(text):
            if text.startswith("<|", i):
                j = text.find("|>", i)
                if j > 0:
                    i = j + 2
                    continue
            out.append(text[i])
            i += 1
        return "".join(out).strip()


def _free_gpu_memory():
    try:
        import torch
        if torch.cuda.is_available():
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mock 引擎（无依赖，协议联调 / 单测 / 压测）
# ---------------------------------------------------------------------------

class MockEngine(BaseEngine):
    supports_streaming = True
    _WORDS = ("今天", "天气", "不错", "我们", "出去", "散步", "并且", "拍", "一些", "照片")

    def load(self):
        self._loaded = True
        self.stream_reset()          # 兜底那一路（单线程路径）复位

    def transcribe(self, audio, sample_rate=16000, language=""):
        t0 = time.time()
        dur = len(audio) / float(sample_rate or 16000)
        n = max(1, min(len(self._WORDS), int(dur * 2.5)))
        text = "".join(self._WORDS[:n])
        if self.punc_enabled:
            text = text + "。"
        return Transcription(text=text, segments=[{"text": text, "start_ms": 0,
                                                   "end_ms": int(dur * 1000)}],
                             elapsed_ms=int((time.time() - t0) * 1000), engine=self.name)

    def stream_reset(self, state: StreamState = None):
        st = self._stream_or(state)
        st.extra["cur"] = []
        st.extra["acc"] = 0

    def stream_feed(self, audio, state: StreamState = None):
        # 每 600ms 出一个词，模拟流式增量
        st = self._stream_or(state)
        cur = st.extra.setdefault("cur", [])
        st.extra["acc"] = st.extra.get("acc", 0) + len(audio)
        need = int(0.6 * self.spec.sample_rate)
        while st.extra["acc"] >= need and len(cur) < len(self._WORDS):
            st.extra["acc"] -= need
            cur.append(self._WORDS[len(cur)])
        return "".join(cur)

    def stream_flush(self, state: StreamState = None):
        st = self._stream_or(state)
        text = "".join(st.extra.get("cur", []))
        if self.punc_enabled and text:
            text = text + "。"
        st.reset()
        return text


# ---------------------------------------------------------------------------
# FunASR 引擎
# ---------------------------------------------------------------------------

class FunASREngineBase(BaseEngine):
    UNIT_SAMPLES = 960  # paraformer 流式：1 个 chunk unit = 60ms @16k

    def _model_kwargs(self):
        kw = dict(model=self._model_ref(), device=self.device,
                  disable_update=True, disable_pbar=True)
        if self.spec.family == "funasr" and self.model_dir:
            kw["model"] = self.model_dir
        return kw

    def _model_ref(self):
        return self.spec.model_id

    def _new_model(self, extra=None):
        from funasr import AutoModel
        kw = self._model_kwargs()
        if extra:
            kw.update(extra)
        try:
            return AutoModel(disable_log=True, **kw)
        except TypeError:
            return AutoModel(**kw)

    def _ensure_torch_device(self):
        """funasr 用 device='cuda:0'；老显卡（compute capability < 8）用 fp32 更稳。"""
        return self.device


class FunASRStreamingEngine(FunASREngineBase):
    supports_streaming = True
    supports_offline = False        # 只能流式；整段转写走 --refine 或 stream 兜底

    def load(self):
        self.guard_vram(force=getattr(self, "force_vram", False))
        self._m = self._new_model()
        self.punc.load()
        self._loaded = True
        self.stream_reset()
        logger.info("FunASR 流式引擎已加载: %s device=%s chunk_size=%s",
                    self.name, self.device, self.chunk_size)

    def unload(self):
        self._m = None
        super().unload()

    def stream_reset(self, state: StreamState = None):
        self._stream_or(state).reset()

    def _step(self):
        return max(1, self.chunk_size[1]) * self.UNIT_SAMPLES

    def stream_feed(self, audio, state: StreamState = None):
        if not self._loaded:
            raise EngineError("引擎未加载")
        st = self._stream_or(state)
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size:
            st.pending = np.concatenate((st.pending, audio))
        step = self._step()
        # 只喂整块；不足一块的尾巴留到 flush（实测补零尾巴会多出字）
        while st.pending.size >= step:
            chunk = st.pending[:step]
            st.pending = st.pending[step:]
            res = self._m.generate(input=chunk, cache=st.cache,
                                   chunk_size=self.chunk_size, is_final=False)
            piece = _extract_text(res)
            if piece:
                st.acc_text = _merge_stream_text(st.acc_text, piece)
        return st.acc_text

    def stream_flush(self, state: StreamState = None):
        if not self._loaded:
            return ""
        st = self._stream_or(state)
        tail = st.pending if st.pending.size else np.zeros(self._step(), dtype=np.float32)
        res = self._m.generate(input=tail, cache=st.cache,
                               chunk_size=self.chunk_size, is_final=True)
        piece = _extract_text(res)
        if piece:
            st.acc_text = _merge_stream_text(st.acc_text, piece)
        text = st.acc_text
        st.reset()
        return self.punc.apply(text or "")


class FunASROfflineEngine(FunASREngineBase):
    supports_streaming = False

    def load(self):
        self.guard_vram(force=getattr(self, "force_vram", False))
        extra = {}
        # 离线引擎让 funasr 自己串 VAD + 标点（精度更好）；标点关掉时不挂 punc
        if self.spec.name in ("paraformer",):
            extra["vad_model"] = "fsmn-vad"
            if self.punc_enabled:
                extra["punc_model"] = "ct-punc"
        elif self.spec.name == "sensevoice":
            extra["vad_model"] = "fsmn-vad"
        self._m = self._new_model(extra)
        if not extra.get("punc_model") and self.punc_enabled:
            self.punc.load()
        self._loaded = True
        logger.info("FunASR 离线引擎已加载: %s device=%s", self.name, self.device)

    def transcribe(self, audio, sample_rate=16000, language=""):
        if not self._loaded:
            raise EngineError("引擎未加载")
        t0 = time.time()
        audio = A.resample(np.asarray(audio, dtype=np.float32), sample_rate, self.spec.sample_rate)
        kw = {"batch_size_s": 300}
        if language:
            kw["language"] = language
        try:
            res = self._m.generate(input=audio, **kw)
        except TypeError:
            res = self._m.generate(input=audio)
        text = _extract_text(res)
        segs = []
        try:
            for item in (res if isinstance(res, list) else [res]):
                if not isinstance(item, dict):
                    continue
                ts = item.get("timestamp")
                if ts:
                    segs.append({"text": item.get("text", ""),
                                 "start_ms": int(ts[0][0]), "end_ms": int(ts[-1][1])})
        except Exception:
            segs = []
        return Transcription(text=text, segments=segs,
                             elapsed_ms=int((time.time() - t0) * 1000),
                             engine=self.name, language=self.language)


# ---------------------------------------------------------------------------
# faster-whisper（CTranslate2）
# ---------------------------------------------------------------------------

class WhisperEngine(BaseEngine):
    supports_streaming = False

    def load(self):
        self.guard_vram(force=getattr(self, "force_vram", False))
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise EngineUnavailable("faster-whisper 未安装（可在 moneyprinter env 里跑，"
                                    "或 pip install faster-whisper）") from exc
        model_ref = self.model_dir or self.spec.model_id
        if not self.model_dir:
            model_ref = _modelscope_snapshot(self.spec.model_id)
        compute = "int8" if self.device != "cpu" else "int8"
        # CTranslate2 只认 device='cuda'（卡号走 device_index），而本服务的 --device
        # 统一解析成 'cuda:0'，直接传会 ValueError: unsupported device cuda:0。
        dev, idx = self.device.partition(":")[0], 0
        if self.device.startswith("cuda") and ":" in self.device:
            try:
                idx = int(self.device.split(":")[1])
            except ValueError:
                idx = 0
        self._m = WhisperModel(model_ref, device=dev, device_index=idx,
                               compute_type=compute,
                               cpu_threads=max(2, (os.cpu_count() or 4) // 2))
        self._loaded = True
        logger.info("faster-whisper 已加载: %s device=%s(idx=%d) compute=%s",
                    self.name, dev, idx, compute)

    def transcribe(self, audio, sample_rate=16000, language=""):
        if not self._loaded:
            raise EngineError("引擎未加载")
        t0 = time.time()
        audio = A.resample(np.asarray(audio, dtype=np.float32), sample_rate, 16000)
        segs, info = self._m.transcribe(
            audio, language=(language or self.language or "zh"), beam_size=1,
            vad_filter=False, condition_on_previous_text=False)
        out, items = [], []
        for s in segs:
            out.append(s.text.strip())
            items.append({"text": s.text.strip(), "start_ms": int(s.start * 1000),
                          "end_ms": int(s.end * 1000)})
        text = "".join(out)
        if self.punc_enabled:
            text = self.punc.apply(text)
        return Transcription(text=text, segments=items,
                             elapsed_ms=int((time.time() - t0) * 1000),
                             engine=self.name, language=getattr(info, "language", ""))


# ---------------------------------------------------------------------------
# Qwen3-ASR：跨 env 子进程 worker
# ---------------------------------------------------------------------------

class QwenWorkerEngine(BaseEngine):
    supports_streaming = False

    def load(self):
        self.guard_vram(force=getattr(self, "force_vram", False))
        py = self.spec.python_bin
        if not py or not os.path.exists(py):
            raise EngineUnavailable(
                f"Qwen3-ASR worker 解释器不可用（{py}）。"
                f"请 pip install qwen-asr，或用 ASR_QWEN_PYTHON 指定另一个解释器")
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qwen_asr_worker.py")
        model_ref = self.model_dir or _modelscope_snapshot(self.spec.model_id)
        self._proc = subprocess.Popen(
            [py, worker, "--model", model_ref, "--dtype", self.spec.dtype,
             "--device", self.device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, text=True, bufsize=1)   # stderr 继承：直接进服务日志，便于排查
        resp = self._call({"cmd": "load"})
        if not resp or not resp.get("ok"):
            raise EngineUnavailable(f"Qwen3-ASR worker 启动失败: {resp}")
        self._loaded = True
        logger.info("Qwen3-ASR worker 已加载: %s (%s)", self.name, resp.get("vram_mb"))

    def _call(self, payload: dict, timeout: int = 300):
        proc = getattr(self, "_proc", None)
        if proc is None or proc.poll() is not None:
            return None
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as exc:
            logger.error("Qwen worker 写入失败: %s", exc)
            return None
        import select
        r, _, _ = select.select([proc.stdout], [], [], timeout)
        if not r:
            logger.error("Qwen worker 超时（%ss）", timeout)
            return None
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception:
            logger.error("Qwen worker 返回非 JSON: %s", line[:200])
            return None

    def transcribe(self, audio, sample_rate=16000, language=""):
        if not self._loaded:
            raise EngineError("引擎未加载")
        t0 = time.time()
        audio = A.resample(np.asarray(audio, dtype=np.float32), sample_rate, 16000)
        pcm = A.float_to_int16(audio)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False,
                                          dir=os.environ.get("ASR_SPOOL_DIR", "/tmp"))
        try:
            A.write_wav(tmp.name, pcm, 16000)
            tmp.close()
            resp = self._call({"cmd": "transcribe", "path": tmp.name,
                               "language": language or self.language})
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        if not resp or not resp.get("ok"):
            raise EngineError(f"Qwen3-ASR 识别失败: {resp}")
        text = resp.get("text", "")
        if self.punc_enabled:
            text = self.punc.apply(text)
        return Transcription(text=text, segments=resp.get("segments", []),
                             elapsed_ms=int((time.time() - t0) * 1000),
                             engine=self.name, language=resp.get("language", ""))

    def unload(self):
        proc = getattr(self, "_proc", None)
        if proc is not None:
            try:
                self._call({"cmd": "unload"}, timeout=10)
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None
        super().unload()


# ---------------------------------------------------------------------------
# 模型下载（modelscope 优先，缺包时交给 funasr 自己下）
# ---------------------------------------------------------------------------

def _modelscope_snapshot(model_id: str) -> str:
    """本地有就用本地，没有就自动下载（走 modelscope，国内快）。

    ASR_MODELS_DIR=/root/models 时优先找 $ASR_MODELS_DIR/<org>--<name> 和
    $ASR_MODELS_DIR/<name>（即 `modelscope download --model X --local_dir /root/models/X`
    的落盘位置），避免重复下载。
    """
    if os.path.isdir(model_id):
        return model_id
    base = os.environ.get("ASR_MODELS_DIR", "").strip()
    if base:
        tail = model_id.split("/")[-1]
        for cand in (os.path.join(base, model_id.replace("/", os.sep)),
                     os.path.join(base, tail),
                     os.path.join(base, model_id.replace("/", "--"))):
            if os.path.isdir(cand):
                logger.info("使用本地模型目录: %s", cand)
                return cand
    try:
        from modelscope import snapshot_download
        return snapshot_download(model_id)
    except Exception as exc:
        logger.warning("modelscope 下载失败（%s），把模型 id 直接交给引擎解析", exc)
        return model_id


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_engine(name: str, device: str = "cpu", model_dir: str = "", language: str = "",
                 punc: Optional[bool] = None, chunk_size=None,
                 force_vram: bool = False) -> BaseEngine:
    name = resolve_engine_name(name)
    spec = ENGINE_SPECS[name]
    if spec.family == "mock":
        eng = MockEngine(spec, device="cpu", punc=punc)
    elif spec.family == "funasr":
        eng = (FunASRStreamingEngine(spec, device, model_dir, language, punc, chunk_size)
               if spec.streaming else FunASROfflineEngine(spec, device, model_dir, language, punc))
    elif spec.family == "whisper":
        eng = WhisperEngine(spec, device, model_dir, language, punc)
    elif spec.family == "qwen":
        eng = QwenWorkerEngine(spec, device, model_dir, language, punc)
    else:
        raise EngineError(f"未知引擎族: {spec.family}")
    # spec 是「流式与否」的唯一事实来源：MockEngine 一个类同时服务 mock 和
    # mock-offline，只靠类属性会把 mock-offline 也标成流式，于是
    # --offline-partials-ms 那条滚动 partial 路径永远测不到。
    eng.supports_streaming = spec.streaming
    eng.force_vram = force_vram
    return eng


def effective_offline_partials_ms(eng: "BaseEngine", requested: int) -> int:
    """非流式引擎的滚动 partial 间隔（0 = 关）。

    requested: -1 = 按引擎自动（默认），0 = 显式关，>0 = 用户指定的间隔。
    非流式引擎默认一个字都不出，换过去却发现「没反应」比多花点算力更糟，
    所以默认自动开；太慢的引擎（spec 里 0）自动时仍然不开。
    """
    if eng is None or eng.supports_streaming:
        return 0
    if requested >= 0:
        return max(0, int(requested))
    return int(eng.spec.offline_partial_ms or 0)


def effective_punc(spec: "EngineSpec", requested: Optional[bool]) -> bool:
    """标点模型（FunASR ct-punc，~1.1GB）到底加不加。三态，跟 offline_partials_ms 一样：

       True   强制加（--punc）
       False  强制不加（--no-punc）
       None   自动：引擎自带标点（Qwen3-ASR 输出本来就带标点）就不加，白省 1.1GB；
              ct-punc 是 FunASR 的模型，非 FunASR 系也挂不上。

    以前这条判断藏在 `punc and spec.family in ("funasr", "mock")` 里，而 punc_enabled
    记的仍是传进来的原始值 —— 于是 /health 对 qwen 报 `punc: true`，实际模型压根没加载，
    排查显存时会被这个假信号带偏。
    """
    if requested is not None:
        return requested
    if spec.self_punctuating:
        return False
    return spec.family in ("funasr", "mock")


def engine_availability(name: str) -> dict:
    """探测引擎在当前 env 是否可用（不加载模型）。"""
    name = resolve_engine_name(name)
    spec = ENGINE_SPECS[name]
    reason = ""
    ok = True
    if spec.family == "funasr":
        try:
            import funasr  # noqa: F401
        except Exception:
            ok, reason = False, "当前解释器未安装 funasr"
    elif spec.family == "whisper":
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            ok, reason = False, "当前解释器未安装 faster-whisper"
    elif spec.family == "qwen":
        if not spec.python_bin or not os.path.exists(spec.python_bin):
            ok, reason = False, f"解释器不存在: {spec.python_bin}"
        elif spec.python_bin == sys.executable:
            import importlib.util
            if importlib.util.find_spec("qwen_asr") is None:
                ok, reason = False, "当前环境未安装 qwen-asr"
    d = spec.as_dict()
    d["available"] = ok
    d["reason"] = reason
    return d

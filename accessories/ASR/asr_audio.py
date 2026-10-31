#coding=utf-8
"""
ASR 音频工具层（无 torch / funasr 依赖，纯 numpy + 标准库，便于单测）。

职责：
1. PCM16 <-> float32 转换（FunASR / Whisper 等引擎统一吃 float32 [-1, 1]）
2. 采样率转换（客户端可能推 8k 电话音频 / 48k 浏览器音频，引擎统一 16k）
3. 能量 / dBFS 计算（供 VAD 使用）
4. 通过 ffmpeg 把 mp3 / wav / m4a / 任意容器解码成 PCM16LE 单声道
5. 简易 wav 读写（标准库 wave，非 PCM16 时自动走 ffmpeg）

约定：本模块里
- "pcm16"  指 np.int16 一维数组（单声道）
- "audio"  指 np.float32 一维数组，取值 [-1, 1]
"""

import io
import os
import shutil
import subprocess
import wave

import numpy as np

# 引擎工作采样率：FunASR paraformer / fsmn-vad / SenseVoice / whisper 都是 16k
ENGINE_SAMPLE_RATE = 16000

INT16_SCALE = 32768.0


# ---------------------------------------------------------------------------
# 格式转换
# ---------------------------------------------------------------------------

def pcm_bytes_to_int16(data: bytes) -> np.ndarray:
    """小端 PCM16 字节流 -> np.int16（奇数字节尾巴直接丢弃，避免半帧）。"""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2")


def int16_to_pcm_bytes(pcm: np.ndarray) -> bytes:
    return np.asarray(pcm, dtype="<i2").tobytes()


def int16_to_float(pcm: np.ndarray) -> np.ndarray:
    """int16 -> float32 [-1, 1]（FunASR 流式接口吃的格式）。"""
    return np.asarray(pcm, dtype=np.float32) / INT16_SCALE


def float_to_int16(audio: np.ndarray) -> np.ndarray:
    a = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return np.round(a * (INT16_SCALE - 1)).astype("<i2")


def ensure_mono(x: np.ndarray) -> np.ndarray:
    """多声道 -> 单声道（取均值）。"""
    arr = np.asarray(x)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    return arr.reshape(-1)


# ---------------------------------------------------------------------------
# 重采样
# ---------------------------------------------------------------------------

def resample(audio: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """采样率转换。优先 scipy.signal.resample_poly（快且质量好），
    其次 librosa，最后退化为线性插值（保证任何环境都能跑）。

    输入/输出都是 float32。
    """
    audio = ensure_mono(np.asarray(audio, dtype=np.float32))
    sr_in, sr_out = int(sr_in), int(sr_out)
    if audio.size == 0 or sr_in == sr_out:
        return audio

    from math import gcd
    g = gcd(sr_in, sr_out)
    up, down = sr_out // g, sr_in // g

    # 数字太大时（例如 44100 -> 16000）resample_poly 的 up/down 仍可接受，
    # 但为稳妥限制单步比例，必要时分两步。
    try:
        from scipy.signal import resample_poly
        if max(up, down) <= 2048:
            return resample_poly(audio, up, down).astype(np.float32)
    except Exception:
        pass

    try:
        import librosa
        return librosa.resample(audio, orig_sr=sr_in, target_sr=sr_out).astype(np.float32)
    except Exception:
        pass

    # 线性插值兜底：质量一般，但只用于"能跑"，不作为默认路径
    n_out = int(round(audio.size * sr_out / float(sr_in)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=audio.size, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, num=n_out, dtype=np.float64)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def pcm16_resample(pcm: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """int16 PCM 的重采样便捷封装。"""
    if int(sr_in) == int(sr_out):
        return np.asarray(pcm, dtype="<i2")
    return float_to_int16(resample(int16_to_float(pcm), sr_in, sr_out))


# ---------------------------------------------------------------------------
# 能量 / dBFS
# ---------------------------------------------------------------------------

def frame_rms_db(pcm: np.ndarray) -> float:
    """一段 PCM16 的 RMS 电平（dBFS）。静音返回 -120。"""
    if pcm is None or len(pcm) == 0:
        return -120.0
    x = np.asarray(pcm, dtype=np.float64) / INT16_SCALE
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 1e-6:
        return -120.0
    return 20.0 * float(np.log10(rms))


def normalize_gain(pcm: np.ndarray, target_db: float = -20.0,
                   max_gain_db: float = 18.0) -> np.ndarray:
    """把整段音频的 RMS 归一到 target_db（电话 8k 录音往往很轻）。
    只放大不超过 max_gain_db，避免把底噪炸出来。"""
    cur = frame_rms_db(pcm)
    if cur <= -95.0:
        return np.asarray(pcm, dtype="<i2")
    gain_db = min(target_db - cur, max_gain_db)
    if abs(gain_db) < 1.0:
        return np.asarray(pcm, dtype="<i2")
    gain = 10.0 ** (gain_db / 20.0)
    x = np.clip(np.asarray(pcm, dtype=np.float32) * gain, -INT16_SCALE, INT16_SCALE - 1)
    return x.astype("<i2")


# ---------------------------------------------------------------------------
# 解码（ffmpeg）与 wav 读写
# ---------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def decode_to_pcm16(source, sample_rate: int = ENGINE_SAMPLE_RATE,
                    channels: int = 1, timeout: int = 120) -> np.ndarray:
    """把任意可解码音频（文件路径 / bytes / base64 str）解码成 PCM16LE 单声道。

    依赖 ffmpeg（本机已安装）。解码失败抛 RuntimeError。
    """
    if isinstance(source, (bytes, bytearray)) and _looks_like_pcm(source):
        return pcm_bytes_to_int16(bytes(source))

    if isinstance(source, (bytes, bytearray)):
        # ffmpeg 需要 seek 或管道，统一走 stdin
        cmd = _ffmpeg_cmd("-", sample_rate, channels)
        data = _run_ffmpeg(cmd, input_bytes=bytes(source), timeout=timeout)
        return pcm_bytes_to_int16(data)

    path = str(source)
    if not os.path.isfile(path):
        raise RuntimeError(f"音频文件不存在: {path}")
    if _is_pcm16_raw(path):
        with open(path, "rb") as f:
            return pcm_bytes_to_int16(f.read())
    data = _run_ffmpeg(_ffmpeg_cmd(path, sample_rate, channels), input_bytes=None, timeout=timeout)
    return pcm_bytes_to_int16(data)


def _ffmpeg_cmd(input_path: str, sample_rate: int = ENGINE_SAMPLE_RATE,
                channels: int = 1) -> list:
    return [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(input_path),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", str(channels),
        "-",
    ]


def _run_ffmpeg(cmd: list, input_bytes=None, timeout: int = 120) -> bytes:
    if not ffmpeg_available():
        raise RuntimeError("未找到 ffmpeg，无法解码压缩音频（可只推 PCM16 裸流）")
    try:
        proc = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg 解码超时（{timeout}s）")
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 解码失败: " + (proc.stderr or b"")[-400:].decode("utf-8", "ignore"))
    return proc.stdout or b""


def _looks_like_pcm(data: bytes) -> bool:
    """粗略判断字节流是否已经是 PCM16（无常见容器魔数）。"""
    if len(data) < 12:
        return True
    head = data[:12]
    magic = (b"RIFF", b"FCCm", b"OggS", b"fLaC", b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xf7\xfd")
    return not any(head.startswith(m) for m in magic)


def _is_pcm16_raw(path: str) -> bool:
    """无扩展名/裸 PCM 判定：不是 wav/mp3/m4a/flac/ogg 就当裸 PCM。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in (".pcm", ".raw", ".s16le", "")


def read_wav(path: str):
    """读 PCM16 wav -> (pcm16 np.ndarray, sample_rate)。非 PCM16 走 ffmpeg。"""
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() == 2 and w.getnchannels() == 1:
                frames = w.readframes(w.getnframes())
                return pcm_bytes_to_int16(frames), w.getframerate()
    except Exception:
        pass
    return decode_to_pcm16(path), ENGINE_SAMPLE_RATE


def write_wav(path: str, pcm: np.ndarray, sample_rate: int = ENGINE_SAMPLE_RATE) -> str:
    """写 PCM16 wav（评测 / 调试用）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(int16_to_pcm_bytes(np.asarray(pcm, dtype="<i2")))
    return path


def synthetic_speech_like(seconds: float = 1.0, sample_rate: int = ENGINE_SAMPLE_RATE,
                          seed: int = 0) -> np.ndarray:
    """生成一段"像语音"的测试音频（调频谐波 + 停顿），用于 warmup / 单测。
    不是真实语音，但足以触发 VAD 与内核预热。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * sample_rate)
    t = np.arange(n, dtype=np.float32) / sample_rate
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        dur = float(rng.uniform(0.35, 0.9))
        seg = min(int(dur * sample_rate), n - pos)
        f0 = float(rng.uniform(120.0, 260.0))
        tt = t[pos:pos + seg]
        mod = 1.0 + 0.35 * np.sin(2 * np.pi * float(rng.uniform(2.0, 5.0)) * tt)
        wave_seg = np.zeros_like(tt)
        for k, amp in enumerate([1.0, 0.5, 0.28, 0.15], start=1):
            wave_seg += amp * np.sin(2 * np.pi * f0 * k * tt)
        env = np.minimum(1.0, np.minimum(tt / 0.02, (tt[-1] - tt) / 0.05 + 1e-6))
        out[pos:pos + seg] = 0.35 * mod * env * wave_seg / 1.93
        pos += seg + int(float(rng.uniform(0.2, 0.5)) * sample_rate)
    out += rng.normal(0.0, 0.0015, size=n).astype(np.float32)
    return float_to_int16(out)

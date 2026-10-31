#coding=utf-8
"""
流式 VAD（语音活动检测）——纯 numpy，无模型依赖，可单测。

为什么自己写一版能量 VAD：
- FunASR 的 fsmn-vad 在 Python 侧的流式接口跨版本不稳定，且 8k 电话音频 / 手机
  推流场景需要能快速调阈值；
- 服务端要的是"分段 + 判停"（endpointing），能量 VAD 加滞回 + 自适应底噪已经
  足够稳，且几乎不占 CPU；
- fsmn-vad 仍可作为离线精修（--vad fsmn）由引擎层调用，二者不冲突。

核心机制：
1. 10ms 帧 RMS 电平（dBFS）
2. 自适应噪声底（快降慢升），阈值 = 底噪 + 启动裕度，且不低于绝对门限
3. 启动/停止双阈值（滞回），避免抖动导致频繁分段
4. pre_roll：把语音起点提前一点，避免吃掉首字
5. min_speech_ms：短于该长度的"语音"直接丢弃（咳嗽、杂音）
6. max_end_silence_ms：判停（说完一句话）
7. max_segment_ms：强制切段，防止长句不判停导致延迟无限增长
"""

from collections import deque

import numpy as np

from asr_audio import frame_rms_db

START = "start"
END = "end"
# 段太短被丢弃时发 CANCEL：客户端需要撤掉已经显示出去的 partial
CANCEL = "cancel"


def _percentile(values, pct: float) -> float:
    """小窗分位数（窗口只有几百个点，直接排序即可）。"""
    a = sorted(values)
    if not a:
        return -120.0
    idx = min(len(a) - 1, max(0, int(round(pct / 100.0 * (len(a) - 1)))))
    return float(a[idx])


class VadEvent:
    __slots__ = ("kind", "start_ms", "end_ms", "reason")

    def __init__(self, kind: str, start_ms: int = 0, end_ms: int = 0, reason: str = ""):
        self.kind = kind
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.reason = reason

    def __repr__(self):  # pragma: no cover - 调试用
        return f"VadEvent({self.kind},{self.start_ms}-{self.end_ms},{self.reason})"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "start_ms": self.start_ms,
                "end_ms": self.end_ms, "reason": self.reason}


class StreamVad:
    """按帧喂 PCM16，返回 VAD 事件列表。

    参数默认值针对中文普通话 + 手机/麦克风场景调过：
    - max_end_silence_ms=800：中文句子间停顿通常 <600ms，800ms 判停兼顾"想一下"
    - max_segment_ms=15000：超过则强制切段（流式引擎会 flush 出 final）
    """

    def __init__(self, sample_rate: int = 16000, frame_ms: int = 10,
                 start_margin_db: float = 7.0, stop_margin_db: float = 4.0,
                 min_level_db: float = -42.0, floor_init_db: float = -48.0,
                 floor_rise_per_sec: float = 12.0, floor_track_ms: int = 1200,
                 min_speech_ms: int = 250, max_end_silence_ms: int = 800,
                 max_segment_ms: int = 15000, pre_roll_ms: int = 200,
                 start_confirm_frames: int = 4, noise_probe_ms: int = 1500):
        self.sample_rate = int(sample_rate)
        self.frame_ms = max(5, int(frame_ms))
        self.frame_len = max(1, int(self.sample_rate * self.frame_ms / 1000))

        self.start_margin_db = float(start_margin_db)
        self.stop_margin_db = min(float(stop_margin_db), self.start_margin_db)
        self.min_level_db = float(min_level_db)
        self.floor = float(floor_init_db)
        self.floor_rise_per_frame = float(floor_rise_per_sec) * self.frame_ms / 1000.0
        self.floor_track_frames = max(10, int(floor_track_ms / self.frame_ms))
        self.floor_update_every = max(1, self.floor_track_frames // 10)

        self.min_speech_ms = int(min_speech_ms)
        self.max_end_silence_ms = int(max_end_silence_ms)
        self.max_segment_ms = int(max_segment_ms)
        self.pre_roll_ms = int(pre_roll_ms)

        self.start_confirm_frames = max(1, int(start_confirm_frames))
        self.noise_probe_ms = max(0, int(noise_probe_ms))
        self.end_silence_frames = max(1, int(self.max_end_silence_ms / self.frame_ms))
        self.max_segment_frames = max(1, int(self.max_segment_ms / self.frame_ms))

        self.reset()

    # -- 状态 -------------------------------------------------------------

    def reset(self):
        self._carry = np.zeros(0, dtype="<i2")
        self._consumed_samples = 0          # 已判定（成帧）的样本数
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._silence_frames_in_seg = 0
        self._pending_start_frames = 0
        self._seg_start_ms = 0
        self._last_speech_end_ms = 0
        self._preroll = deque(maxlen=max(1, int(self.pre_roll_ms / self.frame_ms)))
        self._noise_win = deque(maxlen=self.floor_track_frames * 2)
        self._frames_since_floor_update = 0
        self._last_db = -120.0
        self.stats = {"frames": 0, "speech_frames": 0, "segments": 0, "rejected_short": 0}

    def in_speech(self) -> bool:
        return self._in_speech

    def current_segment_ms(self) -> int:
        if not self._in_speech:
            return 0
        return int(self._consumed_samples * 1000 / self.sample_rate) - self._seg_start_ms

    def threshold_db(self) -> float:
        base = self.floor + (self.start_margin_db if not self._in_speech else self.stop_margin_db)
        return max(base, self.min_level_db)

    # -- 主入口 -----------------------------------------------------------

    def feed(self, pcm) -> list:
        """喂 PCM16（任意长度），返回事件列表（可能为空 / 多个）。"""
        if pcm is None or len(pcm) == 0:
            return []
        buf = np.concatenate((self._carry, np.asarray(pcm, dtype="<i2")))
        n_frames = len(buf) // self.frame_len
        if n_frames == 0:
            self._carry = buf
            return []
        events = []
        for i in range(n_frames):
            frame = buf[i * self.frame_len:(i + 1) * self.frame_len]
            events.extend(self._one_frame(frame))
        self._carry = buf[n_frames * self.frame_len:]
        return events

    def _update_floor(self, db: float):
        """噪声底跟踪（只统计非语音帧，避免把人声学成底噪）。

        - 更安静：立刻跟下去（快降）
        - 更吵：窗口未满时按速率上限缓慢抬升，避免关门声之类的瞬态把阈值顶高
        - 窗口满（默认 1.2s 非语音帧）：直接取分位数，让稳态噪声（风扇、电流声、
          车内路噪）在 1~2s 内被自适应压掉；底噪上限 -25dBFS，防止把轻声说话也屏蔽掉
        """
        self._noise_win.append(db)
        self._frames_since_floor_update += 1
        if db < self.floor:
            self.floor = max(-70.0, db)
            self._frames_since_floor_update = 0
            return
        if self._frames_since_floor_update < self.floor_update_every:
            return
        n = len(self._noise_win)
        if n < self.floor_track_frames:
            cap = self.floor + self.floor_rise_per_frame * self._frames_since_floor_update
            target = min(db, cap)
        else:
            target = _percentile(self._noise_win, 40.0)
        self.floor = max(-70.0, min(-18.0, target))
        self._frames_since_floor_update = 0

    def _one_frame(self, frame) -> list:
        self.stats["frames"] += 1
        db = frame_rms_db(frame)
        self._last_db = db
        frame_start_ms = int(self._consumed_samples * 1000 / self.sample_rate)
        frame_end_ms = int((self._consumed_samples + len(frame)) * 1000 / self.sample_rate)
        self._consumed_samples += len(frame)

        events = []
        thr = self.threshold_db()
        voiced = db >= thr

        if not self._in_speech:
            self._update_floor(db)

            if voiced:
                self._pending_start_frames += 1
                self._preroll.append(frame_start_ms)
                if self._pending_start_frames >= self.start_confirm_frames:
                    # 起点回退到本次连续语音的第一帧，再往前 pre_roll
                    first_ms = self._preroll[0] if self._preroll else frame_start_ms
                    self._seg_start_ms = max(0, first_ms - self.pre_roll_ms)
                    self._in_speech = True
                    self._speech_frames = self._pending_start_frames
                    self._silence_frames = 0
                    self._silence_frames_in_seg = 0
                    self._last_speech_end_ms = frame_end_ms
                    self.stats["speech_frames"] += self._pending_start_frames
                    self.stats["segments"] += 1
                    events.append(VadEvent(START, self._seg_start_ms, 0, "vad"))
                self._pending_start_frames = min(self._pending_start_frames,
                                                 self.start_confirm_frames * 4)
            else:
                self._pending_start_frames = 0
                self._preroll.clear()
            return events

        # 已在语音段内
        if voiced:
            self._speech_frames += 1
            self._silence_frames = 0
            self._last_speech_end_ms = frame_end_ms
            self.stats["speech_frames"] += 1
        else:
            self._silence_frames += 1
            self._silence_frames_in_seg += 1

        seg_ms = self._last_speech_end_ms - self._seg_start_ms

        # 整段连一个低于阈值的帧都没有 = 完全平稳的信号，多半是稳态噪声（风扇/路噪/电流声）。
        # 这时允许底噪在段内继续跟踪并把它压掉；真人说话一定有能量谷值，不会走到这里。
        if self._silence_frames_in_seg == 0 and seg_ms >= self.noise_probe_ms:
            self._update_floor(db)

        if self._silence_frames >= self.end_silence_frames:
            events.extend(self._close_segment("vad"))
        elif seg_ms >= self.max_segment_ms:
            # 强制切段：先出 final，若仍在说话则立即开新段
            events.extend(self._close_segment("max_len"))
            if voiced:
                self._seg_start_ms = max(0, frame_start_ms - self.pre_roll_ms)
                self._in_speech = True
                self._speech_frames = 1
                self._silence_frames = 0
                self._silence_frames_in_seg = 0
                self._last_speech_end_ms = frame_end_ms
                self.stats["segments"] += 1
                events.append(VadEvent(START, self._seg_start_ms, 0, "max_len"))
        return events

    def _close_segment(self, reason: str) -> list:
        start, end = self._seg_start_ms, self._last_speech_end_ms
        self._in_speech = False
        self._pending_start_frames = 0
        self._silence_frames = 0
        self._preroll.clear()
        self._frames_since_floor_update = 0     # 底噪窗口跨段保留，避免稳态噪声反复触发
        if end - start < self.min_speech_ms:
            # 咳嗽/关门声：START 已经发出去了，必须补一个 CANCEL，
            # 否则客户端会留着一条永远等不到 final 的 partial
            self.stats["rejected_short"] += 1
            return [VadEvent(CANCEL, start, end, "short")]
        return [VadEvent(END, start, end, reason)]

    def force_end(self) -> list:
        """客户端 flush / 断开时调用：把当前段收尾。"""
        if not self._in_speech:
            return []
        return self._close_segment("flush")

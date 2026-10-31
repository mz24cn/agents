#coding=utf-8
"""
ASR 客户端 demo：文件转写 / 麦克风实时推流 / 合成音频协议自检。

    # 1) 文件（wav/mp3/m4a 自动解码，默认按实时节奏推送）
    python asr_client.py --file /root/test.mp3
    python asr_client.py --file a.wav --no-realtime          # 尽快推（压测）

    # 2) 麦克风（需 pip install sounddevice）
    python asr_client.py --mic --frame-ms 40
    python asr_client.py --mic --list-devices

    # 3) 无麦克风也能验证链路（合成音频 + 完整协议）
    python asr_client.py --demo

    # 4) 手机/本地设备推 8k 电话音频
    python asr_client.py --file phone.wav --sample-rate 8000 --frame-ms 100

服务端地址：--url ws://127.0.0.1:7996/asr/ws
"""

import argparse
import asyncio
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asr_audio as A                                       # noqa: E402

try:
    from websockets import connect as ws_connect
except ImportError:
    print("需要 websockets: conda run -n asr pip install websockets", file=sys.stderr)
    raise

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


class Printer:
    """把 partial 打在同行（原地刷新），final 换行落定，观感接近字幕。"""

    def __init__(self, quiet_partial=False):
        self.cur = ""
        self.quiet = quiet_partial
        self.lines = []
        self.events = []

    def partial(self, text, seg_id):
        if self.quiet:
            return
        if text != self.cur:
            self.cur = text
            sys.stdout.write(f"\r\033[K{DIM}[{seg_id}] {YELLOW}{text}{RESET}")
            sys.stdout.flush()

    def final(self, msg):
        if self.cur:
            sys.stdout.write("\r\033[K")
        self.cur = ""
        seg_id = msg.get("seg_id")
        took = msg.get("took_ms")
        rtf = msg.get("rtf")
        self.lines.append(msg.get("text", ""))
        self.events.append(msg)
        print(f"[{seg_id}] {GREEN}{msg.get('text')}{RESET} "
              f"{DIM}({msg.get('start_ms')}~{msg.get('end_ms')}ms "
              f"reason={msg.get('reason')} took={took}ms rtf={rtf}){RESET}")

    def cancel(self, seg_id=None):
        """服务端判定这段是杂音：把还在刷新的 partial 擦掉。"""
        if self.cur:
            sys.stdout.write("\r\033[K")
        self.cur = ""

    def done(self):
        if self.cur:
            sys.stdout.write("\r\033[K")
            self.cur = ""


async def run(args) -> int:
    url = args.url
    if args.token:
        url += ("&" if "?" in url else "?") + f"token={args.token}"
    if args.session_id:
        url += ("&" if "?" in url else "?") + f"session_id={args.session_id}"

    pr = Printer(quiet_partial=args.quiet_partial)
    t0 = time.time()
    first_partial_at = None
    finals = 0
    ready = asyncio.Event()
    ack = {"ev": asyncio.Event()}

    async with ws_connect(url, max_size=8 << 20, ping_interval=20) as ws:
        # ---- 接收任务
        async def recv():
            nonlocal first_partial_at, finals
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    continue
                d = json.loads(msg)
                t = d.get("type")
                if t == "ready":
                    print(f"{DIM}ready: engine={d.get('engine')} device={d.get('device')} "
                          f"streaming={d.get('streaming')} warmup={d.get('warmup')} "
                          f"vad={d.get('vad')}{RESET}")
                    ready.set()
                elif t == "flush_ack":
                    ack["ev"].set()
                elif t == "partial":
                    if first_partial_at is None:
                        first_partial_at = time.time() - t0
                    pr.partial(d.get("text", ""), d.get("seg_id"))
                elif t == "final":
                    finals += 1
                    pr.final(d)
                elif t == "segment_start":
                    pass
                elif t == "segment_cancel":
                    pr.cancel(d.get("seg_id"))
                elif t == "stats":
                    print(f"{DIM}stats: {json.dumps(d, ensure_ascii=False)}{RESET}")
                elif t in ("error",):
                    print(f"{RED}error: {d}{RESET}")
                elif t in ("replay",):
                    pr.final(dict(d, type="final"))
                elif args.verbose:
                    print(f"{DIM}{json.dumps(d, ensure_ascii=False)}{RESET}")

        rt = asyncio.ensure_future(recv())

        # ---- 发送配置
        cfg = {"type": "config", "sample_rate": args.sample_rate, "language": args.language}
        if args.vad_end_silence is not None:
            cfg["vad"] = {"max_end_silence_ms": args.vad_end_silence,
                          "max_segment_ms": args.vad_max_segment}
        await ws.send(json.dumps(cfg, ensure_ascii=False))

        # ---- 取音频
        if args.mic:
            pcm, sr = None, args.sample_rate
            await send_microphone(ws, args, ready)
        else:
            if args.demo:
                pcm, sr = A.synthetic_speech_like(args.demo_seconds), 16000
                if args.sample_rate != 16000:
                    pcm = A.pcm16_resample(pcm, 16000, args.sample_rate)
                    sr = args.sample_rate
            else:
                if not args.file:
                    print("需要 --file / --mic / --demo", file=sys.stderr)
                    return 2
                pcm = A.decode_to_pcm16(args.file, args.sample_rate)
                sr = args.sample_rate
            await ready.wait()
            await send_pcm(ws, pcm, sr, args)

        # ---- 收尾：flush 后等服务端把积压音频处理完（flush_ack），不能只按固定秒数等
        await ws.send(json.dumps({"type": "flush"}))
        try:
            await asyncio.wait_for(ack["ev"].wait(), timeout=args.max_wait)
        except asyncio.TimeoutError:
            print(f"{YELLOW}等待 flush_ack 超时（{args.max_wait}s）{RESET}")
        await ws.send(json.dumps({"type": "stats"}))
        await asyncio.sleep(0.4)
        rt.cancel()

    pr.done()
    dur = time.time() - t0
    audio_s = (len(pcm) / float(sr)) if args.mic is False and pcm is not None else dur
    print(f"\n{DIM}--- 连接 {dur:.1f}s | final 段数 {finals} | "
          f"首个 partial 延迟 {('%.2fs' % first_partial_at) if first_partial_at else 'N/A'}{RESET}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("".join(pr.lines) + "\n")
        print(f"已写入 {args.out}")
    return 0 if finals or args.demo else 1


async def send_pcm(ws, pcm: np.ndarray, sr: int, args):
    frame = max(1, int(sr * args.frame_ms / 1000))
    total = len(pcm)
    sent_ms = 0
    t_start = time.time()
    for i in range(0, total, frame):
        chunk = pcm[i:i + frame]
        await ws.send(chunk.tobytes())
        sent_ms += int(len(chunk) * 1000 / sr)
        if not args.no_realtime:
            # 按真实时间节奏推流，才能测出 partial 延迟与判停行为
            ahead = sent_ms / 1000.0 - (time.time() - t_start)
            if ahead > 0:
                await asyncio.sleep(min(ahead, 0.5))
        if args.verbose and i % (frame * 25) == 0:
            print(f"{DIM}sent {sent_ms/1000:.1f}s / {total/sr:.1f}s{RESET}")
    if args.no_realtime:
        print(f"{DIM}已推完 {total/sr:.1f}s 音频（非实时）{RESET}")


async def send_microphone(ws, args, ready):
    try:
        import sounddevice as sd
    except ImportError:
        print("麦克风模式需要: conda run -n asr pip install sounddevice", file=sys.stderr)
        raise SystemExit(2)

    sr = args.sample_rate
    frame = max(1, int(sr * args.frame_ms / 1000))
    if args.list_devices:
        print(sd.query_devices())
        raise SystemExit(0)

    q = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def callback(indata, frames, t, status):
        if status:
            print(f"{YELLOW}mic status: {status}{RESET}", file=sys.stderr)
        loop.call_soon_threadsafe(q.put_nowait, indata[:, 0].copy())

    await ready.wait()
    print(f"{DIM}对着麦克风说话（{sr}Hz, {args.frame_ms}ms/帧, Ctrl-C 结束）{RESET}")
    with sd.InputStream(samplerate=sr, channels=1, dtype="int16",
                        blocksize=frame, device=args.device_index, callback=callback):
        while True:
            data = await q.get()
            await ws.send(data.tobytes())


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ASR 流式客户端 demo")
    p.add_argument("--url", default="ws://127.0.0.1:7996/asr/ws")
    p.add_argument("--file", default="", help="wav/mp3/m4a/flac/裸 pcm")
    p.add_argument("--mic", action="store_true")
    p.add_argument("--demo", action="store_true", help="用合成音频自检协议")
    p.add_argument("--demo-seconds", type=float, default=6.0)
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument("--frame-ms", type=int, default=40)
    p.add_argument("--language", default="zh")
    p.add_argument("--token", default="")
    p.add_argument("--session-id", default="")
    p.add_argument("--no-realtime", action="store_true", help="尽快推流（压测/离线加速）")
    p.add_argument("--tail-wait", type=float, default=1.5, help="flush_ack 之后再等几秒")
    p.add_argument("--max-wait", type=float, default=120.0, help="等待 flush_ack 的上限秒数")
    p.add_argument("--vad-end-silence", type=int, default=None)
    p.add_argument("--vad-max-segment", type=int, default=None)
    p.add_argument("--device-index", type=int, default=None, help="麦克风设备序号")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--out", default="", help="把 final 文本写入文件")
    p.add_argument("--quiet-partial", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

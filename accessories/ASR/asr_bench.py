#coding=utf-8
"""ASR 流式延迟/RTF 基准：按实时节奏推 wav，记录首个 partial 延迟、final 段尾延迟、
服务端 took_ms/rtf，并抓 /health 的显存快照。

    python asr_bench.py --file speech.wav --rounds 3 --tag paraformer-streaming
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.request

ASR_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ASR_DIR)

import asr_audio as A                                                  # noqa: E402
from websockets import connect as ws_connect                          # noqa: E402


def health(url_http):
    try:
        with urllib.request.urlopen(url_http, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as exc:                                          # noqa: BLE001
        return {"error": str(exc)}


async def one_round(args, pcm, sr, idx):
    """推一轮音频，返回该轮的指标。"""
    url = args.url
    if args.token:
        url += ("&" if "?" in url else "?") + f"token={args.token}"

    ev = []                       # 收到的事件（带单调时钟）
    t0 = {"send": None}
    ack = {"ev": asyncio.Event()}
    rdy = {"ev": asyncio.Event()}

    async def recv():
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            d = json.loads(msg)
            d["_wall"] = time.monotonic()
            ev.append(d)
            if d.get("type") == "flush_ack":
                ack["ev"].set()
            elif d.get("type") in ("ready", "config_ack"):
                rdy["ev"].set()

    async with ws_connect(url, max_size=8 << 20, ping_interval=20) as ws:
        rt = asyncio.ensure_future(recv())
        cfg = {"type": "config", "sample_rate": sr, "language": args.language}
        if args.max_segment_ms:
            # 用 config 帧覆盖，服务端不用为「长段」那一档重启
            cfg["vad"] = {"max_segment_ms": args.max_segment_ms}
        await ws.send(json.dumps(cfg, ensure_ascii=False))
        try:
            await asyncio.wait_for(rdy["ev"].wait(), timeout=30)
        except asyncio.TimeoutError:
            errs = [d for d in ev if d.get("type") == "error"]
            raise RuntimeError(f"服务端没有 ready/config_ack；事件={errs or ev}") from None

        frame = max(1, int(sr * args.frame_ms / 1000))
        total = len(pcm)
        sent_ms = 0
        t_start = time.monotonic()
        t0["send"] = t_start
        for i in range(0, total, frame):
            chunk = pcm[i:i + frame]
            await ws.send(chunk.tobytes())
            sent_ms += int(len(chunk) * 1000 / sr)
            ahead = sent_ms / 1000.0 - (time.monotonic() - t_start)
            if ahead > 0:
                await asyncio.sleep(min(ahead, 0.5))
        # 推完时的「音频位置 ↔ 墙钟」锚点：用它反推比直接用 t_start 准，
        # 因为发送循环偶尔会慢半拍，误差会一路累积到最后一个 partial。
        t0["send"] = time.monotonic() - sent_ms / 1000.0

        await ws.send(json.dumps({"type": "flush"}))
        try:
            await asyncio.wait_for(ack["ev"].wait(), timeout=args.max_wait)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.3)
        rt.cancel()

    base = t0["send"]

    def wall_at_audio(ms):
        """音频位置 ms（毫秒）对应的墙钟时刻：实时推流下 = t0 + ms/1000。"""
        return base + ms / 1000.0

    out = {"round": idx, "events": len(ev), "partials": 0,
           "first_partial_ms": None, "first_partial_audio_ms": None,
           "finals": []}

    seg_start = {}
    for d in ev:
        t = d.get("type")
        if t == "segment_start":
            seg_start[d.get("seg_id")] = d.get("start_ms")
        elif t == "partial":
            out["partials"] += 1
            if out["first_partial_ms"] is None:
                s = seg_start.get(d.get("seg_id"))
                if s is None:
                    s = d.get("start_ms") or 0
                out["first_partial_ms"] = round((d["_wall"] - wall_at_audio(s)) * 1000)
                out["first_partial_audio_ms"] = (d.get("end_ms") or 0) - (s or 0)
        elif t == "final":
            end_ms = d.get("end_ms") or 0
            # end_ms 是「语音结束」的位置；VAD 还要再等 max_end_silence 才判停，
            # 所以 e2e_tail 才是「说完到 final 落地」的用户体感。
            out["finals"].append({
                "seg_id": d.get("seg_id"),
                "start_ms": d.get("start_ms"), "end_ms": end_ms,
                "reason": d.get("reason"),
                "took_ms": d.get("took_ms"), "rtf": d.get("rtf"),
                "tail_ms": round((d["_wall"] - wall_at_audio(end_ms)) * 1000),
                "e2e_tail_ms": round((d["_wall"] - wall_at_audio(
                    end_ms + args.end_silence_ms)) * 1000),
                "spk": d.get("spk"), "sim": d.get("sim"), "spk_ms": d.get("spk_ms"),
                "text": d.get("text"),
            })
    return out


async def run(args):
    pcm = A.decode_to_pcm16(args.file, args.sample_rate)
    dur_s = len(pcm) / float(args.sample_rate)
    print(f"音频 {args.file}: {dur_s:.2f}s @{args.sample_rate}Hz")

    h = health(args.http)
    print(f"health: engine={h.get('engine')} refiner={h.get('refiner')} punc={h.get('punc')} "
          f"offline_partials_ms={h.get('offline_partials_ms')} mem={h.get('mem')}")
    if not args.end_silence_ms:
        # 0 = 跟服务端一致，从 /health 的 vad 里取，免得 e2e 段尾算歪
        args.end_silence_ms = (h.get("vad") or {}).get("max_end_silence_ms", 800)
    print(f"e2e 段尾按服务端 max_end_silence_ms={args.end_silence_ms} 折算")

    res = []
    for i in range(args.rounds):
        r = await one_round(args, pcm, args.sample_rate, i)
        res.append(r)
        fp = r["first_partial_ms"]
        print(f"  round{i}: partials={r['partials']} 首个partial={fp}ms "
              f"finals={len(r['finals'])} "
              + " | ".join(f"tail={f['tail_ms']}ms took={f['took_ms']}ms rtf={f['rtf']}"
                           f" len={f['end_ms']-f['start_ms']}ms" for f in r["finals"]))
        if i + 1 < args.rounds:
            await asyncio.sleep(args.gap)

    h2 = health(args.http)
    tails = [f["tail_ms"] for r in res for f in r["finals"] if f["tail_ms"] is not None]
    e2e = [f["e2e_tail_ms"] for r in res for f in r["finals"]
           if f.get("e2e_tail_ms") is not None]
    tooks = [f["took_ms"] for r in res for f in r["finals"] if f["took_ms"] is not None]
    rtfs = [f["rtf"] for r in res for f in r["finals"] if f["rtf"] is not None]
    seglens = [(f["end_ms"] - f["start_ms"]) for r in res for f in r["finals"]]
    fps = [r["first_partial_ms"] for r in res if r["first_partial_ms"] is not None]
    texts = [f["text"] for r in res for f in r["finals"]]

    def stat(xs):
        if not xs:
            return "n/a"
        f = (lambda v: f"{v:.4f}") if max(abs(x) for x in xs) < 10 else (lambda v: f"{v:.0f}")
        return (f"min={f(min(xs))} med={f(statistics.median(xs))} max={f(max(xs))}"
                f" mean={f(statistics.mean(xs))}")

    summary = {
        "tag": args.tag, "file": args.file, "audio_s": round(dur_s, 2),
        "rounds": args.rounds,
        "end_silence_ms": args.end_silence_ms,
        "max_segment_ms": args.max_segment_ms or None,
        "first_partial_ms": stat(fps),
        "tail_ms": stat(tails),
        "e2e_tail_ms": stat(e2e),
        "took_ms": stat(tooks),
        "rtf": stat(rtfs),
        "seg_len_ms": stat(seglens),
        "finals": len(tails),
        "health_mem_after": h2.get("mem"),
        "health_engine": h2.get("engine"), "health_refiner": h2.get("refiner"),
        "health_punc": h2.get("punc"),
        "health_offline_partials_ms": h2.get("offline_partials_ms"),
        "health_spk": h2.get("spk"),
        "counters": h2.get("counters"),
        "rounds_detail": res,
        "sample_text": texts[0] if texts else "",
    }
    print("\n== summary ==")
    print(json.dumps({k: v for k, v in summary.items() if k != "rounds_detail"},
                     ensure_ascii=False, indent=2))
    print("文本样例:", summary["sample_text"])
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print("已写入", args.json)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7996/asr/ws")
    p.add_argument("--http", default="http://127.0.0.1:7996/health")
    p.add_argument("--file", required=True)
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument("--frame-ms", type=int, default=40)
    p.add_argument("--language", default="zh")
    p.add_argument("--token", default="")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--gap", type=float, default=2.0)
    p.add_argument("--max-wait", type=float, default=120.0)
    p.add_argument("--tag", default="")
    p.add_argument("--end-silence-ms", type=int, default=0,
                   help="服务端 max_end_silence_ms，用于折算 e2e 段尾延迟；0=从 /health 自动取")
    p.add_argument("--max-segment-ms", type=int, default=0,
                   help=">0 时用 config 帧覆盖服务端 max_segment_ms（跑「长段」那一档）")
    p.add_argument("--json", default="")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))

#coding=utf-8
"""
Qwen3-ASR 子进程 worker（JSON-Lines 服务端）

为什么单独起进程：qwen-asr 依赖 transformers==4.57 + 一整套重依赖，和 asr 主环境
（torch 2.4.1 + funasr + transformers 4.44）不兼容；放子进程里跑，主服务不被污染，
显存也能随进程退出干净释放。

协议（stdin 收 JSON 行，stdout 回 JSON 行）：
    {"cmd":"load"}                                   -> {"ok":true,"vram_mb":...,"languages":[...]}
    {"cmd":"transcribe","path":"x.wav","language":"zh"} -> {"ok":true,"text":"...","language":"Chinese"}
    {"cmd":"unload"}                                 -> {"ok":true} 然后退出

注意：Qwen3-ASR 的真流式（init_streaming_state/streaming_transcribe）**只支持 vLLM
后端**；用不了 vLLM 时走 transformers 后端 + 服务端 VAD 分段（准流式），dtype 用 fp16
（老显卡没有 bf16，代码里按 compute capability 自动降级）。

用法（由 ASR_server.py 自动拉起，一般不用手动跑）：
    python qwen_asr_worker.py --model <模型快照目录> --dtype float16 --device cuda:0
"""

import argparse
import gc
import json
import sys

LANG_MAP = {
    "zh": "Chinese", "cn": "Chinese", "chinese": "Chinese", "zh-cn": "Chinese",
    "en": "English", "english": "English",
    "yue": "Cantonese", "cantonese": "Cantonese",
    "ja": "Japanese", "ko": "Korean", "fr": "French", "de": "German",
    "es": "Spanish", "ru": "Russian", "th": "Thai", "vi": "Vietnamese",
    "it": "Italian", "pt": "Portuguese", "ar": "Arabic", "hi": "Hindi",
}


def map_language(lang: str):
    if not lang:
        return None
    key = str(lang).strip().lower()
    if key in ("", "auto", "none"):
        return None
    return LANG_MAP.get(key, str(lang).strip().capitalize())


def vram_mb():
    try:
        import torch
        if torch.cuda.is_available():
            return int(torch.cuda.memory_allocated() / 2 ** 20)
    except Exception:
        pass
    return None


def _stub_broken_deps():
    """qwen_asr 的强制对齐模块在 import 时会 `import nagisa`（日文分词的 C 扩展）。
    它的预编译轮子在无 AVX2 的 CPU 上会直接 SIGILL 把进程干掉，而本机不需要时间戳对齐。
    这里先在**子进程**里探测（本进程探测会被信号打死），不可用就用占位模块顶掉它。
    """
    if "nagisa" in sys.modules:
        return
    import subprocess
    try:
        rc = subprocess.run([sys.executable, "-c", "import nagisa"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=120).returncode
    except Exception:
        rc = 1
    if rc == 0:
        return
    import types

    def _unavailable(*_a, **_k):
        raise RuntimeError("nagisa 在本机不可用（CPU 缺 AVX2），Qwen3-ASR 的时间戳对齐已禁用")

    stub = types.ModuleType("nagisa")
    stub.tagging = _unavailable
    sys.modules["nagisa"] = stub


class Worker:
    def __init__(self, model: str, dtype: str, device: str):
        self.model, self.dtype, self.device = model, dtype, device
        self.m = None

    def load(self):
        import torch
        _stub_broken_deps()
        from qwen_asr import Qwen3ASRModel
        dtype_obj = getattr(torch, self.dtype, torch.float16)
        if dtype_obj is getattr(torch, "bfloat16", None) and torch.cuda.is_available():
            major, _ = torch.cuda.get_device_capability(0)
            if major < 8:                       # Pascal/Turing 没有 bf16
                dtype_obj = torch.float16
        kw = dict(max_inference_batch_size=1, max_new_tokens=512)
        try:
            self.m = Qwen3ASRModel.from_pretrained(
                self.model, dtype=dtype_obj, device_map=self.device, **kw)
        except TypeError:                        # 旧版 transformers 用 torch_dtype
            self.m = Qwen3ASRModel.from_pretrained(
                self.model, torch_dtype=dtype_obj, device_map=self.device, **kw)
        langs = []
        try:
            langs = self.m.get_supported_languages()
        except Exception:
            pass
        return {"vram_mb": vram_mb(), "languages": langs[:8]}

    def transcribe(self, req: dict) -> dict:
        if self.m is None:
            raise RuntimeError("模型未加载")
        path = req.get("path")
        if not path:
            raise RuntimeError("缺少 path")
        lang = map_language(req.get("language", ""))
        res = self.m.transcribe(audio=path, language=lang)
        item = res[0] if isinstance(res, (list, tuple)) and res else res
        text = getattr(item, "text", "") or ""
        language = getattr(item, "language", "") or ""
        return {"text": text.strip(), "language": language, "segments": []}

    def unload(self):
        self.m = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Qwen3-ASR worker (JSON-Lines over stdio)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--dtype", default="float16")
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()

    w = Worker(args.model, args.dtype, args.device)

    def reply(obj: dict):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            reply({"ok": False, "error": f"bad json: {exc}"})
            continue
        cmd = (req.get("cmd") or "").lower()
        try:
            if cmd == "load":
                info = w.load()
                reply({"ok": True, **info})
            elif cmd == "transcribe":
                reply({"ok": True, **w.transcribe(req)})
            elif cmd == "unload":
                w.unload()
                reply({"ok": True})
                break
            elif cmd == "ping":
                reply({"ok": True, "loaded": w.m is not None})
            else:
                reply({"ok": False, "error": f"unknown cmd {cmd}"})
        except Exception as exc:
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

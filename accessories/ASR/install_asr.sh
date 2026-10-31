#!/usr/bin/env bash
# ASR 依赖安装：假定当前 shell 已经激活了某个虚拟环境，直接 pip 装进当前环境。
# 所有引擎共用同一个环境。
#
# 用法:
#   bash install_asr.sh                      # 装全部引擎
#   bash install_asr.sh --no-qwen            # 跳过 qwen-asr（它会把 transformers 钉到 4.57.6）
#   bash install_asr.sh --no-whisper         # 跳过 faster-whisper
#   bash install_asr.sh --torch-cuda cu124   # 指定 torch 的 CUDA 构建（Pascal/sm_61 需 <=cu126）
#   PIP_INDEX=https://xxx pip bash install_asr.sh
set -euo pipefail

SKIP_QWEN=0
SKIP_WHISPER=0
TORCH_CUDA=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-qwen)     SKIP_QWEN=1 ;;
        --no-whisper)  SKIP_WHISPER=1 ;;
        --torch-cuda)  TORCH_CUDA="${2:-}"; shift ;;
        -h|--help)     sed -n '2,11p' "$0"; exit 0 ;;
        *)             echo "未知参数: $1"; exit 1 ;;
    esac
    shift
done

PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

echo "== python: $(command -v python) ($(python -V 2>&1))"
python -m pip install -U pip

# ---- torch ------------------------------------------------------------------
# 默认走 PyPI 的默认轮子（CUDA 12.x）。需要指定 CUDA 构建时用 --torch-cuda。
if [[ -n "$TORCH_CUDA" ]]; then
    python -m pip install torch torchaudio --index-url "https://download.pytorch.org/whl/$TORCH_CUDA"
else
    python -m pip install -i "$PIP_INDEX" torch torchaudio
fi

# ---- FunASR（paraformer 流式/离线、SenseVoice、VAD、ct-punc）------------------
# WS 与 HTTP 同端口：ASGI 服务用 uvicorn + starlette
python -m pip install -i "$PIP_INDEX" funasr modelscope librosa soundfile pynvml \
    "websockets>=12" "uvicorn>=0.30" "starlette>=0.37"

# ---- faster-whisper 后端 ------------------------------------------------------
if [[ $SKIP_WHISPER -eq 0 ]]; then
    python -m pip install -i "$PIP_INDEX" faster-whisper || \
        echo "!! faster-whisper 安装失败，whisper 引擎将不可用（其余引擎不受影响）"
fi

# ---- Qwen3-ASR 后端 -----------------------------------------------------------
# 注意：qwen-asr 依赖 transformers==4.57.6，装完会覆盖前面的 transformers 版本。
# FunASR 的 paraformer/SenseVoice/punc 不依赖 transformers，混用没问题；
# 如果介意，用 --no-qwen 跳过。
if [[ $SKIP_QWEN -eq 0 ]]; then
    python -m pip install -i "$PIP_INDEX" qwen-asr || \
        echo "!! qwen-asr 安装失败，qwen3-asr-* 引擎将不可用（其余引擎不受影响）"
fi

echo
echo "== 自检：各引擎可用性"
python ASR_server.py --print-engines

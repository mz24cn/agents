# 流式语音识别（ASR）服务

对着麦克风说中文，屏幕上边说边出字；停一下自动断句、补标点，还能标出这句话是谁说的。
网页聊天里的语音输入、命令行客户端、`POST /transcribe` 整文件转写，用的都是这一个服务。

一个进程、**一个端口**（默认 `7996`）：WebSocket 推音频流，HTTP 做控制面和健康检查。
模型常驻显存，多路并发，引擎可以热切换。

---

## 能力

| | |
|---|---|
| 实时出字 | 说的时候就有 `partial`（当前段累积文本），停一下出 `final` |
| 自动断句 | 能量 VAD：静音 `--max-end-silence-ms`（默认 800ms）收尾；单段超过 `--max-segment-ms`（默认 15s）强制切段 |
| 标点 | 引擎自带（Qwen3-ASR）或 ct-punc 补标点，按引擎自动选 |
| 说话人 | 可选，final 带 `S1/S2…` 标签（`--spk campplus`） |
| 并发 | `--max-sessions`（默认 1，建议 ≤4），每路独立的流式状态 |
| 抗抖动 | 断线重连用 `last_seg_id` 补发漏掉的 final；短于 `--min-speech-ms` 的杂音丢弃 |
| 采样率 | 客户端任意采样率（浏览器常见 48kHz）；浏览器端和服务端都带抗混叠重采样到 16kHz |
| 整文件转写 | `POST /transcribe`，wav / mp3 / 裸 pcm 自动识别 |
| 热切引擎 | `POST /engine {"name":"..."}`，不用重启；显存不够会明确拒绝 |

实测：GTX 1080 Ti 11GB ↔ RTX 5060 8GB：

两台机器各跑一遍 `asr_bench.py`（40ms/帧实时推流，`--max-end-silence-ms 800`），
素材是各自的真实旁白（1080 Ti 74.8s、5060 70.5s），每格是 2~3 轮里所有 final 的中位数。
「段尾」是服务端 `took_ms`（解码耗时，**不含** 800ms 判停等待）。

RTF 强烈依赖段长，不标段长的 RTF 没有可比性，所以两台都跑两档：

| 档位 | GTX 1080 Ti（这段素材停顿少，VAD 基本不提前切） | RTX 5060（这段素材停顿多） |
|---|---|---|
| 短段 | config 帧 `max_segment_ms: 5000` → 段长中位 5.0s | 默认 `--max-segment-ms 15000` → VAD 自己切出中位 3.2s |
| 长段 | 默认 15000 → 段长中位 15.0s | `--max-segment-ms 30000` → 中位 5.5s、最长 19.3s |

两段素材的停顿结构不同，中位段长对不上，所以**跨卡只比 RTF、不比绝对 ms**，
比 RTF 也只挑段长接近的档（下面的跨卡表用 1080 的 5.0s 档 ↔ 5060 的 5.5s 档）。

GTX 1080 Ti 11GB（torch 2.4.1+cu121 / funasr 1.4.16 / faster-whisper 1.2.1（ctranslate2 4.8.4）/
qwen-asr 0.0.6 / Python 3.10）：

| 配置 | 首个 partial | final 段尾（5s 段 / 15s 段） |
|---|---|---|
| `paraformer-streaming` | 1.29s | 137ms / 178ms（RTF 0.027 / 0.013） |
| `paraformer-streaming --refine paraformer` | 1.28s（不变） | 361ms / 492ms（RTF 0.072 / 0.035） |
| `paraformer-streaming --refine qwen3-asr-0.6b` | 1.28s（不变） | 1144ms / 3028ms（RTF 0.234 / 0.216） |
| `sensevoice`（GPU，滚动 partial） | 0.74s | 140ms / 213ms（RTF 0.028 / 0.015） |
| `qwen3-asr-0.6b`（滚动 partial） | 1.86s | 1054ms / 3083ms（RTF 0.212 / 0.220） |
| `qwen3-asr-1.7b`（不开滚动 partial） | — | 1075ms / 3058ms（RTF 0.224 / 0.221） |
| `whisper`（large-v3 int8，滚动 partial） | 1.76s | 764ms / 1786ms（RTF 0.154 / 0.172） |

RTX 5060 8GB（torch 2.11.0+cu128 / funasr 1.4.16 / faster-whisper 1.2.1（ctranslate2 4.8.4）/
qwen-asr 0.0.6 / Python 3.12）：

| 配置 | 首个 partial | final 段尾（3.2s 段 / 5.5s 段） |
|---|---|---|
| `paraformer-streaming` | ~1.20s | 48ms / 51ms（RTF 0.016 / 0.008） |
| `paraformer-streaming --refine paraformer` | ~1.21s（不变） | 125ms / 172ms（RTF 0.040 / 0.026） |
| `paraformer-streaming --refine qwen3-asr-0.6b` | ~1.21s（不变） | 386ms / 812ms（RTF 0.133 / 0.149） |
| `sensevoice`（GPU，滚动 partial） | ~0.87s | 74ms / 83ms（RTF 0.024 / 0.014） |
| `sensevoice`（CPU，滚动 partial） | ~0.89s | 146ms / 210ms（RTF 0.047 / 0.037） |
| `qwen3-asr-0.6b`（滚动 partial） | ~1.93s | 317ms / 575ms（RTF 0.109 / 0.108） |
| `qwen3-asr-1.7b`（不开滚动 partial） | — | 366ms（RTF 0.121） |
| `whisper`（large-v3 int8，滚动 partial） | ~1.73s | 433ms / 650ms（RTF 0.143 / 0.101） |

复现（`asr_bench.py` 就在本目录，按 40ms/帧实时推流，所以「首个 partial」「段尾」都是墙上时间）：

```bash
python ASR_server.py --port 7996 --gpu 0 --max-end-silence-ms 800 \
    --engine paraformer-streaming --refine paraformer &
python asr_bench.py --file narration_70s.wav --rounds 3 --tag streaming --json out.json
#   ^^^^^^^^^^^^^^^^ 换成你自己的旁白 wav（16kHz 单声道即可，其它采样率会自动重采样）
# 换段长档位不用重启服务：asr_bench.py --max-segment-ms 30000（走 config 帧覆盖）
# e2e 段尾（含判停等待）默认从 /health 的 vad.max_end_silence_ms 取，不用手填
# 并发那一档：同时起 N 个 asr_bench.py 进程就行（服务端 --max-sessions 要 ≥N）
```

换卡能看出来的三件事：

1. **首个 partial 基本与显卡无关**（流式：1080 Ti 1.28~1.29s、5060 1.20~1.21s，三种流式配置
   在各自那台机器上全落在 ±0.01s 内，跟精修用哪个引擎无关）。
   它由 `--chunk-size`（600ms 当前块 + 300ms 右 lookahead）和 `--partial-interval-ms` 决定，不是算力瓶颈——
   想更快出字去调参数，换卡没用。滚动 partial 的首次出字同理，等于「该引擎的间隔 + 一次重扫」：
   sensevoice 0.74s（5060 0.87s）、whisper 1.76s、qwen3-0.6b 1.86s。
2. **换卡的收益在离线侧，不在流式侧**（段长对齐后比 RTF：1080 的 5.0s 档 ↔ 5060 的 5.5s 档）：

   | 引擎 | 1080 Ti | 5060 | 倍率 |
   |---|---|---|---|
   | `paraformer-streaming` | 0.027 | 0.008 | 3.4× |
   | `paraformer`（离线，作精修） | 0.072 | 0.026 | 2.8× |
   | `sensevoice` | 0.028 | 0.014 | 2.0× |
   | `qwen3-asr-0.6b` | 0.212 | 0.108 | 2.0× |
   | `qwen3-asr-1.7b` | 0.224 | 0.121 | 1.8× |
   | `whisper`（int8） | 0.154 | 0.101 | 1.5× |

   流式那一路在 1080 上就已经 0.027，换卡只是把 0.14s 的段尾变成 0.05s——本来就贴着 VAD/协议开销；
   而 Qwen 精修（transformers 后端的大模型前向）只快 ~1.6~2×，新架构在这类小模型上红利吃不满。
   要「实时 + 准确」，瓶颈永远是精修那一下。
3. **8GB 刚好卡在 `qwen3-asr-1.7b` 的门槛上**：常驻 4.7GB，叠 ct-punc 1.2GB 就是 5.9GB，
   带桌面的机器还要被桌面合成器吃掉 ~2GB，所以 1.7b 只能 `--no-punc` 跑（加载后驱动级空闲只剩 ~1.0GB）。
   已加载 `whisper --refine paraformer` 时热切 1.7b 会被显存校验挡掉
   （空闲 4255MB < 4600MB → 500 + 自动回滚原引擎，服务不退出），符合预期。
   11GB 的 1080 Ti 就宽松得多：1.7b 常驻之后驱动级空闲还剩 6.4GB，ct-punc、离线精修都塞得下——
   **能不能同时装下「大模型 + ct-punc + 精修」，11GB 和 8GB 是两种答案。**

`POST /transcribe` 整文件吞吐（单请求；5060 用 70.5s 素材、1080 Ti 用 74.8s，
所以**只比 RTF**）：

| 引擎 | RTX 5060 | GTX 1080 Ti |
|---|---|---|
| `sensevoice`（GPU） | 0.74s / RTF 0.011 | 1.6s / 0.021 |
| `paraformer`（离线，作精修） | 0.80s / 0.011 | 1.8s / 0.024 |
| `paraformer-streaming --refine paraformer` | — | 1.8s / 0.024 |
| `sensevoice`（CPU） | 2.20s / 0.031 | — |
| `paraformer-streaming`（无精修，走流式） | 2.82s / 0.040 | 9.7s / 0.130 |
| `qwen3-asr-0.6b` | 5.7s / 0.081 | 15.2s / 0.203 |
| `whisper`（int8） | 6.2s / 0.088 | 50.5s / 0.675（见坑③） |
| `qwen3-asr-1.7b` | 7.4s / 0.105 | 16.4s / 0.219 |
| `paraformer-streaming --refine qwen3-asr-0.6b` | — | 14.8s / 0.197 |

主引擎是流式时 `/transcribe` 自动走精修引擎，所以**混合模式的整文件吞吐 = 精修引擎的吞吐**
（`--refine paraformer` 1.8s，`--refine qwen3-asr-0.6b` 14.8s）。

并发（`paraformer-streaming --refine paraformer`，N 路同时推流）：

| `--max-sessions` | 5060：partial / 段尾 took / 显存 | 1080 Ti：partial / 段尾 took / 显存 |
|---|---|---|
| 1 | 1209ms / 137ms / 4951 MB | 1287ms / 484ms / 2812 MB |
| 2 | 1230ms / 242ms / 5057 MB | 1342ms / 941ms / 2820 MB |
| 4 | 1280ms / 468ms / 5055 MB | 1475ms / 593ms / 2852 MB |

两台的档位不同（5060：3.2s 短段、不带 spk；1080 Ti：15s 长段、带 `--spk campplus`），
took 绝对值不可横比；显存口径也不同（5060 是 `nvidia-smi` 进程占用含桌面基线，
1080 Ti 是 `/health` 的 torch 侧）。能横比的是趋势：**partial 几乎不动、显存几乎不涨**（+40~100MB，
torch 分配池，不是权重翻倍），和「已知限制」一致。
段尾 took 取决于「同一时刻有几路真的在排队」：5060 那轮四路几乎同相位，137→242→468 近似线性；
1080 这轮四路推流相位错开，484→~850→~730，四路并没有继续恶化。
要压段尾就减少同时说话的路数，换卡帮不上多少。

顺带：`--spk campplus` 走 CPU，**这台 1080 Ti 的机器上每段 400~500ms（最长 959ms）**，
而 5060 那台只要 37~83ms——CPU 差 5~10 倍。它跟解码并发、不吃显存，但会实打实推迟 final：
实测 took 484ms + spk 413ms ≈ 段尾 1054ms。自己这台是多少，看 final 里的 `spk_ms`。

只有实测才看得见的三条坑：

- **坑①** `whisper`（large-v3）在这段中文旁白上会**中途转繁体**（`我寫五個點`、`總所謂活動大神`），
  它那一行的定位是「多语种/英文」，不是「中文兜底」。
- **坑②** `took_ms` 用 `time.time()` 算，虚拟机 NTP 校时会把它带偏：复现过负值（`-996ms`、RTF `-0.316`），
  也复现过明显偏小的值（19.3s 的段报 `30ms`，重跑同一配置是 `1450ms`）。
  统计时先按物理上限筛一遍（`rtf<0` 或长段 `rtf` 远低于该引擎 `/transcribe` 的 RTF 就丢掉）。
  **服务端已改用 `time.monotonic()`**；5060 那张表是改之前测的（个别 `took_ms` 可能偏），
  1080 Ti 那张是改之后测的。
- **坑③** `whisper` 的**单段耗时方差极大**：1080 Ti 上 `/transcribe` 六段里五段 1.9~3.2s，
  但 46.6→61.6s 那段（15s 音频）解码了 36s，把整文件 RTF 从中位的 ~0.19 拉到 0.675；
  同一份素材在 5060 上没撞上（6.2s / 0.088）。faster-whisper 默认带 temperature 回退
  （首遍没过置信度/幻觉阈值就换温度重解码），撞上就是几倍。
  所以 whisper 的数只看 P50、别看均值，也别当它是「慢但稳」的兜底。

准确率排序（同一批真实旁白，RTX 5060 上复核，排序不变）：
**qwen3-asr-0.6b > paraformer（离线）> paraformer-streaming（流式）> sensevoice > whisper**。
流式模型明显弱于离线模型，所以「实时 + 准确」的正解是流式出 partial、离线做精修，见下文混合模式。

---

## 快速开始

```bash
# 推荐配置：流式出字 + 离线精修 + 说话人分离
python ASR_server.py \
    --engine paraformer-streaming --refine paraformer --gpu 1 \
    --max-end-silence-ms 800 --max-sessions 2 --spk campplus \
    --log-file ./asr_server.log
```

自检：

```bash
curl -s localhost:7996/health      # status/engine/refiner/punc/显存/计数
curl -s localhost:7996/engines     # 各引擎是否可用（依赖没装会 available=false）
python ASR_server.py --print-engines
```

三种用法：

```bash
# 1) 浏览器试音：打开 http://<host>:7996/webui
# 2) 命令行：wav/mp3/裸 pcm 自动识别，或实时麦克风
python asr_client.py --file test.wav --url ws://127.0.0.1:7996/asr/ws
python asr_client.py --mic
# 3) 网页应用：聊天输入框的麦克风按钮，走 runtime 的 /v1/asr/ws 桥接
```

---

## 模型：用哪些、从哪下、存到哪

全部来自 **ModelScope**（魔搭，国内直连快）。模型 id 写在 `asr_engines.py` 的 `ENGINE_SPECS` /
`SPK_MODELS` 里，`GET /engines` 也能看到。

| 用途 | 模型 id | 体积 |
|---|---|---|
| 流式识别（默认引擎 `paraformer-streaming`） | `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online` | 849 MB |
| 离线识别 / 精修（`paraformer`） | `iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch` | 848 MB |
| 离线识别（`sensevoice`） | `iic/SenseVoiceSmall` | 897 MB |
| 离线识别（`whisper`） | `Systran/faster-whisper-large-v3` | 2.9 GB |
| 离线识别（`qwen3-asr-0.6b`） | `Qwen/Qwen3-ASR-0.6B` | 1.8 GB |
| 离线识别（`qwen3-asr-1.7b`） | `Qwen/Qwen3-ASR-1.7B` | 4.4 GB |
| 标点（ct-punc） | `iic/punc_ct-transformer_cn-en-common-vocab471067-large` | 1.2 GB |
| 说话人声纹（`--spk campplus`，默认推荐） | `iic/speech_campplus_sv_zh-cn_16k-common` | 28 MB |
| 说话人声纹（`--spk eres2netv2`） | `iic/speech_eres2netv2_sv_zh-cn_16k-common` | 68 MB |
| 离线精修时的句子切分 | `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch` | 3.9 MB |

**什么时候会下载**：某个模型**第一次被需要、且本地找不到**的时候自动下载——

- 启动加载引擎（或 `POST /engine` 热切引擎）时下载识别模型；
- 第一次给 final 补标点时下载 ct-punc（引擎自带标点的 Qwen 不会下载它）；
- 第一次抽声纹时下载 `--spk` 指定的模型；
- `--refine paraformer`（离线 paraformer）还会带上它自带的句子切分模型 fsmn-vad（3.9MB）。

也就是说：首次启动可能要等几分钟下载，之后重启是秒级加载（本地已有）。

**存到哪里**：

1. 设了环境变量 `ASR_MODELS_DIR`（例如 `/data/asr-models`）就先在里面找这三种目录名：
   `<org>/<name>`、`<name>`、`<org>--<name>`；找到就用，不再下载。
2. 否则走 `modelscope.snapshot_download`，缓存到 `~/.cache/modelscope/models/<org>--<name>`
   （旧版 modelscope 是 `~/.cache/modelscope/hub/models/<org>/<name>`）。
3. `modelscope` 包缺失或下载失败时，会把模型 id 原样交给引擎（FunASR 用自己的源），
   日志里会有一条 warning。

想预先下好（离线机、换机搬迁）：

```bash
export ASR_MODELS_DIR=/data/asr-models
modelscope download --model iic/SenseVoiceSmall \
    --local_dir $ASR_MODELS_DIR/iic--SenseVoiceSmall
```

`--model-dir /path/to/model` 可以直接指定本地模型目录，跳过查找与下载。

---

## 使用条件

**Python 环境**：一个装齐依赖的环境即可跑全部引擎，`bash install_asr.sh` 装进当前激活的环境
（`--no-qwen` / `--no-whisper` 可跳过；`--torch-cuda cu124` 指定 torch 构建）。
Qwen 引擎跑在子进程里，默认用同一个解释器，需要隔离时设 `ASR_QWEN_PYTHON=/path/to/python`。

**依赖**：默认引擎只需要 funasr + torch。`whisper` 需要 `faster-whisper`，`qwen3-*` 需要
`qwen-asr`——没装不会崩，`/engines` 里 `available=false`，选用时明确报错。

**磁盘**：模型按需下载，全量约 13 GB。只跑「默认流式 + 离线精修 + 标点」约 2.9 GB，
再加 `qwen3-asr-0.6b` 1.8 GB、`qwen3-asr-1.7b` 4.4 GB。

**GPU**：`--gpu N` 在导入 torch 前写入 `CUDA_VISIBLE_DEVICES`；`--device auto` 时没有 CUDA
会自动退到 CPU（慢但能用）。显存需求：

| 引擎 | 最低空闲显存 | GTX 1080 Ti 实测 | RTX 5060 实测 |
|---|---|---|---|
| `paraformer-streaming` | 1900 MB | 1.95 GB（含 ct-punc） | ~0.9 GB（叠 ct-punc 后 ~2.1 GB） |
| `paraformer` | 2200 MB | +0.87 GB（作 `--refine`，合计 2.82 GB） | +0.86 GB（作 `--refine` 时） |
| `sensevoice` | 1800 MB | 2.00 GB（含 ct-punc） | ~2.1 GB（含 ct-punc） |
| `whisper`（int8） | 2000 MB | ~1.9 GB（CT2 持有，不走 torch） | ~1.9 GB |
| `qwen3-asr-0.6b` | 2600 MB | ~1.5 GB（worker 子进程持有） | ~2.0 GB（子进程持有） |
| `qwen3-asr-1.7b` | 4600 MB | ~4.7 GB（权重决定，同 5060） | ~4.7 GB |
| ct-punc 标点 | +1100 MB | ~1.1 GB | +1.17 GB |
| 每多一路会话 | +3~5 MB | 推理排队，不翻倍 | +~30 MB（4 路共 +100MB，是分配池） |

> 两列口径不同：1080 Ti 那列是 `/health` 里的 `torch_alloc_mb`（torch 侧，含 ct-punc），
> 所以 whisper（CT2）和 Qwen（worker 子进程）这两行要按进程净占用看；
> 5060 那列是 `nvidia-smi` 看到的**进程净占用**（扣掉桌面基线，含 CUDA 上下文/分配池 ~0.3~0.5GB）。
> 模型本身两边一致（paraformer ~0.85GB、ct-punc ~1.1GB），换卡不会让权重变小——
> 8GB 卡上该省还得靠 `--no-punc`。

启动和切引擎前会按**驱动级空闲显存**校验，不够就报错并给出当时的显存快照。换引擎是
「先卸载旧的、再校验、再装新的」，所以不会因为旧引擎占着卡而误判不够；新引擎装不上会
**自动回滚**到旧引擎，精修引擎装不上只**降级**（不带精修继续跑）。

**端口与鉴权**：默认 `7996` 单端口，WS 是 `/asr/ws`，HTTP 是 `/health`、`/transcribe` 等。
默认绑 `0.0.0.0`（远程可连）；对外建议加 `--token xxx`（HTTP 用 `X-ASR-Token` 头或 `?token=`，
WS 用 `?token=`），`POST /shutdown` 非本机调用必须有 token。

**浏览器**：麦克风只在 **https / localhost / 127.0.0.1 / file:** 下可用（浏览器安全上下文要求），
页面是 https 时会自动用 `wss://`。页面会先探 `/v1/asr/health`，服务没配好就直接不显示麦克风按钮。

**接进网页应用**：runtime 侧设两个环境变量即可，未设置时语音输入整体不启用（按钮隐藏）：

```bash
export AGENTS_ASR_URL=http://127.0.0.1:7996     # 也可带路径，直接当 WS 地址
export AGENTS_ASR_TOKEN=xxx                     # 服务开了 --token 时才需要
```

runtime 提供 `GET /v1/asr/health`（探活，永远 200，`available` 里带原因）和
`GET /v1/asr/ws`（浏览器 ↔ ASR 服务的桥接，token 只在服务端注入，不从浏览器传）。

---

## 引擎怎么选

| name | 流式 | 自带标点 | 什么时候用 |
|---|---|---|---|
| `paraformer-streaming` | ✅ | ❌ | 默认。要「边说边出字」就用它当主引擎 |
| `paraformer` | ❌ | ❌ | 中文离线精度最高，常作 `--refine` 精修 |
| `sensevoice` | ❌ | ❌ | 多语种 + 情感，短音频很强，带 emoji 副产物 |
| `whisper` | ❌ | ❌ | faster-whisper large-v3，多语种/英文场景 |
| `qwen3-asr-0.6b` | ❌ | ✅ | 中文准确率明显高一档，显存够就值得 |
| `qwen3-asr-1.7b` | ❌ | ✅ | 最准但最重（≥4.6GB），慢，默认不开滚动 partial |
| `mock` / `mock-offline` | ✅/❌ | — | 不加载任何模型，跑协议/压测/单测 |

表里的「流式 ❌」只表示**不是增量解码**，不等于没有实时反馈——见下一节。

### 「边说边出」有两种

| | 哪些引擎 | 机制 | 代价 |
|---|---|---|---|
| 真流式 partial | `paraformer-streaming` | 每喂一个 chunk（`--chunk-size`，默认约 600ms）做一次增量前向 | 几乎免费 |
| 滚动 partial | 其余全部，**默认自动开** | 每 N ms 把**当前这一段**重扫一遍，partial 可能整句重写（事件带 `rolling:true`） | 总解码量 ≈ 4× 段长，与段长无关 |

`--offline-partials-ms`：`-1`=按引擎自动（默认）、`0`=强制关、`>0`=指定间隔。
自动档按引擎快慢分档：sensevoice 600 / paraformer 700 / whisper 1200 / qwen3-0.6b 1500 /
**qwen3-1.7b 不开**（太慢，重扫追不上实时，还会拖累别的会话）。`/health` 里的
`offline_partials_ms` 就是实际生效值。

重扫范围**只有当前段**（段首额外带 `--pre-roll-ms` 默认 200ms 缓冲），不是从开麦起的全部音频；
间隔还会随段长自适应（`max(基准, 段已长/4)`），所以长句不会把解码量平方级放大。

### 混合模式（推荐）：流式 partial + 离线精修 final

```bash
--engine paraformer-streaming --refine paraformer      # 或 --refine qwen3-asr-0.6b
```

partial 仍由流式引擎实时给（延迟不变），段结束时用离线引擎把**整段音频**重扫一遍作为 final。
精修结果明显偏短会自动回退到流式结果。代价：段尾多几百 ms
（`--refine paraformer` 5060 多 ~0.1s、1080 Ti 多 ~0.2~0.3s；`--refine qwen3-asr-0.6b`
5060 多 ~0.3~0.8s、1080 Ti 多 ~1.0~2.9s）、多一份显存。

`POST /transcribe` 在流式主引擎下会自动改用精修引擎（响应里 `engine_used` 标明实际干活的引擎）。

### 标点

| 开关 | 含义 |
|---|---|
| 都不带（默认） | **自动**：引擎自带标点（Qwen3-ASR）就不加载 ct-punc；非 FunASR 系（whisper）本来也挂不上 |
| `--punc` | 强制加载 ct-punc（+1.1GB 显存） |
| `--no-punc` | 强制不加，省 1.1GB |

`/health` 里的 `punc` 是**实际生效值**。注意：`--engine qwen3-asr-* --refine paraformer` 时
final 会没标点（精修引擎不叠 ct-punc），这种搭配要显式 `--punc`。

---

## 说话人分离（`--spk`）

```bash
--spk campplus          # campplus | eres2netv2 | mock（空=关闭）
```

按 VAD 段抽声纹 embedding，和**本路会话**已有的质心比余弦：≥ `--spk-threshold`（默认 0.45）
算同一个人并更新质心，否则新开一个 `S1/S2/…`。final 事件多带三个字段：

| 字段 | 说明 |
|---|---|
| `spk` | `S1`/`S2`…（会话内编号，`reset` 后重新数） |
| `sim` | 与该质心的余弦（新建质心时为 `null`）；`spk_short: true` 表示段太短、沿用上一位 |
| `spk_ms` | 抽声纹耗时（CAM++ 在 CPU 上：5060 那台 37~83ms，这台 1080 Ti 的机器 400~500ms——CPU 差一个数量级） |

- 默认跑 **CPU**（`--spk-device cpu`）：CAM++ 28MB、RTF≈0.03、零显存，也不和 ASR 抢锁。
  放 GPU 才需要 `--spk-device cuda:0`（约 +50MB，且会串行进引擎锁）。
- 实测区分度：同一人不同句 ≈ +0.69，不同人 −0.17~+0.23，所以 0.45 的阈值很宽。
- 短于 `--spk-min-ms`（默认 800ms）的段不抽声纹，直接沿用上一位。
- 加载失败不影响识别，只是 final 不带 `spk`；`/health` 的 `spk` 字段报告模型/设备/是否已加载。
- **只能分开「轮流说」的人**：两个人同时开口（重叠语音）分不开，那需要端到端 diarization；
  partial 也带不了 `spk`（声纹要整段音频才准）。

---

## 接口

WS：`ws://host:7996/asr/ws`（旧客户端连根路径 `/` 也认）。客户端发**二进制 = PCM16LE 音频**，
**文本 = 控制帧 JSON**。

控制帧：

| 帧 | 说明 |
|---|---|
| `{"type":"config", ...}` | 连接后首帧：`sample_rate`/`language`/`max_end_silence_ms`/`last_seg_id` 等 |
| `{"type":"flush"}` | 强制收尾当前段（文件推完必须发） |
| `{"type":"reset"}` | 清空会话状态（含说话人编号） |
| `{"type":"stats"}` / `{"type":"ping"}` | 查询 / 心跳 |

服务端事件：`ready`、`config_ack`、`segment_start`、`partial`、`final`、`segment_cancel`、
`replay`、`stats`、`error`、`flush_ack`、`pong`。

- `partial`：当前段累积文本（不加标点），按 `--partial-interval-ms`（默认 300ms 音频时间）节流；
  滚动模式带 `rolling:true`。
- `final`：段结束文本（已补标点/精修），带 `start_ms/end_ms/reason/took_ms/rtf`；开了 `--spk`
  再带 `spk/sim/spk_ms`。
- `segment_cancel`：短于 `--min-speech-ms` 的杂音被丢弃，客户端应删掉该 seg 的 partial。
- 断线补发：重连时 `config` 带 `last_seg_id`，服务端把漏掉的 final 用 `replay` 事件补发。

HTTP 控制面（与 WS 同端口）：

| 接口 | 说明 |
|---|---|
| `GET /health` | 状态、引擎、标点、显存、并发、说话人、计数器 |
| `GET /stats` | 详细统计 |
| `GET /engines` | 引擎列表与可用性 |
| `GET /webui` | 浏览器推流页面 |
| `POST /engine {"name":"sensevoice"}` | 热切引擎（需无活动会话） |
| `POST /warmup` | 触发 warmup |
| `POST /transcribe` | JSON `{"path":…}` / `{"audio_b64":…}` / `{"url":…}`，可带 `language`、`with_segments`；或直接 POST 音频字节（`?format=mp3`） |
| `POST /shutdown` | 退出服务（非本机调用需 `--token`） |

```bash
curl -s localhost:7996/transcribe -d '{"path":"./test.mp3"}'
curl -s -X POST --data-binary @./test.mp3 "localhost:7996/transcribe?format=mp3"
```

---

## 参数速查

| 参数 | 默认 | 作用 |
|---|---|---|
| `--engine` | `paraformer-streaming` | 主引擎 |
| `--refine` | 空 | 段结束用离线引擎重扫整段作为 final |
| `--gpu N` / `--device` | 不设 / `auto` | 选卡；无 CUDA 自动退 CPU |
| `--max-sessions` | 1 | 并发路数（建议 ≤4） |
| `--chunk-size` | `0,10,5` | 流式块 [左,当前,右]，单位 60ms；`[4,10,8]` 更准但更慢 |
| `--max-end-silence-ms` | 800 | 静音多久判停（手机/浏览器推流建议 500–800） |
| `--max-segment-ms` | 15000 | 超长强制切段（整段进 final，不会只留尾巴） |
| `--min-speech-ms` | 250 | 短于此判为杂音 → `segment_cancel` |
| `--pre-roll-ms` | 200 | 段首回补，避免吃字（按毫秒裁剪） |
| `--partial-interval-ms` | 300 | partial 节流（音频时间） |
| `--offline-partials-ms` | -1 | 非流式滚动 partial：-1=按引擎自动，0=关，>0=指定间隔 |
| `--punc` / `--no-punc` | 自动 | ct-punc 标点（+1.1GB）；自带标点的引擎自动跳过 |
| `--spk` / `--spk-device` / `--spk-threshold` / `--spk-max-speakers` / `--spk-min-ms` | 空 / cpu / 0.45 / 8 / 800 | 说话人分离 |
| `--start-margin-db` / `--min-level-db` | 7 / -42 | VAD 触发阈值与噪声底 |
| `--token` | 空 | HTTP/WS 鉴权 |
| `--log-file` | 空 | 落盘日志 |
| `--model-dir` | 空 | 直接指定本地模型目录（离线机） |
| `--language` / `--sample-rate` | `zh` / 16000 | 引擎语言、客户端默认采样率（config 帧可覆盖） |
| `--vad` | `energy` | `none` = 不切段，一直往一段里堆 |
| `--no-warmup` / `--warmup-audio` | 加载后自动 warmup | 跳过 warmup / 用指定音频 warmup |
| `--idle-unload-min` | 0（常驻） | 空闲多久卸载模型腾显存 |
| `--lock-file` / `--spool-dir` | `/tmp/asr_server.lock` / `/tmp/asr_spool` | 单实例锁 / 断线补发暂存 |
| `--max-body-mb` | 64 | `/transcribe` 请求体上限 |
| `--force-vram` | 关 | 跳过显存校验（只在测量用） |

---

## 常见问题

**点了麦克风一个字都没有。** 先看按钮旁边的电平条：说话时不动 = 浏览器根本没采到音，
跟模型无关。服务端日志里 `audio_ms=0` 也是同一结论。客户端会直接说明原因：
「浏览器没有采到任何音频」/「已送出音频，但服务端没识别出语音」/「浏览器暂停了音频采集」。

**说完一段、停顿十几秒后再说就没字。** 两种可能，日志能分开：

- 浏览器不再送音频（标签页被降级、AudioContext 被挂起）：`audio_ms` 远小于墙上时长，
  客户端会报「浏览器中途停止了送音频」（2.5s 没有麦克风块就触发）。让页面保持前台再试。
- 底噪被顶起来，VAD 切不出段：浏览器 `autoGainControl` 在静音时抬增益，静音越久抬得越高，
  阈值（= 底噪 + 7dB）跟着涨，等人再说话已经在阈值下面。静音满 8s 会打一行
  `静音心跳 … 底噪=… 阈值=… 最近峰值=…`，底噪/阈值一路涨就是它；
  客户端也会报「服务端没切出语音段（底噪被自动增益抬高/声音太轻）」。

**某台机器不出字（别的机器正常）。** 链路是 `浏览器 → /v1/asr/ws（runtime 桥）→ 本服务`，
只有最后一段会回错误码，所以「点了没字」多半出在前两段：

| 提示 | 含义 | 先查 |
|---|---|---|
| 浏览器没有采到任何音频 | 一个采样点都没发出去 | AudioContext 被挂起（标签页在后台/策略拦截）、麦克风被系统静音 |
| 已送出音频，但服务端没识别出语音 | 音频上去了，VAD 没开门 | 系统默认输入设备选错、音量太小（`--min-level-db`）、浏览器噪声抑制把语音压掉了 |
| 浏览器暂停了音频采集 | 授权之后音频图仍未运行 | 让页面保持前台后重试 |

**报显存不足。** 错误信息带当时的显存快照，能分清是没让位还是同卡别的进程占着。
办法：`--no-punc`（省 1.1GB）、换更小的引擎、释放同卡其它进程。精修引擎装不上不会让服务起不来，
会自动降级为不带精修（`/health` 里 `refiner:null` + `last_error`）；腾出显存后用
`POST /engine {"name":"<当前引擎名>"}`（同名=重载）重试。

**标点重复 / 没标点。** Qwen3-ASR 自带标点，服务默认不会再叠 ct-punc；
`--engine qwen3-* --refine paraformer` 这种搭配 final 会没标点，需要 `--punc`。

---

## 已知限制

- **并发是排队**：每路会话有独立的流式状态（约 3~5MB/路），但推理被全局锁串行，
  路数再多也只是排队，建议 ≤4。要真并行得每路一个引擎实例（权重翻倍，约 +1.9GB/路）。
- **重叠语音分不开**（两人同时开口），partial 带不了说话人标签。
- **Qwen3-ASR 走 VAD 分段（准流式）**：强制切段时跨段的句子可能在相邻两段各出现一次，
  加大 `--max-segment-ms`（如 30000）可减少接缝；整段 `/transcribe` 不受影响。
  它的真流式接口只在 vLLM 后端下可用，本服务用 transformers 后端。
- 精修后的文本按段补标点，段边界处偶有多余句号。
- 无 MCP 封装。

---

## 文件与测试

| 文件 | 作用 |
|---|---|
| `ASR_server.py` | 主服务（WS + HTTP 控制面、会话状态机、VAD、断线补发） |
| `asr_engines.py` | 引擎抽象层（funasr / whisper / qwen worker / mock）、标点、声纹 |
| `asr_vad.py` | 能量自适应 VAD（START / END / CANCEL，噪声底跟踪） |
| `asr_audio.py` | 重采样、PCM16 编解码、mp3 解码（ffmpeg）、增益归一 |
| `asr_client.py` | 命令行客户端（麦克风 / 文件推流） |
| `webui_asr.html` | 浏览器麦克风推流页面 |
| `asr_bench.py` | 延迟/RTF 基准：按实时节奏推 wav，量首个 partial、段尾 `took_ms`/RTF，抓显存快照 |
| `qwen_asr_worker.py` | Qwen3-ASR 子进程 worker（JSON-Lines） |
| `install_asr.sh` | 往当前虚拟环境装各引擎依赖 |
| `../../tests/test_asr_*.py` | 89 项测试（见下） |

```bash
cd /cc/agents && python -m pytest tests/test_asr_streaming.py tests/test_asr_multisession.py \
    tests/test_asr_speaker.py tests/test_asr_preroll.py tests/test_asr_switch.py \
    tests/test_asr_bridge.py -q
```

用 `mock` 引擎，不加载任何模型：VAD 状态机、WS 协议、HTTP 转写、mp3 解码、断线补发、
并发拒绝、强制切段、精修回退、说话人聚类、段首预滚、换引擎的显存账（卸载顺序/回滚/降级/
同名重载）、标点自动档、静音心跳与段级日志。

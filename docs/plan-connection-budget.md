# 连接预算方案（Phase 2）

> 前置：`runtime/server.py` 的 `_ThreadingHTTPServer.request_queue_size` 已从标准库默认
> 5 提到 128（Phase 1，已落地）。那只修掉了 accept backlog 溢出导致的 1s 级 SYN 重传
> 尾部延迟，**没有**改变浏览器侧的连接预算问题。本文是 Phase 2。
>
> 修订记录：初版把 A 阶段写成"5-6 → 4 条"，那是基于"POST 流与 GET 流会并存"的错误
> 假设。实际代码里 `ChatPage.svelte:237` 已经用 `directlyConsumedSessions` 阻止了并存，
> 所以每会话今天就已经只占 1 条。A 不减少连接数，见 §2 与 §4。

> **实施状态（2026-11-20）**：本文的连接预算部分仍然有效（每 tab 一条共享帧通道、
> `request_queue_size`）。其中"隐藏页面交还连接"已于同日撤销：可见性不再决定任何连接的生命周期，
> 隐藏页继续读取它正在观看的推理（换来的是回到前台时不再有一段空洞）。但文中所有"无人观察 → 取消非 flight 推理"的策略已于 2026-11-20
> 删除：flight / 非 flight 二元取消，推理与客户端连接彻底解耦，取消只来自显式 stop/force 或进程退出。
> 因此 `DETACHED_WINDOW_S`、`SESSION_UNOBSERVED_GRACE_S`、hidden-page hold（`/v1/sessions/frames/hold`）
> 都不存在了，文中的窗口对比表按此作废。
>
> **术语也已取消**：「飞行模式 / flight mode」不再是产品概念——README、UI、代码与 API 里都没有它，
> 后台持续推理是所有推理的默认行为。本文与 `design-review-inference-stream.md` 里残留的
> 「flight / 非 flight」只是当时的措辞，按「有没有浏览器在看」读即可。另外，一个会话同一时刻只有
> 一个推理世代：对推理中的会话再发请求（流式或非流式）一律 409 `session_inferring`，不接管也不取消。

## 0. 术语澄清：会话流是 SSE，不是 WebSocket

| 通道 | 协议 | 端点 |
|---|---|---|
| 会话推理流（发起方） | **SSE** | `POST /v1/infer/stream` |
| 会话推理流（旁观/续传） | **SSE** | `GET /v1/sessions/{id}/stream` |
| 会话状态事件 hub | **SSE** | `GET /v1/sessions/events` |
| 终端 | **WS** | `WS /v1/terminals/ws`（每 tab 一条，帧带通道 id；C 之前是每终端一条） |
| 语音输入 | **WS** | `WS /v1/asr/ws` |

- 会话流**从来没有** WebSocket，本地模式与远程环境模式都是 SSE；远程模式下浏览器连的是
  子端 runtime 的同名端点（`web/src/lib/remote-execution.svelte.js` 的 `buildRemoteUrl`）。
- 终端 WS 在本地模式与远程环境模式连的是**同一条**：`WS /v1/terminals/ws`
  （`components/Terminal.svelte` 的 `muxUrl` + `lib/terminals-mux.js`）。属于哪个环境由
  `open` 帧里的 `env_id` 决定（缺省 `local`），母端按它去拨子端并按帧原样回传
  （`runtime/terminal_env_bridge.py`）—— 隧道与直连都在母端这一跳完成，浏览器既不直连
  子端、也不为每个环境多开一条 socket，子端凭据也不进浏览器 URL。
- 本文只有 **D 阶段**才引入"把帧塞进 WS"的想法，且是可选的。B 阶段是 **SSE 多路复用**。

## 1. 当前每 tab 连接账本（代码事实）

常驻长连接只有 5 个来源：

| 来源 | 数量 | 代码位置 |
|---|---|---|
| 事件 hub SSE | 每 tab **1**（Sidebar / ChatPage / AuthSettingsPage 共享） | `api.js:564` `sessionEventsHub`，`handler_api.py:2124` |
| `POST /v1/infer/stream` | 每个**本 tab 发起**且未结束的推理 **1** | `api.js:245`，`handler_infer.py` |
| `GET /v1/sessions/{id}/stream` | 每个**正在流式**且在本 tab 打开过、且未被 POST 直接消费的会话 **1** | `api.js:402`，`handler_api.py:2226` |
| `WS /v1/terminals/ws` | 每 tab **1**（本地终端与各子环境终端都在这一条上，帧带 `env_id`） | `components/Terminal.svelte`，`handler_base.py` `_handle_terminal_mux_ws`，`terminal_env_bridge.py` |
| `WS /v1/asr/ws` | 语音输入期间 **1** | `lib/asr-client.js:494`，`handler_base.py:663` |

两条去重逻辑（已存在，别重复计数）：

- `ChatPage.svelte:237`：`directlyConsumedSessions.has(sid)` 时**不**开 GET —— 所以
  一个由本 tab 发起的推理，全程只占 **1** 条（POST），不会 POST+GET 并存。
- `ChatPage.svelte:2027/2042`：只有 `sessionStore` 里已有条目的会话才会开 GET —— 本 tab
  从未打开过的后台流式会话**不占**连接（靠 hub：一条共享帧通道订阅所有在看的会话）。

**终端在隐藏时不释放连接，这是有意的。** `Terminal.svelte` 的 `$effect` 只在
`visible === true` 时首次挂载通道，`visible` 翻回 false 时**不**关闭：PTY 在服务端
一直活着，但脱离期间打印的内容不会补发给浏览器（见 §5），所以那份持续喂进来的
xterm 回滚缓冲就是唯一的历史——关掉 socket 换来的是"点回终端图标时少一段输出"。
代价是 T = 本 tab 曾经打开过终端的会话数（`ChatPage.svelte` 为 `terminals` 里每个
条目都挂一个 `<Terminal>`），而不是当前可见的 1 个。C 阶段用共享 socket 消掉这个
代价，而不是关掉连接。

### 稳态公式

```
现状：  1 (hub) + N (流式会话) + T (曾开过的终端) [+1 ASR]
```

浏览器对同一 origin 的 HTTP/1.1 并发上限通常是 6，**WS 也计入这个池**。所以
N=3、T=2 就已经 6 条顶格，第 7 条（任何普通 `fetch`）在浏览器里排队 —— 这就是
"两个以上并发推理时请求容易卡住"的浏览器侧机制。

## 2. 关键结论：只有 B 能让连接数与并发会话数解耦

| 阶段 | 稳态连接数 | 与 N（并发流式会话）有关？ |
|---|---|---|
| 现状 | `1 + N + T` | **有关** |
| A（POST→GET 交接 + 服务端宽限期） | `1 + N + T`（**不变**） | **仍有关** |
| B（帧通道多路复用） | `1 + 1 + T` | **无关** |
| C（终端 WS 多路复用） | `1 + 1 + 1 = 3` | 无关 |
| D（单条控制 WS） | `1` | 无关 |

A 的价值不在连接数，而在**健壮性**（见 §3）；如果你的实际场景是"同时跑 ≥4 个推理"，
**必须做 B**，A 单独上不会改善。

## 3. A 阶段（健壮性补丁，不是连接优化）

现状：发起方 POST 流一路持有到 `[DONE]`。它一旦断（代理掐断、网络抖动、tab 挂起），
`onStreamErr` 已经会 handoff 到可续传的 GET（`ChatPage.svelte:1584-1596`，机制已存在）。
但服务端侧 `_cancel_unobserved_stream_locked`（`server_state.py:1156`）在"starter 断开且
无旁观者"时，会直接取消**非 flight** 推理。

A 做两件事：

1. **服务端宽限期**（已随 B 落地，见 §4"交接竞态"一节）：starter 断开后先进入
   `SESSION_UNOBSERVED_GRACE_S`（默认 10s）窗口等待重连/交接，而不是立即取消。
2. **可选显式交接**：用户切走会话时主动结束 POST 并 handoff 到 GET（复用现有
   `subscribeToExistingSession(sid, {handoff:true})`），配合 `X-Agents-Handoff: 1`
   把头把主动交接的 WARNING 降为 INFO。

收益：后台推理不再因为发起方连接被中间设备掐断而被静默取消；日志不再刷 WARNING。
**不改变稳态连接数。**

## 4. B 阶段（主力：帧通道多路复用）

目标：把 N 条 per-session SSE 压成 **1 条**，与并发会话数无关。

- 服务端：新增 `GET /v1/sessions/frames?conn=<id>`，一条 SSE 承载多个会话的帧；
  用短请求 `POST /v1/sessions/frames/subscribe` / `.../unsubscribe` 动态增减订阅集。
  broker 侧复用现成的 `subscribe_session_stream` / `publish_session_stream_frame`
  （`server_state.py:1431`），只是把"一个 handler 一个队列"改成"一个 handler 多路队列"。
  帧里已带 `seq`，续传语义（`after`）按会话各自维护。
- 前端：把 `subscribeSessionStream` 改成 hub（照抄已验证的 `sessionEventsHub`，
  `api.js:564`）：组件仍按 sessionId 注册监听，底层只有一条连接；`lastSeq` 按会话存。

B 之后：`1 (hub) + 1 (frames) + T`。N=10 也不涨。

代价与对策：
- 一条连接上的 head-of-line：单条 TCP 上串行写，帧量大时共享带宽。帧本身很小
  （token 级），且服务端每连接已有 `sse_write_lock` 串行化，实测不是问题；必要时
  对帧做 10–20ms 批量（前端已有 `createStreamFrameBatcher`）。
- 取消语义：`_cancel_unobserved_stream_locked` 依赖"该会话有无 subscriber"。多路复用后
  必须在 subscribe/unsubscribe 命令里同步维护 per-session subscriber 计数，否则后台
  推理会被误取消或永不取消。**这是 B 最大的风险点**，需要针对性回归用例。

### B 已落地（实测）

后端（`server_state.py` / `handler_api.py` / `handler_base.py`）：

- `GET /v1/sessions/frames?conn=<tab id>`：按 `conn` 取通道，**没有则创建、有则复用**；
  每次 attach 递增 `generation`，旧连接收到 `attach_flush` 后自行退出，不会双写。
- `POST /v1/sessions/frames/subscribe|unsubscribe|close`：短命令增减会话；`subscribe`
  带该会话自己的 `after`，在锁内原子地注册 subscriber 并回放 gap
  （`sub_init` → 回放帧 → live），帧不会早于 `sub_init`、也不会被回放重复。
- detach 后订阅保留 `FRAME_CHANNEL_GRACE_S` 宽限期，宽限结束才释放，从而保证
  "无人观察的非 flight 推理要取消"这条规则不被多路复用破坏（重连不误取消）。
- 旧的 `GET /v1/sessions/{id}/stream` 保留：第三方客户端、远程旁观不受影响。

前端（`api.js` 的 `sessionFramesHub`）：每 tab 一条 SSE；`conn` id 存 `sessionStorage`
（同一 tab 刷新后可复用并按各自游标续传）；45s watchdog、1s 重连；游标只在 UI 批量
落帧后（`onApplied` → `frameBatcher.then(ack)`）推进，重连不会跳过未应用的帧。
`inferStream` 收到 `init` 后**先确认后端已把会话交给通道**（最多等
`FRAMES_HANDOFF_WAIT_MS`）再 abort POST；确认不了就不放手，继续用 POST 当数据面
（`frames_handoff_fallback`）。

### B 落地后的回归：交接竞态把推理"误判为无人观看"

症状：非飞行模式下频繁出现 `Error: user interrupted.`，用户并没有点停止；同一会话改开
飞行模式后不再报错，但要等会话结束才能一次性看到全部内容。

链路（与日志逐条对得上）：

1. `init` 一到，前端 ack 完就 abort POST —— 这是 B 的正常交接，不是用户中断。
2. 服务端下一次写帧时看到 `BrokenPipeError` → `disconnect_session_stream_starter()`
   （日志：`starter SSE disconnected: BrokenPipeError`）。
3. 此刻通道的 subscribe 还没登记（POST 与 subscribe 是两条连接，落地顺序不保证），
   `_cancel_unobserved_stream_locked` 判定"starter 已断 + 无订阅者"→ **立即**取消 →
   `Error: user interrupted.`（日志：约 14s 后的 error event）。
4. 飞行模式"看起来正常"的原因：这条取消规则整体跳过。但通道同样没订阅成功，帧只能在会话
   结束后从落盘/补帧一次性吐出 —— 即"长时间没输出，结束后一次性看到"。

两侧同时修：

- **服务端**：无人观看不再立即取消，先进入 `SESSION_UNOBSERVED_GRACE_S`（默认 10s，设 `0`
  恢复旧严格策略）宽限期；期间任何观察者回来（POST 重新挂上、subscribe 落到通道）就撤销，
  到点再复查一次才真取消。换代（`begin_session_stream`）时旧代待取消不丢弃；飞行模式开启
  撤销待取消、关闭时若仍无人观看则重新开窗。
- **前端**：交接顺序改为"确认通道已接管 → 再释放 POST"（见上），subscribe 命令按通道
  generation 幂等、失败重试一次；会话页每 5s 巡检 `backendStreamingSessions`，把通道里丢
  失的 watch 重新挂上（重连风暴、订阅被拒、done 竞态都靠它兜底）。
- **回归**：`tests/test_infer_stream_handoff.py` 用真实 HTTP + 真实通道 + 慢速脚本模型跑
  完整交接；`test_zero_grace_restores_the_legacy_interruption` 锁住"0 宽限 = 旧行为"，
  保证这组用例真的能抓到这个回归。

### 第二个回归：会话切到后台就被取消

症状：非飞行模式下，推理中的会话一旦切走（打开别的会话）就必然
`Error: user interrupted.`。不是有意为之——`App.svelte` 的注释写着页面切换"不影响流式推理"。

链路：会话切到后台时该会话在帧通道上的订阅被释放（`POST /v1/sessions/frames/unsubscribe`）。
B 之后 starter POST 早已交接并释放，这一退订让 generation 的观察者归零 → 宽限到点 → 取消
（日志：`session_stream CANCELLED sid=... reason=last_subscriber_left`）。
即"取消无人观看"的规则把**会话级**退订误读成**浏览器级**离开。

修的是判定粒度：观察者归零后先问"最后观看它的 tab 还连着吗"
（`note_session_watched_by` 记录 conn，`_frame_channel_is_live` 查通道 `connected`）：

- tab 的帧通道仍在 → 不取消；用户点一下就回来。取消改由该通道被丢弃时触发
  （`_drop_frame_channel_locked` 对该 conn 看过的 generation 重新判定，reason=`tab_left`）。
- tab 真走了（关页/跳转/断线超过 `FRAME_CHANNEL_GRACE_S`）→ 照旧进入宽限期后取消。
- 从不曾有人观看、也没有通道的推理（第三方 API 客户端）→ 照旧立即取消，规则未被削弱。

回归：`tests/test_frame_channel.py::test_closing_a_conversation_does_not_cancel_its_running_inference`
（切走 → 跨过宽限期不取消、帧仍可补放 → 回来重新订阅 → 关通道才取消）。

实测（`/tmp/probe_b_ledger.py`，客户端模拟 Chrome 的 6 连接/origin 池）：

| 场景 | tab 持有 socket | `/v1/models` 延迟 | 帧完整性 |
| --- | --- | --- | --- |
| 旧：1 hub + per-session SSE（N=6） | 6/6（第 6 条已开不出） | **卡死 >2s**（等空闲 socket） | — |
| 新：1 hub + 1 帧通道（N=6） | 2/6 | p50 1.3ms / p90 1.7ms / max 3.4ms，0 卡死 | 246 帧、6/6 会话顺序无缺口 |
| 新：1 hub + 1 帧通道（N=12） | 2/6 | p50 1.4ms / p90 3.5ms / max 3.6ms，0 卡死 | 492 帧、12/12 会话顺序无缺口 |

"活跃会话数增加而连接不增加"已成立；B 之后剩下的瓶颈是终端 WS（T）。

### 第三个回归：后台会话看不到输出，切回来才"唰"地一大片

症状：发完消息后推理确实在跑，但屏幕上什么都没有；切到别的会话（或抛出
`Error: user interrupted.`）之后，忽然一次涌出大量输出。与连接数无关——帧确实到了
浏览器，只是没有落地。

链路：ChatPage 把流式帧合帧后再渲染，而调度只挂了 `requestAnimationFrame`。浏览器对
**隐藏 tab 不执行 rAF 回调**，于是这条会话不在前台时：帧既不写进 store，`then()` 里的
排空回调也不跑（那里才是在推进续传游标、结束流、`onStreamDone`），游标冻结在旧值 →
每次重连/重新订阅都按旧游标把整代帧重放一遍 → 用户看到的就是一次性的"文字墙"。

修法（都在前端）：

- 合帧策略搬到 `web/src/lib/stream-batcher.js`：可见时按 rAF 合帧（每帧一次 DOM 提交），
  不可见时退回 10Hz 定时排空 —— 合帧是为了别把 DOM 打爆，不是为了等一次 painting。
  ChatPage 只留一个带 `tool_use_id` 访问器的薄封装。
- `web/src/lib/api.js` 加 `FRAMES_MAX_PENDING_APPLIES = 512` 兜底：帧已经交给页面了，
  只是页面没 ack；超过上限就自行推进游标，否则一次重连就要整段重放（实测去掉兜底后
  重订阅的 `after` 退回 0，即从头重放整代）。

回归：`web/src/lib/stream-batcher.test.js`（把旧的 rAF-only 调度塞回去会红 3 条）、
`web/src/lib/api.test.js::releases the resume cursor when the page never acknowledges frames`。

结论性提醒：§7.1 设想的"隐藏 tab 主动释放通道"**不要做**。它把一条还在推理的会话主动
变成"无人观看"，正是前两个回归的形状；即便母端现在只在该 conn 的通道真消失时才取消
（`tab_left`），隐藏期间的实时输出也照样丢了。

### 第四个回归：关掉最后一条会话，整个 tab 的通道被关掉

现场日志（两条同一毫秒）：

```
session_stream unobserved sid=… reason=last_subscriber_left grace=10.0s
frame_channel dropped  conn=… generation=1 released=['…']
… 10s 后 session_stream CANCELLED … → infer_stream error event: Error: user interrupted.
```

判读要点（都在 `_drop_frame_channel_locked` 里发出，所以同毫秒）：

- `released=['<sid>']` 非空 ⇒ 该 tab **没有**退订这条会话；是**通道本身**被丢弃。
  所以这不是第二个回归那条路径（那条 released 会是空）。
- `generation=1` ⇒ 这个通道一辈子只 attach 过一次，浏览器**从未重连**。前端 EOF→1s 重连、
  45s watchdog→abort+重连都该在 ≤46s 内回来，因此只有两种可能：页面真走了，或页面
  自己把通道关了（`framesTeardown`：`entries` 归零 → `POST /frames/close`）。
- 取消本身在丢弃后 10s（`SESSION_UNOBSERVED_GRACE_S`）落地。

第二种可能正是漏掉的一环：tab 只 watching 一条会话时，关掉/切走这条会话会让
`sessionFramesHub.entries` 归零，前端随即 `framesTeardown()` **关掉整个 tab 的通道** ——
一个还在推理的 generation 于是被母端判定为"浏览器走了"。per-tab 存活判定救不了它，
因为通道真的没了。

改法：把"没人路由"与"tab 走了"分开。

- 前端 `framesUnwatch` / `framesFinish` 不再立刻关通道，而是 `framesScheduleIdleTeardown()`
  挂 60s（`FRAMES_IDLE_TEARDOWN_MS`）；期间重新打开任何会话都会 `framesClearIdleTeardown()`
  复用同一条连接（不新增连接，仍 1 条）。tab 真关了则传输断开，由母端
  `FRAME_CHANNEL_GRACE_S`（60s）+ `SESSION_UNOBSERVED_GRACE_S`（10s）兜底取消。
- 母端补齐通道生命周期日志，让下一次现场日志能直接分辨两种原因：
  `frame_channel attach … generation=N`（重连会看到 N=2、3…）、
  `frame_channel detached … grace=… sessions=[…]`（倒计时开始的那一刻）、
  `frame_channel dropped … cause=grace_expired|close_command|channel_limit released=[…] detached_for=…s`
  —— `cause=close_command` 且 `detached_for≈0` = 页面主动关的；
  `cause=grace_expired` 且 `detached_for≈60` = 浏览器走了没回来。
  GET 侧原本被 `except OSError: pass` 吞掉的写失败也补了一行
  `frame_channel stream ended … error=…`。
- 取消与倒计时两条日志都带上 `latest_seq=… last_frame_age=…`，用来回答"被取消时这条
  推理是在干活还是早就闲着"；同一次取消只报一次（通道丢弃会两次触发判定）。

回归：`tests/test_frame_channel.py::test_channel_lifecycle_is_traceable_from_the_log`、
`web/src/lib/api.test.js::{holds the channel open when the last watched session closes,
releases a channel that stayed idle past the hold window}`（把立刻 teardown 的旧写法塞回去
前者会红）。前端诊断开关 `localStorage.setItem('session_stream_debug','1')`，对应母端
`SESSION_STREAM_DEBUG=1`。

### 第五个回归：后台 tab 被浏览器掐掉，60s 后推理被算成"无人观看"

新日志（补了通道生命周期之后第一次能读全）：

```
18:01:22.723 frame_channel attach   conn=… generation=1 reattached=False sessions=[]
18:01:37.406 frame_channel stream ended conn=… generation=1 error=[Errno 32] Broken pipe
18:01:37.406 frame_channel detached conn=… generation=1 grace=60.0s sessions=['…']
18:01:40.491 infer_stream: starter SSE disconnected: BrokenPipeError      ← 交接，正常
18:02:33.385 session_stream unobserved … latest_seq=58 last_frame_age=43.6s
18:02:33.385 frame_channel dropped … cause=grace_expired released=['…'] detached_for=60.0s
18:02:43.387 session_stream CANCELLED … latest_seq=61 last_frame_age=0.7s
18:03:01.498 infer_stream error event: Error: user interrupted.
18:03:45.700 frame_channel stream ended conn=… generation=1 error=[Errno 32] Broken pipe
```

判读：

- `cause=grace_expired` + `detached_for=60.0s` ⇒ **不是**页面自己关的（那会是
  `cause=close_command`、`detached_for≈0`），是传输在 18:01:37 就没了，60s 通道宽限
  用尽后母端丢弃通道、释放订阅，再过 10s 取消。**这条路径的策略是对的**：非飞行模式、
  无人观看、浏览器 60s 没回来。
- attach 只有一行、generation 一直是 1 ⇒ 浏览器在整整 60s 里**没有重连**。前端 EOF→1s
  重连、45s watchdog 都靠 JS 计时器，而后台 tab 会被浏览器冻结（timer 停摆、SSE 被切）。
  所以"切走标签页 ≈ 1.5 分钟"就足以杀掉一次非飞行推理——这正是用户反复遇到的形状。
- 18:03:45 又出现一次同 conn 同 generation 的 `stream ended`：说明通道被丢弃之后还有一次
  attach（新通道对象 generation 重新从 1 开始；那次 attach 行没贴出来），页面在 18:03:19
  重新收到帧（console 里 `generation=1` 是前端自己的计数）——即页面一直活着，只是被冻结了。

改法：让"页面走开"与"页面关了"在协议上可区分，而不是靠传输的时机去猜。

- 新增 `POST /v1/sessions/frames/hold {conn, seconds}`：页面主动声明"我只是被切到后台，
  预计离开 N 秒"。母端把通道宽限变成 `max(FRAME_CHANNEL_GRACE_S, hold_until - now)`；
  hold 到期仍没回来，照旧丢弃并走无人观看取消（hold 是上限，不是免死金牌）。
- 前端在 `visibilitychange→hidden`、`pagehide(persisted)`（进 bfcache 是冻结不是关闭）、
  以及 `chan_init`（后台 tab 里刚建立的通道，永远不会触发 visibilitychange）三处发送
  hold（`FRAMES_HIDDEN_HOLD_S = 300`）；回到前台时若连接已断则立即重连。真正关闭页面走
  的是 `/frames/close`，语义不变。
- 通道现在计数 attach 的 reader（`handlers`）。同一个 conn 可以有两个 reader（复制标签页
  会复制 sessionStorage ⇒ 同一个 conn id；或代理把被替换请求的上游留着），旧 reader 退出
  不能再把还在推流的通道标成"浏览器走了"。最后一个 reader 离开时才起倒计时——否则
  "关掉标签页就停止"会反过来失效。日志相应带上 `handlers=`、`hold=`。

回归：`tests/test_frame_channel.py::{test_hidden_page_hold_outlives_the_channel_grace_window,
test_a_stale_reader_detach_does_not_drop_the_live_channel,
test_the_countdown_starts_when_the_last_reader_leaves, test_frames_hold_endpoint_extends_the_channel}`、
`web/src/lib/api.pageVisibility.test.js`（三条：隐藏时 hold、bfcache 时 hold、后台建立的通道在
chan_init 时 hold；去掉监听器即红）。

仍然没被证明的：为什么那个 tab 60s 内没重连（冻结？代理切断后浏览器没恢复？）。下一次日志看
`frame_channel attach … generation=2/3` 是否出现即可分辨：出现了=浏览器重连过（那取消只可能
发生在两次重连之间的空档）；始终 generation=1=页面被冻结或已走。

## 5. C 阶段（终端 WS 多路复用）—— 已落地

`WS /v1/terminals/ws`：一个 tab 一条 socket 承载它打开的全部终端。

- 帧协议（两端都是 JSON 文本帧）：
  - 浏览器 → 服务端 `{"op":"open"|"input"|"resize"|"close", "c": terminal_id, ...}`
  - 服务端 → 浏览器 控制 `{"op":"opened"|"closed"|"error", "c": id}`，
    PTY 输出 `{"c": id, "d": chunk}`
- 每个通道驱动的还是同一份持久 PTY（`get_or_create_terminal`）：关通道或整条
  socket 掉线只是 detach，**不杀 shell**；重连 `open` 同一个 id 会 re-attach 到
  同一个进程（`tests/test_terminal_mux.py` 里用 pid 不变来钉住这一点）。
- 写帧加锁：PTY 输出从各自的 reader 线程来，输入/控制从 handler 线程来。
- 端点名直接用 `/v1/terminals/ws`：旧的"每终端一条连接、帧即按键"协议整体删除
  （`_handle_websocket` / `_start_pty_session{,_unix,_win32}`，-219 行），同一个 URL
  只说通道协议——收到裸帧回 `{"op":"error","error":"malformed frame"}` 而不是开
  shell（`test_endpoint_only_speaks_the_channel_protocol`）。
- 前端 `web/src/lib/terminals-mux.js`：按 origin 一条 hub socket，组件持有
  channel；socket 掉线自动重拨并重新 `open` 所有仍在的 channel（各自带回它的
  `env_id`）；最后一个 channel 关闭才释放 socket。

### 5.1 子环境终端也在这一条上（已落地）

远程执行里终端一度是「例外」：先是浏览器直连子端，后来是每环境一条
`/v1/env-proxy/{env}/v1/terminals/ws` 桥。两者都按**环境**消耗浏览器的连接额度，
而池子只有 ~6 条 —— 开几个子环境的终端就会把普通请求饿死（现象是「终端打不开」，
其实是排队连不上）。因此路由从 URL 挪进协议：

- 帧：`{"op":"open","c":id,"env_id":"local"|"…","cols":…,"rows":…}`；
  此后的 input/resize/close 只认通道 id，环境在 open 时就定了。
- 母端（`runtime/terminal_env_bridge.py`）：每个（tab, 环境）**一条**母端→子端连接，
  该环境的第一个通道懒拨、最后一个通道关闭即拆；拨号在 worker 线程里做，
  一个拨不通的子端不会卡住整条 socket 的读循环。
- **失败只落在该环境的通道上**：`{"op":"error","c":id,"error":"environment unreachable: …"}`，
  同 tab 的本地终端与其他环境不受影响（旧桥是一次失败的 HTTP 握手，整条终端通道作废）。
- 空闲不等于掉线：PTY 可以几分钟不输出，链路的拆除只来自通道关闭、浏览器离开或
  子端真的 EOF。（顺带修掉一处潜伏 bug：隧道流的 `recv(timeout)` 超时也返回 None，
  老桥会把安静超过 5s 的远程终端当成断开。）
- 子端凭据只在母端：直连用登记记录里的 token 注入 `Authorization: Bearer`，
  隧道走子端早就建好的那条反向 WS 隧道，浏览器 URL 里始终没有 token。
- **通道属于它自己的会话**：终端按会话渲染（侧边栏在任何一行上都能开），所以环境
  也必须按会话解析（`web/src/lib/session-env-registry.svelte.js`）。把「当前会话的
  激活绑定」当全局用会有两个后果：切到远端会话后、环境记录还没异步解析完时点开终端
  会在**母端**起一个 PTY；删除后台远端会话时去母端 DELETE 一个不存在的终端，把真正的
  PTY 留在子端。为此母端补了 `GET /v1/sessions/{id}/meta`（只回 meta）——读一个会话的
  `remote_env` / `workspace` 绑定不该下载整段对话。
- 回归：`tests/test_terminal_env_bridge.py`（连接账本、失败隔离、retarget、
  空闲 vs EOF）、`tests/test_terminal_mux.py`（env_id 缺省、坏 env 只伤该通道、
  远程通道计入上限、local↔环境来回切换）、`tests/test_remote_envs.py`
  （真直连子端：env_id 通道、token 注入、拨不通只报该通道）、`tests/test_tunnel.py`
  （隧道环境的 env_id 通道）。

**隐藏终端仍然保持附着，这是有意的**（不是 bug）：服务端没有给浏览器做
scrollback 回放——`output_buffer` 是给 agent 的 terminal 工具消费的
（`builtin_tools_misc.py:240-243` 读走即清空），detach 期间的输出对 UI 就是丢了；
重新 attach 服务端只回一个 id 帧，不补历史。所以浏览器里那份 xterm 回滚缓冲
就是唯一的历史。共享 socket 之后，"随时可恢复"不再多占连接：T 条终端 = 1 条。

账本因此变成：

```
C 之后：  1 (hub SSE) + 1 (frames SSE) + 1 (终端 mux WS) [+1 ASR]   —— 与 N、T 无关
```

实测（`/tmp/probe_c_ledger.py`、`/tmp/probe_final_ledger.py`；服务端 socket 计数取
`/proc/net/tcp` 里该监听端口的 ESTABLISHED 条目）：

| 场景 | tab 持有 | `/v1/models` | 正确性 |
| --- | --- | --- | --- |
| 5 终端走 1 条 mux WS | 3 | p50 1.0ms / p90 1.2ms / max 1.9ms | 输出按通道路由 5/5；`close` 后重新 `open`，`export KEEPME=alive` 的变量还在 → shell 进程确实跨通道存活 |
| 6 流式会话 **+** 5 终端（同一个 tab） | 3（2 SSE + 1 WS） | p50 1.2ms / p90 1.4ms / max 1.6ms，0 卡死 | 246 帧覆盖 6/6 会话、按序无缺口；终端输出路由 5/5 |

对照 §4 里旧账本 N=6 时"6/6 socket、`/v1/models` 卡死 >2s"：现在 N 和 T 都不再消耗
槽位，6 槽里常驻 3 条，剩下给 ASR 和瞬时请求。

## 6. D 阶段（可选）

- 单条控制 WS：把 hub + frames + 终端合成 1 条 WS（3 → 1）。收益小、改动大，仅在
  终端 + 多会话同时重度使用时才值得。
- 前置 h2 反代（Caddy/nginx/uvicorn）：彻底消除 6 连接上限，但与"零第三方依赖"定位
  冲突，只作为部署选项写文档。

## 7. 零风险缓解（可先上，与 A/B/C 无关）

1. **后台 tab 主动释放**：`visibilitychange` 隐藏时关掉 frames/终端通道，回前台按
   `after=lastSeq` 续传。会话帧有 broker 保留，不丢输出；终端见 §5 的回放限制。
2. **连接预算看门狗**：前端统计长连接数，>4 时按优先级降级（先关隐藏终端，再关
   非当前会话的帧订阅），并在 UI 上提示。
3. **静态资源换 origin**：`web/dist` 由 FastAPI 同 origin 提供，一次性请求也会瞬时占
   用 6 槽位；换端口/域名可另得 6 槽。

## 8. 建议顺序

1. **B**（解决多会话并发顶格的正主）——**已完成**，含 §4 的 subscriber 计数回归用例
   （`tests/test_frame_channel.py`）与 §4 表的实测。
2. **A** 的宽限期已作为 B 的附带补丁落地（`FRAME_CHANNEL_GRACE_S`）。
3. **C**（终端 WS 多路复用）——**已完成**，见 §5。账本现在是
   `1 hub + 1 frames + 1 终端 mux [+1 ASR]`，与 N、T 无关；隐藏终端继续附着
   （服务端不给浏览器补历史，见 §5），共享 socket 让这件事不再多占连接。
   端点就是 `/v1/terminals/ws`（旧协议已删），子环境终端也在这一条上（§5.1）。
   回归：`tests/test_terminal_mux.py`（12 例：单 socket 多终端路由、winsize 落到
   PTY、`close` 只摘一个通道且 PTY 存活、socket 掉线后重开复用同一 pid、坏帧不
   掉线、通道上限、旧协议不再被接受、鉴权门）；`tests/test_tunnel.py` 里 4 条
   终端桥接用例改走通道协议后，浏览器→母端→隧道→子端 PTY 的端到端路径重新变绿
   （该文件此前 4 红是因为它还在用早已改名的 `/v1/tunnel-proxy/` 前缀）。
4. §7 的缓解随时可上（其中"隐藏 tab 主动释放"对终端要谨慎：会丢隐藏期间的输出）。

---

## 9. 收敛过度防御：一个对账循环 + 一个后端窗口（下一步方案，未实施）

五个回归修完后，前端为"流式存活"引入了 8 个计时器、母端 2 个。它们各自对应一次真实故障，
但叠在一起已经超出可推理的规模，而且其中两个（前端空闲关闭、看门狗）是在用**连接**和
**主动断线**去买本来应该用**声明**买的东西。本节按三条约束重新收敛。

### 9.0 约束

1. 看门狗要不要，必须有证据，不能靠"感觉需要"。
2. 每个 tab 只有 ~6 条 HTTP 长连接额度，**空闲时占着一条不可接受**（代价太高）。
3. 计时器数量必须收敛；8 个重叠语义的计时器本身就是异常信号。

### 9.1 先确认三个代码事实（它们决定了方案）

| 事实 | 出处 | 后果 |
| --- | --- | --- |
| 续传游标在**前端**（`subscribe` 带 `after=lastSeq`），母端**没有**服务端游标，也没有 `/frames/ack` | `runtime/handler_base.py:264-267`（只有 subscribe/unsubscribe/close/hold） | 通道对象被销毁**不丢任何状态**，重连即重放 |
| 帧缓冲"保留到该次推理结束" | `runtime/server_state.py:1708` 注释 | 只要推理还活着，重开连接就能补齐，不需要为续传保连接 |
| 通道每 ≤15s 写一次 `: keepalive` | `runtime/handler_api.py:2566-2569` | 传输死亡会在下一次 keepalive 被 `BrokenPipe` 抓到并落日志，**母端不依赖前端看门狗**来发现断线 |

`runtime/server_state.py:1708-1710` 那段注释本身就是旧教条（"非飞行模式下，producer 只能在至少
一条浏览器连接存活期间运行"）——五个回归全部源于这句话。方案的核心是把它改成：
**producer 的存活取决于"有没有观看权声明"，而不是"有没有 socket"。**

### 9.2 约束 1：看门狗的证据链

已有的事实（不是猜测）：

- 昨天那次故障的日志里，母端 18:01:37 就是靠 keepalive 写入失败发现传输死亡的
  （`frame_channel stream ended ... error=[Errno 32] Broken pipe`）。
- 同一次里前端 45s 看门狗**一次都没触发**（隐藏 tab 计时器被冻结），它也没有在任何日志里救过任何东西。
- 结论：看门狗对后台/冻结场景**结构性无效**——后台场景唯一的解是 hold。它唯一可能的价值是：
  **可见 tab + 半开 socket（无 FIN）**，此时前端 reader 卡在 `read()` 里不返回 EOF。该场景至今**未在任何日志中观察到**。

决定性实验（改 nginx 即可，母端不用改代码，约 30 分钟）：

1. 前置代理对 `/v1/sessions/frames` 设 `proxy_read_timeout 10s`，可见 tab、通道空闲 60s。
2. 看两端日志：
   - 前端出现 EOF（reader 正常结束）→ 代理会发 FIN → **删掉看门狗**，EOF→重连足够。
   - 前端无 EOF，而母端有 `frame_channel stream ended error=` → 半开成立 → 保留 staleness 规则，
     但阈值改为可推导的 `2 × keepalive = 30s`，且只在 `document.visibilityState === 'visible'` 时生效，
     并并入 §9.4 的对账循环，不再是独立计时器。

长期取证（一行日志，灰度几天看分布）：重连时打 `frames_reconnect cause=eof|watchdog|visibility|reconcile`。
若 `watchdog` 计数长期为 0，就是删除它的最终依据。

### 9.3 约束 2：连接账本

| 场景 | 现在 | 目标 |
| --- | --- | --- |
| 可见、≥1 条会话被订阅 | 1 条 frames | 1 条 |
| 可见、没订阅任何会话 | `FRAMES_IDLE_TEARDOWN_MS=60s` 内仍占 1 条 | **0 条**（立即 `close reason=idle`） |
| 隐藏 tab | 占 1 条，另外还发 hold=300s | **0 条**（关连接 + 只发 hold） |
| bfcache（`pagehide persisted`） | 占 1 条（多半已被浏览器掐） | **0 条** |

理由：JS 被冻结时那条 socket 毫无用处（字节堆在浏览器网络进程里，回前台一次性刷出——和基于
websocket 的行情页后台刷新是同一种现象），只是白占 6 个槽位中的一个。**保命的信息量在声明里
（hold / 窗口），不在连接里**，所以"关连接"和"不取消推理"并不冲突。这也正是 §7 第 1 条早就写下
的"后台 tab 主动释放"，当时缺的只是母端窗口，现在补上。

### 9.4 设计：把"存活权"从连接搬到母端窗口

**母端：两个计时器合成一个。**

- 新增 `SESSION_DETACHED_WINDOW_S`（默认 120s）。释放订阅 / 通道关闭 / starter 断开，都不再
  "10s 后取消"，而是把该 generation 标成 `detached_until = now + 窗口`；任何观看者回来
  （`attach` 或 `subscribe`）就撤销。
- 通道对象在 `handlers == 0` 时**立即**销毁（`FRAME_CHANNEL_GRACE_S` 降到 0–2s，只用于合并日志），
  因为 §9.1 已证明销毁不丢重放能力。
- `hold` 退化成这个窗口的**上限覆盖**：`detached_until = max(window, hold_until)`。
- 后端计时器：2 → 1。`SESSION_UNOBSERVED_GRACE_S` 保留为兼容别名（`0` 仍能复现旧行为做对照测试）。

**前端：一个 5s 对账循环（reconciler）取代 5 个计时器。**每个 tick 只做四件事：

1. `should_open = visible && (entries.size > 0 || 本地已知 streaming 的会话 > 0)`；
   否则确保通道关闭（`close reason=idle`）。→ 取代 `FRAMES_IDLE_TEARDOWN_MS` 和 1s 重连计时器。
2. 任一 entry `registered == false` → 重发 subscribe。→ 取代 `FRAMES_SUBSCRIBE_RETRY_MS` 与
   ChatPage 的 `streamWatchSweep`（那个 5s 清扫本来就是同一个对账循环，只是没合并）。
3. 通道"最后一字节距今 > 2×keepalive 且可见" → 重连。→ 取代 45s 看门狗（是否保留取决于 §9.2）。
4. reader 结束 → 下一个 tick 重连（带游标重放）。→ 取代 `FRAMES_RECONNECT_MS`。

保留的 one-shot：`FRAMES_HANDOFF_WAIT_MS`（2s，交接握手必须等）。
events hub 的 45s watchdog + 1s reconnect 用同一 tick、不同 predicate 覆盖。
**净结果：前端 8 → 1 个 interval + 1 个 one-shot。**

**取消语义（写进测试，不再靠时序）：**

| 情况 | 行为 |
| --- | --- |
| 飞行会话，浏览器断开 | 允许取消（语义不变） |
| 非飞行：退订 / 关掉最后一条会话 / 隐藏 / 断线 | 进窗口，不立即取消 |
| 页面显式 `hold=0`（`pagehide` 且非 persisted，尽力而为） | 立即取消 |
| 窗口到期仍无观看者 | 取消；日志必须带 `cause= detached_for= latest_seq= last_frame_age=` |

**对账循环必须可观测**：每 tick 若真做了动作就打一行 `reconcile action=open|close|resubscribe|reconnect`，
否则"一个循环修所有"会变成新的不可观测。

### 9.5 删除 / 保留清单

删：`FRAMES_IDLE_TEARDOWN_MS`、`FRAMES_RECONNECT_MS`、`FRAMES_SUBSCRIBE_RETRY_MS`、
ChatPage `streamWatchSweep`、两个 45s watchdog（视 §9.2 结论，至少 frames 那个改为可见页专属 + 30s）、
`FRAME_CHANNEL_GRACE_S` 缩到 0–2s。
留（这些是语义修复，不是防御）：交接握手 2s、`handlers` 引用计数、hold 协议、`stream-batcher.js`、
`FRAMES_MAX_PENDING_APPLIES`、取消原因日志。

代价与对策：

- 窗口变长 → 真离开的浏览器最多浪费 120s 非飞行算力；`hold=0` 提供快速通道；可用 env 调小。
- 立即销毁通道 → 回前台/重开会话要重连+重放（已实现；帧缓冲保留到 generation 结束）。
- 单循环修所有 → 故障不再"某处自愈"，靠 `reconcile action=` 保持可观测。

### 9.6 验收（先立测，再删码）

1. 现有回归必须全绿：`test_infer_stream_handoff.py`(4) + `test_frame_channel.py`(15) +
   `api.pageVisibility.test.js`(3)。
2. 新增：
   - `test_closing_the_channel_immediately_does_not_cancel_the_generation`（母端窗口）
   - `test_a_hidden_page_releases_the_connection_but_keeps_the_generation`（前端：隐藏后 0 条 open
     connection，且发过 hold）
   - `test_an_idle_tab_holds_no_long_connection`（所有场景 `openConnections.length <= 1`）
3. 灰度观测字段：`frames_reconnect cause=`、`reconcile action=`、母端 `cause=/detached_for=`。

---

## 9.6 已实施（本轮）

按 9.5 的顺序做完，且**先写测试再改实现**。

### 后端：存活窗口搬到母端

- `DETACHED_WINDOW_S = 120`（`SESSION_DETACHED_WINDOW_S`）。取代"通道断了 = 人走了"的猜测：
  自最后一次被订阅起，一段推理要连续无人观察满这个窗口才被放弃。
- `FRAME_CHANNEL_GRACE_S` 从 60s 降到 **2s**，职责收窄为"通道对象的重连宽限"。
  `SESSION_UNOBSERVED_GRACE_S` 语义改为 `DETACHED_WINDOW_S` 的别名（测试用它注入窗口），
  并加了防呆：已放弃的 generation 不会被二次放弃（二次调用返回 False）。
- `close` / `hold` 协议都带上 `reason` 与 `hold_seconds`；释放时把缺席时长
  **转移到 generation 上**（`stream['hold_until']`），通道对象消失不再影响窗口。
- 新增测试：`test_closing_the_channel_does_not_cancel_within_the_detached_window`、
  `test_a_released_generation_is_cancelled_when_the_window_expires`、
  `test_a_hidden_page_announcement_outlives_the_channel`。

### 前端：一个对账循环 + 空闲/后台不占连接

- **一个** `setInterval(reconcileStreams, 2000)` 取代帧通道看门狗、帧通道重连、
  帧通道空闲关闭、ChatPage 的 5s watch 巡检、事件 hub 看门狗、事件 hub 重连。
  判据是证据：`lastByteAt`（SSE keepalive 15s/30s 会刷新）超过 2× keepalive 才算断。
- **空闲 tab 长连接为 0**：最后一个 watch 退订 → 立刻 `close reason=idle hold_seconds=0`。
  母端把窗口从这一刻起算，推理继续跑；回到前台 `subscribe` → cursor 重放。
  注意 `hold_seconds=0` 表示"不额外延长，按窗口算"，**不是**"立刻放弃"——
  这是 9.3 原文里没写清的地方，实现里已按前者处理并写进回归测试。
- **后台 tab 长连接为 0**：`visibilitychange=hidden` → 关闭通道与事件 hub，
  并 `hold reason=page_hidden hold_seconds=FRAMES_HIDDEN_DETACHED_WINDOW_S(=窗口)`；
  对账循环在 hidden 期间不再建连接，可见后立刻重开并 cursor 重放。
  台账：可见 1 帧通道 + 1 状态流，隐藏 **0 条**。
- 后台期间不消费 rAF：`stream-batcher.js` 在隐藏时改用计时器批次（上一轮已做）。

### 日志怎么读（现在）

```
frame_channel close ... reason=idle|page_hidden|unload hold=...     # 浏览器主动交还，不是崩溃
session_stream unobserved ... grace=120.0s window=<剩余> latest_seq=N  # 进入窗口，不是取消
session_stream CANCELLED ... unobserved for >=window               # 只有这一行才是真放弃
frames_reconcile / frames_teardown ...                             # 前端 streamDebug，一循环一行
```

### 与计划的偏差（一处）

9.3 里写"关页面时 hold_seconds=0"，实现改为**交还时一律至少一个窗口**（真卸载时
120s 后放弃，窗口内重新打开等于恢复），理由：`pagehide` 无法区分真关闭与 bfcache，
赌错一次的代价是一段已生成结果的浪费，而浏览器自己的 unload 超时远小于一个窗口。

---

## 10. 第六个回归：更新到“连接预算”这一版之后，中断反而变高频

现场日志的关键几行（另一套环境）：

```
frame_channel attach conn=1x8j5f493f27w64n generation=4 reattached=False handlers=4 sessions=['...']
frame_channel detach without attach conn=1x8j5f493f27w64n generation=4
frame_channel expired conn=1x8j5f493f27w64n generation=4
channel reader exiting conn=1x8j5f493f27w64n generation=4 expired_after_disconnect=True
session_stream CANCELLED sid=261118_115234 reason=last_subscriber_left (unobserved for 2.0s)
session_stream begin sid=261118_115332 starter=True cancel=True latest_seq=0 grace=2.0s
```

### 10.1 日志自己交代的三件事

1. **放弃窗口是 2.0 秒**：`session_stream begin ... grace=2.0s` 和 `CANCELLED ... (unobserved for 2.0s)`
   两处独立自证。这正是 9.6 里那个“只用于通道对象复用”的 2s 被同时当成“推理没人看”的窗口用了——
   计划里要避免的反向错误（两个问题一个计时器）发生了，只是方向相反：短的那个赢了。
   结果：通道只要断开超过 ~2s，正在跑的推理就被放弃。这就是“高频”的来源。
2. **取消由通道到期直接触发**：`frame_channel expired` 与 `CANCELLED` 同一毫秒。
3. **tab 的通道每几秒重建一次，且 attach 计数不守恒**：25 秒内 attach/detach 成对出现
   （attach 后 85–155ms 就 detach），`handlers=4` 一路涨，并出现 `detach without attach`。
   attach 后 150ms 内就断，说明那条 SSE 请求几乎一建立就死掉——不是网络抖动，是请求本身被废掉。

### 10.2 “请求一建立就死”的机制，本仓库已经记录了

`tests/test_keepalive_framing.py` 的文档字符串写的就是这个：浏览器会把多个请求排队复用同一条
TCP 连接；任何**应答了但没读 body** 的 POST，body 会留在连接上，被解析成下一个请求的开头：

```
{"conn":"tab-1","seconds":300}GET /v1/sessions/... HTTP/1.1
-> 501 Unsupported method
```

那个样例 body 就是 `/v1/sessions/frames/hold` 的载荷。也就是说：前端开始频繁发
`/frames/close`、`/frames/hold`（9.6 的设计）之后，如果运行中的后端**没有这两条路由**（404）
或者**应答时不 drain body**，每一次“我还活着/我离开了”的公告都会毒掉同一条连接上的下一个请求——
而下一个请求往往就是帧通道 SSE 本身。于是：attach → 150ms 内死 → 重连 → 再被毒掉。
这与 10.1 第 3 点的形状完全吻合；再叠加 2.0s 的窗口，每一次风暴都杀掉一段推理。

### 10.3 这一版改动在这里的角色：放大，而不是起因

- 公告变多（idle/hidden/pagehide 都发 `close`/`hold`）→ 若后端不 drain，触发频率同步变高；
- `FRAME_CHANNEL_GRACE_S` 从 60s 降到 2s（本意只是对象回收）→ 若窗口跟着变 2s，风暴立刻致命。

### 10.4 本轮加固（不改策略，只让这类错误不能静默发生）

1. **重新 attach 就是证据**：`_frame_channel_releases` 记录每个已释放通道曾观看的会话；
   同一 `conn` 再次 attach 时 `rescue_sessions_of_reattached_conn()` 撤掉这些 generation 的放弃计时器。
   即“只要这个 tab 有一条读者连着，它就没有在放弃任何东西”——即使它的 `/frames/subscribe`
   命令因为上面的 landmine 全在失败。读者一断，grace 到期会重新计时，真走了仍然按窗口取消。
   回归测试：`test_a_reconnecting_tab_that_cannot_re_subscribe_is_still_an_observer`。
2. **窗口低于重连容差就大声说**：`session liveness policy: the unobserved window is 2.0s, below the
   10s reconnect tolerance ...`（每进程一次）。回归测试：`test_an_underconfigured_abandonment_window_says_so`。

对照探针（窗口 2s、每 1.2s 重连、subscribe 命令失效）：

```
rescue OFF (previous): alive through the storm=False
rescue ON  (current) : alive through the storm=True
```

### 10.5 现场要先确认的两件事

1. **跑的是哪个构建**：`frame_channel expired` / `channel reader exiting` / `session_stream begin` /
   `session_stream dropped owner_gone` / `detach without attach` 这几串在本仓库工作树**和全部提交历史**里
   都不存在（`git log --all -S` 为空）。所以那台机器执行的不是这棵树的代码；先把进程对应的
   代码版本与 `web/dist` 的构建对齐（同一棵树、同一次构建），否则所有判读都失去意义。
2. **窗口值**：看 `session_stream begin ... grace=` 或取消行的 `(unobserved for Xs)`。X=2 就是
   配置/版本问题；`SESSION_DETACHED_WINDOW_S` 应当 ≥60（默认 120），`SESSION_FRAME_CHANNEL_GRACE_S`
   才是那个 2s。

---

## 11. 第七个回归：幽灵通道（根因确认 + 一次收紧，而不是再加一层兜底）

### 11.1 根因只有一句

**"有人能收帧"这件事被回答了两次**：`POST /v1/sessions/frames/subscribe` 的 JSON ack，和通道本身上送
的 `sub_init`。前者只说明"命令被接受"，却被 handoff gate 当作"通道已经在观察这个会话"的证据。

于是：浏览器手里的 conn 在服务端注册表里已经没有通道对象了（通道被 grace/close/重启清掉，而 conn id
存在 sessionStorage 里跨重载存活），subscribe 命令却**凭空造出**一个 `generation=0 / handlers=0` 的通道
对象并回 `registered:true` → 前端认定交接完成，abort 掉当时唯一的观察者（starter POST）→ 2 秒防泄漏
宽限把这个幽灵的订阅收回、给那代推理挂上 120s 无人观察窗口 → `session_stream CANCELLED reason=unobserved`。
当天日志 12 次签名：

```
frame_channel dropped conn=… generation=0 cause=grace_expired handlers=0 released=['<sid>'] detached_for=2.0s
```

另一半同样成立且互相咬合：**通道被 drop 时读者不退出**（`_drop_frame_channel_locked` 只从注册表 pop，
不进队列放哨兵）。孤儿读者继续每 15s 往浏览器里写 `: keepalive`（当天实测 26–29s，最长一例
11:19:29 drop → 11:39:44 才 Broken pipe，约 20 分钟）。前端唯一的存活判据是"两个心跳无字节"，被孤儿
喂成永远新鲜 → 永不重连 → 反复 subscribe → 反复造幽灵。而且一旦进入不会自愈：前端的
`entry.registeredGeneration` 仍认为"已注册"。

### 11.2 收紧成一条不变式

> **通道只在它的 SSE 读者附着时存在。没有读者的 conn 不可订阅。**

后端（`runtime/server_state.py`、`runtime/handler_api.py`）：

1. `frame_channel_subscribe`：注册表里没有该 conn，或 `handlers == 0`（detached 复用宽限期内也算没有
   读者）→ 返回 `registered:false`，**不创建通道对象**；HTTP 层带 `reason:"no_reader"`。
   `registered` 从此是"投递路径存在"的陈述，不是"命令被接受"的陈述。
2. `_new_frame_channel_locked`：删掉"出生即挂 grace 定时器"。只有 `attach_frame_channel` 会建对象，
   半成品不可能存在，这条防泄漏兜底随之失去存在理由。
3. `_drop_frame_channel_locked`：先 `channel["closed"] = True` 并投递 `{"type":"close"}` 哨兵；读者循环
   改成 `while not channel["closed"]` 并识别该哨兵。通道的死亡因此对浏览器可见（SSE 结束），
   前端既有的"两个心跳无字节 → 重连"判据自动接管，**不需要任何客户端僵尸检测器**。

前端（`web/src/lib/api.js`）：

4. `framesWatchingReady()` 等的是**通道自己送出** `sub_init`/首帧（`entry.delivered`），不再是命令 ack；
   `framesTeardown`/`framesUnwatch` 立即把等待 settle 成 false，fallback 不必等满 2s 边界。
5. 命令被拒 → `entry.registeredGeneration = -1` + `hub.reconnectRequested = true`，由**唯一的** 2s 对账
   tick 走 `framesReconnectChannel()`（重开 GET，再按各自游标重订阅）。不在命令回调里即时重发，
   也不会形成"拒绝→重开→再拒绝"的循环：只有一个时钟。
6. 删掉的兜底：`FRAMES_SUBSCRIBE_RETRY_MS` 的盲重试（在同一条没有投递能力的通道上重发同一命令，只会
   再产出一个幽灵注册）；`reconnectAfterApplications`（全仓没有任何地方把它置 true，纯死代码）。

### 11.3 与 §10.4 的关系

§10.4 设想的 `rescue_sessions_of_reattached_conn` / `_frame_channel_releases` **未实施**，被 §11.2 的
不变式取代：与其在重连后去"救"一批被误释放的会话，不如让"没有读者的订阅"从一开始就不成立。
保留的只有 §10.4 第 2 条（窗口低于默认值时 `session liveness policy:` 说一次），实现为
`_warn_liveness_policy_once()`。

### 11.4 验证

- 后端 `tests/test_frame_channel.py` 26 项：新增"无读者的订阅不注册"、"detached 通道仍拒订阅"、
  "drop 之后读者必须退出"、"endpoint 对没有读者的 conn 回 `registered:false, reason:no_reader`"；
  另修正 12 项先前"订阅先于 attach"的用例——那种状态在新不变式下不合法。
- 前端 `src/lib/api.test.js` 新增 2 项：命令被 ack 但通道不投递 → POST 必须继续自载；订阅被拒 → 重开
  通道而不是重发命令。
- 全量：后端 2149 passed；前端 46 files / 673 passed；`vite build` 通过。

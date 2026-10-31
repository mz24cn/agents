# 连接预算方案（Phase 2）

> 前置：`runtime/server.py` 的 `_ThreadingHTTPServer.request_queue_size` 已从标准库默认
> 5 提到 128（Phase 1，已落地）。那只修掉了 accept backlog 溢出导致的 1s 级 SYN 重传
> 尾部延迟，**没有**改变浏览器侧的连接预算问题。本文是 Phase 2。
>
> 修订记录：初版把 A 阶段写成"5-6 → 4 条"，那是基于"POST 流与 GET 流会并存"的错误
> 假设。实际代码里 `ChatPage.svelte:237` 已经用 `directlyConsumedSessions` 阻止了并存，
> 所以每会话今天就已经只占 1 条。A 不减少连接数，见 §2 与 §4。

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
  从未打开过的后台流式会话**不占**连接（靠 hub + flight mode）。

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

实测（`/tmp/probe_b_ledger.py`，客户端模拟 Chrome 的 6 连接/origin 池）：

| 场景 | tab 持有 socket | `/v1/models` 延迟 | 帧完整性 |
| --- | --- | --- | --- |
| 旧：1 hub + per-session SSE（N=6） | 6/6（第 6 条已开不出） | **卡死 >2s**（等空闲 socket） | — |
| 新：1 hub + 1 帧通道（N=6） | 2/6 | p50 1.3ms / p90 1.7ms / max 3.4ms，0 卡死 | 246 帧、6/6 会话顺序无缺口 |
| 新：1 hub + 1 帧通道（N=12） | 2/6 | p50 1.4ms / p90 3.5ms / max 3.6ms，0 卡死 | 492 帧、12/12 会话顺序无缺口 |

"活跃会话数增加而连接不增加"已成立；B 之后剩下的瓶颈是终端 WS（T）。

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

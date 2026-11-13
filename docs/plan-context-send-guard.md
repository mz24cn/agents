# 上下文溢出守卫：把最后一道检查移到"发送前"

> 结论先行：**推理结束后的压缩保留不变**（它拿的是供应商上报的精确 token，且不占
> 用户等待时间）；**新增的是发送前守卫**，它只对"上一次度量之后新加入的内容"负责，
> 并在确实超限时必须生效。两者分工不同，不是重复检查。

## 0. 时序漏洞

`infer_stream` 的工具循环每轮都把整个 message 列表重发一遍。既有两处压缩时机：

| 时机 | 代码 | 触发依据 |
|---|---|---|
| 会话完成 | `persist_conversation(compress=True)` | `MAX_TOKENS_IN_CONTEXT`（默认 65536） |
| 工具轮结束 | `on_round_complete` → `InLoopContextCompressor.__call__`（`server_state.py`） | 该轮 prompt+completion ≥ `ModelConfig.max_context` 的 90% |

两处量的都是**已经发出去的那次请求**（`runtime.py` 的 `_maybe_refresh_context`）。
而在那之后还会加入：

1. 本轮的工具结果（`messages.append(tool_msg)`，`runtime.py` 工具轮处）；
2. 暂停边界上插入的用户插话（`pause_gate` 的 `append`，位于 `build_request` 之前）；
3. skill 渐进披露注入的 system 消息；
4. 新一轮进来时的入口上下文（`assemble_context` 不掌握 `max_context`，只按 K=10 轮收口）。

终端轮（无工具调用）根本不经过 `on_round_complete`。所以"上一轮 60% → 本轮工具吐
40 万字符"这种突发，旧守卫看不见，provider 直接拒。

## 1. 分工

- **推理后压缩（不动）**：token 数精确；压缩发生在用户等待之外；压缩后的下一轮才是
  小上下文。
- **发送前守卫（新增）**：`Runtime._guard_context_before_send`，位置在
  `_ensure_tool_call_results` + 插话 + skill 注入之后、`protocol.build_request` 之前。
  它测的正是即将序列化的那份列表，因此结论对即将发出的请求成立。

守卫位于 `infer_stream`（单代理 / `delegate` / `talk_to` 都走它）；`group_chat` 的
`infer_stream` 调用今天没有接任何上下文钩子（见 §5）。

## 2. 预算

```
input_budget = ModelConfig.max_context − output_reserve
```

`output_reserve` 取值优先级：

1. `CONTEXT_OUTPUT_RESERVE_TOKENS`（env，覆盖一切）；
2. 模型自身 `generate_params` 里的 `max_tokens` / `max_completion_tokens` /
   `max_output_tokens`（取其中最大值）；
3. 否则 `min(8192, max(1024, window // 16))`。

保护性规则：reserve 超过窗口一半会被压到一半；算出的预算若非正数，守卫**关闭**而不是
拒发一切。`max_context == 0`（窗口未知）时守卫完全不介入，行为与今天一致。

单轮窗口（ollama 一类）把 input 与 output 记在同一个窗口上，所以不减 reserve 的
"input 没超"判断是假的。

## 3. 超限后的阶梯（每步复测，够了就停）

| 步 | 动作 | 性质 |
|---|---|---|
| 1 | 增量压缩一轮（`compress_context`，`trigger_threshold = input_budget`） | 有 LLM，但 delta 只是"最后一轮工具 + 新插话"，prompt 很小 |
| 2 | **深压缩**（`compress_context(deep=True)`）：除最后一轮外全部进摘要 | 有 LLM；把"未摘要窗口本身装不下"这段有损空间变成有摘要可查 |
| 3 | 超长消息**转储为文件**（见 §4） | 纯磁盘，无损 |
| 4 | 仍不行：不发注定被拒的请求，按既有约定 yield 一条 `Error: context too large to send: …` | 省一次失败往返 |

阶梯实现在 `InLoopContextCompressor.ensure_within_budget(messages, input_budget,
estimate, max_context)`，返回 `(replacement, tokens_after)`；`runtime` 只负责度量与
拒发，不重复实现压缩逻辑。

硬不变式：

- **最新一轮 round-group 永不消失**。`compress_context` 的 watermark 最深只到
  `len(turns) - 2`，`assemble_context` 的 tool-chain 回溯会把进行中的
  assistant(tool_calls) 拉回逐字窗口，所以模型不会丢掉它正在等的那批工具结果；
- **绝不原地改写 `Message`**。工具/插话消息对象与 handler 的 `collected_messages` 是
  同一实例，转储用 `dataclasses.replace` 复制，落盘的 conversation.json 与 UI 转录始终
  逐字保留原始工具输出；
- 协议配对不破：不产生孤立 tool_result、不留无配对 tool_call；
- 未落盘（`IncrementalConversationPersister.is_current()` 为假）时跳过"压缩+重建"
  （重建会从磁盘取，丢掉进行中的一轮），但**仍执行转储**，守卫不因 ready 为假而失效。

## 4. 超长输出转储（只在压缩决策下发生）

`context_manager.py` 的 `spill_reference / spill_content_to_file / is_spilled_content`：

- **仅当已经判定要压缩时才转储**，否则多长都不转（用户明确约束）；
- 落点 `CONTEXT_SPILL_DIR`，默认 `/tmp/agents-context-spill/<session_id>/`。选 `/tmp`
  是因为文件工具的路径校验对 workspace 外的路径只放行 `/tmp`
  （`builtin_tools_coding._validate_path`），写进上下文的引用**保证**能被代理自己读回；
- 文件名 `<label>-<sha256(content)[:16]>.txt`，内容寻址 → 天然幂等：同内容不重复写、
  已转储过的消息不会被二次转储；
- 替换文本有界（head 2000 + tail 1200 字符 + 一行引用，含原始字符数与 token 估算），
  所以它能安全放进受预算约束的压缩 prompt；
- 阈值 `_CONTEXT_SPILL_MIN_TOKENS = 4000`；每会话保留最近 32 个文件（LRU 清理）；
  带 `images` 的消息不转储；
- 两个调用点：`_build_compression_prompt`（超长 turn 不再被静默砍到 20000 字符，
  而是让摘要模型看到路径并把它记进 summary）、`ensure_within_budget` 的第 3 步
  （把超长的那条 in-flight 消息换成引用，优先最大的那条，够了即停）。

## 5. 度量成本

守卫每次发送前都要测一遍即将发出的列表，所以估算器必须便宜。`estimate_llm_tokens`
从"逐字符 Python 循环"改成"单遍正则（按连续段计数）"，分类范围共用同一份
`_CJK_TOKEN_CHAR_RANGES`，对随机混排文本逐一核对**计数完全一致**，实测速度：

| 文本 | 旧（逐字符） | 新（单遍） |
|---|---|---|
| ASCII 1.08 MB（≈27 万 token） | 0.90 s | 0.022 s |
| 中文 1.40 MB（140 万 token） | 0.99 s | 0.007 s |

即典型 128k-token 上下文（≈500 KB ASCII）的整体估算约 10 ms，相对一轮模型请求可忽略。另新增
`estimate_chat_prompt_tokens_capped(messages, tools, cap)`：只关心"是否超预算"的调用方
在越线那一刻即停走，病态大上下文上也不会白扫。`context_manager._estimate_tokens_fast`
现在直接复用同一个实现（历史上它是同一逻辑的第二份拷贝，且是慢的那份）。

结论：**不需要**为守卫维护"锚点 + 增量"记账，全量估算足够便宜，去掉一套状态机反而少
一类错（对象 id 复用、下标漂移、重建后失效判定）。

## 6. 已知未接路径

- `group_chat.py` 的 `infer_stream` 调用今天既不传 `on_round_complete` 也不传
  `on_context_overflow`（群聊按整段生成后落盘，中途重建不安全）。要让群聊吃到这套保护，
  需要先给它接上增量持久化 + `ready` 判定；当前它会走"无补救 → 只告警、仍发送"的分支，
  行为与今天一致。
- 非流式 `Runtime.infer()` 未接守卫（上下文增长只发生在流式工具循环里）。
- ollama 的 `num_ctx` 未按 `max_context` 下发，端点自身默认窗口可能小于注册表里的
  `max_context`；那是另一种"窗口未知"，客户端 input 守卫救不了。

## 7. 测试

`tests/test_in_loop_context_compression.py`（守卫段）：预算推导（env / 模型 cap / 夹半 /
窗口未知）、"上一轮小、本轮工具结果炸"被拦、插话被计入度量、补救无效时拒发且只发一次
请求、窗口未知即 no-op、无补救只告警、`ensure_within_budget` 的浅→深→转储阶梯、
转储幂等 + LRU + 非法 session_id、落盘对象逐字未被改写、
`tests/test_context_manager_compress.py`：超长 turn 转储进 prompt（含文件内容断言）与
转储失败时的截断回退。

## 8. 复盘：压缩自身被拒 + 兜底无法收敛（会话 261102_160808）

一个 4477 turn 的长会话反复报 `Error: context too large to send: about 188780 prompt
tokens do not fit the 188416`，而它的 `summary.md` / `memory.md` 都在。链条是三段独立的
故障，任何一段单独修好都不够：

1. **压缩请求自己超出了窗口。** `_build_compression_prompt` 用 `estimate_llm_tokens`
   在**估算单位**里花预算；该会话的压缩 prompt 自测 179 454 token，端点实数 242 845
   （比值 1.35：CJK + `\uXXXX` 转义被低估）。于是号称“已被窗口约束”的压缩请求每次都被
   400 拒绝，`summarized_up_to_turn` 永远停在 4036，摘要追不上历史。
   现在：`_compression_prompt_budget(window, model_id)` 先除以该模型实测的
   estimate→actual 因子（与守卫生成同一个结论，只允许更严）；被拒时
   `_learn_compression_overflow` 从错误文本里读出端点计到的 token 数，把这一次**精确**
   样本直接抬到因子上（EWMA 平滑需要 4 次同样注定失败的尝试才收敛）。离线拿该会话真
   数据回放：第 1 次同样被拒 → 学到 1.35 → 第 2 次压缩成功，`summarized_up_to_turn`
   4036 → 4466，下一次请求从 222 条 / ~189 000 token 降到 12 条 / ~4 000 token。
2. **摘要模型坏了就等于没有压缩。** 11-12 全天 `DeepSeekOfficial` 返回 402
   Insufficient Balance，`_select_compression_model` 因为它的窗口更大而一直选它，
   失败只进 backoff、不切换候选，整台机器所有会话的压缩都停了。
   未改（策略问题：用推理模型替摘要模型要烧一次很大的本地请求），留作运维决定。
3. **兜底不收敛。** `select_unsummarized_turns` 第 4 条“回溯到本轮起点”在长工具循环里
   会一路退到 floor（最后一次 user 轮在 4255），把 222 条 / ~190 000 token 全拉进窗口；
   而 `ensure_within_budget` 第 3 步只转储 ≥4000 token 的消息，这批窗口里只有 4 条达阈值，
   转储完还差 364 token → 直接拒发。它甚至把承载 summary+memory 的 system 消息也转储了。
   现在：system 消息不再是转储候选；转储榨干后 `_drop_oldest_for_budget` 按最旧顺序丢消息直到
   预算内（保持“窗口不得以孤儿 tool 结果开头”“至少一条 user”两条协议约束，并把丢了多少
   写进 system 消息，全文仍在 `conversation.json`）。

对应的回归测试：`test_compression_budget_honours_the_measured_factor`、
`test_an_oversized_rejection_teaches_the_next_attempt`、
`test_a_non_overflow_failure_teaches_nothing`、
`test_guard_never_offloads_the_summary_and_memory_message`、
`test_guard_drops_oldest_turns_when_offloading_cannot_help`。

## 9. memory.md 的字符预算（以及"已存 memory 回填"）

`## 8` 里那条会话的 memory.md 是 200 条：文件本身 60 282 字符（一半是 JSON 键名），
真正注入 system 消息的是 32 743 字符 ≈ 10 600 估算 token，**常驻每一次请求**。
条目只增不减，而整个 memory 块会被合进 system 消息。两个独立缺陷：

1. **压缩 prompt 里没有已存 memory。** 只有 `## Previous summary`，没有 memory ——
   模型不知道自己已经记过什么，每轮换一种说法重述同一条事实（该会话里"450 家医院 /
   81 家三甲 / 已完成 16 家"至少出现两次），而 `_merge_memory_entries` 只能按
   `(entry_type, content)` **精确**去重，看不见改写过的重复。
   现在压缩 prompt 多一段 `## Existing memory`（紧凑一行、取最新后缀、
   `MEMORY_CONTEXT_MAX_CHARS` 默认 16 384 字符），Task 2 明确要求"不得重述已有条目，
   被新turn推翻的事实只写当前值"；echo 计入 `overhead`，所以不会挤掉 delta。
2. **没有字符上限。** 原来只有条数上限（`_MEMORY_MAX_ENTRIES = 200`），单条长度不限。
   现在三个上限都可配（`env.json` 里写键名即可，会同步到 os.environ）：

   | 键 | 默认 | 含义 |
   |---|---|---|
   | `MEMORY_MAX_CHARS` | 32768 | memory 块在**一次请求里**的字符预算，按注入形态计费（`## Memory\n{type}: {content}`） |
   | `MEMORY_MAX_ENTRIES` | 200 | 条目数 |
   | `MEMORY_ENTRY_MAX_CHARS` | 2000 | 单条上限：一条粘贴的工具输出不该吃掉整个预算；截断带 `[entry truncated]` 标记，原文仍在 conversation.json / 转储文件里 |
   | `MEMORY_CONTEXT_MAX_CHARS` | 16384 | 压缩 prompt 看到多少已存 memory（0 = 关闭回填） |

   超限按**最旧先丢**（`_trim_memory_entries`），最新一条一定保留。裁剪发生在三处：
   `save_memory` 写入前、`_merge_memory_entries` 合并后、以及**注入点**
   （`assemble_context` 与群聊的 `_build_agent_system_messages`）——
   所以已经超限的旧会话不需要迁移：下一次请求就只注入 32768 字符，下一次压缩把文件本身裁进预算。
   丢弃是永久性的（memory.md 就是唯一存储），所以合并处会 log 一行
   `memory trimmed N -> M entries`。

实测（拿那条会话的真实 memory.md 离线跑 `_trim_memory_entries`，未写盘）：32768 的注入预算
只会丢掉最旧的 **6 条**（省下 ~250 token/请求）—— 因为它的膨胀主因是**重复**而不是条数，
所以真正起作用的是一致性修复（回填 echo + 提示词不许重述），上限只是兜底。
要更硬的限制就把 `MEMORY_MAX_CHARS` 调到 16384（该会话约 5 300 token）。

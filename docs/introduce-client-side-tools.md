# 客户端工具调用（Client Side Tool Calls）——让工具在调用方那一侧执行

玲珑 Agent Service 原本只有一类执行位置：工具跑在服务端（进程内 Function、MCP 子进程、Skill 提示词注入）。
可有些能力天生不在服务端：浏览器里才能点的按钮、用户本机才有的文件、事发线程里才存在的栈帧、
只有宿主进程才拿得到的 GPU 状态。把它们硬塞成服务端工具，要么得在被调试进程上开一个监听端口，
要么得让模型去调 CLI——两条路都在被调试方额外开口子。

`client_side` 工具类型把这件事反过来：**服务端只保留工具的注册 JSON（名称 / 描述 / 参数 schema），
把它交给模型，模型照常调用；服务端不执行，而是把这次调用原样交回客户端，由客户端本地执行后
连同同一个 `session_id` 再发一次请求，服务端把会话历史与结果合并，继续原来的推理循环。**

## 一、它是什么：一个「没有函数实体的 Function 工具」

| | Function 工具 | client_side 工具 |
|---|---|---|
| 服务端存什么 | 注册 JSON + `function_file_path` / `function_name` | **只有注册 JSON** |
| 模型看到什么 | 工具清单里的一个 function | 完全一样 |
| 谁执行 | 服务端（进程内 import 后调用） | **客户端** |
| 结果怎么回到会话 | 服务端当场写成 `role=tool` | 客户端下一次请求里带 `role=tool` |

`runtime/models.py` 里：

```python
CLIENT_SIDE_TOOL_TYPE = "client_side"
TOOL_TYPES = ("function", "mcp", "skill", CLIENT_SIDE_TOOL_TYPE)
```

`ToolConfig.is_client_side()` 是判定点；`ToolConfig.from_session_definition()` 负责把推理请求里
即时提供的定义转成 `ToolConfig`，接受两种写法并**强制** `tool_type = "client_side"`：

```json
{"name": "local_file_read", "description": "读取客户端本机文件",
 "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
```

```json
{"type": "function", "function": {"name": "local_file_read", "description": "...",
 "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
```

## 二、推理循环多了一条正常退出路径

循环的退出条件一直是两类：模型这一轮没有工具调用（**正常**），或者达到 `max_tool_rounds`、
被 abort / soft-pause（**异常**，会留一句可读的收尾文本而不是伪造 tool 结果）。
现在中间那条也是正常退出：**这一轮出现了客户端工具调用**。

关键点是**按执行侧切分同一轮**（`Runtime._partition_tool_calls_by_execution_side`）：
模型完全可能在一轮里并行调用服务端工具和客户端工具。规则是——

1. 服务端那一半照常执行（并行、远程环境、`format: "json"` 等语义都不变），结果写成 `role=tool`；
2. 客户端那一半**只留声明、不留结果**，循环 `break`，`client_tool_calls = [{id, name, arguments}, ...]`
   按声明顺序返回；
3. 因此落盘历史里会出现一条「有 `tool_calls` 但没有对应 tool 结果」的 assistant 消息——这正是
   下一轮续跑的锚点，客户端的结果就是补上它的那一块。

判定一个调用属于客户端侧，走的是 `_find_tool_by_name(call.name, scope=tools)`：先按本轮工具清单
（含会话内即时声明的）解析名字 / label / MCP 短名，认不出再回落到全局注册表——这样历史里
重放出来的陈旧调用也一定会被认成客户端调用，**绝不会在服务端被执行**。`Runtime.call_tool()`
对 `client_side` 工具同样直接返回说明文本而不是执行它。

## 三、交回与续跑：一个完整的往返

**交回（服务端 → 客户端）**

- 非流式 `POST /v1/infer`：信封里多一个字段
  `"client_tool_calls": [{"id": "call_1", "name": "local_file_read", "arguments": "{\"path\": \"…\"}"}]`
  （`messages` 里没有它们的 tool 结果；`success` 仍是 `true`）
- 流式 `POST /v1/infer/stream`：在 `usage` 帧之后、`[DONE]` 之前发一帧控制消息
  `{"role": "status", "source": "system", "status": "client_tool_calls", "client_tool_calls": [...]}`

**续跑（客户端 → 服务端）**：把结果按标准 tool 消息发回**同一个 `session_id`**：

```json
{
  "model_id": "qwen3-14b",
  "session_id": "abc123",
  "client_tools": [ {"name": "local_file_read", "description": "...", "parameters": {...}} ],
  "messages": [
    {"role": "tool", "tool_use_id": "call_1", "name": "local_file_read",
     "content": "……文件内容……"}
  ]
}
```

服务端按 `session_id` 取回会话历史，与这条 tool 结果合并成完整 message 数组继续推理。
`client_tools` 可以不重复给：会话 meta 里持久化了本次会话声明的客户端工具，后续请求缺省会原样恢复
（同名时**以会话内声明为准**，并遮蔽注册表里的同名工具）。协议自洽由既有的
`_normalize_tool_call_order()` / `_ensure_tool_call_results()` 兜底：客户端忘了补结果时，
悬空的 `tool_calls` 会被补成 `[interrupted]`，不会把不合法的请求发给上游模型。

## 四、两种注册方式

| 方式 | 怎么做 | 可见范围 | 适用 |
|---|---|---|---|
| 预注册 | Setup 页面新增/编辑工具选 `Client Side`；或 `POST /v1/tools` 带 `tool_type: "client_side"`（自动 tool_id `client_side-<name>`），推理时用 `tool_ids` 选用 | 全局，进 `ToolRegistry` 与磁盘 | 工具固定、多个会话复用、要在 UI 里管理 |
| 即时提供 | 推理请求体带 `client_tools` 数组 | **只在本次会话**，不进 `ToolRegistry`，`GET /v1/tools` 看不见 | 工具随宿主进程动态变化（内窥现场、浏览器上下文、插件清单） |

Setup / Tools 页面里客户端工具带紫色 `CLIENT` 徽标，表单不再要求 `function_file_path` / `function_name`；
「测试调用」只做 JSON 校验并明确回一句「服务端不会执行客户端工具」。

## 五、随产品附带的客户端工具：`AskUser` 与 `local_shell`

规范集中在 [`runtime/client_tools.py`](../runtime/client_tools.py)（名称、描述、参数 Schema），
并作为**内置工具**在启动时注册（`runtime/builtin_tools_misc.py` 的 `ASKUSER_TOOL_CONFIG`），
两端各自实现：浏览器 `web/src/lib/client-tools.js` + `web/src/components/chat/AskUserDialog.svelte`，
终端 `tui/client_tools.py` + `tui/ui.py` 的 `QuestionModal`。`tests/test_client_tool_catalogue.py`
把三者的名称、参数和返回文案钉在一起，防止漂移。

**`AskUser`——只有用户能拍板的事，由模型自己来问。** 参数：`question`（问题描述）、
`options`（2–8 个互斥短选项）、`timeout`（默认 `0`：不设超时，一直等用户选）、
`default_option`（倒计时结束时自动选择的选项）。返回值就是用户选中的那个选项原文，
模型可以照抄。服务端只登记这份 JSON、把它交给模型，从不执行；模型一调用，本轮推理正常结束，
调用方弹窗/选择器收集答案，再把选项作为 `role: "tool"` 结果送回同一个 `session_id` 继续推理。
它是**内置工具**（`ASKUSER_TOOL_CONFIG`，`tool_id: AskUser`，`builtin: true`），和 `write_file`
那类内置工具一样在服务启动时自动注册、带内置标记，并且**不写进 `tools.json`**
（`ToolRegistry.save()` 跳过内置）——开箱可用，不需要用户自己注册一条。
**是否交给模型，由客户端决定，
而不是每轮自动声明**：网页只在工具列表勾选了它时才在 `client_tools` 里声明，没勾选就显式传空数组——
这也会清掉会话此前记住的那份，取消勾选就是收回；TUI 没有工具列表可勾，默认声明，`--no-ask-user` 关掉
（终端里这是模型问人的唯一途径）。曾经每轮无条件声明，结果用户没启用也会被弹窗，这是错的。
用户没有选择时不会伪造答案：返回
`(the user closed the question without choosing)`，
或者超时自动选时明确标注
`(auto-selected: the user did not answer within Ns)`。

**对话框不会因为切走就丢掉。** 问题按会话排队，谁问的归谁：切到别的会话或别的页面时，
对话框只是暂时不在屏幕上，问题仍在等；切回那条会话（或回到聊天页）它就重新出现，
侧边栏会在等待回答的会话后面打一个 `❓`。倒计时只在对对话框可见的那条会话上走，
看不见时不会替用户做决定。结束一个问题只有四种途径：用户回答、用户关掉对话框、
停止那一轮、删除会话——后两种送回的是"未回答"，不会假装用户选过。

**`local_shell`——只在 TUI 且刻意开启时才存在。** `tui.py --local-shell`
（可选 `--local-shell-cwd PATH`）才会把它放进 `client_tools`，默认完全不声明；
它**故意不写进 `tools.json`**：需要注册客户端 bash 工具的场景极少，现实里基本只有
「TUI 连远程 agentsvc、要让远端模型操作本机」这一种。功能对齐服务端 `exec_shell` 的返回形状
（`exit_code` / `stdout` / `stderr` / `truncated`，超限另有 `omitted_lines`；失败为
`error` / `message`），POSIX 用 `bash --norc --noprofile -c`、Windows 用
`powershell.exe -NoProfile -NonInteractive -Command`。刻意简化：无后台模式、
无 workspace git 快照、无 abort 钩子——交回一轮不是做这些的地方。

界面上也给了它们位置，分组规则集中在 `web/src/lib/tool-groups.js` 一处：工具页按类型分组并给每组一个类型徽标（`CLIENT` / `FUNCTION` / `SKILL` / `MCP`，客户端工具排最前），聊天页的工具选择器用同一套分组与命名——此前它不认识 `client_side`，那一组只显示成裸的 `client_side`。在聊天页勾上内置的 `AskUser`（走的是 `tool_ids`，不是请求里的 `client_tools`）同样会被交回浏览器，由页面弹窗接住。

## 六、示例与测试

- [`examples/example_client_side_tools.py`](../examples/example_client_side_tools.py)：三种工具并用——
  服务端 function 工具 `workspace_stats`、预注册客户端工具 `list_dir`、会话内即时声明的 `read_local_file`。
  第一轮模型并行调用服务端与客户端两侧，服务端结果当场执行、客户端结果交回本地执行；
  第二轮不带 `client_tools`（服务端从会话 meta 恢复）带同一个 `session_id` 续跑。
- `tests/test_client_side_tools.py`：模型侧用一段本地 SSE 假端点驱动真实循环，覆盖
  注册校验、`/v1/tools/call` 不执行、混合并行轮的服务端半边执行、交回轮不执行客户端工具、
  会话 meta 持久化与恢复、会话内即时工具遮蔽注册表同名工具等。
- `tests/test_example_client_side_tools.py`：给示例脚本接一个本地模型端点，验证客户端确实本地执行、
  并行调用确实横跨两侧、会话 meta 确实持久化了即时声明的客户端工具。

## 七、把内窥服务换成客户端工具：在线调试器

改造前的 `accessories/online_debugger.py`：被调试线程里起一个只读 HTTP 内窥服务（默认 `127.0.0.1:51110`），
诊断 agent 用 `curl`/CLI 去取现场。它要监听端口、要 agent 会 shell、还要处理跨进程冷却与去重。

改造后：六个现场接口全部变成**会话内客户端工具**——

| 工具 | 在谁那里执行 | 干什么 |
|---|---|---|
| `debugger_snapshot` | 事发进程 | 取一次完整现场快照（事件 / 栈帧 / 局部变量 / 源码上下文 / 工作目录） |
| `debugger_event` | 事发进程 | 按 `eid` 取某次现场快照（现场字典常驻内存，不重复序列化） |
| `debugger_frame` | 事发进程 | 取某帧的局部变量与源码上下文 |
| `debugger_source` | 事发进程 | 读涉事文件片段（可越出崩溃点看调用方） |
| `debugger_eval` | 事发进程 | 在事发帧里对表达式求值（默认只读，`allow_exec=true` 才允许语句） |
| `debugger_status` | 事发进程 | 读宿主进程状态（线程 / 内存 / 已登记现场数） |

崩溃时 `report()` 在**事发线程**里打包快照，连同提示词一次 `POST /v1/infer` 交给 agent；
模型想深挖时发出的 `debugger_*` 调用被交回事发进程本地执行，结果带同一个 `session_id` 续跑——
**没有监听端口、没有 CLI 调用**，跨线程/跨进程的边界由会话续跑承载。
`tests/test_online_debugger.py` 用假模型端点验证：模型两次交回、两次都在本地执行、
三次请求都带同一个 `session_id`、最终报告交回宿主。

同一份实现另有 JavaScript 版 [`accessories/online_debugger.js`](../accessories/online_debugger.js)：
零依赖（Node 内置 `http` / `vm` / `crypto`），`node accessories/online_debugger.js --selftest` 跑 31 项自检，
覆盖快照采集、本地执行交回、SSE 帧重建（并行调用分帧拼接、按工具名识别交回、交回清单取
「服务端点名的 ∪ 宣布过但无回执的」并按 id 去重）、交回轮数上限、指纹带行号、Bearer 鉴权头、
`workspace` 显式覆盖、诊断契约里的红→绿验证步骤、纯传输故障的一次重试。
两版的配置项与默认值逐一对齐（同名环境变量，18 项一致），改动同步维护。

## 八、边界

- 交回是**成功**结局：`success=true`、无 `error`，会话停在 assistant 的工具调用那一轮；
  如果客户端再也不回来，这条会话就停在「中断的会话」状态，Web UI 的「继续推理」或 `"continue": true` 可清掉重发。
- 交回轮数由客户端自己封顶（调试器默认 30 轮，Python 与 JS 版都有），服务端不额外限制：
  每一轮都还受服务端自身的 `max_tool_rounds` 约束。
- `debugger_eval` 会在事发进程里求值——这是客户端的明确授权行为，默认禁用语句执行、
  默认跳过 `self / conn / session` 一类对象；不需要就把这个工具从清单里去掉。
- 工具名冲突时会话内声明优先（同名注册表工具本轮会被它替换）；不同名则两组工具都在清单里。

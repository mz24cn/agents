#!/usr/bin/env node
/**
 * online_debugger.js — Node 进程「异常快照 + client side tool calls 诊断」最小实现。
 * （accessories/online_debugger.py 的 JavaScript 版本，零第三方依赖，Node 18+）
 *
 * 接入方式（和 Python 版一一对应）：
 *     const debuggerx = require('./online_debugger.js');
 *     debuggerx.start({ project: 'X7991' });        // 进程启动时调用一次（幂等）
 *     ...
 *     try {
 *         ...
 *     } catch (e) {
 *         debuggerx.report('步骤名', e, { order_id, items });   // 第三个参数是可选现场字典
 *     }
 *
 * 组成：
 * 1. 异常快照：解析 V8 调用栈（文件/行/列/函数），把涉事源码的上下文行读进快照（Python 版是
 *    读 f_locals，见下面"语言差异"），加上 error 自有属性、cause 链、report() 传入的现场字典。
 * 2. 现场查询 = client side tool calls（核心机制，与 Python 版完全一致）：
 *      debugger_status / debugger_events / debugger_event / debugger_frame /
 *      debugger_threads / debugger_eval（可选，默认开）
 *    这些工具在服务端（agent service）只有注册 JSON、没有任何可执行实体；模型一旦调用它们，
 *    推理循环**正常退出**并把调用交回本进程，本进程就地执行，再把结果（role="tool" +
 *    tool_use_id）连同同一个 session_id 发回 /v1/infer，服务端按 session id 合并历史继续推理。
 *    ⇒ 不需要监听端口，不需要给 agent 配 exec_shell/CLI，也不需要它 curl 本机。
 * 3. 诊断闭环：把「快照 + 现场工具说明」拼成 user prompt，POST {agents_url}/v1/infer
 *    （agent_ids=["Coder"] 或 model_id，workspace=事发目录，session_id="new"），按上面的交回
 *    机制反复续跑，直到模型给出最终报告（## 问题 / ## 根因 / ## 修改 / ## 验证/风险 /
 *    ## 后续动作）打印到 stdout；会话全程由 agentsvc 按 session_id 存档。
 * 4. 错误指纹：错误名 + 栈帧函数序列的 md5；同一指纹进程内只派发一次；sync 模式另落一个
 *    跨进程冷却文件（~/.debugger_cooldown.json）。
 *
 * 语言差异（V8 决定，不是实现偷懒）：
 *  - V8 的栈里没有 Python 那样的 f_locals，抓不到"案发瞬间的局部变量"。所以快照改抓
 *    涉事源码上下文行（±6 行）+ error 自有属性 + cause 链；要看具体值，两种办法：
 *      a) report(step, err, {…}) 把关心的变量显式带进快照（推荐，最便宜）；
 *      b) 让模型调 debugger_eval 在本进程作用域求值。
 *  - Node 没有同步 HTTP：async 模式用 fetch；sync 模式（要把事件循环冻在案发点）用 curl
 *    子进程发请求，本地工具执行仍在**本进程**里跑，快照与进程状态都保持冻结。
 *
 * 配置优先级：start() 参数 > 环境变量 > 默认值（变量名与 Python 版完全相同）
 *     DEBUGGER_ENABLED=0 / DEBUGGER_AGENT_ENABLED=0
 *     DEBUGGER_AGENTS_URL=http://localhost:7988
 *     DEBUGGER_AGENT_ID=Coder      （设成空串则改用 DEBUGGER_MODEL_ID）
 *     DEBUGGER_MODEL_ID=xxx
 *     DEBUGGER_MAX_ROUNDS=30       可选：工具轮数上限；默认不设，交给 agent service 自身
 *     DEBUGGER_HANDOFF_ROUNDS=80   客户端工具交回轮次上限（防会话无限往返）。
 *                                  服务端 max_tool_rounds 只数单个请求内的服务端轮次：
 *                                  纯交回轮不计数、续跑请求重新计数，所以这条上限实际
 *                                  由调用方掌握（真机 2026-11-08：30 曾被正好跑满）。
 *     DEBUGGER_VERIFY=0            不要求红→绿复现验证（默认要求：改了代码就得先写能在
 *                                  改动前失败的复现，改后跑通，命令与退出码进报告）
 *     DEBUGGER_API_KEY=...         服务侧需要 Authorization: Bearer 时填（只进请求头）
 *     DEBUGGER_WORKSPACE=/repo     会话 workspace；跨端诊断必须显式给仓库路径，否则
 *                                  用的是端上推导出的目录，服务端 read_file 读不到
 *     DEBUGGER_STREAM=1            用 /v1/infer/stream（SSE）跑诊断（仅 async 模式）
 *     DEBUGGER_EVAL=0              禁用 debugger_eval
 *     DEBUGGER_SYNC=1              report() 就地同步诊断（默认关）
 *     DEBUGGER_SYNC_TIMEOUT=900    sync 诊断最长等待（秒）
 *     DEBUGGER_COOLDOWN_SEC=1800   sync 模式同指纹跨进程冷却窗口（秒）
 *     DEBUGGER_AGENT_TIMEOUT=1200  单次请求超时（秒）
 *
 * 自检（离线，不连任何服务）：
 *     node accessories/online_debugger.js --selftest
 * 真跑一次（需要可用的 agent service）：
 *     node accessories/online_debugger.js --agents-url http://localhost:7988 \
 *         --agent-id Coder --sync
 */

'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

// ---------------------------------------------------------------- 配置与全局状态

const DEFAULTS = {
  project: null,
  agents_url: 'http://localhost:7988',
  agent_id: 'Coder',
  model_id: '',            // agent_id 为空串时用这个
  max_rounds: null,
  handoff_rounds: 80,
  agent_timeout: 1200,
  agent_enabled: true,
  eval_enabled: true,
  stream: false,
  max_events: 32,
  sync: false,
  sync_timeout: 900,
  cooldown_sec: 1800,
  cooldown_file: '~/.debugger_cooldown.json',
  api_key: '',
  workspace: '',
  verify_fix: true,
};

const CFG = Object.assign({}, DEFAULTS);
CFG.started = null;

const EVENTS = new Map();          // eid  -> event（完整快照）
const FINGERPRINTS = new Map();    // fp   -> {count, first_ts, last_ts, eid, dispatched, reported}
const FRAME_CACHE = new Map();     // fid  -> frame snapshot（插入序即 LRU）
const FRAME_CACHE_MAX = 512;
const QUEUE = [];                  // async 模式待派发的event
const STATE = { started: false, worker: false, dispatch_ok: 0, dispatch_fail: 0, handoffs: 0 };
const SINK = [];                   // 观察模式（agent_enabled=false）下攒着的事件

// ---------------------------------------------------------------- 小工具

const now = () => new Date().toISOString().replace('T', ' ').slice(0, 19);
const md5 = (s) => crypto.createHash('md5').update(String(s)).digest('hex');
const print = (msg) => { try { process.stdout.write(`[debugger] ${msg}\n`); } catch (_) { /* ignore */ } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function sleepSync(ms) {
  /** sync 模式（curl 子进程那条路）里的阻塞等待；Atomics.wait 是 Node 的同步睡法。 */
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch (_) {
    const until = Date.now() + ms;
    execFileSync('sleep', [String(Math.ceil(ms / 1000))]);
    while (Date.now() < until) { /* 兜底 */ }
  }
}

function headers() {
  /** 令牌只进请求头：不进日志、不进事件、不进自检输出（与 Python 版 _headers 一致）。 */
  const h = { 'Content-Type': 'application/json' };
  if (CFG.api_key) h.Authorization = `Bearer ${CFG.api_key}`;
  return h;
}

function safeRepr(value, limit = 200) {
  let text;
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value);
    if (text === undefined) text = String(value);
  } catch (e) {
    return `<repr失败 ${e && e.name}: ${e && e.message}>`;
  }
  if (text.length > limit) text = `${text.slice(0, limit)}…(共${text.length}字符)`;
  return text;
}

function expandHome(p) {
  return p.startsWith('~') ? path.join(os.homedir(), p.slice(1)) : p;
}

// ---------------------------------------------------------------- 快照

function parseStack(err, selfFile) {
  const raw = String((err && err.stack) || '');
  const out = [];
  for (const line of raw.split('\n')) {
    const m = /^\s*at (?:(.*?) \()?(?:async )?([^\s()]+):(\d+):(\d+)\)?\s*$/.exec(line);
    if (!m) continue;
    const [, fn, file, lineNo, col] = m;
    if (selfFile && file.endsWith(path.basename(selfFile))) continue;
    if (file.startsWith('node:')) continue;
    out.push({
      func: fn && fn.length ? fn : '<anonymous>',
      file,
      line: Number(lineNo),
      column: Number(col),
    });
    if (out.length >= 12) break;
  }
  return out;
}

function sourceContext(file, line, span = 6) {
  let lines;
  try {
    lines = fs.readFileSync(file, 'utf8').split('\n');
  } catch (_) {
    return null;
  }
  const from = Math.max(1, line - span);
  const to = Math.min(lines.length, line + span);
  const ctx = [];
  for (let i = from; i <= to; i++) ctx.push({ line: i, text: lines[i - 1] });
  return ctx;
}

function captureFrame(frame) {
  const fid = md5(`${frame.func}@${frame.file}:${frame.line}:${frame.column}`).slice(0, 12);
  const snap = {
    fid,
    file: frame.file,
    line: frame.line,
    column: frame.column,
    func: frame.func,
    src: (sourceContext(frame.file, frame.line, 0) || [{ text: '' }])[0].text.trim(),
  };
  const ctx = sourceContext(frame.file, frame.line);
  if (ctx) snap.context = ctx;
  if (!FRAME_CACHE.has(fid)) FRAME_CACHE.set(fid, snap);
  while (FRAME_CACHE.size > FRAME_CACHE_MAX) {
    FRAME_CACHE.delete(FRAME_CACHE.keys().next().value);
  }
  return snap;
}

function errorChain(err) {
  const parts = [];
  const seen = new Set();
  let cur = err;
  while (cur && parts.length < 4 && !seen.has(cur)) {
    seen.add(cur);
    parts.push({ type: cur.name || 'Error', msg: String(cur.message || cur).slice(0, 1200) });
    cur = cur.cause;
  }
  return parts;
}

function errorOwnProps(err) {
  const out = {};
  for (const key of Object.keys(err || {})) {
    if (key === 'stack' || key === 'message') continue;
    out[key] = safeRepr(err[key]);
  }
  return out;
}

function makeFingerprint(name, frames) {
  // 行号必须在指纹里：同一条调用链里同一个函数的不同落点（例如某个大函数里有五处会走
  // 到同一个下游调用）过去会撞成同一个指纹，于是后到的未知变体被第一条 report 吞掉，
  // 连现场快照都不留。带上行号会让去重变弱（同一逻辑分支改动行号后算新错误），这是取舍。
  const basis = name + '|' + frames
    .map((f) => `${f.func}@${path.basename(f.file)}:${f.line || 0}`)
    .join('|');
  return md5(basis).slice(0, 12);
}

function deriveWorkspace(frames) {
  // 工作区 = 事发源码所在目录：自最内层帧向外，第一个不在 node_modules 的真实文件。
  for (let i = frames.length - 1; i >= 0; i--) {
    const f = frames[i].file || '';
    if (!f || f.startsWith('node:') || f.includes('node_modules')) continue;
    if (!fs.existsSync(f)) continue;
    return path.dirname(f);
  }
  return process.cwd();
}

// ---------------------------------------------------------------- report()

function report(step, err, context) {
  /** 在 catch 块里调用：抓案发快照，按指纹去重后派发（async 入队 / sync 就地）。永不抛异常。 */
  try {
    if (process.env.DEBUGGER_ENABLED === '0') return null;
    if (!STATE.started) start({});
    const frames = parseStack(err, __filename).map(captureFrame);
    const chain = errorChain(err);
    const fp = makeFingerprint((err && err.name) || 'Error', frames);
    const eid = md5(`${fp}-${Date.now()}-${Math.random()}`).slice(0, 8);
    const ts = now();
    const event = {
      eid,
      fingerprint: fp,
      step: String(step),
      type: (err && err.name) || 'Error',
      msg: String((err && err.message) || err).slice(0, 1500),
      chain,
      frames,
      error_props: errorOwnProps(err),
      context: context === undefined ? undefined : safeRepr(context, 4000),
      context_object: context,
      ts,
      pid: process.pid,
      count: 1,
      project: CFG.project || path.basename(process.cwd()),
      workspace: CFG.workspace || deriveWorkspace(frames),
      stack: String((err && err.stack) || '').slice(0, 4000),
    };
    let dispatch = false;
    const rec = FINGERPRINTS.get(fp);
    if (!rec) {
      FINGERPRINTS.set(fp, { count: 1, first_ts: ts, last_ts: ts, eid, dispatched: false, reported: false });
      EVENTS.set(eid, event);
      while (EVENTS.size > CFG.max_events) EVENTS.delete(EVENTS.keys().next().value);
      dispatch = !!CFG.agent_enabled;
    } else {
      rec.count += 1;
      rec.last_ts = ts;
      event.count = rec.count;
    }
    if (dispatch && CFG.sync) {
      dispatchSync(event);
    } else if (dispatch) {
      if (QUEUE.length >= 64) {
        print(`诊断队列已满，丢弃 eid=${eid}（${step}）`);
      } else {
        QUEUE.push(event);
        drainQueue();
      }
    } else if (CFG.agent_enabled) {
      print(`指纹 ${fp} 已派发过，本轮仅计数（累计 ${FINGERPRINTS.get(fp).count} 次）`);
    } else {
      SINK.push(event);
    }
    return event;
  } catch (e) {
    print(`report 内部异常: ${e && e.stack}`);
    return null;
  }
}

// ---------------------------------------------------------------- 现场接口（客户端工具的实现体）

function statusInfo() {
  const mem = process.memoryUsage();
  const active = typeof process._getActiveHandles === 'function' ? process._getActiveHandles().length : null;
  return {
    pid: process.pid,
    project: CFG.project,
    node: process.version,
    platform: `${process.platform}/${process.arch}`,
    started: CFG.started ? new Date(CFG.started * 1000).toISOString() : null,
    uptime_sec: Math.round(process.uptime()),
    cwd: process.cwd(),
    rss_mb: Math.round(mem.rss / 1048576),
    heap_used_mb: Math.round(mem.heapUsed / 1048576),
    active_handles: active,
    loaded_modules: Object.keys(require.cache).length,
    events_stored: EVENTS.size,
    fingerprints: FINGERPRINTS.size,
    queued: QUEUE.length,
    dispatch_ok: STATE.dispatch_ok,
    dispatch_fail: STATE.dispatch_fail,
    client_tool_handoffs: STATE.handoffs,
    agent: `${CFG.agent_id || CFG.model_id}@${CFG.agents_url}`,
    max_rounds: CFG.max_rounds,
    agent_enabled: CFG.agent_enabled,
    eval_enabled: CFG.eval_enabled,
    sync: CFG.sync,
    workspace: CFG.workspace || null,
    auth: CFG.api_key ? 'Bearer' : 'none',   // 只回显形态，不回显令牌
    verify_fix: CFG.verify_fix,
  };
}

function threadsInfo() {
  // Node 不提供枚举 worker_threads 的 API；主线程信息直接给出，worker 需宿主登记。
  let wt = null;
  try {
    wt = require('worker_threads');
  } catch (_) { /* 某些运行时没有 worker_threads */ }
  return {
    main: { threadId: wt ? wt.threadId : 0, is_main: wt ? wt.isMainThread : true, status: 'unknown' },
    workers: Array.from(registeredWorkers.values()),
    note: 'Node 无法枚举 worker_threads；如需实时状态请对每个 worker 调 registerWorker()',
  };
}

const registeredWorkers = new Map();

function registerWorker(worker, meta) {
  /** 宿主可选登记：让 debugger_threads 能看到某个 worker 的身份信息。 */
  try {
    registeredWorkers.set(worker, Object.assign(
      { threadId: worker.threadId, name: worker.name || 'worker' }, meta || {}));
  } catch (_) { /* ignore */ }
}

function eventsSummary() {
  const events = [];
  for (const e of EVENTS.values()) {
    const rec = FINGERPRINTS.get(e.fingerprint) || {};
    events.push({
      eid: e.eid, fingerprint: e.fingerprint, step: e.step, type: e.type,
      msg: String(e.msg).slice(0, 200), ts: e.ts, count: rec.count || 1,
      dispatched: rec.dispatched, session_id: e.session_id,
    });
  }
  const fingerprints = {};
  for (const [fp, rec] of FINGERPRINTS) fingerprints[fp] = rec.count;
  return { events, fingerprints };
}

function frameSnapshot(fid) {
  const snap = FRAME_CACHE.get(fid);
  if (!snap) return { error: `fid=${fid} 不在缓存（先从 debugger_event 或 debugger_threads 取）` };
  FRAME_CACHE.delete(fid); FRAME_CACHE.set(fid, snap);   // LRU touch
  return Object.assign({ fid, queried_at: now() }, snap);
}

function evalExpr(expr) {
  if (!CFG.eval_enabled) return { ok: false, error: 'debugger_eval 已被 DEBUGGER_EVAL=0 关闭' };
  if (!String(expr || '').trim()) return { ok: false, error: 'expr 必填' };
  try {
    // 只在本进程内求值；能看到 debugger 自身状态，以及 globalThis 上的宿主对象。
    const scope = { EVENTS, FINGERPRINTS, FRAME_CACHE, CFG, STATE, QUEUE, SINK, statusInfo, eventsSummary };
    const result = new Function(...Object.keys(scope), `return (${expr});`)(...Object.values(scope));
    return { ok: true, scope: 'debugger 模块作用域', expr, result: safeRepr(result, 4000) };
  } catch (e) {
    return { ok: false, scope: 'debugger 模块作用域', expr, error: `${e.name}: ${e.message}` };
  }
}

const CLIENT_TOOLS = [
  {
    name: 'debugger_status',
    description: '目标进程概况：pid、Node 版本、运行时长、内存、活跃句柄数、事件数、诊断派发统计',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'debugger_events',
    description: '已捕获的错误事件列表：eid、指纹、出现次数、是否已派发、session_id',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'debugger_event',
    description: '某个错误事件的完整快照（cause 链 + 各帧源码上下文 + error 自有属性 + 现场字典）',
    parameters: {
      type: 'object',
      properties: { eid: { type: 'string', description: '事件 id' } },
      required: ['eid'],
    },
  },
  {
    name: 'debugger_frame',
    description: '指定 fid 的栈帧详情：文件/行/列/函数 + 涉事源码上下文行（±6 行）',
    parameters: {
      type: 'object',
      properties: { fid: { type: 'string', description: '帧 id' } },
      required: ['fid'],
    },
  },
  {
    name: 'debugger_threads',
    description: '主线程与已登记 worker 的信息（Node 无法枚举 worker_threads）',
    parameters: { type: 'object', properties: {} },
  },
  {
    name: 'debugger_eval',
    description: '在目标进程内求值一个 JavaScript 表达式（能看到 debugger 自身状态与 globalThis）',
    parameters: {
      type: 'object',
      properties: { expr: { type: 'string', description: '表达式' } },
      required: ['expr'],
    },
  },
];

function clientToolsPayload() {
  return CFG.eval_enabled
    ? CLIENT_TOOLS.map((t) => t)
    : CLIENT_TOOLS.filter((t) => t.name !== 'debugger_eval');
}

const CLIENT_HANDLERS = {
  debugger_status: () => statusInfo(),
  debugger_threads: () => threadsInfo(),
  debugger_events: () => eventsSummary(),
  debugger_event: (a) => EVENTS.get(a.eid || '') || { error: `eid=${a.eid} 不存在或已被环形淘汰` },
  debugger_frame: (a) => frameSnapshot(a.fid || ''),
  debugger_eval: (a) => evalExpr(a.expr),
};

function executeClientTool(name, args) {
  /** 本地执行一个客户端工具调用，返回结果字符串（作为 role="tool" 内容回传）。 */
  if (typeof args === 'string') {
    try { args = JSON.parse(args || '{}'); } catch (_) { args = {}; }
  }
  if (!args || typeof args !== 'object') args = {};
  const handler = CLIENT_HANDLERS[name];
  if (!handler) return JSON.stringify({ error: `本进程未实现客户端工具 ${name}` });
  let payload;
  try {
    payload = handler(args);
  } catch (e) {
    payload = { error: `${e.name}: ${e.message}` };   // 现场查询失败也要正常回答这次调用
  }
  return JSON.stringify(payload, (k, v) => (v instanceof Error ? String(v) : v));
}

// ---------------------------------------------------------------- Coder agent 调用

const SKILL_TMPL = (p) => `你是运行时故障诊断代理，代号 ${p.agent_id}。目标程序 ${p.project}（pid=${p.pid}，启动于 ${p.started}）\
内置了 debugger，把「进程现场查询」以 **client side tool calls** 的形式提供给你：这些工具在服务端\
只有注册 JSON、不会由服务端执行，你调用它们时推理循环会正常结束，由目标进程就地执行并把结果回传到\
同一个会话继续。诊断必须基于这些工具返回的第一手现场数据，不要凭空推理。

可用现场工具（就在本会话的工具列表里）：
  debugger_status                      进程概况与统计（Node 版本、内存、活跃句柄、事件数）
  debugger_events                      已捕获错误事件列表：eid、指纹、出现次数、是否已派发
  debugger_event(eid)                  某错误的完整快照（cause 链 + 各帧源码上下文 + error 属性 + 现场字典）
  debugger_frame(fid)                  某帧的文件/行/列/函数 + 涉事源码上下文行
  debugger_threads                     主线程与已登记 worker 的信息
  debugger_eval(expr)                  在目标进程内求值 JS 表达式（能看到 debugger 自身状态与 globalThis）
说明：V8 抓不到案发瞬间的局部变量，所以快照带的是涉事源码上下文与 report() 传入的现场字典；
需要具体值就调 debugger_eval（例如读 globalThis 上的对象）；现场查询只读，不会改变进程状态。
工作区：本次会话 workspace 已设为 ${p.workspace}，你在服务端自己的工具（exec_shell 的 cwd、
read_file/search_code/edit_file）都在该目录下工作，源码请直接读文件，不必经 debugger。
注意：服务端工具由服务端执行，现场工具由目标进程执行，两者可以在同一轮并行调用里混用。

任务：
1) 先读下方异常快照，缺什么现场数据就用现场工具补什么（允许多轮）。
2) 用 read_file / search_code 阅读涉事源码，定位根因。
3) 处置规则：
   - 代码缺陷且有把握修复：用 edit_file 直接做最小化修改（改动文件即可，勿重启进程）。
   - 网络抖动/限流/对端瞬时错误：不要改代码，给出重试/退避/降级建议。
   - 涉及数据库写入、外部系统配置的动作：只写建议，不要执行。
${p.verify_step}5) 最后一条消息严格按以下格式输出（本次会话由 agentsvc 按 session_id 全程存档）：
## 问题
（一句话现象 + 触发条件）
## 根因
（基于现场数据的判断，引用你查到的帧/属性/表达式结果）
## 修改
（文件路径 + 改动摘要；未改代码则写"未修改"并说明理由）
## 验证/风险
${p.verify_hint}
## 后续动作
（如：需重启进程生效 / 建议重跑哪些任务 / 无需动作；**若改了代码，必须点明「改动尚未进入运行态，
需重启或热重载才对当前流量生效」**，以及建议重跑哪些任务来确认）

本次异常快照（eid=${p.eid}，step=${p.step}）：
\`\`\`json
${p.snapshot}
\`\`\`
`;

function buildPrompt(event) {
  const slim = Object.assign({}, event);
  delete slim.stack;
  delete slim.context_object;
  const workspace = event.workspace || process.cwd();
  // 红→绿这一步过去不在任务列表里，只出现在报告格式里：那一段本来就有，但它只被要求
  // *描述*怎么验，于是模型改完 edit_file 就交报告、把这段写成计划，自愈链停在「补丁落在磁盘」。
  // 现在把它做成一条有终态的动作：改动前失败、改动后通过，命令与退出码作为证据。
  const verifyStep = CFG.verify_fix
    ? `4) 改了代码就必须做红→绿验证：先用 exec_shell 写一条能复现本次故障的最小复现
   （在 ${workspace} 下，用同一输入跑出快照里那个异常类型与栈顶位置），确认它在改动**前**失败；
   打上改动，重跑同一条命令确认通过。复现命令与两次退出码写进「## 验证/风险」作为证据。
   复现不出来就写「未验证：<原因>」，不要把「打算怎么验」写成「已经验过」。
   只给建议、没有改代码的（瞬时错误、数据库、外部配置）跳过这一步，直接说明依据。
`
    : `4) 本次不要求执行验证（DEBUGGER_VERIFY=0）：在「验证/风险」里写清该怎样验，并注明「未执行验证」。\n`;
  const verifyHint = CFG.verify_fix
    ? `先给证据：复现命令、改动前失败（异常类型 / 退出码）、改动后通过（退出码）；没有证据就写「未验证：<原因>」。另注可能的副作用`
    : `（打算如何确认修复有效，并注明「未执行验证」；可能的副作用）`;
  return SKILL_TMPL({
    agent_id: CFG.agent_id || CFG.model_id,
    project: event.project || '?',
    pid: event.pid,
    started: CFG.started ? new Date(CFG.started * 1000).toISOString() : now(),
    eid: event.eid,
    step: JSON.stringify(event.step),
    workspace,
    snapshot: JSON.stringify(slim, null, 1),
    verify_step: verifyStep,
    verify_hint: verifyHint,
  });
}

function identityPayload() {
  if (CFG.agent_id) return { agent_ids: [CFG.agent_id] };
  return { model_id: CFG.model_id };
}

function firstPayload(prompt, workspace) {
  const payload = Object.assign({}, identityPayload(), {
    // 必须用 messages：agent 带 system_prompt 时服务端会组装 messages，裸 text 字段会被挤掉。
    messages: [{ role: 'user', content: prompt }],
    workspace,
    // "new"：让 agentsvc 新建会话并完整记录全过程（含每轮工具调用）。
    session_id: 'new',
    // 客户端工具：只随本次会话下发，不进服务端 ToolRegistry，服务端也不会执行它们。
    client_tools: clientToolsPayload(),
  });
  if (CFG.max_rounds) payload.max_tool_rounds = Number(CFG.max_rounds);
  return payload;
}

function resumePayload(sessionId, workspace, results) {
  /** 本地执行结果 + 会话 session id：服务端按 session id 合并历史继续推理。 */
  const payload = Object.assign({}, identityPayload(), {
    workspace,
    session_id: sessionId,
    // 工具集要和首轮一致（服务端不替请求补工具）；client_tools 重复带一次最稳妥——
    // 服务端也会从会话记录里恢复，显式带上则不依赖会话 meta 的存续。
    client_tools: clientToolsPayload(),
    messages: results,
  });
  if (CFG.max_rounds) payload.max_tool_rounds = Number(CFG.max_rounds);
  return payload;
}

// ---------------------------------------------------------------- 传输层

function mergeToolCallFrames(calls) {
  /** 把流式 assistant 帧里被切碎的工具调用片段拼回完整调用（按 _index 归并）。 */
  const merged = new Map();
  calls.forEach((call, i) => {
    const key = call._index !== undefined ? call._index
      : (call.index !== undefined ? call.index : (call.id || i));
    const slot = merged.get(key) || { id: null, name: '', arguments: '' };
    if (call.id) slot.id = call.id;
    const nested = call.function || {};
    const name = call.name || nested.name;
    if (name) slot.name += name;
    let args = call.arguments !== undefined ? call.arguments : nested.arguments;
    if (args && typeof args === 'object') args = JSON.stringify(args);
    if (args) slot.arguments += args;
    merged.set(key, slot);
  });
  return Array.from(merged.values()).filter((c) => c.name || c.id);
}

function inferPath() {
  return CFG.stream ? '/v1/infer/stream' : '/v1/infer';
}

async function postAsync(payload) {
  /** 非流式或流式（fetch + SSE 解析），返回 {status, envelope}。 */
  const url = CFG.agents_url + inferPath();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CFG.agent_timeout * 1000);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const raw = await res.text();
    if (!res.ok) return { status: res.status, envelope: safeParse(raw) || { error: raw.slice(0, 800) } };
    if (!CFG.stream) return { status: res.status, envelope: safeParse(raw) || {} };
    return { status: res.status, envelope: collectStream(raw) };
  } catch (e) {
    return { status: 0, envelope: { error: `调用 ${url} 失败: ${e}` } };
  } finally {
    clearTimeout(timer);
  }
}

function postSync(payload) {
  /** sync 模式：用 curl 子进程发同步请求（Node 没有同步 HTTP），只走非流式。 */
  const url = CFG.agents_url + '/v1/infer';
  try {
    const raw = execFileSync(
      'curl',
      ['-sS', '-X', 'POST', url, '-H', 'Content-Type: application/json']
        .concat(CFG.api_key ? ['-H', `Authorization: Bearer ${CFG.api_key}`] : [])
        .concat(['--max-time', String(CFG.agent_timeout), '--data-binary', '@-']),
      { input: JSON.stringify(payload), maxBuffer: 32 * 1024 * 1024, encoding: 'utf8' },
    );
    const parsed = safeParse(raw);
    if (!parsed) return { status: 502, envelope: { error: String(raw).slice(0, 800) } };
    // curl 不区分状态码：失败推理也带 JSON 信封，用 success 字段判断。
    return { status: parsed.success === false ? 500 : 200, envelope: parsed };
  } catch (e) {
    return { status: 0, envelope: { error: `调用 ${url} 失败: ${e.message}` } };
  }
}

function safeParse(text) {
  try { return JSON.parse(text); } catch (_) { return null; }
}

function collectStream(raw) {
  /** 把 SSE 文本重建成与 /v1/infer 同构的信封（含 client_tool_calls）。 */
  const envelope = { messages: [], client_tool_calls: [] };
  const names = new Set(clientToolsPayload().map((t) => t.name));
  const announced = [];
  const answered = new Set();
  let reported = [];
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('data: ') || trimmed === 'data: [DONE]') continue;
    const frame = safeParse(trimmed.slice(6));
    if (!frame) continue;
    if (frame.session_id) envelope.session_id = frame.session_id;
    const role = frame.role;
    if (role === 'assistant' && frame.tool_calls) {
      announced.push(...mergeToolCallFrames(frame.tool_calls));
    } else if (role === 'tool') {
      answered.add(frame.tool_use_id);
    } else if (role === 'status' && frame.status === 'client_tool_calls') {
      reported = frame.client_tool_calls || [];
    }
    if (!role) continue;                       // init / usage 等控制帧不是 message
    const prev = envelope.messages[envelope.messages.length - 1];
    if (role === 'assistant' && !frame.tool_calls && prev && prev.role === 'assistant'
        && !prev.tool_calls) {
      prev.content = (prev.content || '') + (frame.content || '');   // 正文分帧下发，拼回一条
      if (frame.thinking) prev.thinking = (prev.thinking || '') + frame.thinking;
      continue;
    }
    envelope.messages.push(Object.assign({}, frame));
  }
  envelope.success = true;
  // 交回清单必须是「服务端点名的 ∪ 宣布过但没有回执的」，不能二选一：只取 status 帧的话，
  // 同一轮里服务端没点名的那几条客户端调用永远不会被执行——真机上四条会话在中段各缺一枚
  // debugger_eval，而推理照常收尾，现场缺一块数据模型却已经下了结论。
  const fallback = announced.filter((c) => names.has(c.name) && !answered.has(c.id));
  const byId = new Map();
  for (const call of reported.concat(fallback)) {
    if (!call || !(call.name || call.id)) continue;
    const key = call.id || `anon:${call.name}`;
    const prev = byId.get(key);
    // 同一个 id 只执行一次；两处来源里缺参数的那份让位给带参数的那份。
    byId.set(key, prev && !prev.arguments && call.arguments ? call : (prev || call));
  }
  envelope.client_tool_calls = Array.from(byId.values());
  return envelope;
}

// ---------------------------------------------------------------- 诊断闭环

function localResults(pending) {
  /** 交回来的调用全部在本地执行，返回待回传的 role="tool" 消息数组。 */
  return pending.map((call) => ({
    role: 'tool',
    name: call.name,
    tool_use_id: call.id,
    content: executeClientTool(call.name, call.arguments),
  }));
}

async function callAgent(prompt, workspace, post = postAsync) {
  /** 跑完整诊断闭环（含多轮客户端工具交回），返回 {ok, text, session_id}。
   *  post 可注入（默认走网络），便于离线自检与自定义传输。 */
  let payload = firstPayload(prompt, workspace);
  let sessionId = null;
  let final = null;
  let handoffRound = 0;
  for (;;) {
    let { status, envelope } = await post(payload);
    // 只重试一次，且只重试「我们自己的传输层失败」（status=0 + 自己拼的「调用 … 失败:」），
    // 不重试 HTTP 4xx/5xx——那通常是参数或路由问题，重试只会把现场埋得更深。不重试的话
    // 会话就永远停在 ⏳ 那一帧，端上连一条失败原因都留不下。
    if (status === 0 && String(envelope.error || '').startsWith('调用 ')) {
      print('推理调用传输故障，2s 后重试一次');
      await sleep(2000);
      ({ status, envelope } = await post(payload));
    }
    sessionId = envelope.session_id || sessionId;
    if (status !== 200) {
      return { ok: false, text: String(envelope.error || `HTTP ${status}`).slice(0, 2000), session_id: sessionId };
    }
    for (const m of envelope.messages || []) {
      if (m.role === 'assistant' && String(m.content || '').trim()) final = m.content;
    }
    const pending = envelope.client_tool_calls || [];
    if (!pending.length) {
      if (final === null) {
        return { ok: false, text: 'agent 无最终文本回复: ' + JSON.stringify((envelope.messages || []).slice(-1)).slice(0, 1500), session_id: sessionId };
      }
      return { ok: true, text: final, session_id: sessionId };
    }
    handoffRound += 1;
    STATE.handoffs += 1;
    if (handoffRound > Number(CFG.handoff_rounds)) {
      return { ok: false, session_id: sessionId,
        text: `客户端工具交回轮次超过 ${CFG.handoff_rounds} 次，中止（session_id=${sessionId}）` };
    }
    payload = resumePayload(sessionId, workspace, localResults(pending));
  }
}

function callAgentSync(prompt, workspace) {
  /** sync 模式：同样一套闭环，只是每轮请求用阻塞式 curl（本地工具仍在**本进程**执行）。 */
  let payload = firstPayload(prompt, workspace);
  let sessionId = null;
  let final = null;
  let handoffRound = 0;
  for (;;) {
    let { status, envelope } = postSync(payload);
    if (status === 0 && String(envelope.error || '').startsWith('调用 ')) {
      // 同 async 分支：只重试一次纯传输故障。sync 模式下这次等待是阻塞的，
      // 最坏情况再多等一个 agent_timeout——sync 本来就是「把事件循环冻在案发点」的模式。
      print('同步推理调用传输故障，2s 后重试一次');
      sleepSync(2000);
      ({ status, envelope } = postSync(payload));
    }
    sessionId = envelope.session_id || sessionId;
    if (status !== 200) {
      return { ok: false, text: String(envelope.error || `HTTP ${status}`).slice(0, 2000), session_id: sessionId };
    }
    for (const m of envelope.messages || []) {
      if (m.role === 'assistant' && String(m.content || '').trim()) final = m.content;
    }
    const pending = envelope.client_tool_calls || [];
    if (!pending.length) {
      return final === null
        ? { ok: false, session_id: sessionId, text: 'agent 无最终文本回复' }
        : { ok: true, text: final, session_id: sessionId };
    }
    handoffRound += 1;
    STATE.handoffs += 1;
    if (handoffRound > Number(CFG.handoff_rounds)) {
      return { ok: false, session_id: sessionId,
        text: `客户端工具交回轮次超过 ${CFG.handoff_rounds} 次，中止（session_id=${sessionId}）` };
    }
    payload = resumePayload(sessionId, workspace, localResults(pending));
  }
}

// ---------------------------------------------------------------- sync 模式跨进程冷却

function cooldownLoad() {
  try {
    const data = JSON.parse(fs.readFileSync(expandHome(CFG.cooldown_file), 'utf8'));
    return data && typeof data === 'object' ? data : {};
  } catch (_) {
    return {};
  }
}

function cooldownHit(fp) {
  const ts = cooldownLoad()[fp];
  if (ts && Date.now() / 1000 - Number(ts) < CFG.cooldown_sec) {
    print(`指纹 ${fp} 于 ${(Date.now() / 1000 - Number(ts)).toFixed(0)}s 前已派发过`
      + `（冷却 ${CFG.cooldown_sec}s），本次跳过诊断`);
    return true;
  }
  return false;
}

function cooldownMark(fp) {
  try {
    const data = cooldownLoad();
    const nowSec = Date.now() / 1000;
    for (const [k, v] of Object.entries(data)) {
      if (nowSec - Number(v) >= 7 * 86400) delete data[k];
    }
    data[fp] = nowSec;
    const file = expandHome(CFG.cooldown_file);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data));
    fs.renameSync(tmp, file);
  } catch (e) {
    print(`写冷却文件失败: ${e}`);
  }
}

// ---------------------------------------------------------------- 诊断执行

function runDiagnosis(ev) {
  const fp = ev.fingerprint;
  if (CFG.sync) cooldownMark(fp);
  let result;
  try {
    result = CFG.sync
      ? callAgentSync(buildPrompt(ev), ev.workspace || process.cwd())
      : null;                              // async 由 runDiagnosisAsync 处理
  } catch (e) {
    result = { ok: false, text: `诊断执行异常: ${e && e.stack}`, session_id: null };
  }
  if (result) finishDiagnosis(ev, result);
  return result;
}

async function runDiagnosisAsync(ev) {
  let result;
  try {
    result = await callAgent(buildPrompt(ev), ev.workspace || process.cwd());
  } catch (e) {
    result = { ok: false, text: `诊断执行异常: ${e && e.stack}`, session_id: null };
  }
  finishDiagnosis(ev, result);
  return result;
}

function finishDiagnosis(ev, result) {
  const rec = FINGERPRINTS.get(ev.fingerprint);
  if (rec) { rec.dispatched = true; rec.reported = result.ok; }
  STATE[result.ok ? 'dispatch_ok' : 'dispatch_fail'] += 1;
  ev.session_id = result.session_id;
  ev.dispatched_at = now();
  print(`eid=${ev.eid} fp=${ev.fingerprint} 诊断${result.ok ? '完成' : '失败'} `
    + `session_id=${result.session_id || '?'} 本地工具执行=${STATE.handoffs} 次`);
  print(`——诊断报告——\n${String(result.text).slice(0, 2000)}\n——报告结束——`);
}

function drainQueue() {
  /** async 模式：后台串行派发；进程退出前可以 await flush()。 */
  if (STATE.worker || !QUEUE.length) return;
  STATE.worker = true;
  const pump = async () => {
    while (QUEUE.length) {
      const ev = QUEUE.shift();
      try {
        await runDiagnosisAsync(ev);
      } catch (e) {
        STATE.dispatch_fail += 1;
        print(`worker 异常: ${e && e.stack}`);
      }
    }
    STATE.worker = false;
  };
  pump().catch((e) => { STATE.worker = false; print(`worker 异常: ${e && e.stack}`); });
}

function flush() {
  /** 让宿主进程在退出前把队列里的诊断跑完。 */
  return new Promise((resolve) => {
    const tick = () => {
      if (!QUEUE.length && !STATE.worker) return resolve();
      if (QUEUE.length && !STATE.worker) drainQueue();
      setTimeout(tick, 200);
    };
    tick();
  });
}

function dispatchSync(ev) {
  /** sync 模式：就地阻塞跑完诊断（进程停在案发点，现场冻结）。 */
  if (cooldownHit(ev.fingerprint)) return;
  runDiagnosis(ev);
}

// ---------------------------------------------------------------- 启动入口

function start(opts) {
  /** 启动诊断通道（幂等）。opts: project/agentsUrl/agentId/modelId/agentEnabled/sync/
   *  maxRounds/stream/evalEnabled/handoffRounds/cooldownFile。 */
  opts = opts || {};
  if (STATE.started) return true;
  if (process.env.DEBUGGER_ENABLED === '0') return false;
  CFG.project = opts.project || path.basename(process.cwd());
  CFG.agents_url = (opts.agentsUrl || process.env.DEBUGGER_AGENTS_URL
    || DEFAULTS.agents_url).replace(/\/+$/, '');
  CFG.agent_id = opts.agentId !== undefined ? opts.agentId
    : (process.env.DEBUGGER_AGENT_ID !== undefined ? process.env.DEBUGGER_AGENT_ID : DEFAULTS.agent_id);
  CFG.agent_id = CFG.agent_id || '';
  CFG.model_id = opts.modelId || process.env.DEBUGGER_MODEL_ID || DEFAULTS.model_id;
  if (!CFG.agent_id && !CFG.model_id) CFG.agent_id = DEFAULTS.agent_id;
  const rounds = opts.maxRounds !== undefined ? opts.maxRounds : process.env.DEBUGGER_MAX_ROUNDS;
  CFG.max_rounds = /^\d+$/.test(String(rounds || '').trim()) ? Number(String(rounds).trim()) : null;
  CFG.agent_timeout = Number(process.env.DEBUGGER_AGENT_TIMEOUT || DEFAULTS.agent_timeout);
  CFG.handoff_rounds = Number(opts.handoffRounds || process.env.DEBUGGER_HANDOFF_ROUNDS
    || DEFAULTS.handoff_rounds);
  CFG.stream = !!opts.stream || process.env.DEBUGGER_STREAM === '1';
  CFG.eval_enabled = !(opts.evalEnabled === false || process.env.DEBUGGER_EVAL === '0');
  CFG.sync = !!opts.sync || process.env.DEBUGGER_SYNC === '1';
  CFG.sync_timeout = Number(process.env.DEBUGGER_SYNC_TIMEOUT || DEFAULTS.sync_timeout);
  CFG.cooldown_sec = Number(process.env.DEBUGGER_COOLDOWN_SEC || DEFAULTS.cooldown_sec);
  CFG.cooldown_file = process.env.DEBUGGER_COOLDOWN_FILE || DEFAULTS.cooldown_file;
  CFG.api_key = String(opts.apiKey || process.env.DEBUGGER_API_KEY || DEFAULTS.api_key || '');
  // workspace 只在**跨端**诊断时必须显式给：端上从栈帧推导出来的是端上的目录，服务端
  // 在那条路径下 read_file / edit_file 全都落空，补丁会落到不存在（或更糟：另一棵树）的位置。
  CFG.workspace = String(opts.workspace || process.env.DEBUGGER_WORKSPACE
    || DEFAULTS.workspace || '');
  CFG.verify_fix = !(opts.verifyFix === false || process.env.DEBUGGER_VERIFY === '0');
  if (opts.agentEnabled !== undefined) CFG.agent_enabled = !!opts.agentEnabled;
  if (process.env.DEBUGGER_AGENT_ENABLED === '0') CFG.agent_enabled = false;
  CFG.started = Date.now() / 1000;
  STATE.started = true;
  print(`已启动：agent=${CFG.agent_id || CFG.model_id}@${CFG.agents_url} `
    + `现场接口=client side tool calls（${clientToolsPayload().length} 个，无监听端口） `
    + `轮数上限=${CFG.max_rounds || 'agent service 自身默认'} `
    + `复现验证=${CFG.verify_fix ? '红→绿' : '不要求'} `
    + `交回轮数上限=${CFG.handoff_rounds} `
    + (CFG.workspace ? `workspace=${CFG.workspace} ` : '')
    + `通道=${CFG.stream && !CFG.sync ? '流式' : '非流式'} `
    + `模式=${CFG.sync ? '同步' : '异步'} `
    + `诊断=${CFG.agent_enabled ? '开' : '关(仅现场快照)'}`);
  return true;
}

function info() {
  return Object.assign({}, CFG);
}

// ---------------------------------------------------------------- 自检 / 命令行

async function selftest() {
  /** 离线自检：注入脚本化 transport 跑通整套交回闭环，不连任何服务。 */
  start({ project: 'selftest', agentEnabled: false, agentsUrl: 'http://127.0.0.1:1' });
  const checks = [];
  const check = (label, cond) => checks.push([label, !!cond]);

  // 涉事代码放在单独的文件里（debugger 自己文件内的栈帧会被滤掉，这是故意的）
  const caseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'debugger-selftest-'));
  const caseFile = path.join(caseDir, 'orders.js');
  fs.writeFileSync(caseFile, [
    "'use strict';",
    "module.exports = function placeOrder(userId, items) {",
    "  const orderId = 'SO-42';",
    "  if (items.length === 0) throw new TypeError('cannot read property of undefined');",
    "  return orderId;",
    "};",
  ].join('\n'));
  const placeOrder = require(caseFile);
  let err;
  try {
    placeOrder('u-1', []);
  } catch (e) {
    err = e;
  }
  const ev = report('下单', err, { user_id: 'u-1', items: [], order_id: 'SO-42' });
  check('report 抓到事件', ev && ev.type === 'TypeError');
  const snapshot = JSON.parse(executeClientTool('debugger_event', { eid: ev.eid }));
  check('快照含现场字典', String(snapshot.context || '').includes('SO-42'));
  const frame = snapshot.frames[0];
  check('快照含栈帧与源码上下文', frame.func === 'placeOrder' && Array.isArray(frame.context));
  const detail = JSON.parse(executeClientTool('debugger_frame', { fid: frame.fid }));
  check('debugger_frame 能读涉事源码行',
    detail.context.some((l) => String(l.text).includes('throw new TypeError')));
  check('快照锚定事发目录', ev.workspace === caseDir);
  const evaluated = JSON.parse(executeClientTool('debugger_eval', { expr: 'EVENTS.size + FINGERPRINTS.size' }));
  check('debugger_eval 在本进程求值', evaluated.ok && evaluated.result === '2');
  check('status 工具读本进程状态', JSON.parse(executeClientTool('debugger_status', {})).project === 'selftest');
  check('未知 id 正常回答', !!JSON.parse(executeClientTool('debugger_event', { eid: 'nope' })).error);
  check('未实现工具正常回答', !!JSON.parse(executeClientTool('no_such_tool', {})).error);

  // 交回闭环：第一轮模型并行调两个客户端工具，第二轮给最终报告。
  const script = [
    {
      status: 200,
      envelope: {
        session_id: 'st-1',
        messages: [{
          role: 'assistant',
          tool_calls: [
            { _index: 0, id: 'c1', name: 'debugger_event', arguments: JSON.stringify({ eid: ev.eid }) },
            { _index: 1, id: 'c2', name: 'debugger_eval', arguments: '{"expr":"statusInfo().project"}' },
          ],
        }],
        client_tool_calls: [
          { id: 'c1', name: 'debugger_event', arguments: JSON.stringify({ eid: ev.eid }) },
          { id: 'c2', name: 'debugger_eval', arguments: '{"expr":"statusInfo().project"}' },
        ],
      },
    },
    {
      status: 200,
      envelope: {
        session_id: 'st-1',
        messages: [{ role: 'assistant', content: '## 问题\nx\n## 根因\ny\n## 修改\n未修改\n' }],
        client_tool_calls: [],
      },
    },
  ];
  const sent = [];
  const post = async (payload) => { sent.push(payload); return script.shift(); };
  const result = await callAgent('模拟提问', process.cwd(), post);
  check('闭环在交回后继续推理', sent.length === 2);
  check('续跑请求带同一个 session_id', sent[1] && sent[1].session_id === 'st-1');
  check('交回的调用已在本地执行', sent[1] && sent[1].messages.length === 2
    && sent[1].messages.every((m) => m.role === 'tool'));
  check('本地结果带 tool_use_id', sent[1].messages[0].tool_use_id === 'c1');
  check('本地结果带工具名', sent[1].messages[1].name === 'debugger_eval');
  check('最终报告交回宿主', result.ok && String(result.text).startsWith('## 问题'));
  check('会话内下发客户端工具', sent[0].client_tools.length === CLIENT_TOOLS.length);

  // 流式帧重建：并行调用片段拼接 + status 帧 + 正文分帧合并
  const frames = [
    { session_id: 's2' },                        // init 控制帧（不是 message）
    { role: 'assistant', tool_calls: [
      { _index: 0, id: 'a', name: 'debugger_event', arguments: '{"e' }] },
    { role: 'assistant', tool_calls: [
      { _index: 0, arguments: 'id":"x"}' },       // 第二片：把 arguments 拼完
      { _index: 1, id: 'b', name: 'debugger_status', arguments: '{}' }] },
    { role: 'assistant', content: '两段' },
    { role: 'assistant', content: '拼成一条' },
    { role: 'tool', tool_use_id: 'b', name: 'debugger_status', content: '{...}' },
    { role: 'status', status: 'client_tool_calls', client_tool_calls: [
      { id: 'a', name: 'debugger_event', arguments: '{"eid":"x"}' }] },
  ];
  const sse = frames.map((f) => `data: ${JSON.stringify(f)}`).join('\n\n')
    + '\n\ndata: [DONE]\n';
  const env = collectStream(sse);
  check('SSE 片段拼回完整调用', env.client_tool_calls.length === 1
    && env.client_tool_calls[0].name === 'debugger_event'
    && env.client_tool_calls[0].arguments === '{"eid":"x"}');
  check('SSE 按工具名也能认出交回', (() => {
    // 服务端没发 status 帧（老版本）时，按本会话下发的客户端工具名自行判定
    const noStatus = frames.filter((f) => f.status !== 'client_tool_calls');
    const sseNoStatus = noStatus.map((f) => `data: ${JSON.stringify(f)}`).join('\n\n')
      + '\n\ndata: [DONE]\n';
    const env2 = collectStream(sseNoStatus);
    return env2.client_tool_calls.length === 1
      && env2.client_tool_calls[0].id === 'a';
  })());
  const texts = env.messages.filter((m) => m.role === 'assistant' && m.content);
  check('SSE 正文分帧拼成一条', texts.length === 1 && texts[0].content === '两段拼成一条');
  check('SSE 读出 session_id', env.session_id === 's2');

  // 交回轮数上限：模型一直调客户端工具，必须停下而不是无限往返。
  CFG.handoff_rounds = 2;
  const looping = {
    status: 200,
    envelope: {
      session_id: 'st-2', messages: [],
      client_tool_calls: [{ id: 'loop', name: 'debugger_status', arguments: '{}' }],
    },
  };
  let loopCalls = 0;
  const loopResult = await callAgent('loop', process.cwd(),
    async () => { loopCalls += 1; return looping; });
  check('交回轮数有上限', loopResult.ok === false
    && String(loopResult.text).includes('交回轮次超过 2 次') && loopCalls === 3);

  // 交回清单：服务端 status 帧只点名 a，b 也宣布了但没有回执 —— 两条都必须执行（真机缺陷）
  const unionFrames = [
    { session_id: 's3' },
    { role: 'assistant', tool_calls: [
      { _index: 0, id: 'x1', name: 'debugger_event', arguments: '{"eid":"q"}' },
      { _index: 1, id: 'x2', name: 'debugger_status', arguments: '{}' }] },
    { role: 'status', status: 'client_tool_calls', client_tool_calls: [
      { id: 'x1', name: 'debugger_event', arguments: '{"eid":"q"}' }] },
  ];
  const unionEnv = collectStream(unionFrames.map((f) => `data: ${JSON.stringify(f)}`).join('\n\n')
    + '\n\ndata: [DONE]\n');
  check('交回清单取「点名的 ∪ 无回执的」', unionEnv.client_tool_calls.length === 2
    && unionEnv.client_tool_calls.map((c) => c.id).sort().join(',') === 'x1,x2');
  const dupEnv = collectStream((unionFrames.slice(0, 2).concat([
    { role: 'status', status: 'client_tool_calls', client_tool_calls: [
      { id: 'x2', name: 'debugger_status' },                       // 同一 id 两份，这份缺参数
      { id: 'x2', name: 'debugger_status', arguments: '{}' }] },   // 流里宣布的那份带参数
  ])).map((f) => `data: ${JSON.stringify(f)}`).join('\n\n') + '\n\ndata: [DONE]\n');
  check('同一 id 只交回一次且保留参数', dupEnv.client_tool_calls.filter((c) => c.id === 'x2').length === 1
    && dupEnv.client_tool_calls.find((c) => c.id === 'x2').arguments === '{}');

  // 指纹带行号：同一个函数里的两个不同落点必须是两个指纹
  const twoCase = path.join(caseDir, 'two_throws.js');
  fs.writeFileSync(twoCase, [
    "'use strict';",
    "module.exports = function probe(which) {",
    "  if (which === 'a') throw new RangeError('branch a');",
    "  if (which === 'b') throw new RangeError('branch b');",
    "};",
  ].join('\n'));
  const probe = require(twoCase);
  const fps = [];
  for (const which of ['a', 'b']) {
    try { probe(which); } catch (e) { const ev2 = report('分支' + which, e); fps.push(ev2.fingerprint); }
  }
  check('指纹含行号：同一函数不同落点不再撞车', fps[0] !== fps[1]);

  // 鉴权头与 workspace 覆盖
  CFG.api_key = 'secret-token';
  check('Bearer 只进请求头', headers().Authorization === 'Bearer secret-token'
    && !JSON.stringify(statusInfo()).includes('secret-token')
    && statusInfo().auth === 'Bearer');
  CFG.api_key = '';
  check('未配令牌时不带鉴权头', !headers().Authorization && statusInfo().auth === 'none');
  CFG.workspace = '/repo/on/server';
  try { throw new Error('ws override probe'); } catch (e) {
    check('workspace 可显式覆盖（跨端诊断）', report('ws', e).workspace === '/repo/on/server');
  }
  CFG.workspace = '';

  // 验证契约：默认要求红→绿并要证据；关掉时明确要求写「未执行验证」
  const promptOn = buildPrompt(ev);
  CFG.verify_fix = false;
  const promptOff = buildPrompt(ev);
  CFG.verify_fix = true;
  check('诊断契约含红→绿验证步骤', promptOn.includes('红→绿')
    && promptOn.includes('改动**前**失败') && promptOn.includes('未验证：'));
  check('关掉验证时仍要求注明未执行', !promptOff.includes('红→绿')
    && promptOff.includes('未执行验证'));
  check('报告要求点明补丁未进入运行态', promptOn.includes('尚未进入运行态'));

  // 传输故障只重试一次：第一次纯传输失败、第二次成功，闭环仍应拿到最终报告
  let tries = 0;
  const flaky = async (payload) => {
    tries += 1;
    if (tries === 1) return { status: 0, envelope: { error: `调用 http://127.0.0.1:1/v1/infer 失败: ECONNRESET` } };
    return { status: 200, envelope: { session_id: 'flaky-1', messages: [{ role: 'assistant', content: '## 问题\nx' }] } };
  };
  const retryStart = Date.now();
  const retryResult = await callAgent('retry probe', process.cwd(), flaky);
  check('纯传输故障重试一次后闭环', retryResult.ok === true && tries === 2
    && Date.now() - retryStart >= 2000);

  let failed = 0;
  for (const [label, ok] of checks) {
    process.stdout.write(`${ok ? '✓' : '✗'} ${label}\n`);
    if (!ok) failed += 1;
  }
  process.stdout.write(`自检 ${checks.length - failed}/${checks.length} 通过\n`);
  return failed ? 1 : 0;
}

async function main() {
  const args = process.argv.slice(2);
  const flag = (name) => {
    const i = args.indexOf(name);
    return i >= 0 ? args[i + 1] : undefined;
  };
  if (args.includes('--selftest')) return selftest();

  start({
    project: flag('--project') || path.basename(process.cwd()),
    agentsUrl: flag('--agents-url'),
    agentId: flag('--agent-id'),
    modelId: flag('--model-id'),
    sync: args.includes('--sync'),
    stream: args.includes('--stream'),
  });
  // 演示：制造一个异常并诊断（真实项目里是在自己的 catch 里调 report()）
  function risky(orderId) {
    const price = 0;
    return price / 0 * orderId.length;      // 故意出错，演示用
  }
  try {
    risky('SO-1');
  } catch (e) {
    report('演示步骤', e, { order_id: 'SO-1', price: 0 });
  }
  await flush();
  return 0;
}

if (require.main === module) {
  main().then((code) => { process.exitCode = code || process.exitCode || 0; });
}

module.exports = {
  start,
  report,
  info,
  flush,
  registerWorker,
  // 供测试/高级用法直接调用
  executeClientTool,
  clientToolsPayload,
  callAgent,
  callAgentSync,
  collectStream,
  mergeToolCallFrames,
  buildPrompt,
  runDiagnosisAsync,
  EVENTS,
  FINGERPRINTS,
  FRAME_CACHE,
  CFG,
  STATE,
  CLIENT_TOOLS,
  DEFAULTS,
};

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""online_debugger.py — Python 进程「异常快照 + client side tool calls 诊断」最小实现。

接入方式（任何 Python 程序，和旧版完全一致）：
    import online_debugger as debugger
    debugger.start(project="X7991")      # 程序启动时调用一次（幂等）
    ...
    try:
        ...
    except Exception as e:
        debugger.report("步骤名", e)      # 快照+派发；异步模式入队即返回，sync 模式就地诊断

组成：
1. 异常快照：沿 exc.__traceback__ 抓每帧的文件/行/函数/源码行/局部变量（截断）。
   traceback 持有帧对象引用，函数已退出后 f_locals 依然可读，所以快照是"案发瞬间"的
   真实数据。合成异常（无 __traceback__）退化为抓当前调用栈。
2. 现场查询 = client side tool calls（本版核心改动）：
   旧版在这里嵌一个只读 HTTP inspector（127.0.0.1:51110），让诊断 agent 用 exec_shell +
   curl 去查——于是要开端口、要配 CLI 工具、还要防端口占用。现在改成把同样的只读现场接口
   声明成 **客户端工具**（tool_type="client_side"）随推理请求下发：
     debugger_status / debugger_events / debugger_event / debugger_frame /
     debugger_threads / debugger_eval（可选，默认开）
   服务端只有注册 JSON，没有任何可执行实体；模型一旦调用它们，推理循环**正常退出**并把
   调用交回本进程，本进程就地读现场执行，再把结果（role="tool" + tool_use_id）连同同一个
   session_id 再发一次 /v1/infer，服务端按 session id 合并会话历史继续推理。
   ⇒ 不需要监听任何端口，不需要 exec_shell/CLI/curl；诊断 agent 用它在服务端自己的
     read_file / search_code / edit_file 读改源码（服务端工具），两条执行侧在同一会话里混用，
     甚至同一轮并行调用里两边都有（服务端那半在服务端跑，客户端那半交回本进程）。
3. 诊断闭环：worker（异步）或 report 调用点（sync）把「快照 + 现场工具说明」拼成 user
   prompt，POST {agents_url}/v1/infer（agent_ids=["Coder"], workspace=事发源码目录,
   session_id="new"），然后按上面的交回机制反复续跑，直到模型给出最终报告（## 问题 /
   ## 根因 / ## 修改 / ## 验证/风险 / ## 后续动作）打印到 stdout。会话全程（含每次工具
   调用）由 agentsvc 按 session_id 落盘备查。
4. 错误指纹：异常类型 + 栈帧函数序列的 md5，内存 map；同一指纹进程内只派发一次；
   sync 模式额外落一个跨进程冷却文件（~/.debugger_cooldown.json），短命子进程反复
   踩同一坑时不会每次阻塞去诊断。

配置优先级：start() 参数 > 环境变量 > 默认值
    DEBUGGER_ENABLED=0                   整套关闭
    DEBUGGER_AGENT_ENABLED=0             观察模式：只做快照，不派发 Coder
    DEBUGGER_AGENTS_URL=http://localhost:7988
    DEBUGGER_API_KEY=<bearer>             可选：上游要 Bearer 时给（hh 那套
                                          HH_AGENTSVC_TOKEN 就是这种令牌）。不设=不带
                                          Authorization 头，直连裸 Agent Service 够用。
    DEBUGGER_AGENT_ID=Coder
    DEBUGGER_AGENT_ID= + DEBUGGER_MODEL_ID=xxx   不用 agent，直接指定服务端模型
    DEBUGGER_WORKSPACE=/path/to/repo       可选：服务端 workspace（它读文件/跑命令的根）。
                                          不设=按事发源码路径推导；在移动端/容器里跑时
                                          那个路径是宿主沙箱内的，服务端读不到，跨端诊断要显式给。
    DEBUGGER_MAX_ROUNDS=30               可选：agent 工具轮数上限；默认不设(null)，由 agent service 自身控制
    DEBUGGER_HANDOFF_ROUNDS=80           客户端工具交回轮次上限（防会话无限往返）。
                                         服务端 max_tool_rounds 只数单个请求内的服务端
                                         轮次：纯交回轮不计数、续跑请求重新计数，所以
                                         这条上限实际由调用方掌握（真机 2026-11-08：
                                         30 曾被正好跑满，会话停在 ⏳ 那一帧）。
    DEBUGGER_STREAM=1                    用 /v1/infer/stream（SSE）跑诊断，默认关（用 /v1/infer）
    DEBUGGER_EVAL=0                      禁用 debugger_eval（默认允许在本地进程求值表达式）
    DEBUGGER_VERIFY=0                    不要求红→绿复现验证（默认要求：改了代码就得
                                         先复现失败、改后复现通过，并把命令与退出码写进报告）
    DEBUGGER_SYNC=1                      子进程模式：report 就地同步诊断（默认关）
    DEBUGGER_SYNC_TIMEOUT=900            sync 诊断最长等待（秒）
    DEBUGGER_COOLDOWN_SEC=1800           sync 模式同指纹跨进程冷却窗口（秒）
    DEBUGGER_AGENT_TIMEOUT=1200          单次 HTTP 请求超时（秒）

安全/边界：
- 现场接口只读为主；唯一"可改进程状态"的是 debugger_eval，默认开启但只在**本进程内**
  执行（没有任何网络端口暴露它），设 DEBUGGER_EVAL=0 即可关掉。
- report() 永不抛异常；队列满即丢；诊断线程为 daemon，随主进程退出。
- 局部变量 repr 截断 200 字符、每帧最多 30 个；conn/cur/self/response 等对象剔除，
  避免大对象与敏感内容进入快照和 prompt。
"""

import hashlib
import itertools
import json
import linecache
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

# ---------------------------------------------------------------- 配置与全局状态

_DEFAULTS = {
    "project": None,
    "agents_url": "http://localhost:7988",
    "agent_id": "Coder",
    # agent_id 设为空串时改用这个 model_id（服务端没有配 Coder agent 时也能跑）
    "model_id": "",
    # 上游要 Bearer 时用（例如 hh 那套 Agent Service：HH_AGENTSVC_TOKEN）。
    # 空 = 不带 Authorization 头 = 直连裸 Agent Service。令牌只进请求头，不进日志/产物。
    "api_key": "",
    # 服务端 workspace（agent 在它自己的机器上读文件/跑命令的根目录）。
    # 空 = 按事发源码路径推导；在移动端/容器里跑时那个路径服务端读不到，需显式给仓库路径。
    "workspace": "",
    # 默认不设（null）：不自行指定工具轮数，交由 agent service 自身的次数控制
    "max_rounds": None,
    # 30 太小: 真机 2026-11-08 有会话正好跑满 30 个交回轮次后被"轮次上限"掐断,
    # Web 侧看到的就是推理停在 🛠️ debugger_eval ⏳。给到 80, 让"次数"这件事由
    # 服务端 max_tool_rounds 兜底, 客户端这侧只防真正的死循环。
    "handoff_rounds": 80,
    "agent_timeout": 1200,
    "agent_enabled": True,
    "eval_enabled": True,
    # 改了代码就要求做一次红→绿复现（红=改动前能复现同一异常，绿=改动后通过）。
    # 关掉它只是回到"报告里描述打算怎么验"，不改变任何现场捕获行为。
    "verify_fix": True,
    "stream": False,
    "max_events": 32,
    "sync": False,          # True：report() 就地同步诊断（短命子进程用）
    "sync_timeout": 900,    # sync 诊断最长等待（秒）
    "cooldown_sec": 1800,   # sync 模式同指纹跨进程去重的冷却窗口（秒）
    "cooldown_file": "~/.debugger_cooldown.json",
}

_CFG = dict(_DEFAULTS)
_CFG["started"] = None       # epoch 秒

_LOCK = threading.Lock()
_EVENTS = OrderedDict()      # eid -> event dict（完整快照，traceback 使帧保活）
_FINGERPRINTS = {}           # fp   -> {"count","first_ts","last_ts","eid","dispatched","reported"}
_FRAME_CACHE = OrderedDict() # fid  -> {"frame","snap"}（LRU，强引用帧避免 id 复用错帧）
_FRAME_CACHE_MAX = 512
_QUEUE = queue.Queue(maxsize=64)
_WORKER_LOCK = threading.Lock()
_COOLDOWN_LOCK = threading.Lock()
_STATE = {"started": False, "worker": False,
          "dispatch_ok": 0, "dispatch_fail": 0, "handoffs": 0}

# 大对象/敏感对象黑名单：不进快照（精确名 + *_conn / *_cursor 这类后缀变体）
_SKIP_LOCALS = frozenset({
    "self", "conn", "cur", "cursor", "connection", "client", "session",
    "response", "resp", "request", "req", "db", "engine",
})
_SKIP_SUFFIXES = tuple(f"_{t}" for t in _SKIP_LOCALS)


def _is_skipped_local(name):
    return name in _SKIP_LOCALS or name.endswith(_SKIP_SUFFIXES)


# ---------------------------------------------------------------- 小工具


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _print(msg):
    """调试器自身的声音: 一律进 stdout, 前缀 `[debugger]`。

    为什么这些 print 不能省: 交回中止原因(撞 handoff 上限 / 没有最终文本 / 调用失败)
    只有这一处出口; 没有它, Web 侧看到的就是推理停在 🛠️ ⏳ 那一帧, 这边一行解释都没有,
    只能靠猜。宿主若没有 stdout(嵌进 GUI 进程、移动端宿主之类), 由宿主自己的集成层接管
    这一层 —— 本文件不为某个具体宿主开特例, 也不为此引入任何非标准库依赖。
    """
    try:
        print(f"[debugger] {msg}")
    except Exception:
        pass


def _safe_repr(obj, limit=200):
    try:
        r = repr(obj)
    except Exception as e:  # 对象的 __repr__ 可能自己炸
        return f"<repr失败 {type(e).__name__}: {e}>"
    head = type(obj).__name__
    if len(r) > limit:
        r = r[:limit] + f"…(共{len(r)}字符)"
    return f"{head}: {r}"


def _register_frame(fid, frame, snap):
    with _LOCK:
        _FRAME_CACHE[fid] = {"frame": frame, "snap": snap}
        _FRAME_CACHE.move_to_end(fid)
        while len(_FRAME_CACHE) > _FRAME_CACHE_MAX:
            _FRAME_CACHE.popitem(last=False)


def _capture_frame(frame, lineno=None, max_vars=30):
    """把一帧序列化进 dict，并按 fid 注册（客户端工具可按 fid 复查）。"""
    code = frame.f_code
    if lineno is None:
        lineno = frame.f_lineno
    fid = f"{id(frame):x}"
    snap = {"fid": fid, "file": code.co_filename, "line": lineno,
            "func": code.co_name,
            "src": linecache.getline(code.co_filename, lineno).strip(),
            "locals": {}}
    truncated = False
    for i, (k, v) in enumerate(itertools.islice(frame.f_locals.items(), max_vars + 1)):
        if i >= max_vars:
            truncated = True
            break
        if k.startswith("__") or _is_skipped_local(k):
            continue
        snap["locals"][k] = _safe_repr(v)
    if truncated:
        snap["locals_note"] = f"局部变量超过{max_vars}个，已截断"
    _register_frame(fid, frame, snap)
    return snap


def _caller_frames(skip_module_file):
    """无 __traceback__ 时：抓当前调用栈，跳过 debugger 自身的帧。"""
    out = []
    f = sys._getframe(1)
    while f is not None and len(out) < 12:
        if f.f_code.co_filename != skip_module_file:
            out.append(_capture_frame(f))
        f = f.f_back
    return list(reversed(out))  # 外层在前，与 traceback 顺序一致


def _exc_chain(exc):
    """异常链摘要（cause/context），最多 4 层。"""
    parts, seen = [], set()
    cur = exc
    while cur is not None and len(parts) < 4 and id(cur) not in seen:
        seen.add(id(cur))
        parts.append({"type": type(cur).__name__, "msg": str(cur)[:1200]})
        nxt = cur.__cause__
        if nxt is None and not cur.__suppress_context__:
            nxt = cur.__context__
        cur = nxt
    return parts


def _make_fingerprint(exc_type_name, frames):
    # basis 必须带**行号**: 只按 func@file 拼, 同一条调用链里同一函数的不同落点会撞成同一指纹。
    # 真机 2026-11-08 16:06:22 / 16:07:12(fp=abe501a9456d, eid=b3bd5bc6)实测: probe_one_dept_unit
    # 里 6170/6199/6235/6250/6259 五个断点的 func 序列(run→...→probe_one_dept_unit→_nav_assert→
    # _synexc)完全一样 => 全归一个指纹: 第二条(SH-198 dept_intro_card)只 count++, 既不落
    # debugger_events.jsonl(_dbg_sink 只写 count==1)也不派 agent —— 后到的**真·未知变体**现场会被
    # 前一条 report 吞掉(这正是 fetch_slots._attach_caller_tb 注释里"指纹按调用链彼此区分"想要的
    # 效果缺的那一半)。带上行号后: 同一落点跨医院仍去重, 不同落点各自留现场。
    basis = exc_type_name + "|" + "|".join(
        f"{f['func']}@{os.path.basename(f['file'])}:{f.get('line')}" for f in frames)
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 快照 + 入队


def _derive_workspace(frames):
    """工作区 = 事发源码所在目录：自最内层帧向外，取第一个不在标准库 / site-packages 的
    真实源码文件，返回其所在目录；找不到则用 cwd。"""
    stdlib_dir = os.path.dirname(os.path.dirname(os.path.abspath(os.__file__)))
    for fr in reversed(frames):
        f = fr.get("file") or ""
        if not f or f.startswith("<") or not os.path.isfile(f):
            continue
        if "site-packages" in f or "dist-packages" in f:
            continue
        if f.startswith(stdlib_dir + os.sep):
            continue
        return os.path.dirname(f)
    return os.getcwd()


def report(step, exc):
    """在 except 块里调用：抓异常瞬间快照，按指纹去重后派发（异步入队 / sync 就地）。永不抛异常。"""
    try:
        if os.environ.get("DEBUGGER_ENABLED", "1") == "0":
            return None
        if not _STATE["started"]:
            start()
        frames = []
        tb = getattr(exc, "__traceback__", None)
        while tb is not None and len(frames) < 12:
            frames.append(_capture_frame(tb.tb_frame, lineno=tb.tb_lineno))
            tb = tb.tb_next
        if not frames:  # 合成异常：无栈可用，退化为调用点现场
            frames = _caller_frames(__file__)
        chain = _exc_chain(exc)
        fp = _make_fingerprint(type(exc).__name__, frames)
        eid = hashlib.md5(f"{fp}-{time.time_ns()}".encode()).hexdigest()[:8]
        ts = _now()
        event = {"eid": eid, "fingerprint": fp, "step": str(step),
                 "type": type(exc).__name__, "msg": str(exc)[:1500],
                 "chain": chain, "frames": frames, "ts": ts,
                 "pid": os.getpid(), "count": 1,
                 "project": _CFG["project"] or os.path.basename(os.getcwd()),
                 # 显式配了 DEBUGGER_WORKSPACE / start(workspace=) 就用它（跨端诊断必须有：
                 # 移动端/容器里推导出来的是那台机器上的路径，服务端读不到）；没配才按事发帧推导。
                 "workspace": _CFG.get("workspace") or _derive_workspace(frames)}
        dispatch = False
        with _LOCK:
            rec = _FINGERPRINTS.get(fp)
            if rec is None:
                _FINGERPRINTS[fp] = {"count": 1, "first_ts": ts, "last_ts": ts,
                                     "eid": eid, "dispatched": False, "reported": False}
                _EVENTS[eid] = event
                while len(_EVENTS) > _CFG["max_events"]:
                    _EVENTS.popitem(last=False)
                dispatch = bool(_CFG["agent_enabled"])
            else:
                rec["count"] += 1
                rec["last_ts"] = ts
                event["count"] = rec["count"]
        if dispatch and _CFG["sync"]:
            _dispatch_sync(event)   # 子进程：阻塞到诊断结束（进程还活着，现场可实时查）
        elif dispatch:
            try:
                _QUEUE.put_nowait(event)
            except queue.Full:
                _print(f"诊断队列已满，丢弃 eid={eid}（{step}）")
        elif _CFG["agent_enabled"]:
            _print(f"指纹 {fp} 已派发过，本轮仅计数（累计 {_FINGERPRINTS[fp]['count']} 次）")
        return event
    except Exception as e:  # 诊断机制绝不允许拖垮主流程
        _print(f"report 内部异常: {e!r}")
        return None


# ---------------------------------------------------------------- 现场接口（客户端工具的实现体）
#
# 这些函数就是 client side tool 的**本地**实现：模型调用对应工具时，推理循环在
# 服务端正常退出，调用被交回本进程，由 _execute_client_tool() 就地跑完，再把结果
# 作为 role="tool" 消息带同一个 session_id 送回。全程没有网络端口。


def _status():
    with _LOCK:
        n_fp = len(_FINGERPRINTS)
    return {"pid": os.getpid(), "project": _CFG["project"],
            "python": sys.version.split()[0],
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_CFG["started"] or 0)),
            "uptime_sec": int(time.time() - (_CFG["started"] or time.time())),
            "threads": threading.active_count(),
            "thread_names": [t.name for t in threading.enumerate()][:40],
            "cwd": os.getcwd(),
            "events_stored": len(_EVENTS), "fingerprints": n_fp,
            "queued": _QUEUE.qsize(),
            "dispatch_ok": _STATE["dispatch_ok"],
            "dispatch_fail": _STATE["dispatch_fail"],
            "client_tool_handoffs": _STATE["handoffs"],
            "agent": f"{_CFG['agent_id']}@{_CFG['agents_url']}",
            # 只报「有没有带令牌」，绝不回显令牌本身（这份 status 会进 prompt 给模型）
            "auth": "Bearer" if _CFG.get("api_key") else "none",
            "workspace": _CFG.get("workspace") or "",   # 空 = 按事发帧推导
            "max_rounds": _CFG["max_rounds"],      # null = 由 agent service 自身的次数控制
            "agent_enabled": _CFG["agent_enabled"],
            "eval_enabled": _CFG["eval_enabled"],
            "verify_fix": _CFG["verify_fix"],   # 报告里的「验证/风险」是否要求证据
            "sync": _CFG["sync"]}


def _all_threads_snapshot():
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for tid, top in sys._current_frames().items():
        chain, f = [], top
        while f is not None and len(chain) < 20:
            chain.append(_capture_frame(f))
            f = f.f_back
        out.append({"tid": tid, "name": names.get(tid, "?"), "frames": chain})  # 栈顶在前
    return out


def _events_summary():
    with _LOCK:
        fp_info = {fp: rec["count"] for fp, rec in _FINGERPRINTS.items()}
        evs = [
            {"eid": e["eid"], "fingerprint": e["fingerprint"], "step": e["step"],
             "type": e["type"], "msg": e["msg"][:200], "ts": e["ts"],
             "count": _FINGERPRINTS.get(e["fingerprint"], {}).get("count", 1),
             "dispatched": _FINGERPRINTS.get(e["fingerprint"], {}).get("dispatched"),
             "session_id": e.get("session_id")}
            for e in _EVENTS.values()
        ]
    return {"events": evs, "fingerprints": fp_info}


def _frame_snapshot(fid):
    with _LOCK:
        hit = _FRAME_CACHE.get(fid)
        if hit:
            _FRAME_CACHE.move_to_end(fid)
    if not hit:
        return {"error": f"fid={fid} 不在缓存（先从 debugger_event 或 debugger_threads 取）"}
    return {"fid": fid, "queried_at": _now(), **hit["snap"]}


def _eval_expr(expr, fid=""):
    if not _CFG["eval_enabled"]:
        return {"ok": False,
                "error": "debugger_eval 已被 DEBUGGER_EVAL=0 关闭"}
    if not str(expr or "").strip():
        return {"ok": False, "error": "expr 必填"}
    if fid:
        with _LOCK:
            hit = _FRAME_CACHE.get(fid)
            if hit:
                _FRAME_CACHE.move_to_end(fid)
        if not hit:
            return {"ok": False, "error": f"fid={fid} 不在缓存（先从 debugger_event 或 debugger_threads 取）"}
        fr = hit["frame"]
        ns = dict(fr.f_globals)
        ns.update(fr.f_locals)     # 快照帧：函数已退出但 traceback 让 f_locals 保活
        scope = f"帧 {hit['snap'].get('func')}()@{hit['snap'].get('line')}"
    else:
        ns = dict(globals())       # debugger 模块作用域：_EVENTS/_CFG/_STATE 及全部内部函数
        scope = "debugger 模块作用域"
    try:
        result = eval(expr, ns)    # 只在本进程内求值；不暴露在任何端口上
        return {"ok": True, "scope": scope, "expr": expr,
                "result": _safe_repr(result, 4000)}
    except Exception as e:
        return {"ok": False, "scope": scope, "expr": expr,
                "error": f"{type(e).__name__}: {e}"}


# 工具名 -> 处理函数（arguments 已解析为 dict）
_CLIENT_HANDLERS = {
    "debugger_status": lambda a: _status(),
    "debugger_threads": lambda a: {"threads": _all_threads_snapshot()},
    "debugger_events": lambda a: _events_summary(),
    "debugger_event": lambda a: (
        _EVENTS.get(a.get("eid") or "") or {"error": f"eid={a.get('eid')} 不存在或已被环形淘汰"}),
    "debugger_frame": lambda a: _frame_snapshot(a.get("fid") or ""),
    "debugger_eval": lambda a: _eval_expr(a.get("expr") or "", a.get("fid") or ""),
}

# 随推理请求下发的客户端工具注册 JSON（服务端只存这些描述，不执行）
_CLIENT_TOOLS = [
    {"name": "debugger_status",
     "description": "目标进程概况：pid、启动时间、运行时长、线程数、事件数、诊断派发统计",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "debugger_events",
     "description": "已捕获的错误事件列表：eid、指纹、出现次数、是否已派发、session_id",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "debugger_event",
     "description": "某个错误事件的完整快照（异常链 + 各帧 fid/源码行/局部变量）",
     "parameters": {"type": "object",
                    "properties": {"eid": {"type": "string", "description": "事件 id"}},
                    "required": ["eid"]}},
    {"name": "debugger_frame",
     "description": "指定 fid 在捕获时刻的局部变量与源码行（异常快照帧同样可查）",
     "parameters": {"type": "object",
                    "properties": {"fid": {"type": "string", "description": "帧 id"}},
                    "required": ["fid"]}},
    {"name": "debugger_threads",
     "description": "所有线程当前调用栈；每帧含 fid，可用 debugger_frame 复查其局部变量",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "debugger_eval",
     "description": "在目标进程内求值一个 Python 表达式（fid 给了就在该快照帧作用域求值，"
                    "否则在 debugger 模块作用域）；只读用途为主",
     "parameters": {"type": "object",
                    "properties": {"expr": {"type": "string", "description": "表达式"},
                                   "fid": {"type": "string", "description": "可选：快照帧 id"}},
                    "required": ["expr"]}},
]


def _client_tools_payload():
    """本次会话要下发的客户端工具定义（DEBUGGER_EVAL=0 时连描述都不给）。"""
    if _CFG["eval_enabled"]:
        return list(_CLIENT_TOOLS)
    return [t for t in _CLIENT_TOOLS if t["name"] != "debugger_eval"]


def _execute_client_tool(name, arguments):
    """本地执行一个客户端工具调用，返回结果字符串（作为 role="tool" 内容回传）。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    handler = _CLIENT_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"本进程未实现客户端工具 {name}"}, ensure_ascii=False)
    try:
        payload = handler(arguments)
    except Exception as e:  # 现场查询失败也要正常回答这次调用
        payload = {"error": f"{type(e).__name__}: {e}"}
    return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- Coder agent 调用

_SKILL_TMPL = """你是运行时故障诊断代理，代号 {agent_id}。目标程序 {project}（pid={pid}，启动于 {started}）\
内置了 debugger，把「进程现场查询」以 **client side tool calls** 的形式提供给你：这些工具在服务端\
只有注册 JSON、不会由服务端执行，你调用它们时推理循环会正常结束，由目标进程就地执行并把结果回传到\
同一个会话（session_id={sid_preview}）继续。诊断必须基于这些工具返回的第一手现场数据，不要凭空推理。

可用现场工具（就在本会话的工具列表里）：
  debugger_status                      进程概况与统计
  debugger_events                      已捕获错误事件列表：eid、指纹、出现次数、是否已派发
  debugger_event(eid)                  某错误的完整快照（含各帧 fid）
  debugger_frame(fid)                  指定帧在捕获时刻的局部变量与源码行（异常快照帧同样可查）
  debugger_threads                     所有线程当前调用栈；每帧含 fid，可用 debugger_frame 复查局部变量
  debugger_eval(expr[, fid])           在目标进程内求值表达式（fid 给了就在该帧作用域，否则在 debugger 模块作用域）
示例子任务：
  先 debugger_event(eid="{eid}") 读全量快照，缺什么现场数据就再调 debugger_frame(fid=...) 或\
  debugger_eval（允许多轮，缺什么补什么）。
说明：局部变量 repr 截断到 200 字符、每帧最多 30 个；conn/cur/self/response 等对象被剔除；
快照是异常瞬间的数据；线程实时状态用 debugger_threads 现抓现查；现场查询只读，不会改变进程状态
（eval 除外，它只在本进程内求值）。
工作区：本次会话 workspace 已设为 {workspace}，你在服务端自己的工具（exec_shell 的 cwd、
read_file/search_code/edit_file）都在该目录下工作，源码请直接读文件，不必经 debugger。
注意：服务端工具（read_file/search_code/edit_file/exec_shell）由服务端执行，现场工具由目标进程执行，
两者可以在同一轮并行调用里混用。

任务：
1) 先读下方异常快照，缺什么现场数据就用现场工具补什么（允许多轮）。
2) 用 read_file / search_code 阅读涉事源码，定位根因。
3) 处置规则：
   - 代码缺陷且有把握修复：用 edit_file 直接做最小化修改（改动文件即可，勿重启进程）。
   - 网络抖动/限流/对端瞬时错误：不要改代码，给出重试/退避/降级建议。
   - 涉及数据库写入、外部系统配置的动作：只写建议，不要执行。
{verify_step}5) 最后一条消息严格按以下格式输出（本次会话由 agentsvc 按 session_id 全程存档）：
## 问题
（一句话现象 + 触发条件）
## 根因
（基于现场数据的判断，引用你查到的帧/变量/接口结果）
## 修改
（文件路径 + 改动摘要；未改代码则写"未修改"并说明理由）
## 验证/风险
{verify_hint}
## 后续动作
（如：需重启进程生效 / 建议重跑哪些任务 / 无需动作；改动尚未进入运行态时必须点明）

本次异常快照（eid={eid}，step={step!r}）：
```json
{snapshot}
```
"""


def _build_prompt(event):
    slim = {k: v for k, v in event.items() if k != "frames"}
    slim["frames"] = event["frames"]
    if _CFG["verify_fix"]:
        verify_step = (
            "4) 改了代码就把它验成红→绿：先写/找到能在**改动前**复现同一异常（同一异常类型与\n"
            "   栈顶位置）的最小复现，确认它失败（红）；打上改动后再跑一遍，确认通过（绿）。\n"
            "   复现命令与退出码留在报告里当证据；复现不出来就写「未验证：<原因>」，\n"
            "   不要用「打算怎么验」顶替。边界：只跑不写真实数据、不外呼的复现，\n"
            "   拿不准就把涉事文件复制到 /tmp 的一次性副本里跑。验证跑在服务端工作区\n"
            "   （{workspace}），它覆盖源码层，不覆盖事发进程的运行态——\n"
            "   补丁是否已进入正在运行的进程，按 5) 的「后续动作」如实说明，\n"
            "   也不要在事发进程里 reload/hook 制造「看起来生效」。\n"
        )
        verify_hint = (
            "（先给证据：复现命令、退出码、改动前失败与改动后通过的关键输出；"
            "无证据则写「未验证：<原因>」；再写可能的副作用）"
        )
    else:
        verify_step = (
            "4) 本次不要求执行验证（DEBUGGER_VERIFY=0）：在「验证/风险」里写清该怎样验，"
            "并注明「未执行验证」。\n"
        )
        verify_hint = "（打算如何确认修复有效，并注明「未执行验证」；可能的副作用）"
    # verify_step 是在 _SKILL_TMPL.format 之前构造的普通字符串，所以它里面的
    # {workspace} 不会被模板替换——这里显式补上，免得报告里出现占位符。
    verify_step = verify_step.replace("{workspace}", event.get("workspace") or os.getcwd())
    return _SKILL_TMPL.format(
        verify_step=verify_step,
        verify_hint=verify_hint,
        agent_id=_CFG["agent_id"],
        project=event.get("project") or "?",
        pid=event.get("pid"),
        started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_CFG["started"] or time.time())),
        sid_preview="(首次请求后由服务端分配)",
        eid=event["eid"],
        step=event["step"],
        snapshot=json.dumps(slim, ensure_ascii=False, indent=1),
        workspace=event.get("workspace") or os.getcwd(),
    )


def _headers():
    """请求头。配了 api_key 才带 Authorization —— 裸 Agent Service 不需要，
    而带 Bearer 的上游（如 hh 那套）不带会直接 401，两种都只能靠这个函数支持。
    令牌只出现在请求头里：不打印、不进事件、不进日志。"""
    h = {"Content-Type": "application/json"}
    if _CFG.get("api_key"):
        h["Authorization"] = "Bearer %s" % _CFG["api_key"]
    return h


def _post_json(path, payload):
    """POST {agents_url}{path} 非流式，返回 (status, dict)。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=f"{_CFG['agents_url']}{path}", data=body,
        headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_CFG["agent_timeout"]) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 500 也带 JSON 信封：失败或被中止的推理如今同样完整落盘，所以能从响应体
        # 里拿回 session_id，用户可以到 Web 侧打开这条会话看它跑到的最后一个轮次。
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        return e.code, parsed or {"error": raw[:800]}
    except Exception as e:
        return 0, {"error": f"调用 {_CFG['agents_url']}{path} 失败: {e!r}"}


def _post_stream(path, payload):
    """POST {agents_url}{path} 走 SSE，把流重建成与 /v1/infer 同构的信封。

    交回的客户端工具调用有两个来源：服务端显式的 status=client_tool_calls 帧，以及
    "按工具名识别 + 本轮没有 tool 结果回应它"的兜底判定（并行调用里服务端那半有结果）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=f"{_CFG['agents_url']}{path}", data=body,
        headers=_headers(), method="POST")
    envelope = {"messages": [], "client_tool_calls": []}
    client_tool_names = {t["name"] for t in _client_tools_payload()}
    announced, answered, reported = [], set(), []
    try:
        with urllib.request.urlopen(req, timeout=_CFG["agent_timeout"]) as r:
            for line in r:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    frame = json.loads(line[6:])
                except ValueError:
                    continue
                if frame.get("session_id"):
                    envelope["session_id"] = frame["session_id"]
                role = frame.get("role")
                if role == "assistant" and frame.get("tool_calls"):
                    announced.extend(_merge_tool_call_frames(frame["tool_calls"]))
                elif role == "tool":
                    answered.add(frame.get("tool_use_id"))
                elif (role == "status"
                      and frame.get("status") == "client_tool_calls"):
                    reported = frame.get("client_tool_calls") or []
                if not role:
                    continue   # init / usage 等控制帧不是 message
                # 正文是分帧下发的：把连续的 assistant 帧拼成一条，才能拿到完整报告
                prev = envelope["messages"][-1] if envelope["messages"] else None
                if (role == "assistant" and not frame.get("tool_calls")
                        and prev is not None and prev.get("role") == "assistant"
                        and not prev.get("tool_calls")):
                    prev["content"] = (prev.get("content") or "") + (frame.get("content") or "")
                    if frame.get("thinking"):
                        prev["thinking"] = (prev.get("thinking") or "") + frame["thinking"]
                    continue
                envelope["messages"].append(dict(frame))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        return e.code, parsed or {"error": raw[:800]}
    except Exception as e:
        return 0, {"error": f"调用 {_CFG['agents_url']}{path} 失败: {e!r}"}
    envelope["success"] = True
    # 交回清单 = 服务端点名的(reported) ∪ 流里宣布过但没有回执的(fallback)，按 id 去重。
    # 只取 reported 会漏答: 并行批次里服务端没点名的那几条客户端调用永远不会被执行,
    # 会话中间就留下一枚 ⏳(真机 2026-11-08 有 4 个会话是这个形态, 全是 debugger_eval)。
    fallback = [c for c in announced
                if c["name"] in client_tool_names and c["id"] not in answered]
    merged_pending = OrderedDict()
    for call in list(reported) + fallback:
        cid = call.get("id")
        if cid is None or cid in answered:
            continue
        prev = merged_pending.get(cid)
        if prev is None or (not (prev.get("arguments") or "") and (call.get("arguments") or "")):
            merged_pending[cid] = call
    envelope["client_tool_calls"] = list(merged_pending.values())
    return 200, envelope


def _merge_tool_call_frames(calls):
    """把流式 assistant 帧里被切碎的工具调用片段拼回完整调用（按 _index 归并）。"""
    merged = OrderedDict()
    for index, call in enumerate(calls):
        key = call.get("_index")
        if key is None:
            key = call.get("index")
        if key is None:
            key = call.get("id") or index
        slot = merged.setdefault(key, {"id": None, "name": "", "arguments": ""})
        if call.get("id"):
            slot["id"] = call["id"]
        nested = call.get("function") or {}
        name = call.get("name") or nested.get("name")
        if name:
            slot["name"] += name
        arguments = call.get("arguments")
        if arguments is None:
            arguments = nested.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if arguments:
            slot["arguments"] += arguments
    return [c for c in merged.values() if c["name"] or c["id"]]


def _identity_payload() -> dict:
    """以哪个身份跑：agent（带它自己的 system prompt 与服务端工具）或裸 model_id。

    DEBUGGER_AGENT_ID= （空）+ DEBUGGER_MODEL_ID=xxx 可以不要 agent，直接指定模型；
    这种情况下服务端工具就只有请求里 tool_ids 给的那些。
    """
    if _CFG["agent_id"]:
        return {"agent_ids": [_CFG["agent_id"]]}
    return {"model_id": _CFG["model_id"]}


def _call_agent(prompt, workspace):
    """跑完整诊断闭环（含多轮客户端工具交回），返回 (ok, 最终文本或错误, session_id)。"""
    payload = {
        **_identity_payload(),
        # 必须用 messages：agent 带 system_prompt 时服务端会组装 messages，
        # 裸 text 字段会被空 messages 挤掉（上游报 "no user query found"）。
        "messages": [{"role": "user", "content": prompt}],
        # 工作区 = 事发源码所在目录，agent 在服务端的 exec_shell(cwd)/search_code/read_file 都锚定这里
        "workspace": workspace,
        # "new"：让 agentsvc 新建会话并完整记录全过程（含每轮工具调用）。非流式与
        # 流式共用服务端同一条推理管线：输入轮在推理开始前就落盘、每个完整轮次增量
        # 落盘，所以跑的过程中就能凭 id 在 Web 侧回查这条会话，必要时也能用
        # POST /v1/infer/abort 把它停掉（诊断跑几十分钟时很值钱）。
        "session_id": "new",
        # 客户端工具：只随本次会话下发，不进服务端 ToolRegistry（GET /v1/tools 查不到），
        # 服务端也不会执行它们；后续续跑请求不必重复带，服务端按 session 记录继续可见。
        "client_tools": _client_tools_payload(),
    }
    # max_rounds 为 null 时不带 max_tool_rounds：推理循环次数交给 agent service 自身
    # 决定（服务端是 body.get("max_tool_rounds") or env_int("MAX_TOOL_ROUNDS", 200)）。
    if _CFG["max_rounds"]:
        payload["max_tool_rounds"] = int(_CFG["max_rounds"])

    session_id = None
    final = None
    handoff_round = 0
    path = "/v1/infer/stream" if _CFG["stream"] else "/v1/infer"
    while True:
        status, data = (_post_stream(path, payload) if _CFG["stream"]
                        else _post_json(path, payload))
        # status==0 且 error 是我们自己拼的"调用 … 失败:"= 纯传输故障(连接被重置/读超时)。
        # 这一步断了但服务端会话还在: 重试一次, 否则这条会话就永远停在 🛠️ ⏳ 那一帧。
        if status == 0 and str(data.get("error") or "").startswith("调用 "):
            _print(f"续跑请求传输失败 => 重试 1 次: {str(data.get('error'))[:160]}")
            time.sleep(2.0)
            status, data = (_post_stream(path, payload) if _CFG["stream"]
                            else _post_json(path, payload))
        session_id = data.get("session_id") or session_id
        if status != 200:
            return False, str(data.get("error") or f"HTTP {status}")[:2000], session_id
        for m in data.get("messages") or []:
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                final = m["content"]
        pending = data.get("client_tool_calls") or []
        if not pending:
            if final is None:
                return False, "agent 无最终文本回复: " + json.dumps(
                    (data.get("messages") or [])[-1:], ensure_ascii=False)[:1500], session_id
            return True, final, session_id

        # 模型调用了客户端工具：本进程就地执行，结果 + session id 再请求一次推理会话。
        handoff_round += 1
        with _LOCK:
            _STATE["handoffs"] += 1
        if handoff_round > int(_CFG["handoff_rounds"]):
            return False, (f"客户端工具交回轮次超过 {int(_CFG['handoff_rounds'])} 次，"
                           f"中止（session_id={session_id}）"), session_id
        results = []
        for call in pending:
            results.append({
                "role": "tool",
                "name": call.get("name"),
                "tool_use_id": call.get("id"),
                "content": _execute_client_tool(call.get("name"), call.get("arguments")),
            })
        payload = {
            **_identity_payload(),
            "workspace": workspace,
            "session_id": session_id,
            # 工具集要和首轮一致（服务端不替请求补工具）；client_tools 重复带一次最
            # 稳妥——服务端也会从会话记录里恢复，显式带上则不依赖会话 meta 的存续。
            "client_tools": _client_tools_payload(),
            "messages": results,
        }
        if _CFG["max_rounds"]:
            payload["max_tool_rounds"] = int(_CFG["max_rounds"])


# ---------------------------------------------------------------- sync 模式跨进程冷却


def _cooldown_load():
    try:
        with open(os.path.expanduser(_CFG["cooldown_file"]), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cooldown_hit(fp):
    """短命子进程每次重开内存指纹表都是空的；同一坑反复同步诊断会把父进程反复拖住
    最长 sync_timeout 秒。同一指纹 cooldown_sec 秒内派发过就跳过。"""
    ts = _cooldown_load().get(fp)
    if ts and time.time() - float(ts) < _CFG["cooldown_sec"]:
        _print(f"指纹 {fp} 于 {time.time() - float(ts):.0f}s 前已派发过"
               f"（冷却 {_CFG['cooldown_sec']}s），本次跳过诊断")
        return True
    return False


def _cooldown_mark(fp):
    try:
        with _COOLDOWN_LOCK:
            data = _cooldown_load()
            now = time.time()
            data = {k: v for k, v in data.items() if now - float(v) < 7 * 86400}
            data[fp] = now
            path = os.path.expanduser(_CFG["cooldown_file"])
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
    except Exception as e:
        _print(f"写冷却文件失败: {e!r}")


# ---------------------------------------------------------------- 诊断执行（worker/sync 共用）


def _run_diagnosis(ev, box=None):
    fp = ev["fingerprint"]
    if _CFG["sync"]:
        _cooldown_mark(fp)
    try:
        ok, text, sid = _call_agent(_build_prompt(ev),
                                    ev.get("workspace") or os.getcwd())
    except Exception as e:
        ok, text, sid = False, f"诊断执行异常: {e!r}", None
    with _LOCK:
        rec = _FINGERPRINTS.get(fp)
        if rec:
            rec["dispatched"] = True
            rec["reported"] = ok
        _STATE["dispatch_ok" if ok else "dispatch_fail"] += 1
        ev["session_id"] = sid
        ev["dispatched_at"] = _now()
    _print(f"eid={ev['eid']} fp={fp} 诊断{'完成' if ok else '失败'} "
           f"session_id={sid or '?'} 本地工具执行={_STATE['handoffs']} 次")
    _print("——诊断报告——\n" + str(text)[:2000] + "\n——报告结束——")
    if box is not None:
        box.update({"ok": ok, "text": text, "session_id": sid})


def _dispatch_sync(ev):
    """在独立线程里跑诊断并 join 等待：超时也要放行，主进程不能因诊断挂死。
    等待期间进程保持存活，现场工具可被实时查询（模型每调一次都由本进程就地执行）。"""
    if _cooldown_hit(ev["fingerprint"]):
        return
    box = {}
    t = threading.Thread(target=_run_diagnosis, args=(ev, box),
                         name="debugger-sync", daemon=True)
    t.start()
    t.join(max(30, _CFG["sync_timeout"]))
    if t.is_alive():
        _print(f"eid={ev['eid']} 同步诊断超过 {_CFG['sync_timeout']}s 未返回，"
               f"放弃等待（进程继续退出，现场工具随之不可用）")


def _worker_loop():
    while True:
        try:
            ev = _QUEUE.get()
        except Exception:
            return
        try:
            _run_diagnosis(ev)
        except Exception as e:
            _STATE["dispatch_fail"] += 1
            _print(f"worker 异常: {e!r}")


def _start_worker():
    with _WORKER_LOCK:
        if _STATE["worker"]:
            return
        _STATE["worker"] = True
    threading.Thread(target=_worker_loop, name="debugger-worker",
                     daemon=True).start()


# ---------------------------------------------------------------- 启动入口


def start(project=None, agents_url=None, agent_id=None, model_id=None,
          agent_enabled=None, sync=None, max_rounds=None, stream=None,
          eval_enabled=None, handoff_rounds=None, api_key=None, workspace=None,
          verify_fix=None, **kwargs):
    """启动诊断通道（幂等）。sync=True 时 report() 就地同步诊断（短命子进程用，
    不常驻 worker），否则后台 worker 异步派发。
    max_rounds=None（默认）不自行指定工具轮数，由 agent service 自身的次数控制决定；
    stream=True 用 /v1/infer/stream 跑诊断；eval_enabled=False 连 debugger_eval 的
    注册 JSON 都不下发（现场工具只剩只读那几个）。
    api_key：上游要求 Bearer 时给（如 hh 那套 Agent Service 的 HH_AGENTSVC_TOKEN）；
    只进请求头，不进日志/事件/产物。workspace：服务端 workspace 根目录，跨端诊断
    （事发在手机上、agent 在服务器上看仓库源码）必须显式给仓库路径。"""
    if _STATE["started"]:
        return True
    with _LOCK:
        if _STATE["started"]:
            return True
        _CFG["project"] = project or os.path.basename(os.getcwd())
        _CFG["agents_url"] = (agents_url or os.environ.get(
            "DEBUGGER_AGENTS_URL") or _DEFAULTS["agents_url"]).rstrip("/")
        # agent_id 传空串（或 DEBUGGER_AGENT_ID=）表示不用 agent，改走 DEBUGGER_MODEL_ID
        _CFG["agent_id"] = (
            agent_id if agent_id is not None
            else os.environ.get("DEBUGGER_AGENT_ID", _DEFAULTS["agent_id"])
        ) or ""
        _CFG["model_id"] = model_id or os.environ.get(
            "DEBUGGER_MODEL_ID", _DEFAULTS["model_id"])
        if not _CFG["agent_id"] and not _CFG["model_id"]:
            _CFG["agent_id"] = _DEFAULTS["agent_id"]  # 两者都没给，回到默认 agent
        # 令牌 / 服务端工作区：start 参数 > 环境变量 > 默认(空)。
        # api_key 只用于请求头，不参与指纹/事件/日志（日志里只出现「Bearer/无」）。
        _CFG["api_key"] = str(api_key or os.environ.get("DEBUGGER_API_KEY", "") or "")
        _CFG["workspace"] = str(workspace or os.environ.get("DEBUGGER_WORKSPACE", "") or "")
        # max_rounds：start 参数 > 环境变量 > 默认(null)；null 或非法值都不带 max_tool_rounds，
        # 推理循环次数交给 agent service 自身的次数控制（它有自己的默认/环境变量）。
        _rounds = max_rounds if max_rounds is not None else os.environ.get("DEBUGGER_MAX_ROUNDS", "")
        _CFG["max_rounds"] = int(_rounds) if str(_rounds).strip().isdigit() else None
        _CFG["agent_timeout"] = int(os.environ.get(
            "DEBUGGER_AGENT_TIMEOUT", _DEFAULTS["agent_timeout"]))
        _CFG["handoff_rounds"] = int(handoff_rounds or os.environ.get(
            "DEBUGGER_HANDOFF_ROUNDS", _DEFAULTS["handoff_rounds"]))
        _CFG["stream"] = bool(stream) or os.environ.get("DEBUGGER_STREAM", "") == "1"
        _CFG["eval_enabled"] = not (
            eval_enabled is False or os.environ.get("DEBUGGER_EVAL", "") == "0")
        # 红→绿复现验证：start 参数 > 环境变量 > 默认(要求)。
        _verify = (verify_fix if verify_fix is not None
                   else os.environ.get("DEBUGGER_VERIFY", "1") != "0")
        _CFG["verify_fix"] = bool(_verify)
        _CFG["sync"] = bool(sync) or os.environ.get("DEBUGGER_SYNC", "") == "1"
        _CFG["sync_timeout"] = int(os.environ.get(
            "DEBUGGER_SYNC_TIMEOUT", _DEFAULTS["sync_timeout"]))
        _CFG["cooldown_sec"] = int(os.environ.get(
            "DEBUGGER_COOLDOWN_SEC", _DEFAULTS["cooldown_sec"]))
        _CFG["cooldown_file"] = os.environ.get(
            "DEBUGGER_COOLDOWN_FILE", _DEFAULTS["cooldown_file"])
        if agent_enabled is not None:
            _CFG["agent_enabled"] = bool(agent_enabled)
        if os.environ.get("DEBUGGER_AGENT_ENABLED") == "0":
            _CFG["agent_enabled"] = False  # 观察模式
        _CFG["started"] = time.time()
        _STATE["started"] = True
        if _CFG["agent_enabled"] and not _CFG["sync"]:
            _start_worker()
    _print(f"已启动：agent={_CFG['agent_id']}@{_CFG['agents_url']} "
           f"鉴权={'Bearer' if _CFG['api_key'] else '无'} "
           f"现场接口=client side tool calls（{len(_client_tools_payload())} 个，无监听端口） "
           f"工作区={_CFG['workspace'] or '(按事发帧推导)'} "
           f"轮数上限={_CFG['max_rounds'] or 'agent service 自身默认'} "
           f"复现验证={'红→绿' if _CFG['verify_fix'] else '不要求'} "
           f"交回轮数上限={_CFG['handoff_rounds']} "
           f"通道={'流式' if _CFG['stream'] else '非流式'} "
           f"模式={'同步' if _CFG['sync'] else '异步'} "
           f"诊断={'开' if _CFG['agent_enabled'] else '关(仅现场快照)'}")
    return True


def info():
    """供宿主程序查看当前配置。"""
    return dict(_CFG)

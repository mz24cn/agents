#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""online_debugger.py — Python 进程「异常快照 + 只读 inspector + Coder agent 诊断」最小实现。

接入方式（任何 Python 程序）：
    import online_debugger as debugger   # 旧调用点写法（debugger.start/report）保持不变
    debugger.start(project="X7991")      # 程序启动时调用一次（幂等）
    ...
    try:
        ...
    except Exception as e:
        debugger.report("步骤名", e)      # 快照+派发；异步模式入队即返回，sync 模式就地诊断

组成：
1. 异常快照：沿 exc.__traceback__ 抓每帧的文件/行/函数/源码行/局部变量（截断）。
   traceback 持有帧对象引用，函数已退出后 f_locals 依然可读，所以快照是"案发瞬间"
   的真实数据。合成异常（无 __traceback__）退化为抓当前调用栈。
2. 只读 inspector：标准库 http.server，绑 127.0.0.1，默认端口 51110（占用则自动+1）。
   GET /（管理首页）/status /threads /frame?fid= /events /event?eid= /help
   诊断 agent 不需要 MCP，直接用 exec_shell + curl 查询进程实时数据；
   /eval /setting /redispatch 是管理操作通道（同样只绑回环）。
3. 诊断闭环：worker（异步）或 report 调用点（sync）把「快照 + inspector 接口说明」
   拼成 user prompt，POST {agents_url}/v1/infer（agent_ids=["Coder"],
   workspace=事发源码目录, session_id="new"）。agent 基于现场数据定位根因、必要时
   直接改码；会话全过程（含每次工具调用）由 agentsvc 按 session_id 落盘备查，
   最终报告（## 问题 / ## 根因 / ## 修改 / ## 验证/风险 / ## 后续动作）打印到 stdout。
4. 错误指纹：异常类型 + 栈帧函数序列的 md5，内存 map；同一指纹进程内只派发一次；
   sync 模式额外落一个跨进程冷却文件（~/.debugger_cooldown.json），短命子进程反复
   踩同一坑时不会每次阻塞去诊断。

配置优先级：start() 参数 > 环境变量 > 默认值
    DEBUGGER_ENABLED=0                   整套关闭
    DEBUGGER_AGENT_ENABLED=0             观察模式：只做快照+inspector，不派发 Coder
    DEBUGGER_INSPECTOR_PORT=51110
    DEBUGGER_AGENTS_URL=http://localhost:7988
    DEBUGGER_AGENT_ID=Coder
    DEBUGGER_MAX_ROUNDS=30               可选：agent 工具轮数上限；默认不设(null)，由 agent service 自身控制
    DEBUGGER_SYNC=1                      子进程模式：report 就地同步诊断（默认关）
    DEBUGGER_SYNC_TIMEOUT=900            sync 诊断最长等待（秒）
    DEBUGGER_COOLDOWN_SEC=1800           sync 模式同指纹跨进程冷却窗口（秒）
    DEBUGGER_AGENT_TIMEOUT=1200          单次诊断超时（秒）

安全/边界：
- inspector 查询接口只读、只绑回环；/ 首页提供 eval/开关派发/再派发 等管理操作（能改
  进程状态，仅限本机使用）；handler 自身异常一律吞掉。
- report() 永不抛异常；队列满即丢；诊断线程为 daemon，随主进程退出。
- 局部变量 repr 截断 200 字符、每帧最多 30 个；conn/cur/self/response 等对象剔除，
  避免大对象与敏感内容进入快照和 prompt。
"""

import hashlib
import html
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- 配置与全局状态

_DEFAULTS = {
    "project": None,
    "inspector_port": 51110,
    "inspector_bind": "127.0.0.1",
    "agents_url": "http://localhost:7988",
    "agent_id": "Coder",
    # 默认不设（null）：不自行指定工具轮数，交由 agent service 自身的次数控制
    "max_rounds": None,
    "agent_timeout": 1200,
    "agent_enabled": True,
    "max_events": 32,
    "sync": False,          # True：report() 就地同步诊断（短命子进程用）
    "sync_timeout": 900,    # sync 诊断最长等待（秒）
    "cooldown_sec": 1800,   # sync 模式同指纹跨进程去重的冷却窗口（秒）
    "cooldown_file": "~/.debugger_cooldown.json",
}

_CFG = dict(_DEFAULTS)
_CFG["port"] = None          # 实际绑定的 inspector 端口
_CFG["started"] = None       # epoch 秒

_LOCK = threading.Lock()
_EVENTS = OrderedDict()      # eid -> event dict（完整快照，traceback 使帧保活）
_FINGERPRINTS = {}           # fp   -> {"count","first_ts","last_ts","eid","dispatched","reported"}
_FRAME_CACHE = OrderedDict() # fid  -> {"file","line","func","snap"}（LRU，强引用帧避免 id 复用错帧）
_FRAME_CACHE_MAX = 512
_QUEUE = queue.Queue(maxsize=64)
_WORKER_LOCK = threading.Lock()
_COOLDOWN_LOCK = threading.Lock()
_STATE = {"started": False, "server": None, "worker": False,
          "dispatch_ok": 0, "dispatch_fail": 0}

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
    """把一帧序列化成 dict 并注册进 fid 缓存（inspector 可按 fid 复查）。"""
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
    basis = exc_type_name + "|" + "|".join(
        f"{f['func']}@{os.path.basename(f['file'])}" for f in frames)
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
                 "workspace": _derive_workspace(frames)}
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
            _dispatch_sync(event)   # 子进程：阻塞到诊断结束（进程还活着，现场可实时 curl）
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


def _print(msg):
    try:
        print(f"[debugger] {msg}")
    except Exception:
        pass


# ---------------------------------------------------------------- Coder agent 调用

_SKILL_TMPL = """你是运行时故障诊断代理，代号 {agent_id}。目标程序 {project}（pid={pid}，启动于 {started}）内置了一个只读 HTTP inspector，基址 http://127.0.0.1:{port}。诊断必须基于它返回的第一手现场数据，不要凭空推理。

接口（全部 GET，返回 JSON）：
  /status                       进程概况与统计
  /threads                      所有线程当前调用栈；每帧含 fid，可用 /frame 复查其局部变量
  /frame?fid=<fid>              指定帧在捕获时刻的局部变量与源码行（异常快照帧同样可查）
  /events                       已捕获错误事件列表：eid、指纹、出现次数、是否已派发
  /event?eid=<eid>              某错误的完整快照（含各帧 fid）
  /help                         以上接口说明
示例：
  curl -s http://127.0.0.1:{port}/events
  curl -s "http://127.0.0.1:{port}/event?eid={eid}"
  curl -s "http://127.0.0.1:{port}/frame?fid=7f8e9a0b1c2d"
说明：局部变量 repr 截断到 200 字符、每帧最多 30 个；conn/cur/self/response 等对象被剔除；
快照是异常瞬间的数据；线程实时状态用 /threads 现抓现查；inspector 只读，不会改变进程状态。
工作区：本次会话 workspace 已设为 {workspace}，exec_shell 的 cwd 以及 read_file/search_code/
edit_file 都在该目录下工作，源码请直接读文件，不必经 inspector。

任务：
1) 先读下方异常快照，缺什么现场数据就 curl 补什么（允许多轮）。
2) 用 read_file / search_code 阅读涉事源码，定位根因。
3) 处置规则：
   - 代码缺陷且有把握修复：用 edit_file 直接做最小化修改（改动文件即可，勿重启进程）。
   - 网络抖动/限流/对端瞬时错误：不要改代码，给出重试/退避/降级建议。
   - 涉及数据库写入、外部系统配置的动作：只写建议，不要执行。
4) 最后一条消息严格按以下格式输出（本次会话由 agentsvc 按 session_id 全程存档）：
## 问题
（一句话现象 + 触发条件）
## 根因
（基于现场数据的判断，引用你查到的帧/变量/接口结果）
## 修改
（文件路径 + 改动摘要；未改代码则写"未修改"并说明理由）
## 验证/风险
（如何确认修复有效；可能的副作用）
## 后续动作
（如：需重启进程生效 / 建议重跑哪些任务 / 无需动作）

本次异常快照（eid={eid}，step={step!r}）：
```json
{snapshot}
```
"""


def _build_prompt(event):
    slim = {k: v for k, v in event.items() if k != "frames"}
    slim["frames"] = event["frames"]
    return _SKILL_TMPL.format(
        agent_id=_CFG["agent_id"],
        project=event.get("project") or "?",
        pid=event.get("pid"),
        started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_CFG["started"] or time.time())),
        port=_CFG["port"],
        eid=event["eid"],
        step=event["step"],
        snapshot=json.dumps(slim, ensure_ascii=False, indent=1),
        workspace=event.get("workspace") or os.getcwd(),
    )


def _call_agent(prompt, workspace):
    """POST {agents_url}/v1/infer（非流式），返回 (ok, 最终文本或错误, session_id)。"""
    payload = {
        "agent_ids": [_CFG["agent_id"]],
        # 必须用 messages：agent 带 system_prompt 时服务端会组装 messages，
        # 裸 text 字段会被空 messages 挤掉（上游报 "no user query found"）。
        "messages": [{"role": "user", "content": prompt}],
        # 工作区 = 事发源码所在目录，agent 的 exec_shell(cwd)/search_code/read_file 都锚定到这里
        "workspace": workspace,
        # "new"：让 agentsvc 新建会话并完整记录全过程（含每轮工具调用）。非流式与
        # 流式共用服务端同一条推理管线：输入轮在推理开始前就落盘、每个完整轮次增量
        # 落盘，所以跑的过程中就能凭 id 在 Web 侧回查这条会话，必要时也能用
        # POST /v1/infer/abort 把它停掉（诊断跑几十分钟时很值钱）。
        "session_id": "new",
    }
    # max_rounds 为 null 时不带 max_tool_rounds：推理循环次数交给 agent service 自身
    # 决定（服务端是 body.get("max_tool_rounds") or env_int("MAX_TOOL_ROUNDS", 200)）。
    if _CFG["max_rounds"]:
        payload["max_tool_rounds"] = int(_CFG["max_rounds"])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=f"{_CFG['agents_url']}/v1/infer", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_CFG["agent_timeout"]) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 500 也带 JSON 信封：失败或被中止的推理如今同样完整落盘，所以能从响应体
        # 里拿回 session_id，用户可以到 Web 侧打开这条会话看它跑到的最后一个轮次。
        raw = ""
        sid = None
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            parsed = json.loads(raw)
            sid = parsed.get("session_id")
            raw = str(parsed.get("error") or raw)
        except Exception:
            pass
        return False, f"HTTP {e.code} {raw[:800]}", sid
    except Exception as e:
        return False, f"调用 {_CFG['agents_url']}/v1/infer 失败: {e!r}", None
    sid = data.get("session_id")
    if not data.get("success", True):
        return False, str(data.get("error"))[:2000], sid
    final = None
    for m in data.get("messages") or []:
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            final = m["content"]
    if final is None:
        return False, "agent 无最终文本回复: " + json.dumps(
            (data.get("messages") or [])[-1:], ensure_ascii=False)[:1500], sid
    return True, final, sid


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
           f"session_id={sid or '?'}")
    _print("——诊断报告——\n" + str(text)[:2000] + "\n——报告结束——")
    if box is not None:
        box.update({"ok": ok, "text": text, "session_id": sid})


def _dispatch_sync(ev):
    """在独立线程里跑诊断并 join 等待：超时也要放行，主进程不能因诊断挂死。
    等待期间进程保持存活，inspector 在线，agent 可以实时 curl 现场。"""
    if _cooldown_hit(ev["fingerprint"]):
        return
    box = {}
    t = threading.Thread(target=_run_diagnosis, args=(ev, box),
                         name="debugger-sync", daemon=True)
    t.start()
    t.join(max(30, _CFG["sync_timeout"]))
    if t.is_alive():
        _print(f"eid={ev['eid']} 同步诊断超过 {_CFG['sync_timeout']}s 未返回，"
               f"放弃等待（进程继续退出，inspector 随之消失）")


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


# ---------------------------------------------------------------- inspector (http.server)


class _HttpError(Exception):
    def __init__(self, code, payload):
        self.code, self.payload = code, payload


def _all_threads_snapshot():
    names = {t.ident: t.name for t in threading.enumerate()}
    me = threading.get_ident()
    out = []
    for tid, top in sys._current_frames().items():
        chain, f = [], top
        while f is not None and len(chain) < 20:
            chain.append(_capture_frame(f))
            f = f.f_back
        out.append({"tid": tid, "name": names.get(tid, "?"),
                    "is_inspector_handler": tid == me,
                    "frames": chain})  # 栈顶在前
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


def _h_eval(query):
    expr = (query.get("expr") or [""])[0]
    fid = (query.get("fid") or [""])[0]
    if not expr.strip():
        raise _HttpError(400, {"error": "expr 必填"})
    if fid:
        with _LOCK:
            hit = _FRAME_CACHE.get(fid)
            if hit:
                _FRAME_CACHE.move_to_end(fid)
        if not hit:
            raise _HttpError(404, {"error": f"fid={fid} 不在缓存（先从 /event 或 /threads 取）"})
        fr = hit["frame"]
        ns = dict(fr.f_globals)
        ns.update(fr.f_locals)     # 快照帧：函数已退出但 traceback 让 f_locals 保活
        snap = hit["snap"]
        scope = f"帧 {snap.get('func')}()@{snap.get('line')}"
    else:
        ns = dict(globals())       # debugger 模块作用域：可用 _EVENTS/_CFG/_STATE 及全部内部函数
        scope = "debugger 模块作用域"
    try:
        result = eval(expr, ns)    # 管理入口：只绑 127.0.0.1，运行任意表达式是有意的
        return {"ok": True, "scope": scope, "expr": expr,
                "result": _safe_repr(result, 4000)}
    except Exception as e:
        return {"ok": False, "scope": scope, "expr": expr,
                "error": f"{type(e).__name__}: {e}"}


def _h_setting(query):
    v = (query.get("agent_enabled") or [""])[0]
    if v not in ("0", "1"):
        raise _HttpError(400, {"error": "当前仅支持 agent_enabled=0|1"})
    _CFG["agent_enabled"] = v == "1"
    if _CFG["agent_enabled"] and not _CFG["sync"]:
        _start_worker()
    _print(f"agent_enabled -> {_CFG['agent_enabled']}")
    return {"ok": True, "agent_enabled": _CFG["agent_enabled"],
            "sync": _CFG["sync"]}


def _h_redispatch(query):
    eid = (query.get("eid") or [""])[0]
    with _LOCK:
        ev = _EVENTS.get(eid)
    if not ev:
        raise _HttpError(404, {"error": f"eid={eid} 不存在或已被环形淘汰"})
    if _CFG["sync"]:
        threading.Thread(target=_run_diagnosis, args=(ev,),
                         name="debugger-redispatch", daemon=True).start()
        return {"ok": True, "eid": eid, "mode": "sync-thread"}
    _start_worker()
    try:
        _QUEUE.put_nowait(ev)
    except queue.Full:
        raise _HttpError(503, {"error": "诊断队列已满"})
    return {"ok": True, "eid": eid, "mode": "queued"}


def _index_html():
    esc = html.escape
    st = _route("/status", {})
    rows = []
    for e in _events_summary()["events"]:
        cells = [e["ts"][:19], e["eid"], str(e["step"]), e["type"],
                 (e["msg"] or "")[:80], str(e["count"]),
                 "已派发" if e["dispatched"] else "未",
                 e.get("session_id") or ""]
        ops = (f"<a href='/event?eid={e['eid']}'>快照</a> "
               f"<a href='/redispatch?eid={e['eid']}'>再诊断</a>")
        rows.append("<tr><td>" + "</td><td>".join(esc(c) for c in cells)
                    + f"</td><td>{ops}</td></tr>")
    table = ("<table><tr><th>时间</th><th>eid</th><th>步骤</th><th>类型</th><th>消息</th>"
             "<th>次数</th><th>诊断</th><th>session_id</th><th>操作</th></tr>"
             + ("".join(rows) or "<tr><td colspan=9>（暂无事件）</td></tr>") + "</table>")
    toggle = ("<a href='/setting?agent_enabled=0'>■ 关闭诊断派发</a>"
              if _CFG["agent_enabled"] else
              "<a href='/setting?agent_enabled=1'>▶ 启动诊断派发</a>")
    css = ("body{font-family:system-ui,sans-serif;margin:24px;background:#fafafa;color:#222}"
           "table{border-collapse:collapse;background:#fff}"
           "td,th{border:1px solid #ddd;padding:4px 8px;font-size:13px}"
           "input[type=text]{padding:4px}input[type=submit]{padding:4px 16px}"
           ".warn{color:#a33}")
    return (f"<!doctype html><meta charset='utf-8'>"
            f"<title>debugger · {esc(str(st['project']))}</title><style>{css}</style>"
            f"<h2>debugger — {esc(str(st['project']))}</h2>"
            f"<p>pid={st['pid']} · {esc(str(st['python']))} · "
            f"inspector :{st['inspector_port']} · 运行 {st['uptime_sec']}s · "
            f"线程 {st['threads']}<br>"
            f"agent={esc(str(st['agent']))} · 派发 ok {st['dispatch_ok']} / fail {st['dispatch_fail']}"
            f" · 队列 {st['queued']} · 指纹 {st['fingerprints']} · 事件 {st['events_stored']}"
            f" · 模式 <b>{'sync' if st['sync'] else 'async'}</b>"
            f" · 诊断开关 <b>{st['agent_enabled']}</b>　{toggle}</p>"
            f"<h3>错误事件</h3>{table}"
            f"<h3>表达式求值（eval）</h3>"
            f"<form method='get' action='/eval'>"
            f"<input type='text' name='expr' size='70' required "
            f"placeholder='如 len(_EVENTS) 或 _CFG[&quot;agents_url&quot;]'> "
            f"<input type='text' name='fid' size='16' placeholder='fid 可选'>"
            f"<input type='submit' value='执行'></form>"
            f"<p class='warn'>eval 在目标进程内执行任意表达式（可读可改）；"
            f"填 fid 时在该快照帧的作用域求值（局部变量可用），留空则在 debugger 模块作用域。</p>"
            f"<p><a href='/help'>接口</a> · <a href='/status'>status</a> · "
            f"<a href='/threads'>线程实时栈</a></p>")


def _route(path, query):
    if path == "/status":
        with _LOCK:
            n_fp = len(_FINGERPRINTS)
        return {"pid": os.getpid(), "project": _CFG["project"],
                "python": sys.version.split()[0],
                "inspector_port": _CFG["port"],
                "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_CFG["started"] or 0)),
                "uptime_sec": int(time.time() - (_CFG["started"] or time.time())),
                "threads": threading.active_count(),
                "events_stored": len(_EVENTS), "fingerprints": n_fp,
                "queued": _QUEUE.qsize(),
                "dispatch_ok": _STATE["dispatch_ok"],
                "dispatch_fail": _STATE["dispatch_fail"],
                "agent": f"{_CFG['agent_id']}@{_CFG['agents_url']}",
                "max_rounds": _CFG["max_rounds"],   # null = 由 agent service 自身的次数控制
                "agent_enabled": _CFG["agent_enabled"], "sync": _CFG["sync"]}
    if path == "/threads":
        return {"threads": _all_threads_snapshot()}
    if path == "/frame":
        fid = (query.get("fid") or [""])[0]
        with _LOCK:
            hit = _FRAME_CACHE.get(fid)
            if hit:
                _FRAME_CACHE.move_to_end(fid)
        if not hit:
            raise _HttpError(404, {"error": f"fid={fid} 不在缓存（先调 /threads 或 /event 注册）"})
        return {"fid": fid, "captured_at": time.time(), **hit["snap"]}
    if path == "/events":
        return _events_summary()
    if path == "/event":
        eid = (query.get("eid") or [""])[0]
        with _LOCK:
            ev = _EVENTS.get(eid)
        if not ev:
            raise _HttpError(404, {"error": f"eid={eid} 不存在或已被环形淘汰"})
        return ev
    if path == "/eval":
        return _h_eval(query)
    if path == "/setting":
        return _h_setting(query)
    if path == "/redispatch":
        return _h_redispatch(query)
    if path == "/help":
        return {"endpoints": ["/ (管理首页 HTML)", "/status", "/threads",
                              "/frame?fid=<fid>", "/events", "/event?eid=<eid>",
                              "/eval?expr=<expr>[&fid=<fid>]",
                              "/setting?agent_enabled=0|1",
                              "/redispatch?eid=<eid>", "/help"],
                "note": "查询接口只读；eval/setting/redispatch 是管理通道（仅 127.0.0.1）"}
    raise _HttpError(404, {"error": f"未知路径 {path}，试试 /help"})


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass  # 客户端断开等，静默

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                self._send(200, _index_html().encode("utf-8"),
                           "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"首页渲染失败: {e!r}".encode("utf-8"),
                           "text/plain; charset=utf-8")
            return
        try:
            payload = _route(parsed.path, parse_qs(parsed.query))
            status = 200
        except _HttpError as e:
            status, payload = e.code, e.payload
        except Exception as e:
            status, payload = 500, {"error": f"inspector 内部错误: {e!r}"}
        self._send(status,
                   json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *args):  # 静音访问日志
        pass


def _pick_port(bind, base):
    for p in range(base, base + 16):
        try:
            srv = ThreadingHTTPServer((bind, p), _Handler)
            return srv, p
        except OSError:
            continue
    raise OSError(f"端口 {base}..{base + 15} 全部不可用")


# ---------------------------------------------------------------- 启动入口


def start(project=None, inspector_port=None, agents_url=None, agent_id=None,
          agent_enabled=None, sync=None, max_rounds=None, **kwargs):
    """启动 inspector + 诊断通道；幂等。sync=True 时 report() 就地同步诊断
    （短命子进程用，不常驻 worker），否则后台 worker 异步派发。
    max_rounds=None（默认）不自行指定推理循环次数，由 agent service 自身的次数控制决定。"""
    if _STATE["started"]:
        return True
    if os.environ.get("DEBUGGER_ENABLED", "1") == "0":
        return False
    with _LOCK:
        if _STATE["started"]:
            return True
        _CFG["project"] = project or os.path.basename(os.getcwd())
        _CFG["inspector_port"] = int(inspector_port or os.environ.get(
            "DEBUGGER_INSPECTOR_PORT", _DEFAULTS["inspector_port"]))
        _CFG["agents_url"] = (agents_url or os.environ.get(
            "DEBUGGER_AGENTS_URL") or _DEFAULTS["agents_url"]).rstrip("/")
        _CFG["agent_id"] = agent_id or os.environ.get(
            "DEBUGGER_AGENT_ID", _DEFAULTS["agent_id"])
        # max_rounds：start 参数 > 环境变量 > 默认(null)；null 或非法值都不带 max_tool_rounds，
        # 推理循环次数交给 agent service 自身的次数控制（它有自己的默认/环境变量）。
        _rounds = max_rounds if max_rounds is not None else os.environ.get("DEBUGGER_MAX_ROUNDS", "")
        _CFG["max_rounds"] = int(_rounds) if str(_rounds).strip().isdigit() else None
        _CFG["agent_timeout"] = int(os.environ.get("DEBUGGER_AGENT_TIMEOUT", _DEFAULTS["agent_timeout"]))
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
        try:
            srv, port = _pick_port(_CFG["inspector_bind"], _CFG["inspector_port"])
        except Exception as e:
            _print(f"inspector 启动失败: {e!r}（诊断快照功能降级为仅入队）")
            srv, port = None, None
        _CFG["port"] = port
        _CFG["started"] = time.time()
        _STATE["server"] = srv
        _STATE["started"] = True
        if srv is not None:
            t = threading.Thread(target=srv.serve_forever,
                                 name="debugger-inspector", daemon=True)
            t.start()
        if _CFG["agent_enabled"] and not _CFG["sync"]:
            _start_worker()
    _print(f"已启动：inspector=http://{_CFG['inspector_bind']}:{port} "
           f"agent={_CFG['agent_id']}@{_CFG['agents_url']} "
           f"轮数上限={_CFG['max_rounds'] or 'agent service 自身默认'} "
           f"模式={'同步' if _CFG['sync'] else '异步'} "
           f"诊断={'开' if _CFG['agent_enabled'] else '关(仅现场快照)'}")
    return True


def info():
    """供宿主程序查看当前配置。"""
    return dict(_CFG)

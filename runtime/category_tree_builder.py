"""会话分类树（tree.json）构建器：触发器 + 构建逻辑。

两个职责：

1. 触发：``GET /v1/sessions/tree`` 发现 tree.json 不存在且会话数量大于 5 时，
   调用 :func:`start_build` 在守护线程中执行本模块的构建逻辑（进程内，
   因此可以复用标题生成所用的推理函数为大模型命名分类）。
2. 构建：扫描 chats_dir 下全部会话，以"文件改动路径(file_journals) >
   workspace > 标题 > 正文"的加权文本做 TF-IDF + KMeans 递归分裂
   （scikit-learn 不可用时退化为纯 Python k-means），生成层次分类树，
   并自底向上递归调用大模型为每个分类命名（模型与标题生成相同：
   ``SUMMARY_MODEL_ID`` label，默认 "summary"；LLM 不可用/失败时回退为
   高频词名称）。

硬性规则（写盘前校验，违反即拒绝写出）：
  * 末级分类最多包含 ``max_leaf_size``（默认 10）个会话，超过即递归拆分；
  * 分类只有在确实拆成至少 2 个非空子组时才拥有子分类，
    任何分类都不会出现"只有一个子分类"的冗余层级；
  * 顶层不加包装层：全部会话若拆成"一个包含子分类的根"，顶层直接取
    该根的子分类；
  * 每个有 conversation.json 的会话至少挂载到一个末级分类；
  * 一个会话可同时出现在多个末级分类下（自动分类 + 人工分类并存，
    树结构本身仍是树，会话连接是图状的）。

命令行（离线手工构建，无大模型命名）::

    python -m runtime.category_tree_builder --chats-dir chat_data
    python -m runtime.category_tree_builder --chats-dir chat_data --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("runtime.category_tree_builder")

# ---------------------------------------------------------------------------
# 触发器（后台构建线程）
# ---------------------------------------------------------------------------

RETRY_COOLDOWN_SECONDS = 30.0
MIN_SESSIONS = 5
MAX_LEAF_SIZE = 10
DEFAULT_SEED = 137

_LOCK = threading.Lock()
_STATE = {
    "building": False,
    "last_error": None,
    "last_attempt": 0.0,
}


def make_summary_llm(session_manager):
    """基于 SessionManager 的推理函数构造 "prompt -> text" 函数。

    使用与标题生成相同的模型（SUMMARY_MODEL_ID label，默认 "summary"，
    经模型注册表解析）。推理函数不可用时返回 None（构建将退化为纯代码命名）。
    """
    infer_fn = getattr(session_manager, "inference_callable", None)
    registry = getattr(session_manager, "model_registry", None)
    if infer_fn is None:
        return None

    def llm(prompt: str) -> str:
        from runtime.models import InferenceRequest, Message

        model_id = os.environ.get("SUMMARY_MODEL_ID", "summary") or "summary"
        if registry is not None:
            config = registry.get(model_id)
            if config is None:
                raise RuntimeError(f"SUMMARY_MODEL_ID={model_id!r} not found in model registry")
            model_id = config.model_id
        result = infer_fn(InferenceRequest(
            model_id=model_id,
            messages=[Message(role="user", content=prompt)],
            stream=False,
        ))
        for msg in reversed(getattr(result, "messages", None) or []):
            if getattr(msg, "role", None) == "assistant" and getattr(msg, "content", None):
                return str(msg.content).strip()
        return ""

    return llm


def start_build(chats_dir: str, llm=None) -> bool:
    """启动一次后台构建（若未在进行中且冷却期已过）。

    Args:
        chats_dir: 会话数据目录。
        llm: 可选的 "prompt -> text" 大模型命名函数（见 make_summary_llm）。

    Returns:
        True 表示当前有构建在进行（新启动或复用），False 表示未启动。
    """
    if os.path.isfile(os.path.join(chats_dir, "tree.json")):
        return False
    with _LOCK:
        if _STATE["building"]:
            return True
        now = time.monotonic()
        if now - _STATE["last_attempt"] < RETRY_COOLDOWN_SECONDS:
            return False
        _STATE["building"] = True
        _STATE["last_attempt"] = now
        _STATE["last_error"] = None

    def _run() -> None:
        try:
            doc = build_chats_dir(chats_dir, llm=llm)
            if doc is None:
                _STATE["last_error"] = None  # 会话不足，跳过
                return
            _atomic_write_json(Path(chats_dir) / "tree.json", doc)
            _STATE["last_error"] = None
            logger.info("category tree build finished: %s", json.dumps(doc["stats"], ensure_ascii=False))
        except Exception as exc:
            _STATE["last_error"] = str(exc)
            logger.warning("category tree build failed: %s", exc, exc_info=True)
        finally:
            with _LOCK:
                _STATE["building"] = False

    threading.Thread(target=_run, name="category-tree-build", daemon=True).start()
    return True


def build_state() -> dict:
    """当前构建状态：{"building": bool, "last_error": str|None}。"""
    with _LOCK:
        return {"building": _STATE["building"], "last_error": _STATE["last_error"]}


def reset_state_for_tests() -> None:
    """重置模块状态（仅测试使用）。"""
    with _LOCK:
        _STATE["building"] = False
        _STATE["last_error"] = None
        _STATE["last_attempt"] = 0.0


# ---------------------------------------------------------------------------
# 会话扫描
# ---------------------------------------------------------------------------

GENERIC_ZH = {
    '可以', '这个', '一个', '如果', '需要', '问题', '修改', '代码', '文件', '使用',
    '进行', '现在', '已经', '检查', '实现', '相关', '处理', '功能', '逻辑', '方式',
    '支持', '完成', '测试', '分析', '查看', '添加', '结果', '通过', '模型', '用户',
    '消息', '会话', '工具', '前端', '后端', '显示', '操作', '运行', '方法', '内容',
    '当前', '应该', '没有', '然后', '我们', '一下', '发现', '确认', '修复', '让我们',
    '继续', '首先', '最后', '确保',
}
GENERIC_EN = {
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'let', 'now',
    'need', 'can', 'will', 'should', 'would', 'then', 'have', 'has', 'not', 'but',
    'are', 'was', 'were', 'you', 'your', 'our', 'use', 'using', 'used', 'add',
    'check', 'verify', 'changes', 'change', 'file', 'code', 'issue', 'problem',
    'support', 'model', 'user', 'message', 'tool', 'assistant', 'system', 'true',
    'false', 'none', 'null', 'python',
}

MAX_NAME_TERMS = 3
MAX_NAME_LEN = 30

# 聚类文档加权（重复次数 = 权重）：文件改动路径 > workspace > 标题。
# workspace 与会话实际改动的文件是"同一项目 / 同一工作目标"的硬信号，
# 权重高于标题，使同项目会话更可能聚到同一分支。
# _FILE_WEIGHT 取 16 为实测甜点位（共享文件会话对同末级分类率 4.7%→6.2%，
# 其中"仅 2 会话共享的文件"对从 8% 提升到 16%）；≥32 时文件签名过度主导，
# 聚类退化为按文件集合精确匹配、分支碎片化，指标反而下降。
_WORKSPACE_WEIGHT = 3
_FILE_WEIGHT = 16
_MAX_FILE_PATHS = 60  # 每个会话参与聚类的文件路径上限

# 大模型命名参数
MAX_NAMING_SAMPLES = 12
NAMING_SAMPLE_LINE_LIMIT = 160
NAMING_WORKERS = 4

_SKLEARN_OK = os.environ.get("SESSION_TREE_FORCE_FALLBACK") not in ("1", "true", "True")


def _optional_sklearn():
    """动态加载可选的 sklearn/numpy 依赖。

    runtime/ 有"零第三方依赖"硬约束（tests/test_dependencies.py 静态扫描
    import 语句），因此这里不用静态 import：sklearn 仅是分类树构建的可选
    加速依赖，环境可用时返回 (numpy, TfidfVectorizer, KMeans, silhouette_score)，
    不可用时返回 None（构建自动退化为纯 Python k-means）。
    """
    import importlib
    try:
        np = importlib.import_module("numpy")
        TfidfVectorizer = importlib.import_module("sklearn.feature_extraction.text").TfidfVectorizer
        KMeans = importlib.import_module("sklearn.cluster").KMeans
        try:
            silhouette_score = importlib.import_module("sklearn.metrics").silhouette_score
        except ImportError:
            silhouette_score = None
        return np, TfidfVectorizer, KMeans, silhouette_score
    except ImportError:
        return None


def _analyzer(text: str) -> list[str]:
    """分词：小写英文标识符（整词+拆分段）+ CJK 2/3/4-gram，去掉通用词。"""
    s = text.lower()
    toks: list[str] = []
    for t in re.findall(r"[a-z_][a-z0-9_./:@-]{1,}", s):
        toks.append(t)
        for x in re.split(r"[./:@_-]+", t):
            if len(x) > 2:
                toks.append(x)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", s):
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                toks.append(run[i:i + n])
    return [t for t in toks if t not in GENERIC_ZH and t not in GENERIC_EN]


def _clean_terms(raw: list[str], limit: int = MAX_NAME_TERMS) -> list[str]:
    """按分数顺序取词，去掉过短/纯符号/通用词/互含子串，保证名称可读。"""
    seen: list[str] = []
    for t in raw:
        t = str(t).strip()
        if len(t) < 2 or t in GENERIC_ZH or t in GENERIC_EN:
            continue
        if re.fullmatch(r"[0-9._:/@\-]+", t):
            continue
        if any(t == s or t in s or s in t for s in seen):
            continue
        seen.append(t)
        if len(seen) >= limit:
            break
    return seen


def _name_from_terms(terms) -> str:
    if not terms:
        return "未命名"
    name = "、".join(str(t) for t in terms[:MAX_NAME_TERMS])
    return name[:MAX_NAME_LEN]


def _normalize_workspace(value) -> str:
    """归一化 workspace：去首尾空白与末尾路径符号（/ 或 \\）。"""
    return re.sub(r"[/\\]+$", "", str(value or "").strip())


def _load_file_journal(session_dir: Path, fallback_workspace: str = "") -> tuple[list[str], str]:
    """从 file_journals/<turn>/manifest.json 收集会话实际改动的文件全路径。

    manifest 的 files 字段为 {相对路径: {path, tools, baseline, ...}}；用 manifest
    自带的 workspace 拼成全路径（缺失时回退会话 meta 的 workspace，再缺失则保留
    相对路径）。按 turn 时间倒序收集，去重并截断到 _MAX_FILE_PATHS。

    Returns:
        (文件全路径列表, manifest 中出现最多的 workspace)——后者供会话 meta
        缺少 workspace 时作为聚类 workspace 因子的回退值。
    """
    journal_root = session_dir / "file_journals"
    if not journal_root.is_dir():
        return [], ""
    try:
        manifests = sorted(
            journal_root.glob("*/manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return [], ""
    paths: list[str] = []
    seen: set[str] = set()
    ws_count: dict[str, int] = {}
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        workspace = _normalize_workspace(data.get("workspace") or fallback_workspace)
        if workspace:
            ws_count[workspace] = ws_count.get(workspace, 0) + 1
        files = data.get("files")
        if not isinstance(files, dict):
            continue
        for key, entry in files.items():
            rel = str(key or "").strip()
            if isinstance(entry, dict) and str(entry.get("path") or "").strip():
                rel = str(entry["path"]).strip()
            rel = rel.lstrip("/")
            if not rel:
                continue
            full = f"{workspace}/{rel}" if workspace else rel
            if full not in seen:
                seen.add(full)
                paths.append(full)
                if len(seen) >= _MAX_FILE_PATHS:
                    break
    journal_ws = max(ws_count, key=ws_count.get) if ws_count else ""
    return paths, journal_ws


def load_sessions(chats_dir: Path) -> list[dict]:
    """扫描 chats_dir 下所有会话（一层目录），提取 user/assistant 正文。

   返回按会话 ID（即时间顺序）排序的列表，每项含：
      sid/title/doc（聚类用加权文本）/user_first（首条用户消息，命名抽样用）/
      workspace（归一化后的工作区，可能为空）/files（file_journals 改动文件全路径，
      每会话去重、上限 _MAX_FILE_PATHS 条）。
    doc 按信号强度加权：file_journals 改动文件路径（×_FILE_WEIGHT）>
    归一化 workspace（×_WORKSPACE_WEIGHT）> 标题与用户消息（×2）>
    assistant 正文（截断，避免冗长回复淹没主题）。
    """
    index: dict = {}
    index_path = chats_dir / "index.json"
    if index_path.is_file():
        try:
            loaded = json.loads(index_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                index = loaded
        except (json.JSONDecodeError, OSError):
            index = {}
    rows: list[dict] = []
    seen: set[str] = set()
    for path in sorted(chats_dir.glob("*/conversation.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("meta") or {}
        sid = str(meta.get("session_id") or path.parent.name)
        if sid in seen:
            continue
        seen.add(sid)
        user_parts: list[str] = []
        assistant_parts: list[str] = []
        for msg in data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    x.get("text", "") for x in content
                    if isinstance(x, dict) and isinstance(x.get("text"), str)
                )
            if not isinstance(content, str):
                continue
            content = content.strip()
            if not content:
                continue
            if role == "user":
                user_parts.append(content)
            elif role == "assistant":
                assistant_parts.append(content)
        user = "\n".join(user_parts)
        assistant = "\n".join(assistant_parts)
        if len(assistant) > 12000:
            assistant = assistant[:6000] + "\n" + assistant[-6000:]
        title = str((index.get(sid) or {}).get("title") or "")
        user_first = (user_parts[0][:200] if user_parts else "")
        files, journal_ws = _load_file_journal(path.parent)
        # workspace 因子：meta workspace 优先，缺失时回退 file_journals 最常见的
        workspace = _normalize_workspace(meta.get("workspace")) or journal_ws
        doc = (
            (workspace + "\n") * _WORKSPACE_WEIGHT
            + (" ".join(files) + "\n") * _FILE_WEIGHT
            + (title + "\n") * 2
            + (user + "\n") * 2
            + assistant
        )
        rows.append({
            "sid": sid, "title": title, "doc": doc, "user_first": user_first,
            "workspace": workspace, "files": files,
        })
    return rows


# ---------------------------------------------------------------------------
# 聚类：scikit-learn（优先）与纯 Python 回退
# ---------------------------------------------------------------------------

def _k_bounds(n: int) -> tuple[int, int]:
    """选择候选 k 的范围。

    每层取 2~8 个簇，靠递归保证每个末级分类不超过 max_leaf_size；
    k 越小层级越浅、簇越主题化。
    """
    if n < 12:
        return 2, 2
    k_lo = 3
    k_hi = min(8, max(k_lo, n // 4))
    return k_lo, k_hi


def _k_range(k_lo: int, k_hi: int, cap: int = 5) -> list[int]:
    if k_hi <= k_lo:
        return [k_lo]
    if k_hi - k_lo + 1 <= cap:
        return list(range(k_lo, k_hi + 1))
    span = k_hi - k_lo
    return sorted({k_lo, k_hi, k_lo + span // 4, k_lo + span // 2, k_lo + 3 * span // 4})


def _top_terms_from_mean(mean, terms, limit: int = MAX_NAME_TERMS) -> list[str]:
    order = list(mean.argsort()[::-1][: limit * 4])
    return _clean_terms([str(terms[i]) for i in order if mean[i] > 0], limit)


def _cluster_sklearn(docs: list[str], k_list: list[int], seed: int):
    """TF-IDF + KMeans，返回 (labels, {lab: terms}, all_terms)；失败返回 None。"""
    components = _optional_sklearn()
    if components is None:
        return None
    np, TfidfVectorizer, KMeans, silhouette_score = components

    n = len(docs)
    min_df = 2 if n >= 8 else 1
    vectorizer = TfidfVectorizer(
        analyzer=_analyzer,
        min_df=min_df,
        max_df=0.9,
        sublinear_tf=True,
        max_features=60000,
        norm="l2",
    )
    X = vectorizer.fit_transform(docs)
    terms = np.array(vectorizer.get_feature_names_out())
    all_mean = np.asarray(X.mean(axis=0)).ravel()
    all_terms = _top_terms_from_mean(all_mean, terms)

    best = None
    for k in k_list:
        if k < 2 or k >= n:
            continue
        km = KMeans(n_clusters=k, random_state=seed, n_init=10, max_iter=200).fit(X)
        labels = [int(l) for l in km.labels_]
        sizes = Counter(labels)
        if len(sizes) < 2:
            continue
        sil = 0.0
        if silhouette_score is not None:
            try:
                sil = float(silhouette_score(X, labels, metric="cosine"))
            except Exception:
                sil = 0.0
        minsize = min(sizes.values())
        maxfrac = max(sizes.values()) / n
        # 避免碎簇与"一个巨型残余簇"
        penalty = max(0, 3 - minsize) * 0.012 + max(0, maxfrac - 0.55) * 0.08
        score = sil - penalty
        if best is None or score > best[0]:
            arr = np.asarray(labels)
            cluster_terms = {}
            for lab in sizes:
                mean = np.asarray(X[arr == lab].mean(axis=0)).ravel()
                cluster_terms[int(lab)] = _top_terms_from_mean(mean, terms)
            best = (score, labels, cluster_terms)
    if best is None:
        return None
    return best[1], best[2], all_terms


def _cluster_fallback(docs: list[str], k: int):
    """纯 Python 稀疏 TF-IDF + k-means（Lloyd + 最远点采样选种子）。

    返回与 _cluster_sklearn 相同结构的 (labels, {lab: terms}, all_terms)；
    失败返回 None。仅依赖标准库，用于 scikit-learn 不可用的环境。
    """
    n = len(docs)
    token_lists = [_analyzer(d) for d in docs]
    df: Counter = Counter()
    for toks in token_lists:
        df.update(set(toks))
    allowed = {t for t, c in df.items() if c >= 2 and c <= max(1, int(round(0.9 * n)))}
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items() if t in allowed}

    def vec_of(toks) -> dict:
        tf: Counter = Counter(t for t in toks if t in allowed)
        if not tf:
            return {}
        v = {t: (1.0 + math.log(c)) * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values()))
        return {t: x / norm for t, x in v.items()} if norm > 0 else {}

    vecs = [vec_of(toks) for toks in token_lists]

    def dot(a: dict, b: dict) -> float:
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        s = 0.0
        for t, w in a.items():
            w2 = b.get(t)
            if w2:
                s += w * w2
        return s

    def top_terms(sel: list[int]) -> list[str]:
        acc: dict = {}
        for i in sel:
            for t, w in vecs[i].items():
                acc[t] = acc.get(t, 0.0) + w
        ordered = [t for t, _ in sorted(acc.items(), key=lambda kv: kv[1], reverse=True)]
        return _clean_terms(ordered)

    # 最远点采样选种子
    sums = [sum(dot(vecs[i], vecs[j]) for j in range(n)) for i in range(n)]
    seeds = [max(range(n), key=lambda i: (sums[i], -i))]
    for _ in range(1, k):
        best_i, best_v = None, -2.0
        for i in range(n):
            v = min(dot(vecs[i], vecs[s]) for s in seeds)
            if v > best_v or (v == best_v and best_i is None):
                best_v, best_i = v, i
        if best_i is None or (best_v >= 1.0 - 1e-9 and len(seeds) >= 2):
            break
        seeds.append(best_i)
    seed_vecs = [vecs[s] for s in seeds]
    if not seed_vecs:
        return None

    labels = [0] * n
    for _ in range(8):
        changed = False
        for i in range(n):
            best_s, best_v = 0, -2.0
            for si, sv in enumerate(seed_vecs):
                v = dot(vecs[i], sv)
                if v > best_v:
                    best_v, best_s = v, si
            if labels[i] != best_s:
                labels[i] = best_s
                changed = True
        if not changed:
            break
        for si in range(len(seed_vecs)):
            members = [i for i in range(n) if labels[i] == si]
            if not members:
                continue
            acc: dict = {}
            for i in members:
                for t, w in vecs[i].items():
                    acc[t] = acc.get(t, 0.0) + w
            norm = math.sqrt(sum(x * x for x in acc.values()))
            seed_vecs[si] = {t: x / norm for t, x in acc.items()} if norm > 0 else {}

    cluster_terms = {
        si: top_terms([i for i in range(n) if labels[i] == si]) for si in range(len(seed_vecs))
    }
    return labels, cluster_terms, top_terms(list(range(n)))


def _group_terms(docs: list[str], limit: int = MAX_NAME_TERMS) -> list[str]:
    """小组文档的头部词（用于无法聚类或需要命名时）。"""
    docs = [d for d in docs if d]
    if not docs:
        return []
    components = _optional_sklearn() if _SKLEARN_OK else None
    if components is not None:
        try:
            np, TfidfVectorizer, _km, _sil = components
            vectorizer = TfidfVectorizer(
                analyzer=_analyzer, min_df=1, max_df=0.95,
                sublinear_tf=True, max_features=20000, norm="l2",
            )
            X = vectorizer.fit_transform(docs)
            mean = np.asarray(X.mean(axis=0)).ravel()
            terms = np.array(vectorizer.get_feature_names_out())
            return _top_terms_from_mean(mean, terms, limit)
        except Exception:
            pass
    n = len(docs)
    df: Counter = Counter()
    lists = [_analyzer(d) for d in docs]
    for toks in lists:
        df.update(set(toks))
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    acc: Counter = Counter()
    for toks in lists:
        for t in set(toks):
            acc[t] += idf.get(t, 1.0)
    return _clean_terms([t for t, _ in acc.most_common(80)], limit)


# ---------------------------------------------------------------------------
# 递归建树
# ---------------------------------------------------------------------------

def build_node(rows: list[dict], max_leaf: int, seed: int, terms=None) -> dict:
    """对一组会话递归建树，返回 {"_name", "_children_raw", "_rows"} 原始节点。

    规则：
      * n <= max_leaf → 末级分类（会话直接挂载，无子分类）；
      * 否则聚类拆分；只有当聚类产生 >= 2 个非空子组时才生成子分类，
        否则退化为按顺序分块（每块 <= max_leaf），仍然满足 >= 2 个子分类；
      * 任何子组若仍 > max_leaf 会递归继续拆分。
    """
    n = len(rows)
    docs = [r["doc"] for r in rows]
    if n <= max_leaf:
        name_terms = terms if terms else _group_terms(docs)
        return {
            "_name": _name_from_terms(name_terms),
            "_children_raw": [r["sid"] for r in rows],
            "_rows": rows,
        }

    k_lo, k_hi = _k_bounds(n)
    labels = cluster_terms = all_terms = None
    if _SKLEARN_OK:
        try:
            labels, cluster_terms, all_terms = _cluster_sklearn(docs, _k_range(k_lo, k_hi), seed)
        except Exception:
            labels = None
    if labels is None:
        try:
            labels, cluster_terms, all_terms = _cluster_fallback(docs, k_lo)
        except Exception:
            labels = None

    labeled = None
    if labels is not None:
        by: dict = {}
        for row, lab in zip(rows, labels):
            by.setdefault(int(lab), []).append(row)
        labeled = [(g, (cluster_terms or {}).get(lab)) for lab, g in by.items() if g]
    if not labeled or len(labeled) < 2:
        # 无法有意义的聚类：按会话顺序分块，保持两条不变量
        labeled = [(rows[i:i + max_leaf], None) for i in range(0, n, max_leaf)]
    labeled.sort(key=lambda item: (-len(item[0]), item[0][0]["sid"]))

    children = [build_node(g, max_leaf, seed, terms=t) for g, t in labeled]
    name_terms = all_terms if all_terms else (terms if terms else _group_terms(docs))
    return {
        "_name": _name_from_terms(name_terms),
        "_children_raw": children,
        "_rows": rows,
    }


def _dedupe_name(name: str, used: set[str]) -> str:
    base = name or "未命名"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}·{suffix}"
        suffix += 1
    return candidate


def assemble(raw_nodes: list[dict]) -> tuple[list[dict], set, int]:
    """给原始节点分配稳定 ID/名称/路径，计算 session_count，返回 (tree, attached, terminals)。

    同一会话可出现在多个末级分类下（图状连接），session_count 按唯一会话计。
    """

    def process(nodes: list[dict], path_ids: list[str], path_names: list[str]) -> list[dict]:
        used: set[str] = set()
        out: list[dict] = []
        for i, raw in enumerate(nodes, 1):
            name = _dedupe_name(raw["_name"], used)
            used.add(name)
            children_raw = raw["_children_raw"]
            node = {"id": i, "name": name, "type": "category", "children": []}
            if any(isinstance(c, dict) for c in children_raw):
                sub = process([c for c in children_raw if isinstance(c, dict)],
                              path_ids + [str(i)], path_names + [name])
                node["children"] = sub
                found: set = set()
                for c in sub:
                    found |= c["__sessions"]
                node["session_count"] = len(found)
                node["__sessions"] = found
            else:
                sids = sorted({str(c) for c in children_raw})
                node["children"] = sids
                node["category"] = "/".join(path_ids + [str(i)])
                node["session_count"] = len(set(sids))
                node["__sessions"] = set(sids)
            out.append(node)
        return out

    tree = process(raw_nodes, [], [])

    attached: set = set()

    def strip(node: dict) -> None:
        nonlocal attached
        for c in node.get("children", []):
            if isinstance(c, dict):
                strip(c)
        if "__sessions" in node:
            attached |= node.pop("__sessions")

    for node in tree:
        strip(node)

    def count_terminals(node: dict) -> int:
        kids = node.get("children", [])
        if all(not isinstance(c, dict) for c in kids):
            return 1
        return sum(count_terminals(c) for c in kids if isinstance(c, dict))

    return tree, attached, sum(count_terminals(n) for n in tree)


def validate(tree: list[dict], all_sids: set, max_leaf: int) -> list[str]:
    """校验硬性规则与全量覆盖，返回错误列表（空列表 = 通过）。

    注意：会话重复出现在多个末级分类是合法的（自动+人工分类并存），
    只校验覆盖与结构规则。
    """
    errors: list[str] = []
    attached: set = set()
    terminals = 0

    def walk(node: dict, path: str) -> None:
        nonlocal terminals
        kids = node.get("children", [])
        dict_kids = [k for k in kids if isinstance(k, dict)]
        str_kids = [k for k in kids if isinstance(k, str)]
        if dict_kids:
            if len(dict_kids) < 2:
                errors.append(f"{path}: 分类只有 {len(dict_kids)} 个子分类（要求 >= 2）")
            for k in dict_kids:
                walk(k, f"{path}/{k.get('id')}")
        else:
            terminals += 1
            if not str_kids:
                errors.append(f"{path}: 末级分类为空")
            if len(str_kids) > max_leaf:
                errors.append(f"{path}: 末级分类有 {len(str_kids)} 个会话（超过 {max_leaf}）")
            if node.get("category") and node.get("category") != path:
                errors.append(f"{path}: category 字段 {node.get('category')!r} 与实际路径不一致")
            attached.update(str_kids)

    if len(tree) == 1 and isinstance(tree[0], dict) and any(
            isinstance(k, dict) for k in tree[0].get("children", [])):
        errors.append("顶层只有一个包含子分类的包装分类（包装层应去掉）")
    for top in tree:
        if isinstance(top, dict):
            walk(top, str(top.get("id")))
    missing = all_sids - attached
    if missing:
        errors.append(f"{len(missing)} 个会话未被挂载: {sorted(missing)[:5]}")
    return errors


# ---------------------------------------------------------------------------
# 大模型递归命名（自底向上）
# ---------------------------------------------------------------------------

def _short_workspace(ws) -> str:
    """工作区缩写：只保留最后两段路径（如 .../agents-runtime/frontend -> agents-runtime/frontend）。

    仅用于命名上下文展示；聚类与匹配始终使用归一化后的完整路径。
    """
    parts = [p for p in re.split(r"[\\/]+", str(ws or "").strip()) if p]
    if not parts:
        return ""
    return "/".join(parts[-2:])


def _workspace_file_context(rows: list[dict], ws_limit: int = 3, file_limit: int = 5) -> list[str]:
    """汇总一组会话的 workspace 与改动文件统计，返回命名上下文行（可能为空）。

    只报告在 >= 2 个会话中出现的"共享"信号（同一项目 / 同一工作目标）；
    仅单个会话触碰的文件视为噪声。workspace 缩写为末两段路径；
    文件展示时去掉 workspace 前缀，只保留相对路径。
    """
    n = len(rows)
    if n == 0:
        return []
    ws_count: Counter = Counter()
    file_count: Counter = Counter()
    full_ws: list[str] = []
    for r in rows:
        ws = _normalize_workspace(r.get("workspace") or "")
        if ws:
            ws_count[_short_workspace(ws)] += 1
            if ws not in full_ws:
                full_ws.append(ws)
        for f in r.get("files") or []:
            file_count[str(f)] += 1
    lines: list[str] = []
    ws_items = [(ws, c) for ws, c in ws_count.most_common(ws_limit) if c >= max(2, n // 4)]
    if ws_items:
        lines.append("常见工作区: " + ", ".join(f"{ws} ({c}/{n} 个会话)" for ws, c in ws_items))
    file_items = [(f, c) for f, c in file_count.most_common(file_limit) if c >= 2]
    if file_items:
        shown: list[str] = []
        for f, c in file_items:
            disp = f
            for ws in full_ws:
                prefix = ws + "/"
                if disp.startswith(prefix):
                    disp = disp[len(prefix):]
                    break
            if len(disp) > 60:
                disp = "…" + disp[-59:]
            shown.append(f"{disp} ({c}/{n})")
        lines.append("常见改动文件: " + ", ".join(shown))
    return lines


def _sample_session_lines(rows: list[dict], limit: int = MAX_NAMING_SAMPLES) -> list[str]:
    """会话太多时均匀抽样：标题 + 首条用户消息，控制 prompt 长度。"""
    n = len(rows)
    if n == 0:
        return []
    if n <= limit:
        idxs = list(range(n))
    else:
        step = n / limit
        idxs = [int(i * step) for i in range(limit)]
    lines = []
    for i in idxs:
        row = rows[i]
        title = row.get("title") or ""
        user = row.get("user_first") or ""
        line = title
        if user:
            line = f"{title}：{user}" if title else user
        lines.append(line[:NAMING_SAMPLE_LINE_LIMIT])
    return lines


def _llm_name(llm, prompt: str) -> str:
    """调用 LLM 取名称；任何失败都返回空串（由调用方回退）。"""
    if llm is None:
        return ""
    try:
        reply = llm(prompt)
    except Exception:
        return ""
    if not isinstance(reply, str) or not reply.strip():
        return ""
    name = reply.strip()
    if name.startswith("```"):
        name = re.sub(r"^```[a-zA-Z0-9]*\s*", "", name)
        name = re.sub(r"\s*```$", "", name).strip()
    name = name.splitlines()[0].strip().strip('"').strip("'").strip()
    name = re.sub(r"^(分类名称|类别名称|名称|名字|category name|name)\s*[:：]\s*", "", name, flags=re.I)
    for sep in ("，", "。", "；", ",", ";"):
        if sep in name:
            name = name.split(sep)[0].strip()
            break
    return name[:20]


def _name_terminal(node: dict, llm) -> str:
    rows = node.get("_rows") or []
    lines = _sample_session_lines(rows)
    if not lines:
        return node["_name"]
    context = _workspace_file_context(rows)
    context_block = (
        "这组会话的工作区与改动文件统计：\n" + "\n".join(context) + "\n"
        if context else ""
    )
    prompt = (
        "我们在把历史会话整理成层级目录。以下是某个分类下有代表性的会话"
        "（标题及用户首条消息）；"
        + context_block
        + "请概括这些会话的共同主题，为该分类起一个简短的名称"
        "（不超过12个字或5个英文单词，不要标点、引号或'会话'字样；"
        "若会话集中在某个项目工作区或一组核心文件上，名称中可体现项目名或文件领域，"
        "例如'agents-runtime 侧边栏'）。只输出分类名称：\n\n"
        + "\n".join(lines)
    )
    return _llm_name(llm, prompt) or node["_name"]


def _name_internal(node: dict, llm) -> str:
    child_names = [c["_name"] for c in node["_children_raw"] if isinstance(c, dict)]
    if not child_names:
        return node["_name"]
    context = _workspace_file_context(node.get("_rows") or [])
    context_block = (
        "以及其下会话的工作区与改动文件统计：\n" + "\n".join(context) + "\n"
        if context else ""
    )
    prompt = (
        "我们在把历史会话整理成层级目录。以下是某个分类下各子分类的名称，"
        + context_block
        + "请给出能概括这些子分类的上级分类名称"
        "（不超过14个字或6个英文单词，不要标点、引号或'分类'字样；"
        "若子分类集中在某个项目工作区上，名称中可体现项目名）。只输出分类名称：\n\n"
        + "、".join(child_names[:40])
    )
    return _llm_name(llm, prompt) or node["_name"]


def _collect_nodes(nodes: list[dict], terminals: list, internals: list, depth: int = 0) -> None:
    for node in nodes:
        kids = [c for c in node["_children_raw"] if isinstance(c, dict)]
        if kids:
            internals.append((depth, node))
            _collect_nodes(kids, terminals, internals, depth + 1)
        else:
            terminals.append(node)


def assign_names_llm(nodes: list[dict], llm) -> None:
    """递归命名：末级分类由会话抽样命名，父分类由子分类名称命名（自底向上）。

    同层并发（线程池，默认 4），任何一次命名失败都回退为高频词名称。
    """
    if llm is None:
        return
    terminals: list = []
    internals: list = []
    _collect_nodes(nodes, terminals, internals)
    with ThreadPoolExecutor(max_workers=NAMING_WORKERS, thread_name_prefix="tree-naming") as pool:
        futures = [pool.submit(_name_terminal, node, llm) for node in terminals]
        for future, node in zip(futures, terminals):
            node["_name"] = future.result()
        internals.sort(key=lambda item: -item[0])
        level: list = []
        current_depth = None

        def flush() -> None:
            if not level:
                return
            futures = [pool.submit(_name_internal, node, llm) for node in level]
            for future, node in zip(futures, level):
                node["_name"] = future.result()
            level.clear()

        for depth, node in internals:
            if current_depth is not None and depth != current_depth:
                flush()
            current_depth = depth
            level.append(node)
        flush()


# ---------------------------------------------------------------------------
# 构建入口
# ---------------------------------------------------------------------------

def build_document(rows: list[dict], max_leaf: int, seed: int, llm=None) -> dict:
    """由会话列表构建完整 tree.json 文档（含校验；失败抛 ValueError）。

    顶层不加包装层：若全部会话拆成"一个含子分类的根"，顶层直接取该根的子分类；
    若全部会话本身就是一个末级分类（<= max_leaf），顶层就是这唯一的分类。
    """
    root = build_node(rows, max_leaf, seed)
    if any(isinstance(c, dict) for c in root["_children_raw"]):
        raw_nodes = [c for c in root["_children_raw"] if isinstance(c, dict)]
    else:
        raw_nodes = [root]
    if llm is not None:
        try:
            assign_names_llm(raw_nodes, llm)
        except Exception as exc:
            logger.warning("LLM 命名失败，回退为高频词命名: %s", exc)
    tree, attached, terminals = assemble(raw_nodes)
    all_sids = {r["sid"] for r in rows}
    errors = validate(tree, all_sids, max_leaf)
    if errors:
        raise ValueError("生成的分类树未通过校验:\n  " + "\n  ".join(errors[:10]))
    membership = Counter()
    for top in tree:
        def count(node: dict) -> None:
            kids = node.get("children", [])
            if all(not isinstance(c, dict) for c in kids):
                for sid in kids:
                    membership[str(sid)] += 1
            else:
                for c in kids:
                    if isinstance(c, dict):
                        count(c)
        count(top)
    doc = {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": (
            "runtime.category_tree_builder — code-only TF-IDF + KMeans recursive split; "
            f"terminal categories capped at {max_leaf} sessions; "
            "a category branches only into >= 2 sub-categories; "
            + ("categories named by LLM (summary model)" if llm is not None
               else "categories named by top terms (offline)")
        ),
        "experimental": True,
        "schema": {
            "category_node": '{id:int,name:string,type:"category",session_count:int,children:[category|string]}',
            "session_leaf": "conversation session ID string",
            "category_query": "join category node IDs with /; terminal paths may have different depths, e.g. 2/2 or 1/1/3",
            "graph_note": "A session ID may occur under multiple terminal category nodes.",
        },
        "stats": {
            "conversation_count": len(rows),
            "attached_conversation_count": len(all_sids),
            "terminal_category_count": terminals,
            "membership_edge_count": sum(membership.values()),
            "multi_category_conversation_count": sum(v > 1 for v in membership.values()),
            "max_categories_per_conversation": max(membership.values()) if membership else 0,
            "max_leaf_size": max_leaf,
        },
        "tree": tree,
    }
    return doc


def build_chats_dir(chats_dir: str | os.PathLike, llm=None,
                    max_leaf: int = MAX_LEAF_SIZE, seed: int = DEFAULT_SEED) -> dict | None:
    """扫描目录并构建文档；会话数不足（<= MIN_SESSIONS）时返回 None。"""
    chats = Path(chats_dir)
    rows = load_sessions(chats)
    if len(rows) <= MIN_SESSIONS:
        return None
    logger.info(
        "building category tree: %d sessions, max_leaf=%d, seed=%d, llm=%s",
        len(rows), max_leaf, seed, "yes" if llm is not None else "no",
    )
    return build_document(rows, max_leaf, seed, llm=llm)


def _atomic_write_json(path: Path, doc: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tree.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _outline(tree: list[dict], depth: int = 0) -> list[str]:
    lines = []
    for node in tree:
        kids = node.get("children", [])
        is_leaf = all(not isinstance(c, dict) for c in kids)
        suffix = f" ({len(kids)} 会话)" if is_leaf else f" ({len(kids)} 子分类)"
        lines.append("  " * depth + f"[{node.get('id')}] {node.get('name')}{suffix}")
        if not is_leaf:
            lines.extend(_outline(kids, depth + 1))
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="扫描历史会话并生成层次分类体系 tree.json（递归拆分，末级 <= N 个会话）"
    )
    ap.add_argument("--chats-dir", default="chat_data", help="会话根目录（含 */conversation.json）")
    ap.add_argument("--output", default=None, help="输出文件（默认 <chats-dir>/tree.json）")
    ap.add_argument("--max-leaf-size", type=int, default=MAX_LEAF_SIZE,
                    help=f"末级分类的最大会话数（默认 {MAX_LEAF_SIZE}）")
    ap.add_argument("--min-sessions", type=int, default=MIN_SESSIONS,
                    help=f"会话数量须大于该值才构建（默认 {MIN_SESSIONS}）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="KMeans 随机种子")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 tree.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = ap.parse_args(argv)

    chats_dir = Path(args.chats_dir)
    if not chats_dir.is_dir():
        print(f"chats dir not found: {chats_dir}", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else chats_dir / "tree.json"
    if output.exists() and not args.force and not args.dry_run:
        print(f"tree already exists, skipping (use --force to overwrite): {output}")
        return 0

    rows = load_sessions(chats_dir)
    if len(rows) <= args.min_sessions:
        print(f"only {len(rows)} sessions found (need > {args.min_sessions}); skipping build")
        return 0

    print(
        f"loaded {len(rows)} sessions; building tree "
        f"(max_leaf_size={args.max_leaf_size}, seed={args.seed}, "
        f"clustering={'sklearn' if _SKLEARN_OK else 'pure-python-fallback'}, "
        f"naming=offline-top-terms)"
    )
    doc = build_document(rows, args.max_leaf_size, args.seed)

    if args.dry_run:
        print(json.dumps(doc["stats"], ensure_ascii=False, indent=2))
        print("\n".join(_outline(doc["tree"])))
        return 0

    _atomic_write_json(output, doc)
    print(f"wrote {output}")
    print(json.dumps(doc["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
  * 末级分类最多包含 ``max_leaf_size``（默认动态值 max(2, 5*log2(总会话数))，
    显式传参可覆盖）个会话，超过即递归拆分；
  * 分类只有在确实拆成至少 2 个非空子组时才拥有子分类，
    任何分类都不会出现"只有一个子分类"的冗余层级；
  * 顶层不加包装层：全部会话若拆成"一个包含子分类的根"，顶层直接取
    该根的子分类；
  * 每个有 conversation.json 的会话挂载到一个末级分类；
  * 单分类语义（v5 起）：一个会话恰好属于一个末级分类，人工挂载即移动，
    被搬空的分类一并剪除；旧数据里同一会话的重复挂载仍可读，并在下一次
    挂载时被顺带清理（森林结构是树，不再借用图状挂载）。

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
import uuid
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


def dynamic_max_leaf(session_count: int) -> int:
    """末级分类容量默认上限：``max(2, ⌊5·log2(总会话数)⌋)``。

    固定 10 条对数百会话的语料过于苛刻（540 会话被切成 118 个碎片）；
    改为随总量对数增长：540 会话 ≈ 45 条/末级，分类更少、语义更完整，
    小语料也不会退化为超大叶子（count=4 → 10）。显式传参
    （build_document / CLI --max-leaf-size）优先于动态值。
    """
    if session_count <= 1:
        return 2
    return max(2, int(5 * math.log2(session_count)))

# 森林（多根）结构版本：
#   v2 顶层 = 主题（旧名"内容类别"）/ 工作区 / 本地终端；
#   v3 追加 最近会话 / 会话历史 两个根，且"工作区"根下按执行环境
#   （remote_env）分层——仅一个环境（本地）时该层自动省略。
#   v4 升级聚类算法（森林结构不变）：节点内公共词抑制、
#   兄弟簇相似度合并（重复主题不再并列）、同层兄弟对比式 LLM 命名（互斥命名+受控合并）。
#   v5 会话改为单分类（每个会话恰好属于一个末级分类），且"终端"根随执行环境
#   展开：存在远程环境时根改名"终端"，子节点为"本地"+各远程环境（双击打开
#   对应环境的终端）；仅有本地环境时保持单根"本地终端"不变。
#   v6 工作区目录节点下挂主题分类树的按工作区筛选副本。
#   v7 移除"最近会话"根：前端把五根折叠成一行模式图标，最近会话直接用会话
#   列表 API 的整体列表（不再有 20 条上限的树根）；森林只剩 主题/工作区/
#   终端/会话历史 四根。
TREE_VERSION = 7
CATEGORY_ROOT_NAME = "主题"
WORKSPACE_ROOT_NAME = "工作区"
TERMINAL_ROOT_NAME = "本地终端"
# 存在远程执行环境时的终端根名（其默认子节点为"本地"，远程环境与之同级）。
TERMINAL_MULTI_ROOT_NAME = "终端"
HISTORY_ROOT_NAME = "会话历史"
UNKNOWN_WORKSPACE_NAME = "未指定工作区"
# 工作区目录下不在主题树中的会话（如上次刷新后新建的）兜底分类节点名。
UNKNOWN_CATEGORY_NAME = "未分类"
TYPE_CATEGORY = "category"
TYPE_WORKSPACE = "workspace"
TYPE_TERMINAL = "terminal"
TYPE_ENVIRONMENT = "environment"
TYPE_RECENT = "recent"  # v3–v6 遗留（旧 tree.json 兼容读取；v7 起不再构建）
TYPE_HISTORY = "history"
TYPE_MONTH = "month"
# 嵌套终端层：终端环境节点下的用户自定义子终端（command 为进入该层的
# CLI 指令，如 ssh/docker exec/wsl/psql；双击时从环境节点起逐层注入）。
TYPE_TERMINAL_LAYER = "terminal_layer"

# 执行环境：会话 meta.remote_env 缺省即"本地"。
LOCAL_ENVIRONMENT_KEY = "local"
LOCAL_ENVIRONMENT_NAME = "本地"


def node_type(node) -> str:
    """节点类型；缺省（v1 数据）视为分类节点。"""
    if not isinstance(node, dict):
        return TYPE_CATEGORY
    return str(node.get("type") or TYPE_CATEGORY)


def is_category_node(node) -> bool:
    return node_type(node) == TYPE_CATEGORY


def normalize_environment(value) -> str:
    """归一化会话执行环境：meta.remote_env（URL/隧道标识）→ 稳定分组键。

    'http://host:port' 与 'https://host:port' 去掉协议头归并为同一环境；
    空值或 'local' 归为本地环境键。分组键同时直接作为节点显示名
    （本地环境除外，显示为"本地"）。
    """
    raw = str(value or "").strip()
    if not raw or raw.lower() == LOCAL_ENVIRONMENT_KEY:
        return LOCAL_ENVIRONMENT_KEY
    raw = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)
    return raw.strip("/") or LOCAL_ENVIRONMENT_KEY


def environment_display_name(key: str) -> str:
    return LOCAL_ENVIRONMENT_NAME if key == LOCAL_ENVIRONMENT_KEY else key


def load_remote_envs(chats_dir) -> list[dict]:
    """读取本环境登记的远程执行环境（remote_envs.json）。

    ``remote_env_manager`` 把登记信息写在 DATA_DIR/remote_envs.json（chat_data
    的父目录）；旧版本曾写在 chat_data 内。两处都查，先命中先用。终端根按登记
    的远程环境展开（而不只是会话出现过的 remote_env），这样 ``rebuild`` 与服务
    器后台构建都展示实例真实的执行环境拓扑。文件缺失或损坏时按"没有远程环境"
    处理；本模块保持零第三方依赖（纯 stdlib）。
    """
    # resolve() 关键：chat_data 常是指向 DATA_DIR/chat_data 的软链接，
    # 不解析时 parent 只是 "."，找不到 DATA_DIR 里的登记文件。
    base = Path(chats_dir).expanduser().resolve()
    for candidate in (base / "remote_envs.json", base.parent / "remote_envs.json"):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        envs = data.get("envs") if isinstance(data, dict) else data
        return [
            e for e in (envs or [])
            if isinstance(e, dict) and str(e.get("id") or "").strip()
        ]
    return []


def month_key(created_at) -> str:
    """'2026-08-28 10:39:29' / '2026-08-28T10:39:29' → '202608'；无法解析返回 ''。"""
    m = re.match(r"\s*(\d{4})-(\d{2})", str(created_at or ""))
    return f"{m.group(1)}{m.group(2)}" if m else ""

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
    tree_path = os.path.join(chats_dir, "tree.json")
    if os.path.isfile(tree_path):
        # 已是当前版本才跳过；旧版本（如 v1 单树 → v2 森林）允许重建升级。
        try:
            with open(tree_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if int(doc.get("version") or 1) >= TREE_VERSION:
                return False
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# v4 分裂质量参数（均可用环境变量覆盖，便于离线调参实验）：
#   DF_MASK_RATIO: 节点内文档频率超过该比例的词在分裂前被屏蔽——
#     同一节点的会话往往共享大量领域词（如 agents/runtime），它们是
#     "共同背景"而非"区分信号"；不屏蔽时 KMeans 只能沿噪声维度切刀，
#     产生彼此无法区分的孪生簇（曾出现 5 个"Agents××"并列）。
#     实测本语料 silhouette 天然只有 0.02 上下，故不再设分数硬闸门，
#     仅靠 SIBLING_SIM_MAX 阻止把同一主题硬切开。
#   SIBLING_SIM_MAX: 兄弟簇 centroid 余弦相似度超过该值视为同一主题，
#     自动合并回一个组继续递归，避免同层出现实质重复的分类。
DF_MASK_RATIO = _env_float("SESSION_TREE_DF_MASK_RATIO", 0.35)
SIBLING_SIM_MAX = _env_float("SESSION_TREE_SIBLING_SIM_MAX", 0.62)
SPLIT_MIN_KEPT_TERMS = 40  # 屏蔽后至少保留的词数，不足则逐级放宽比例

# 大模型命名参数
MAX_NAMING_SAMPLES = 12
NAMING_SAMPLE_LINE_LIMIT = 160
NAMING_WORKERS = 4

# 命名 prompt 共用规则：抑制 LLM 幻觉造词与高频词回退产生的碎词名。
NAMING_RULE = ("名称必须由完整的常用词或常见技术词组成，禁止出现把正常词切开后的非词片段"
               "（例如把“文件管理”写成“件管理”），也不要用顿号罗列多个零碎词拼成名称。\n")

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
    """按分数顺序取词，去掉过短/纯符号/通用词/互含子串，保证名称可读。

    CJK n-gram 会把连续中文切成重叠片段（如 ``文件管`` + ``件管理``）；
    相邻两项若以 >=2 字首尾重叠则拼接回完整词（``文件管理``），避免
    高频词回退名称出现 ``文件管、件管理`` 这类碎词。
    """
    seen: list[str] = []
    for t in raw:
        t = str(t).strip()
        if len(t) < 2 or t in GENERIC_ZH or t in GENERIC_EN:
            continue
        if re.fullmatch(r"[0-9._:/@\-]+", t):
            continue
        if any(t == s or t in s or s in t for s in seen):
            continue
        if seen:
            prev = seen[-1]
            overlap = 0
            for k in range(min(len(prev), len(t)) - 1, 0, -1):
                if prev[-k:] == t[:k]:
                    overlap = k
                    break
            stitched = prev + t[overlap:] if overlap else ""
            if (stitched and _is_cjk(prev) and _is_cjk(t) and len(stitched) <= 8
                    and (overlap >= 2 or stitched in set(raw))):
                # 重叠 >=2 字：几乎必为同一词被 n-gram 切开（文件管+件管理）；
                # 重叠 1 字：仅当拼接结果本身就是候选词（爱奇艺）才拼，避免
                # "推理"+"理由" 这类合法但无关的词被硬拼成生造词。
                seen[-1] = stitched
                continue
        seen.append(t)
        if len(seen) >= limit:
            break
    return seen


def _is_cjk(token: str) -> bool:
    return bool(re.fullmatch(r"[\u4e00-\u9fff]+", token))


def _name_from_terms(terms) -> str:
    if not terms:
        return "未命名"
    name = "、".join(str(t) for t in terms[:MAX_NAME_TERMS])
    return name[:MAX_NAME_LEN]


def _normalize_workspace(value) -> str:
    """归一化 workspace：去首尾空白；**保留原始分隔符风格**（Windows 反斜杠
    原样保留，不做 \\ → / 替换）；仅合并重复分隔符（UNC 前导 ``\\\\`` 保留）
    并去除尾部分隔符。分组键即该规范形式；跨风格的同一路径（``C:\\a`` 与
    ``C:/a``）来自同一台机器的写法通常一致，不额外合并。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//") or text.startswith("\\\\"):
        sep = text[0]
        body = re.sub(r"[/\\]{2,}", lambda _, s=sep: s, text[2:])
        ws = sep + sep + body
    else:
        ws = re.sub(r"/{2,}", "/", text)
        ws = re.sub(r"\\{2,}", lambda _: "\\", ws)
    ws = re.sub(r"[/\\]+$", "", ws)
    if not ws:
        return text[0]  # 根 '/' 或 '\\' 本身
    return ws


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
            label = str(key or "").strip()
            if isinstance(entry, dict) and str(entry.get("path") or "").strip():
                label = str(entry["path"]).strip()
            if not label:
                continue
            if label.startswith("/") or re.match(r"^[A-Za-z]:[/\\]", label):
                # Absolute label: a permitted /tmp scratch file or a Windows
                # drive-qualified path, already a full path.
                full = label
            elif workspace:
                # 连接分隔符跟随 workspace 的原始风格：Windows 工作区拼出
                # C:\proj\src\a.py，Linux 工作区拼出 /cc/agents/x.py。
                sep = "\\" if "\\" in workspace and "/" not in workspace else "/"
                full = f"{workspace.rstrip('/\\')}{sep}{label}"
            else:
                full = label
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
        entry = index.get(sid) or {}
        # 事实字段：创建时间（会话历史按月分组）、最近活动时间（最近会话
        # 排序，与会话列表 last_inference_at 同序）、执行环境（工作区分层）。
        created_at = str(meta.get("created_at") or entry.get("created_at") or "")
        last_activity = str(entry.get("last_inference_at") or meta.get("updated_at") or "")
        environment = normalize_environment(meta.get("remote_env"))
        # 原始环境标识（URL/隧道 id，未去协议头）：终端节点用它做路由，
        # 与前端 remoteEnvs / terminals-mux 的 env_id 完全一致。
        environment_id = str(meta.get("remote_env") or "").strip()
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
            "created_at": created_at, "last_activity": last_activity,
            "environment": environment, "environment_id": environment_id,
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


def _mask_shared_columns(np, X, n: int, mask_ratio: float, min_kept: int):
    """A: 公共词抑制——屏蔽节点内高文档频率的词再聚类。

    同一节点的会话共享的领域词/路径段是"共同背景"而非"区分信号"；
    按节点内 df > ratio*n 屏蔽对应列，并对行重新 L2 归一化。屏蔽后
    可用词不足 min_kept 时逐级放宽比例，仍不足则放弃屏蔽。
    返回 (处理后矩阵, 被屏蔽词数)。
    """
    df = np.asarray((X > 0).sum(axis=0)).ravel()
    ratio = mask_ratio
    mask = df > ratio * n
    kept = int((~mask).sum())
    while kept < min_kept and ratio < 0.95:
        ratio += 0.1
        mask = df > ratio * n
        kept = int((~mask).sum())
    if kept < min_kept:
        mask = np.zeros(len(df), dtype=bool)
    Xc = X.copy()
    if mask.any():
        # multiply 完成列屏蔽：不改变稀疏结构，无 csc 列写警告
        Xc = X.multiply((~mask).astype(float)).tocsr()
        norms = np.sqrt(np.asarray(Xc.multiply(Xc).sum(axis=1)).ravel())
        zero_rows = list(np.where(norms == 0)[0])
        if zero_rows:
            # 行内词全被清零：回退为原始行，避免零向量导致余弦距离退化
            lil = Xc.tolil()
            for r in zero_rows:
                lil[r, :] = X[r, :]
            Xc = lil.tocsr()
            norms = np.sqrt(np.asarray(Xc.multiply(Xc).sum(axis=1)).ravel())
        safe = np.where(norms > 0, norms, 1.0)
        Xc = Xc.multiply((1.0 / safe).reshape(-1, 1)).tocsr()
    return Xc, int(mask.sum())


def _merge_similar_clusters(np, Xc, labels: list[int], cluster_terms: dict, sim_max: float):
    """B: 兄弟可分性闸门——centroid 余弦相似度 >= sim_max 的簇合并为同一组。"""
    labs = sorted(set(labels))
    if len(labs) < 2 or sim_max <= 0:
        return labels, cluster_terms
    arr = np.asarray(labels)
    cents = {l: np.asarray(Xc[arr == l].mean(axis=0)).ravel() for l in labs}
    parent = {l: l for l in labs}

    def find(x: int) -> int:
        while parent[x] != x:
            x = parent[x]
        return x

    for i, a in enumerate(labs):
        for b in labs[i + 1:]:
            ca, cb = cents[a], cents[b]
            na = float(np.sqrt((ca * ca).sum()))
            nb = float(np.sqrt((cb * cb).sum()))
            if na <= 0 or nb <= 0:
                continue
            if float((ca * cb).sum()) / (na * nb) >= sim_max:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
    groups: dict = {}
    for l in labs:
        groups.setdefault(find(l), []).append(l)
    if len(groups) == len(labs):
        return labels, cluster_terms
    relabel: dict = {}
    merged_terms: dict = {}
    for new_lab, members in sorted(groups.items()):
        acc: list = []
        for m in members:
            relabel[m] = new_lab
            for t in cluster_terms.get(m, []):
                if t not in acc:
                    acc.append(t)
        merged_terms[new_lab] = acc[:MAX_NAME_TERMS]
    return [relabel[l] for l in labels], merged_terms


def _merge_small_clusters(np, Xc, labels: list[int], cluster_terms: dict,
                          min_child: int, cap: int):
    """最小容量约束：把小于 min_child 的簇并入 centroid 最相似的兄弟簇。

    碎片叶（1~6 个会话）是末级数量远超 总量/max_leaf 的主要原因。仅当
    合并后不超过 cap 时才并；无合法目标的孤小簇保留（宁缺毋滥）；若合并
    后只剩一组（说明切分纯属人为），返回 None 由调用方换下一个 k。
    """
    if min_child <= 1:
        return labels, cluster_terms
    cur = list(labels)
    terms = dict(cluster_terms or {})

    def centroids():
        arr = np.asarray(cur)
        labs = sorted(set(cur))
        return {l: np.asarray(Xc[arr == l].mean(axis=0)).ravel() for l in labs}

    cents = centroids()
    skipped: set = set()
    while True:
        sizes = Counter(cur)
        smalls = [l for l, c in sizes.items() if c < min_child and l not in skipped]
        if not smalls:
            return cur, terms
        s = min(smalls, key=lambda l: (sizes[l], l))
        cs = cents.get(s)
        if cs is None or not cap:
            # 无法安全合并：保留孤小簇，不再重试（sizes 单调增长，不会死循环）
            skipped.add(s)
            continue
        ns = float(np.sqrt((cs * cs).sum()))
        best, best_sim = None, -1.0
        for t, ct in cents.items():
            if t == s or t in skipped or sizes[s] + sizes[t] > cap:
                continue
            nt = float(np.sqrt((ct * ct).sum()))
            if nt <= 0:
                continue
            sim = float((cs * ct).sum()) / (ns * nt)
            if sim > best_sim:
                best, best_sim = t, sim
        if best is None:
            skipped.add(s)
            continue
        cur = [best if l == s else l for l in cur]
        acc = list(terms.get(best, []))
        for t2 in terms.get(s, []):
            if t2 not in acc:
                acc.append(t2)
        terms[best] = acc[:MAX_NAME_TERMS]
        terms.pop(s, None)
        cents = centroids()
        if len(set(cur)) < 2:
            return None, None


def _cluster_sklearn(docs: list[str], k_list: list[int], seed: int,
                     cap: int = 0, min_child: int = 0):
    """TF-IDF + KMeans（v4：公共词抑制 + 兄弟簇相似度合并）。

    返回 (labels, {lab: terms}, all_terms)；分裂无效或失败返回 None
    （None 时调用方退化为顺序分块，仅用于满足末级容量硬规则）。
    """
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

    # A: 只在"这组会话的差异"上聚类
    if 0 < DF_MASK_RATIO < 1.0:
        Xc, masked_count = _mask_shared_columns(np, X, n, DF_MASK_RATIO, SPLIT_MIN_KEPT_TERMS)
    else:
        Xc, masked_count = X, 0

    best = None
    has_sil = silhouette_score is not None
    for k in k_list:
        if k < 2 or k >= n:
            continue
        km = KMeans(n_clusters=k, random_state=seed, n_init=10, max_iter=200).fit(Xc)
        labels = [int(l) for l in km.labels_]
        sizes = Counter(labels)
        if len(sizes) < 2:
            continue
        cluster_terms = {}
        arr = np.asarray(labels)
        for lab in sizes:
            # 词表取自屏蔽后的矩阵：簇名关键词只反映差异
            mean = np.asarray(Xc[arr == lab].mean(axis=0)).ravel()
            cluster_terms[int(lab)] = _top_terms_from_mean(mean, terms)
        # B: 兄弟簇可分性——相似簇合并回同一组继续递归；
        #   全部并成一组说明这次 k 路切分纯人为，换下一个 k。
        labels, cluster_terms = _merge_similar_clusters(np, Xc, labels, cluster_terms, SIBLING_SIM_MAX)
        if len(set(labels)) < 2:
            continue
        # C: 最小容量——碎叶并入 centroid 最相似的兄弟簇；全部并成一组说明
        # 该 k 纯属人为切分；无法安全合并的孤小簇保留（比硬凑更诚实）。
        merged = _merge_small_clusters(np, Xc, labels, cluster_terms, min_child, cap)
        if merged[0] is not None:
            labels, cluster_terms = merged
        if len(set(labels)) < 2:
            continue
        sil = 0.0
        if has_sil:
            try:
                sil = float(silhouette_score(Xc, labels, metric="cosine"))
            except Exception:
                sil = 0.0
        sizes = Counter(labels)
        minsize = min(sizes.values())
        maxfrac = max(sizes.values()) / n
        if min_child > 1:
            small = sum(1 for v in sizes.values() if v < min_child)
            penalty = small * 0.02 + max(0, maxfrac - 0.55) * 0.08
        else:
            penalty = max(0, 3 - minsize) * 0.012 + max(0, maxfrac - 0.55) * 0.08
        score = sil - penalty
        if best is None or score > best[0]:
            best = (score, labels, cluster_terms)
    if best is None:
        return None
    if masked_count:
        logger.debug(
            "split masked %d shared terms (df>%s, n=%d), best score %.3f",
            masked_count, DF_MASK_RATIO, n, best[0],
        )
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
    min_child = max(3, math.ceil(max_leaf / 5))
    labels = cluster_terms = all_terms = None
    if _SKLEARN_OK:
        try:
            labels, cluster_terms, all_terms = _cluster_sklearn(
                docs, _k_range(k_lo, k_hi), seed, cap=max_leaf, min_child=min_child)
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


# ---------------------------------------------------------------------------
# 森林根：主题（聚类树整体下移一层）/ 工作区（按 workspace 事实归组）/
# 本地终端（动作节点，双击开终端；子节点 ssh/docker/virsh 等后续实现）
# ---------------------------------------------------------------------------

def build_category_root(category_tree: list[dict]) -> dict:
    """把既有分类树整体挂到"主题"根下；重算叶子 category 路径。"""
    attached: set = set()

    def relabel(node: dict, path_ids: list[str]) -> None:
        kids = node.get("children", [])
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    relabel(k, path_ids + [str(k.get("id", ""))])
        else:
            node["category"] = "/".join(path_ids)
            attached.update(str(c) for c in kids if isinstance(c, str))

    for top in category_tree:
        relabel(top, ["1", str(top.get("id", ""))])
    return {
        "id": 1,
        "name": CATEGORY_ROOT_NAME,
        "type": TYPE_CATEGORY,
        "session_count": len(attached),
        "children": category_tree,
        "stats": {},
    }


def _workspace_display_name(ws: str, used: set) -> str:
    """'/cc/agents' → 'agents'；同名冲突时向左多取一段（'runtime/frontend'）。

    Windows 路径按 `[/\\]+` 切分但保留原分隔符风格：'C:\\\\Users\\\\dev\\\\proj' →
    'proj'，冲突时 'dev\\\\proj'——名字直接复用路径原文的段与分隔符。"""
    segs = [s for s in re.split(r"[/\\]+", str(ws)) if s]
    if not segs:
        return UNKNOWN_WORKSPACE_NAME
    sep = "\\" if "\\" in str(ws) else "/"
    for take in range(1, len(segs) + 1):
        cand = sep.join(segs[-take:])
        if take == len(segs) or cand not in used:
            used.add(cand)
            return cand
    return ws  # 不可达，保险返回


def copy_category_subtree_for_sessions(nodes: list, sids: set) -> list:
    """把主题分类树中含 sids 会话的部分复制一份，保持原分类结构与名称。

    筛选后没有会话的子分类整体移除；剥掉原主题树的 category 路径字段
    （工作区根下按工作区节点 id 形成新的查询路径）。id 在各自兄弟层保持
    唯一即可（后端按路径逐段查 id），因此沿用原 id 不做重排。"""
    copied: list = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        kids = node.get("children") or []
        clone = {k: v for k, v in node.items() if k not in ("children", "category")}
        if any(isinstance(k, dict) for k in kids):
            sub = copy_category_subtree_for_sessions(
                [k for k in kids if isinstance(k, dict)], sids)
            if not sub:
                continue
            clone["children"] = sub
            clone["session_count"] = sum(
                int(c.get("session_count") or 0) for c in sub)
        else:
            hits = [str(s) for s in kids if isinstance(s, str) and str(s) in sids]
            if not hits:
                continue
            clone["children"] = hits
            clone["session_count"] = len(hits)
        copied.append(clone)
    return copied


def _copied_category_sids(nodes: list) -> set:
    out: set = set()
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        kids = node.get("children") or []
        if any(isinstance(k, dict) for k in kids):
            out |= _copied_category_sids([k for k in kids if isinstance(k, dict)])
        else:
            out.update(str(c) for c in kids if isinstance(c, str))
    return out


def workspace_category_children(category_roots: list, sids: list) -> list:
    """主题树 + 单个工作区的会话集 → 挂在工作区目录下的分类副本。

    未出现在主题树中的会话（上次刷新之后新建/挂载的）兜底进"未分类"节点，
    保证工作区目录下的会话总数与工作区 session_count 一致、不丢会话。"""
    allowed = {str(s) for s in sids}
    copied = copy_category_subtree_for_sessions(category_roots, allowed)
    missing = [str(s) for s in sids if str(s) not in _copied_category_sids(copied)]
    if missing:
        top_ids = [int(n.get("id") or 0) for n in copied if isinstance(n, dict)]
        copied.append({
            "id": (max(top_ids) + 1) if top_ids else 1,
            "name": UNKNOWN_CATEGORY_NAME,
            "type": TYPE_CATEGORY,
            "session_count": len(missing),
            "children": missing,
            "stats": {},
        })
    return copied


def _workspace_nodes(rows: list[dict], start_id: int = 1,
                     category_roots: list | None = None) -> list[dict]:
    """一组会话 → workspace 目录节点（按会话数降序；未指定的排最后）。

    category_roots 提供时，目录节点下挂主题分类树的按工作区筛选副本
    （空分类剪除）；缺省时保持 v5 形态——children 直接是会话 id。"""
    groups: dict = {}
    for r in rows:
        groups.setdefault(str(r.get("workspace") or ""), []).append(str(r["sid"]))
    ordered = sorted((ws for ws in groups if ws), key=lambda ws: (-len(groups[ws]), ws))
    if "" in groups:
        ordered.append("")

    used: set = set()
    nodes: list = []
    for offset, ws in enumerate(ordered):
        sids = sorted(set(groups[ws]))
        name = _workspace_display_name(ws, used) if ws else UNKNOWN_WORKSPACE_NAME
        children = (workspace_category_children(category_roots, sids)
                    if category_roots else sids)
        nodes.append({
            "id": start_id + offset,
            "name": name,
            "type": TYPE_WORKSPACE,
            "workspace": ws,
            "session_count": len(sids),
            "children": children,
            "stats": {},
        })
    return nodes


def build_workspace_root(rows: list[dict],
                         category_roots: list | None = None) -> dict:
    """"工作区"根：执行环境子层 → workspace 目录节点（事实分组，无大小上限）。

    先按会话执行环境（meta.remote_env，缺省即"本地"）分层，层内按归一化
    workspace 分组；未记录 workspace 的会话归入"未指定工作区"节点，保证
    所有会话在森林中都有归属。仅有一个执行环境时（常见为全部本地），
    环境层在数据中直接省略——前端无需任何特判。多环境时"本地"排首位，
    其余按会话数降序。category_roots 为当前主题分类子树（顶层分类节点列表）：
    提供时每个工作区目录内挂主题树按会话筛选后的副本，复用已命名分类。
    """
    env_groups: dict = {}
    for r in rows:
        env_groups.setdefault(str(r.get("environment") or LOCAL_ENVIRONMENT_KEY), []).append(r)

    def env_count(env_rows: list[dict]) -> int:
        return len({str(r["sid"]) for r in env_rows})

    children: list = []
    if len(env_groups) <= 1:
        children = _workspace_nodes(next(iter(env_groups.values()), []),
                                    category_roots=category_roots)
        workspace_leaf_count = len(children)
    else:
        ordered_envs = sorted(
            env_groups.items(),
            key=lambda kv: (0 if kv[0] == LOCAL_ENVIRONMENT_KEY else 1, -env_count(kv[1]), kv[0]),
        )
        workspace_leaf_count = 0
        for offset, (env_key, env_rows) in enumerate(ordered_envs, 1):
            workspace_nodes = _workspace_nodes(env_rows, category_roots=category_roots)
            workspace_leaf_count += len(workspace_nodes)
            children.append({
                "id": offset,
                "name": environment_display_name(env_key),
                "type": TYPE_ENVIRONMENT,
                "environment": env_key,
                "session_count": env_count(env_rows),
                "children": workspace_nodes,
                "stats": {},
            })
    return {
        "id": 2,
        "name": WORKSPACE_ROOT_NAME,
        "type": TYPE_WORKSPACE,
        "session_count": len({str(r["sid"]) for r in rows}),
        "children": children,
        "stats": {
            "workspace_count": workspace_leaf_count,
            "environment_count": len(env_groups),
        },
    }


def build_terminal_root(rows: list[dict] | None = None,
                        remote_envs: list[dict] | None = None,
                        existing_root: dict | None = None) -> dict:
    """终端动作节点：无会话挂载；双击在对应执行环境打开终端窗口。

    仅本地环境（没有登记远程环境）时保持单根"本地终端"，与旧数据一致。
    存在远程执行环境时根改名"终端"，其默认子节点为"本地"（env_id=''），
    每个远程环境是与"本地"同级的子节点（env_id=登记的原始环境标识，如
    http://host:port 或隧道 id）；双击子节点打开该环境的终端（前端经
    terminals-mux 的 env_id 路由，父端拨号子端；通道键为 env_id+
    session_id，因此各环境的草稿会话 'new' 互不冲突）。

    远程环境**只以登记配置（remote_envs.json）为准**：终端节点必须可连接，
    会话中出现过但已注销的 remote_env 不再成为终端节点（历史会话环境仍
    保留在"工作区"根，那是历史事实而非可连接入口）。existing_root 传入旧
    终端根时，按 env_id 把其下 type=terminal_layer 的用户自定义子终端层
    （含 command 与更深子层）整体移植到新根，因此事实刷新/重建不会丢失
    用户配置的嵌套终端链。排序：本地→远程按历史会话数降序。
    """
    env_sids: dict = {}
    for r in rows or []:
        env_key = str(r.get("environment") or LOCAL_ENVIRONMENT_KEY)
        env_sids.setdefault(env_key, set()).add(str(r["sid"]))

    # 登记配置是权威来源："实例有其他远程执行环境"应当出现在树上，即使
    # 还没有任何会话绑定到它；反之未登记的环境即使有历史会话也不生成节点。
    env_ids: dict = {}
    for rec in remote_envs or []:
        env_id = str(rec.get("id") or "").strip()
        if not env_id:
            continue
        env_key = normalize_environment(env_id)
        if env_key == LOCAL_ENVIRONMENT_KEY:
            continue
        env_sids.setdefault(env_key, set())
        env_ids.setdefault(env_key, env_id)

    remote_keys = sorted(
        (k for k in env_ids if k != LOCAL_ENVIRONMENT_KEY),
        key=lambda k: (-len(env_sids.get(k) or ()), k),
    )

    # 旧根中的用户自定义终端层按 env_id 移植。
    old_layers: dict = {}
    for child in (existing_root or {}).get("children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == TYPE_TERMINAL_LAYER:
            # 单环境形态（根即"本地"环境节点）：终端层直接挂在根下。登记了
            # 远程环境后根只当容器，这些层必须落到"本地"子节点下，否则一次
            # 事实刷新就把用户自建的嵌套终端整条丢掉。
            old_layers.setdefault("", []).append(child)
            continue
        if child.get("type") != TYPE_TERMINAL:
            continue
        layers = [g for g in child.get("children") or []
                  if isinstance(g, dict) and g.get("type") == TYPE_TERMINAL_LAYER]
        if layers:
            key = str(child.get("env_id") or "")
            old_layers.setdefault(key, []).extend(layers)

    if not remote_keys:
        # 单环境形态：根本身就是"本地"环境节点（无环境子级），因此根也要
        # 带上 env 字段——前端的右键菜单（新建/编辑/删除子终端）与
        # _find_terminal_env_node 的回落分支都以此识别"可连接的环境节点"。
        return {
            "id": 3,
            "name": TERMINAL_ROOT_NAME,
            "type": TYPE_TERMINAL,
            "environment": LOCAL_ENVIRONMENT_KEY,
            "env_id": "",
            "session_count": 0,
            "children": list(old_layers.get("") or []),
            "stats": {},
        }

    def env_node(env_key: str, offset: int) -> dict:
        env_id = "" if env_key == LOCAL_ENVIRONMENT_KEY else env_ids.get(env_key, env_key)
        return {
            "id": offset,
            "name": environment_display_name(env_key),
            "type": TYPE_TERMINAL,
            "environment": env_key,
            "env_id": env_id,
            "session_count": 0,
            "children": list(old_layers.get(env_id) or []),
            "stats": {},
        }

    children = [env_node(LOCAL_ENVIRONMENT_KEY, 1)]
    for offset, env_key in enumerate(remote_keys, 2):
        children.append(env_node(env_key, offset))
    return {
        "id": 3,
        "name": TERMINAL_MULTI_ROOT_NAME,
        "type": TYPE_TERMINAL,
        "session_count": 0,
        "children": children,
        "stats": {"environment_count": len(children)},
    }


def derive_layer_name(command: str) -> str:
    """由进入指令推导可读的终端层名称（可在创建对话框中覆盖）。"""
    cmd = " ".join(str(command or "").split())
    if not cmd:
        return "嵌套终端"
    tokens = cmd.split()
    first = tokens[0].lower()
    if first == "ssh":
        target = next((t for t in tokens[1:] if "@" in t and not t.startswith("-")),
                      next((t for t in tokens[1:] if not t.startswith("-")), ""))
        return f"ssh {target}".strip()
    if first == "docker" and "exec" in tokens:
        rest = [t for t in tokens[tokens.index("exec") + 1:] if not t.startswith("-")]
        return f"docker:{rest[0]}" if rest else "docker"
    if first == "wsl":
        return "WSL"
    if first == "su":
        rest = [t for t in tokens[1:] if not t.startswith("-")]
        return f"su:{rest[0]}" if rest else "su"
    if first in ("psql", "mysql", "sqlplus", "redis-cli", "mongo"):
        return first
    if first == "kubectl" and any(t in ("exec", "attach") for t in tokens):
        rest = [t for t in _strip_cli_opts(
            tokens[1:], ("-n", "--namespace", "-c", "--container"))
            if t not in ("exec", "attach")]
        return f"kubectl:{rest[0]}" if rest else "kubectl"
    return cmd[:24]


def _strip_cli_opts(tokens: list, valued_opts=()) -> list:
    """去除 CLI flag；valued_opts 连同其值一并去除（如 -n ns）。"""
    out: list = []
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if tok.lower() in valued_opts:
                skip = True
            continue
        out.append(tok)
    return out


def _terminal_root_of(doc: dict) -> dict | None:
    for root in doc.get("tree") or []:
        if isinstance(root, dict) and root.get("type") == TYPE_TERMINAL:
            return root
    return None


def _next_tree_node_id(doc: dict) -> int:
    highest = 0

    def walk(node) -> None:
        nonlocal highest
        if not isinstance(node, dict):
            return
        try:
            highest = max(highest, int(node.get("id") or 0))
        except (TypeError, ValueError):
            pass
        for kid in node.get("children") or []:
            walk(kid)

    for root in doc.get("tree") or []:
        walk(root)
    return highest + 1


def _find_terminal_env_node(root: dict, env_id: str) -> dict | None:
    """终端根下的环境子节点；单环境形态回落为根自身。

    只有本地环境时树里只有一个根"本地终端"，没有环境子节点——根就是本地环境
    节点（前端也按 ``env_id=''`` 请求它）。此时必须把根当作环境节点返回，否则
    最常见的"在本地终端下新建子终端"会一路失败（``env_id=''`` 找不到节点）。
    """
    wanted = str(env_id or "")
    has_env_child = False
    for child in root.get("children") or []:
        if not isinstance(child, dict) or child.get("type") != TYPE_TERMINAL:
            continue
        has_env_child = True
        if str(child.get("env_id") or "") == wanted:
            return child
    if not wanted and not has_env_child:
        return root
    return None


def _find_terminal_layer(node: dict, uid: str, parent: dict | None = None) -> tuple | None:
    for kid in node.get("children") or []:
        if isinstance(kid, dict) and kid.get("type") == TYPE_TERMINAL_LAYER:
            if str(kid.get("uid") or "") == str(uid):
                return kid, node
            found = _find_terminal_layer(kid, uid, node)
            if found:
                return found
    return None


def add_terminal_layer(doc: dict, env_id: str, command: str,
                       name: str = "", parent_uid: str = "") -> dict:
    """在终端环境节点（或某个已有终端层）下挂一层嵌套终端，返回新节点。"""
    command = str(command or "").strip()
    if not command:
        raise ValueError("command is required")
    root = _terminal_root_of(doc)
    if root is None:
        raise ValueError("no terminal root in tree; rebuild first")
    env_node = _find_terminal_env_node(root, str(env_id or ""))
    if env_node is None:
        raise ValueError(f"terminal environment node not found: env_id={env_id!r}")
    parent = env_node
    if parent_uid:
        found = _find_terminal_layer(env_node, parent_uid)
        if found is None:
            raise ValueError(f"parent terminal layer not found: uid={parent_uid!r}")
        parent = found[0]
    node = {
        "id": _next_tree_node_id(doc),
        "uid": uuid.uuid4().hex[:16],
        "name": str(name or "").strip() or derive_layer_name(command),
        "type": TYPE_TERMINAL_LAYER,
        "command": command,
        "session_count": 0,
        "children": [],
        "stats": {},
    }
    parent.setdefault("children", []).append(node)
    return node


def remove_terminal_layer(doc: dict, env_id: str, uid: str) -> bool:
    """删除终端层及其整个子树（字面义删除）；返回是否命中。"""
    root = _terminal_root_of(doc)
    if root is None:
        return False
    env_node = _find_terminal_env_node(root, str(env_id or ""))
    if env_node is None:
        return False
    found = _find_terminal_layer(env_node, str(uid or ""))
    if found is None:
        return False
    node, parent = found
    parent["children"] = [k for k in parent.get("children") or [] if k is not node]
    return True


def update_terminal_layer(doc: dict, env_id: str, uid: str,
                          command: str, name: str = "") -> dict | None:
    """更新终端层的进入指令（必填）与显示名称（留空沿用原名）；未命中返回 None。"""
    command = str(command or "").strip()
    if not command:
        raise ValueError("command is required")
    root = _terminal_root_of(doc)
    if root is None:
        return None
    env_node = _find_terminal_env_node(root, str(env_id or ""))
    if env_node is None:
        return None
    found = _find_terminal_layer(env_node, str(uid or ""))
    if found is None:
        return None
    node, _parent = found
    node["command"] = command
    new_name = str(name or "").strip()
    if new_name:
        node["name"] = new_name
    return node


def terminal_layer_chain(doc: dict, env_id: str, uid: str) -> list[dict]:
    """从环境节点到指定终端层的节点链（含各层 command），未命中返回 []。"""
    root = _terminal_root_of(doc)
    if root is None:
        return []
    env_node = _find_terminal_env_node(root, str(env_id or ""))
    if env_node is None:
        return []
    if not uid:
        return [env_node]

    def walk(node: dict, path: list) -> list | None:
        for kid in node.get("children") or []:
            if not isinstance(kid, dict) or kid.get("type") != TYPE_TERMINAL_LAYER:
                continue
            if str(kid.get("uid") or "") == str(uid):
                return path + [kid]
            found = walk(kid, path + [kid])
            if found:
                return found
        return None

    return walk(env_node, [env_node]) or []


def _recency_sort(rows: list[dict]) -> list[dict]:
    """按最近活动时间降序（与会话列表 last_inference_at 语义同序）。"""
    return sorted(
        rows,
        key=lambda r: (str(r.get("last_activity") or r.get("created_at") or ""), str(r.get("sid"))),
        reverse=True,
    )


def build_history_root(rows: list[dict]) -> dict:
    """"会话历史"根：YYYYMM 月份节点按时间倒序，会话按创建时间归入月份。

    月份节点为末级（children 为会话 ID，列表内按最近活动降序，与查询接口
    一致）；前端仅在点击月份时才请求该月会话列表，未展开不加载。创建时间
    无法解析的会话不进入历史根（实际数据均带 created_at）。
    """
    groups: dict = {}
    for r in rows:
        mk = month_key(r.get("created_at"))
        if mk:
            groups.setdefault(mk, []).append(r)
    children: list = []
    attached: set = set()
    for offset, mk in enumerate(sorted(groups, reverse=True), 1):
        sids: list = []
        for r in _recency_sort(groups[mk]):
            sid = str(r["sid"])
            if sid not in sids:
                sids.append(sid)
        attached.update(sids)
        children.append({
            "id": offset,
            "name": mk,
            "type": TYPE_MONTH,
            "session_count": len(sids),
            "children": sids,
            "stats": {},
        })
    return {
        "id": 5,
        "name": HISTORY_ROOT_NAME,
        "type": TYPE_HISTORY,
        "session_count": len(attached),
        "children": children,
        "stats": {"month_count": len(children)},
    }


def validate(tree: list[dict], all_sids: set, max_leaf: int) -> list[str]:
    """校验硬性规则与全量覆盖，返回错误列表（空列表 = 通过）。

    单分类模式：每个会话在分类子树中至多出现于一个末级分类；重复挂载
    属于数据错误（旧版多分类图语义已废弃）。
    """
    errors: list[str] = []
    attached: set = set()
    seen_in: dict = {}
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
            for sid in str_kids:
                seen_in.setdefault(str(sid), []).append(path)
            attached.update(str_kids)

    if len(tree) == 1 and isinstance(tree[0], dict) and any(
            isinstance(k, dict) for k in tree[0].get("children", [])):
        errors.append("顶层只有一个包含子分类的包装分类（包装层应去掉）")
    for top in tree:
        if isinstance(top, dict):
            walk(top, str(top.get("id")))
    duplicated = {sid: paths for sid, paths in seen_in.items() if len(paths) > 1}
    for sid, paths in sorted(duplicated.items())[:5]:
        errors.append(f"会话 {sid} 出现在 {len(paths)} 个末级分类（单分类要求至多 1 个）: {paths}")
    if len(duplicated) > 5:
        errors.append(f"共 {len(duplicated)} 个会话重复挂载（单分类模式不允许）")
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


def _naming_state(llm) -> dict:
    """命名熔断状态挂在 llm 对象上：每次构建独立计数。

    端点连续多次传输失败（超时/断连）时判定其不可用，后续命名直接
    回退关键词名，避免整场构建被不稳定端点拖到数小时。
    """
    st = getattr(llm, "_tree_naming_state", None)
    if st is None:
        st = {"failures": 0}
        try:
            llm._tree_naming_state = st
        except Exception:
            pass
    return st


def _naming_record(state: dict, ok: bool) -> None:
    if ok:
        state["failures"] = 0
    else:
        state["failures"] += 1


def _llm_name(llm, prompt: str) -> str:
    """调用 LLM 取名称；任何失败都返回空串（由调用方回退）。"""
    if llm is None:
        return ""
    state = _naming_state(llm)
    if state["failures"] >= 3:
        return ""
    try:
        reply = llm(prompt)
    except Exception:
        _naming_record(state, False)
        return ""
    if not isinstance(reply, str) or not reply.strip():
        _naming_record(state, False)
        return ""
    _naming_record(state, True)
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
        "例如'agents-runtime 侧边栏'）。\n"
        + NAMING_RULE + "只输出分类名称：\n\n"
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
        "若子分类集中在某个项目工作区上，名称中可体现项目名）。\n"
        + NAMING_RULE + "只输出分类名称：\n\n"
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


# ---------------------------------------------------------------------------
# C: 对比式兄弟命名——同层兄弟一次调用，互斥命名 + 受控合并建议
# ---------------------------------------------------------------------------

NAMING_SIBLING_SAMPLES = 5


def _is_terminal_raw(node: dict) -> bool:
    return not any(isinstance(c, dict) for c in node.get("_children_raw") or [])


def _sibling_block(index: int, node: dict) -> str:
    rows = node.get("_rows") or []
    lines = [f"组{index}（{len(rows)} 个会话）:"]
    if _is_terminal_raw(node):
        for line in _sample_session_lines(rows, NAMING_SIBLING_SAMPLES):
            lines.append("  会话: " + line)
    else:
        child_names = "、".join(
            str(c["_name"]) for c in node["_children_raw"] if isinstance(c, dict)
        )[:400]
        lines.append("  其下子分类: " + child_names)
    for line in _workspace_file_context(rows):
        lines.append("  " + line)
    return "\n".join(lines)


def _clean_one_name(value) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    name = name.splitlines()[0].strip().strip('"').strip("'").strip()
    name = re.sub(r"^(\d+)\s*[:：.、]\s*", "", name)
    return name[:20]


def _parse_names_reply(reply):
    """解析对比命名返回：优先 JSON {"names":{...},"merges":[[...]]}，
    其次退化为逐行 "N: 名称"。返回 (names|None, merges)。"""
    if not isinstance(reply, str) or not reply.strip():
        return None, []
    s = reply.strip()
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    lo, hi = s.find("{"), s.rfind("}")
    if 0 <= lo < hi:
        try:
            data = json.loads(s[lo:hi + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            raw_names = data.get("names")
            names = {}
            if isinstance(raw_names, dict):
                for k, v in raw_names.items():
                    key = str(k).strip()
                    clean = _clean_one_name(v)
                    if key.isdigit() and clean:
                        names[key] = clean
            merges: list = []
            raw_merges = data.get("merges")
            if isinstance(raw_merges, list):
                for grp in raw_merges:
                    if isinstance(grp, (list, tuple)) and len(grp) >= 2:
                        idxs = [str(x).strip() for x in grp if str(x).strip().isdigit()]
                        if len(idxs) >= 2:
                            merges.append(idxs)
            if names or merges:
                return names, merges
    names = {}
    for line in s.splitlines():
        m = re.match(r"^\s*(\d+)\s*[:：.、]\s*(.+?)\s*$", line)
        if m and _clean_one_name(m.group(2)):
            names[m.group(1)] = _clean_one_name(m.group(2))
    if names:
        return names, []
    return None, []


def _merge_terminal_siblings(children: list, group_idxs: list, used: set, max_leaf: int) -> bool:
    """应用一组兄弟合并（仅末级组；合并后 ≤ max_leaf；父节点剩余子分类 ≥2）。"""
    members = []
    for key in group_idxs:
        pos = int(key) - 1
        if not 0 <= pos < len(children):
            return False
        node = children[pos]
        if pos in used or not isinstance(node, dict) or not _is_terminal_raw(node):
            return False
        members.append(pos)
    if len(members) < 2 or len(children) - len(members) + 1 < 2:
        return False
    merged_sids: list = []
    merged_rows: list = []
    for pos in members:
        node = children[pos]
        for sid in node["_children_raw"]:
            if sid not in merged_sids:
                merged_sids.append(sid)
        merged_rows.extend(node.get("_rows") or [])
    if len(merged_sids) > max_leaf:
        return False
    merged = {
        "_name": children[members[0]]["_name"],
        "_children_raw": merged_sids,
        "_rows": merged_rows,
    }
    for pos in members:
        used.add(pos)
    insert_at = members[0]
    remaining = [c for pos, c in enumerate(children) if pos not in used]
    remaining.insert(insert_at, merged)
    children[:] = remaining
    return True


def _name_sibling_set(children: list, llm, max_leaf: int) -> None:
    """对一组兄弟分类做一次对比命名调用；解析失败回退逐组独立命名。

    children 必须是父节点实际的 _children_raw 列表（或顶层列表本身），
    合并建议通过切片赋值原地生效。
    """
    children[:] = [c for c in children if isinstance(c, dict)]
    if not children:
        return
    blocks = "\n\n".join(_sibling_block(i, node) for i, node in enumerate(children, 1))
    prompt = (
        f"我们在把历史会话整理成层级目录。下面是同一父分类下的 {len(children)} 组兄弟分类"
        "（相互并列、成员不重叠）。\n"
        "请为每组起一个简短且相互互斥的名称（不超过12个汉字或5个英文单词，不要标点、引号、"
        "'分类'或'会话'字样），要求用户只看名称就能说出每组的边界；任何两个名称不得实质同义，"
        "优先使用不同的对象、动作或场景词。\n"
        "名称必须由完整的常用词或常见技术词组成，禁止出现把正常词切开后的非词片段"
        "（例如把'文件管理'写成'件管理'），也不要用顿号罗列多个零碎词拼成名称。\n"
        f"若发现两组实际是同一主题、无法实质区分，可在 merges 中建议合并（下标从1开始；"
        f"只有两组均为末级组且合并后会话数不超过 {max_leaf} 时才会生效）。\n"
        '只输出 JSON，形如 {"names": {"1": "...", "2": "..."}, "merges": []}：\n\n'
        + blocks
    )
    state = _naming_state(llm) if llm is not None else {"failures": 3}
    reply = ""
    if state["failures"] < 3:
        try:
            reply = llm(prompt)
        except Exception:
            reply = ""
        _naming_record(state, bool(isinstance(reply, str) and reply.strip()))
    names, merges = _parse_names_reply(reply)
    if names is None:
        # 对比命名失败：回退为旧的逐组独立命名
        for node in children:
            node["_name"] = (
                _name_terminal(node, llm) if _is_terminal_raw(node)
                else _name_internal(node, llm)
            )
        return
    for node, i in zip(children, range(1, len(children) + 1)):
        name = names.get(str(i))
        if name:
            node["_name"] = name
    used: set = set()
    for group in merges:
        _merge_terminal_siblings(children, group, used, max_leaf)


def assign_names_llm(nodes: list[dict], llm, max_leaf: int = MAX_LEAF_SIZE) -> None:
    """对比式自底向上命名：每个父节点下的一组兄弟分类一次 LLM 调用。

    先处理最深的内部节点（其子分类已命名，可作为上下文），最后命名顶层
    并列分类；同层并发（线程池，默认 NAMING_WORKERS），单次调用解析失败
    时该组回退为逐组独立命名，再失败则保留高频词名称。
    """
    if llm is None:
        return
    internals: list = []
    _collect_nodes(nodes, [], internals)
    internals.sort(key=lambda item: -item[0])
    with ThreadPoolExecutor(max_workers=NAMING_WORKERS, thread_name_prefix="tree-naming") as pool:
        level: list = []
        current_depth = None

        def flush() -> None:
            if not level:
                return
            # 传入真实 _children_raw：合并原地生效
            futures = [pool.submit(_name_sibling_set, node["_children_raw"], llm, max_leaf)
                       for node in level]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
            level.clear()

        for depth, node in internals:
            if current_depth is not None and depth != current_depth:
                flush()
            current_depth = depth
            level.append(node)
        flush()
        # 顶层并列分类（无父节点）最后命名
        _name_sibling_set(nodes, llm, max_leaf)


# ---------------------------------------------------------------------------
# 构建入口
# ---------------------------------------------------------------------------

def build_document(rows: list[dict], max_leaf: int | None = None, seed: int = DEFAULT_SEED,
                   llm=None, remote_envs: list[dict] | None = None,
                   existing_document: dict | None = None) -> dict:
    """由会话列表构建完整 tree.json 文档（含校验；失败抛 ValueError）。

    max_leaf 为 None 时取 :func:`dynamic_max_leaf`（max(2, 5*log2(会话数))）。
    顶层不加包装层：若全部会话拆成"一个含子分类的根"，顶层直接取该根的子分类；
    若全部会话本身就是一个末级分类（<= max_leaf），顶层就是这唯一的分类。
    """
    if max_leaf is None:
        max_leaf = dynamic_max_leaf(len(rows))
    root = build_node(rows, max_leaf, seed)
    if any(isinstance(c, dict) for c in root["_children_raw"]):
        raw_nodes = [c for c in root["_children_raw"] if isinstance(c, dict)]
    else:
        raw_nodes = [root]
    if llm is not None:
        try:
            assign_names_llm(raw_nodes, llm, max_leaf=max_leaf)
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
        "version": TREE_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": (
            "runtime.category_tree_builder — code-only TF-IDF + KMeans recursive split "
            "(v5: per-node shared-term masking, sibling-sim merge, "
            f"terminal categories capped at {max_leaf} sessions, "
            "single-membership (each session in exactly one terminal category); "
            "a category branches only into >= 2 sub-categories; "
            + ("categories named by LLM (contrastive sibling naming)" if llm is not None
               else "categories named by top terms (offline)")
        ),
        "experimental": True,
        "schema": {
            "category_node": '{id:int,name:string,type:"category",session_count:int,children:[category|string]}',
            "session_leaf": "conversation session ID string",
            "category_query": "join category node IDs with /; terminal paths may have different depths, e.g. 2/2 or 1/1/3",
            "graph_note": "Single-membership mode: each session ID occurs under exactly one terminal category node.",
            "forest_note": (
                "v7 forest roots: 1=" + CATEGORY_ROOT_NAME + " (clustering categories, type=category), "
                "2=" + WORKSPACE_ROOT_NAME + " (type=workspace; optional " + TYPE_ENVIRONMENT + " layer from "
                "meta.remote_env collapsed when only the local environment exists; each workspace directory "
                "node hosts a filtered copy of the category tree restricted to that workspace's sessions, "
                "empty categories pruned, unmounted sessions fall back to a " + UNKNOWN_CATEGORY_NAME
                + " node; workspace path separators \\ and / keep their original style, only duplicated/"
                "trailing separators collapse), "
                "3=" + TERMINAL_ROOT_NAME + "/" + TERMINAL_MULTI_ROOT_NAME + " (action node, type=terminal; "
                "renamed \"" + TERMINAL_MULTI_ROOT_NAME + "\" with \"本地\" + one child per registered remote "
                "environment (remote_envs.json; each child carries env_id for terminals-mux routing; when only "
                "the local environment exists there is no environment child and the root itself is that node, "
                "carrying environment/env_id=\"\" with user terminal_layer children directly under it), "
                "5=" + HISTORY_ROOT_NAME + " (type=history with " + TYPE_MONTH + " YYYYMM leaves, time desc); "
                "the v3-v6 recent-sessions root is gone: the sidebar mode bar lists recent sessions via "
                "the plain session list API instead"
            ),
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
        "tree": [
            build_category_root(tree),
            build_workspace_root(rows, category_roots=tree),
            build_terminal_root(rows, remote_envs,
                                existing_root=_terminal_root_of(existing_document)
                                if existing_document else None),
            build_history_root(rows),
        ],
    }
    workspace_root = doc["tree"][1]
    terminal_root = doc["tree"][2]
    history_root = doc["tree"][3]
    doc["stats"]["workspace_node_count"] = workspace_root["stats"].get("workspace_count", 0)
    doc["stats"]["environment_node_count"] = workspace_root["stats"].get("environment_count", 0)
    doc["stats"]["terminal_environment_count"] = len(terminal_root.get("children") or [])
    doc["stats"]["month_node_count"] = history_root["stats"].get("month_count", 0)
    return doc


def build_chats_dir(chats_dir: str | os.PathLike, llm=None,
                    max_leaf: int | None = None,
                    seed: int = DEFAULT_SEED) -> dict | None:
    """扫描目录并构建文档；会话数不足（<= MIN_SESSIONS）时返回 None。

    max_leaf 为 None 时按总会话数取动态上限 dynamic_max_leaf。
    """
    chats = Path(chats_dir)
    rows = load_sessions(chats)
    if len(rows) <= MIN_SESSIONS:
        return None
    if max_leaf is None:
        max_leaf = dynamic_max_leaf(len(rows))
    logger.info(
        "building category tree: %d sessions, max_leaf=%d, seed=%d, llm=%s",
        len(rows), max_leaf, seed, "yes" if llm is not None else "no",
    )
    existing_document = None
    try:
        with open(Path(chats) / "tree.json", "r", encoding="utf-8") as fh:
            existing_document = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass
    return build_document(rows, max_leaf, seed, llm=llm,
                          remote_envs=load_remote_envs(chats),
                          existing_document=existing_document)


def refresh_facts(doc: dict, rows: list[dict], remote_envs: list[dict] | None = None) -> dict:
    """就地刷新森林文档的事实根，保留主题子树（含全部 LLM 命名）。

    重新生成"工作区/终端/最近会话/会话历史"四个纯事实根并同步 stats/version。
    适用场景：任意旧版本 → 当前 TREE_VERSION 的升级，或事实数据（workspace、
    remote_env、月份窗口）漂移但不想重新付聚类与命名的 LLM 成本。主题子树若
    违反 v5 单分类语义则抛 ``ValueError``，此时应走完整重建而不是刷新。
    """
    remote_envs = remote_envs or []
    roots = [r for r in doc.get("tree") or [] if isinstance(r, dict)]
    category_root = next((r for r in roots if r.get("type") == TYPE_CATEGORY), None)
    if category_root is None:
        raise ValueError(f"no type={TYPE_CATEGORY!r} root in existing tree; run a full build instead")

    # 保留的主题子树必须先满足单分类语义，否则刷新会把坏数据带到新版本。
    seen_in: dict[str, list[str]] = {}
    leaf_count = 0

    def _walk(node: dict, path: str) -> None:
        nonlocal leaf_count
        kids = node.get("children", [])
        if any(isinstance(k, dict) for k in kids):
            for k in kids:
                if isinstance(k, dict):
                    _walk(k, f"{path}/{k.get('id')}")
        else:
            leaf_count += 1
            for sid in (str(c) for c in kids if isinstance(c, str)):
                seen_in.setdefault(sid, []).append(path)

    _walk(category_root, str(category_root.get("id")))
    duplicates = {sid: paths for sid, paths in seen_in.items() if len(paths) > 1}
    if duplicates:
        example = sorted(duplicates.items())[:3]
        raise ValueError(
            f"{len(duplicates)} sessions attached to multiple categories "
            f"(v5 requires single-category); run a full rebuild instead: {example}")

    live_sids = {str(r["sid"]) for r in rows}
    stale = set(seen_in) - live_sids
    if stale:
        logger.warning("%d attached sessions no longer exist on disk: %s",
                       len(stale), sorted(stale)[:5])

    # 按 type 找根而不是按名字；根名随后统一为当前常量（根名可随版本改）。
    category_root["name"] = CATEGORY_ROOT_NAME
    old_terminal_root = next((r for r in roots if r.get("type") == TYPE_TERMINAL), None)
    workspace_root = build_workspace_root(
        rows, category_roots=category_root.get("children") or [])
    terminal_root = build_terminal_root(rows, remote_envs, existing_root=old_terminal_root)
    history_root = build_history_root(rows)

    doc["version"] = TREE_VERSION
    doc["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    doc["tree"] = [category_root, workspace_root, terminal_root, history_root]
    doc.setdefault("schema", {})["forest_note"] = (
        "v7 forest roots: 1=" + CATEGORY_ROOT_NAME + " (clustering categories, type=category, "
        "single-membership: each session under exactly one terminal category), "
        "2=" + WORKSPACE_ROOT_NAME + " (type=workspace; optional " + TYPE_ENVIRONMENT
        + " layer from meta.remote_env collapsed when only the local environment exists; each workspace "
        "directory node hosts a filtered copy of the category tree restricted to that workspace's "
        "sessions, empty categories pruned, unmounted sessions fall back to a " + UNKNOWN_CATEGORY_NAME
        + " node), "
        "3=" + TERMINAL_ROOT_NAME + "/" + TERMINAL_MULTI_ROOT_NAME + " (action node, type=terminal; "
        "renamed \"" + TERMINAL_MULTI_ROOT_NAME + "\" with \"本地\" + one child per registered remote "
        "environment (remote_envs.json; each child carries env_id for terminals-mux routing; when only "
        "the local environment exists there is no environment child and the root itself is that node, "
        "carrying environment/env_id=\"\" with user terminal_layer children directly under it), "
        "5=" + HISTORY_ROOT_NAME + " (type=history with " + TYPE_MONTH + " YYYYMM leaves, time desc); "
        "the v3-v6 recent-sessions root is gone: the sidebar mode bar lists recent sessions via "
        "the plain session list API instead"
    )
    stats = doc.setdefault("stats", {})
    stats["conversation_count"] = len(live_sids)
    stats["attached_conversation_count"] = len(seen_in)
    stats["terminal_category_count"] = leaf_count
    stats["membership_edge_count"] = sum(len(p) for p in seen_in.values())
    stats["multi_category_conversation_count"] = len(duplicates)
    stats["max_categories_per_conversation"] = max((len(p) for p in seen_in.values()), default=0)
    stats["workspace_node_count"] = workspace_root["stats"].get("workspace_count", 0)
    stats["environment_node_count"] = workspace_root["stats"].get("environment_count", 0)
    stats["terminal_environment_count"] = len(terminal_root.get("children") or [])
    stats["month_node_count"] = history_root["stats"].get("month_count", 0)
    stats.pop("recent_session_count", None)
    stats.pop("recent_limit", None)
    stats.setdefault("max_leaf_size", dynamic_max_leaf(len(live_sids)))
    return doc


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


class _CliSessionShim:
    """只提供 make_summary_llm 需要的两个属性（推理函数 + 模型注册表）。"""

    def __init__(self, infer_fn, registry, chats_dir: str) -> None:
        self.inference_callable = infer_fn
        self.model_registry = registry
        self.chats_dir = chats_dir


def cli_summary_llm(chats_dir: str):
    """离线 CLI 用 --llm 构建命名 LLM（与服务器后台构建同一模型路径）。

    与 handler 无关：只装配 models.json + Runtime.infer，惰性导入避免
    模块级依赖 runtime.server（构建器本身保持零第三方依赖、可独立导入）。
    """
    from runtime.server import _MODELS_PATH
    from runtime.mcp_client import MCPClientManager
    from runtime.registry import ModelRegistry, ToolRegistry
    from runtime.runtime import Runtime
    from runtime.skill_manager import SkillManager

    registry = ModelRegistry()
    if os.path.isfile(_MODELS_PATH):
        registry.load(_MODELS_PATH)
    else:
        logger.warning("models config not found at %s; LLM naming will fail fast", _MODELS_PATH)
    tool_registry = ToolRegistry()
    runtime_obj = Runtime(
        model_registry=registry,
        tool_registry=tool_registry,
        mcp_manager=MCPClientManager(),
        skill_manager=SkillManager(tool_registry),
    )
    shim = _CliSessionShim(runtime_obj.infer, registry, chats_dir)
    return make_summary_llm(shim)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="扫描历史会话并生成层次分类体系 tree.json（递归拆分，末级 <= N 个会话）"
    )
    ap.add_argument("--chats-dir", default="chat_data", help="会话根目录（含 */conversation.json）")
    ap.add_argument("--output", default=None, help="输出文件（默认 <chats-dir>/tree.json）")
    ap.add_argument("--max-leaf-size", type=int, default=None,
                    help="末级分类的最大会话数（默认 max(2, 5*log2(总会话数))）")
    ap.add_argument("--min-sessions", type=int, default=MIN_SESSIONS,
                    help=f"会话数量须大于该值才构建（默认 {MIN_SESSIONS}）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="KMeans 随机种子")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 tree.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    ap.add_argument("--llm", action="store_true",
                    help="用 SUMMARY_MODEL 模型做 LLM 命名（默认离线高频词命名）")
    ap.add_argument("--refresh-facts", action="store_true",
                    help="只刷新事实根（工作区/终端/最近/历史），保留主题子树与 LLM 命名；"
                         "不跑聚类与命名，可用于旧版本→当前版本升级")
    args = ap.parse_args(argv)

    chats_dir = Path(args.chats_dir)
    if not chats_dir.is_dir():
        print(f"chats dir not found: {chats_dir}", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else chats_dir / "tree.json"

    if args.refresh_facts:
        if not output.is_file():
            print(f"tree not found: {output}; run a full build instead", file=sys.stderr)
            return 2
        doc = json.loads(output.read_text("utf-8"))
        print(f"current version={doc.get('version')} -> target={TREE_VERSION}")
        try:
            doc = refresh_facts(doc, load_sessions(chats_dir), load_remote_envs(chats_dir))
        except ValueError as exc:
            print(f"refresh refused: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(doc["stats"], ensure_ascii=False, indent=2))
        if args.dry_run:
            print("dry-run: not written")
            return 0
        _atomic_write_json(output, doc)
        print(f"wrote {output}")
        return 0

    if output.exists() and not args.force and not args.dry_run:
        print(f"tree already exists, skipping (use --force to overwrite): {output}")
        return 0

    rows = load_sessions(chats_dir)
    if len(rows) <= args.min_sessions:
        print(f"only {len(rows)} sessions found (need > {args.min_sessions}); skipping build")
        return 0

    llm = None
    if args.llm:
        llm = cli_summary_llm(str(chats_dir))
    cap = args.max_leaf_size
    if cap is None:
        cap = dynamic_max_leaf(len(rows))
    print(
        f"loaded {len(rows)} sessions; building tree "
        f"(max_leaf_size={cap}, seed={args.seed}, "
        f"clustering={'sklearn' if _SKLEARN_OK else 'pure-python-fallback'}, "
        f"naming={'LLM (SUMMARY_MODEL)' if llm is not None else 'offline-top-terms'})"
    )
    existing_document = None
    try:
        with open(output, "r", encoding="utf-8") as fh:
            existing_document = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass
    doc = build_document(rows, cap, args.seed, llm=llm,
                         remote_envs=load_remote_envs(chats_dir),
                         existing_document=existing_document)

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

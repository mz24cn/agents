"""runtime/model_probe.py — 探测上游模型端点自己公布的元数据（窗口、模态、能力）。

``ModelConfig.max_context`` 一直是人工填写的，而多数服务端其实知道自己
（甚至每个模型）的运行时窗口。本模块向**上游**问一次，把各家形状归一成
一行行可展示的数据，供 Setup 的模型编辑页渲染：

    GET {api_base}/models          # OpenAI 兼容列表（首选）
    GET {root}/api/tags            # 原生列表：context_length + capabilities
    GET {root}/props               # 实例属性：default_generation_settings.n_ctx
    GET {root}/health              # 健康检查：max_context

字段名各家不统一，按**端点形状**归类（不按厂商归类）实测见过的几类：

    本地 GGUF 服务    ``meta.n_ctx`` / ``meta.n_ctx_train`` / ``meta.n_params``
                      / ``meta.ftype`` / ``aliases``；实例级信息在 ``/props``
    带加载状态的实例  ``meta.n_ctx`` / ``architecture.input_modalities`` /
                      ``status.value``
    原生 runner       ``/v1/models`` 只有 id —— 窗口和模态在 ``/api/tags`` 的
                      ``details.context_length`` 与 ``capabilities`` 里
    托管 API          ``context_window`` / ``context_length`` /
                      ``max_output_tokens`` / ``max_output_length`` /
                      ``input_modalities`` / ``output_modalities`` / ``effort``
    能力声明          ``api_capabilities`` / ``supported_features`` /
                      ``supported_sampling_parameters`` / ``chat_template_caps``
    只有分类字段      ``type``（``text_model`` / ``visual_model`` /
                      ``speech_model`` / ``image_generation``）—— 模态的唯一线索
    只有路由信息      ``supported_endpoint_types``；错误也可能以 HTTP 200 +
                      ``{"code":401,"msg":...}`` 返回

所以探测是 best-effort：拿不到就返回 0 / 空列表，让前端保留人工配置值。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from runtime.models import _parse_token_count

DEFAULT_TIMEOUT = 10.0

# 上下文窗口的字段名，按可信度排序。``n_ctx`` 是运行时窗口（可能被 --ctx-size
# 改小），``n_ctx_train`` 是模型训练窗口——只在前者缺失时兜底，并在 source 里
# 标明，避免把「模型能吃 256K」误当成「这个实例开了 256K」。
_CONTEXT_FIELDS = (
    "max_context",
    "max_model_len",
    "context_length",
    "context_window",
    "context_window_size",
    "n_ctx",
    "max_input_tokens",
    "max_seq_len",
    "max_position_embeddings",
    "model_max_length",
)
_TRAIN_CONTEXT_FIELDS = ("n_ctx_train", "context_length_train")
# 一层嵌套：有的放 meta.*，有的放 details.* / info.* / payload.*
_CONTEXT_CONTAINERS = (
    "meta", "details", "info", "metadata", "properties", "payload",
    "architecture", "model",
)
_OUTPUT_FIELDS = (
    "max_output_tokens",
    "max_output_length",
    "max_completion_tokens",
    "max_output",
    "max_tokens_out",
)

# ``capabilities`` 是能力词表，不是模态；映射成模态后才有地方展示。
_CAPABILITY_MODALITY = {
    "vision": "image",
    "image": "image",
    "audio": "audio",
    "video": "video",
}
# 有些端点只用 ``type`` 表达同样的信息，实测取值：text_model / visual_model
# （其实是 T2V 视频生成）/ speech_model（TTS）/ image_generation——注意它们
# 描述的是**输出**，别当成「支持图片输入」。顺序敏感：具体词在前。
_TYPE_MODALITY = (
    (("visual_model", "t2v", "video_gen", "text2video"), ("text",), ("video",)),
    (("image_generation", "image_gen", "t2i"), ("text",), ("image",)),
    (("tts", "speech_model", "text2speech"), ("text",), ("audio",)),
    (("asr", "audio_input", "speech_input"), ("text", "audio"), ("text",)),
    (("embedding",), ("text",), ()),
    (("vision", "multimodal", "vlm", "visual"), ("text", "image"), ("text",)),
    (("text",), ("text",), ("text",)),
)
# 值得展示的能力（其余原样保留，最多 4 个，避免表格被塞满）。模态类能力
# （vision/audio/completion）不进这里——它们已经体现在输入/输出列了。
_FEATURE_KEYS = (
    "tools", "tool_calls", "function_calling", "parallel_tools", "json_mode",
    "structured_output", "reasoning", "thinking", "reasoning_effort",
    "preserve_reasoning", "web_search", "cache",
)
# 这些词是模态而不是能力，已经体现在输入/输出列里，不再重复进能力列。
_MODALITY_CAPS = frozenset({
    "vision", "audio", "video", "image", "completion", "embedding", "text",
})


class ModelProbeError(Exception):
    """探测失败。``code`` 供前端映射文案，``message`` 只用于日志。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def models_url_candidates(api_base: str) -> list[str]:
    """由 ``api_base`` 推出模型列表 URL 候选（已含 /v1 就不再补 /v1）。"""
    base = (api_base or "").strip().rstrip("/")
    if not base:
        return []
    if base.endswith("/v1"):
        return [f"{base}/models"]
    return [f"{base}/v1/models", f"{base}/models"]


def _root_url(api_base: str) -> str:
    """``https://host/v1`` → ``https://host``（原生端点如 /api/tags、/props 挂在根上）。"""
    base = (api_base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _request_json(url: str, api_key: str, timeout: float):
    """GET 一个 JSON 端点。返回 payload；HTTP/网络错误抛 ModelProbeError。

    ``User-Agent`` 必须显式设置：有网关（实测）对默认的
    ``Python-urllib/3.x`` 直接回 403。``Accept-Encoding: identity`` 是因为
    ``urllib`` 不会自己解压 gzip/br，否则 JSON 解析会莫名失败。
    """
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "Mozilla/5.0 (compatible; agents-gateway/1.0)",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        if exc.code in (401, 403):
            raise ModelProbeError("auth_failed", f"上游拒绝密钥：HTTP {exc.code}") from exc
        raise ModelProbeError(
            "upstream_error", f"上游 HTTP {exc.code}：{detail.strip()[:160] or exc.reason}",
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelProbeError("unreachable", f"连不上上游：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise ModelProbeError("unreachable", f"上游无响应：{exc}") from exc

    try:
        return json.loads(raw), status
    except json.JSONDecodeError as exc:
        head = raw.strip().splitlines()[0][:120] if raw.strip() else ""
        raise ModelProbeError(
            "invalid_response",
            f"{url} 返回的不是 JSON（可能是登录页/代理拦截页）：{head!r}",
        ) from exc


def _looks_like_auth(message: str) -> bool:
    lowered = (message or "").lower()
    if any(word in lowered for word in (
        "invalid token", "invalid api key", "unauthorized", "forbidden",
    )):
        return True
    mentions_key = any(word in lowered for word in (
        "token", "api key", "apikey", "key", "密钥",
    ))
    rejected = any(word in message for word in ("无效", "过期", "失效", "无权限", "无权")) or any(
        word in lowered for word in ("invalid", "expired", "revoked", "denied")
    )
    return mentions_key and rejected


def _payload_error(payload) -> tuple[str, str] | None:
    """有些网关用 HTTP 200 传错误：``{"code":401,"msg":"token 无效"}``、
    ``{"success":false,"message":...}``。不识别的话会被误报成「不是模型列表」。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("data") or payload.get("models"):
        return None
    code = payload.get("code")
    message = str(
        payload.get("message") or payload.get("msg") or payload.get("error") or "",
    ).strip()
    bad_code = isinstance(code, int) and code not in (0, 200)
    if not (payload.get("success") is False or bad_code or message):
        return None
    if (bad_code and code in (401, 403)) or _looks_like_auth(message):
        return "auth_failed", message or "上游拒绝密钥"
    return "upstream_error", message or "上游未提供模型列表"


def _scan_number(item: dict, fields: tuple[str, ...], containers=()):
    """在 item（及其一层嵌套）里找第一个能解析成 token 数的字段。

    返回 ``(值, 命中字段路径)``；找不到返回 ``(0, "")``。K/M 记法（``128K``）
    复用 ``runtime.models._parse_token_count``，与保存配置时的解析一致。
    """
    for name in fields:
        value = _parse_token_count(item.get(name))
        if value:
            return value, name
    for container in containers:
        sub = item.get(container)
        if not isinstance(sub, dict):
            continue
        for name in fields:
            value = _parse_token_count(sub.get(name))
            if value:
                return value, f"{container}.{name}"
    return 0, ""


def _modalities_from_type(value: str) -> tuple[list[str], list[str]]:
    token = (value or "").lower()
    for needles, inputs, outputs in _TYPE_MODALITY:
        if any(n in token for n in needles):
            return list(inputs), list(outputs)
    return [], []


def _modalities(item: dict) -> tuple[list[str], list[str]]:
    """输入/输出模态。优先级：architecture → 顶层 → capabilities → type。"""
    arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    inputs = [str(x).lower() for x in (arch.get("input_modalities") or [])]
    outputs = [str(x).lower() for x in (arch.get("output_modalities") or [])]
    if not inputs:
        inputs = [str(x).lower() for x in (item.get("input_modalities") or [])]
    if not outputs:
        outputs = [str(x).lower() for x in (item.get("output_modalities") or [])]
    if not inputs:
        single = item.get("modalities") or arch.get("modalities") or []
        inputs = [str(x).lower() for x in single]

    caps = [str(x).lower() for x in (item.get("capabilities") or [])]
    if not inputs and caps:
        inputs = ["text"]
        for cap in caps:
            modality = _CAPABILITY_MODALITY.get(cap)
            if modality and modality not in inputs:
                inputs.append(modality)
        if not outputs:
            outputs = ["text"]  # 能聊天的模型，输出就是文本
    if not inputs:
        for holder in ("details", "meta"):
            sub = item.get(holder)
            if isinstance(sub, dict) and sub.get("capabilities"):
                return _modalities({**item, "capabilities": sub["capabilities"]})
    if not (inputs or outputs):
        for key in ("type", "model_type", "kind"):
            ti, to = _modalities_from_type(str(item.get(key) or ""))
            if ti or to:
                return ti, to
    return inputs, outputs


def _features(item: dict) -> list[str]:
    """值得在表格里露一面的能力（tools / reasoning / vision …）。

    ``tools`` 尤其有用：模型编辑页的「支持工具」勾选框可据此判断。
    字典形状（``api_capabilities`` 之类）只取已知能力键，未知键是
    实现细节，不值得展示；列表形状（``capabilities`` 之类）原样保留。
    """
    listed: list[str] = []
    known: list[str] = []
    for key in ("supported_features", "capabilities", "api_capabilities",
                "supported_parameters", "features"):
        value = item.get(key)
        if isinstance(value, dict):
            known.extend(str(k).lower() for k in value if str(k).lower() in _FEATURE_KEYS)
        elif isinstance(value, list):
            listed.extend(str(v).lower() for v in value)
    seen: list[str] = []
    for feature in known + listed:
        if feature in _FEATURE_KEYS and feature not in seen:
            seen.append(feature)
    for feature in listed:  # 未知但可能 informative 的排在后面；模态类不算能力
        if (feature not in seen and feature not in _MODALITY_CAPS
                and len(seen) < 4):
            seen.append(feature)
    return seen[:4]


def _human_size(value) -> str:
    """字节数 → ``15.3GB``。小于 1MB 的不显示（远端托管模型的 stub 条目只有
    几百字节，显示出来只会误导）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number < 1024 * 1024:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.0f}{units[index]}" if number >= 100 else f"{number:.1f}{units[index]}"


def _details_text(item: dict) -> str:
    """一行小字：参数量 / 量化 / 体积 / 并发槽 / 服务端版本——本地模型最关心这些。"""
    sub = item.get("details") if isinstance(item.get("details"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    parts: list[str] = []
    for key in ("parameter_size", "quantization_level", "quantization",
                "model_ftype", "family", "build_info"):
        for holder in (item, sub):
            value = str(holder.get(key) or "").strip()
            if value and "unknown" not in value.lower():
                parts.append(value)
    size = _human_size(item.get("size") or sub.get("size") or meta.get("size"))
    if size:
        parts.append(size)
    slots = item.get("total_slots") or sub.get("total_slots")
    if isinstance(slots, (int, float)) and slots > 0:
        parts.append(f"{int(slots)} slot")
    return " · ".join(dict.fromkeys(parts))


def _join_details(*texts: str) -> str:
    """合并来自列表和 ``/props`` 两处的小字，去重保序。"""
    parts: list[str] = []
    for text in texts:
        parts.extend(part for part in str(text or "").split(" · ") if part)
    return " · ".join(dict.fromkeys(parts))


def _effort_levels(item: dict) -> list[str]:
    """思考力度档位（``effort.supported_levels``）。

    对 agent 有用：``generate_params`` 里的 reasoning effort 只能取这些值。
    """
    effort = item.get("effort")
    if isinstance(effort, dict):
        levels = effort.get("supported_levels") or effort.get("levels") or []
    elif isinstance(effort, list):
        levels = effort
    else:
        levels = []
    if not isinstance(levels, list):
        return []
    return [str(level).lower() for level in levels if str(level).strip()]


def _status_text(item: dict) -> str:
    status = item.get("status")
    if isinstance(status, dict):
        return str(status.get("value") or status.get("state") or "")
    return str(status or "")


def extract_max_context(item: dict) -> tuple[int, str]:
    """单个模型条目的窗口，返回 ``(值, 命中字段路径)``；取不到返回 ``(0, "")``。

    ``n_ctx_train`` 之类「训练窗口」只会在运行时窗口缺失时才用，并在路径上
    标 ``(train)``——两者不是一回事（``--ctx-size`` 会把运行时窗口改小）。
    """
    value, source = _scan_number(item, _CONTEXT_FIELDS, _CONTEXT_CONTAINERS)
    if value:
        return value, source
    value, field = _scan_number(item, _TRAIN_CONTEXT_FIELDS, _CONTEXT_CONTAINERS)
    return (value, f"{field}(train)") if value else (0, "")


def _item_name(item: dict) -> str:
    """模型条目的名字。``name`` / ``model`` 也要认——各家字段不统一。"""
    return str(item.get("id") or item.get("name") or item.get("model_name")
               or item.get("model") or "")


def _normalize_model_item(item: dict) -> dict:
    """单个模型条目 → 展示行。字段名各家不同，逐个候选试。"""
    name = _item_name(item)
    max_context, source = extract_max_context(item)
    max_output, _ = _scan_number(item, _OUTPUT_FIELDS, _CONTEXT_CONTAINERS)
    inputs, outputs = _modalities(item)
    return {
        "model_name": name,
        "max_context": max_context,
        "max_context_source": source,
        "max_output": max_output,
        "input_modalities": inputs,
        "output_modalities": outputs,
        "features": _features(item),
        "effort_levels": _effort_levels(item),
        "details": _details_text(item),
        "status": _status_text(item),
        "owned_by": str(item.get("owned_by") or ""),
    }


def parse_models_payload(payload) -> list[dict]:
    """``{"data":[...]}`` / ``{"models":[...]}`` → 归一化列表。"""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or []
    else:
        items = []
    if not isinstance(items, list):
        raise ModelProbeError("invalid_response", "模型列表字段不是数组")
    return [
        _normalize_model_item(item)
        for item in items
        if isinstance(item, dict) and _item_name(item)
    ]


def _has_list_field(payload) -> bool:
    """payload 里是否真的有 ``data``/``models`` 字段（哪怕是空数组）。"""
    if isinstance(payload, list):
        return True
    return isinstance(payload, dict) and (
        "data" in payload or "models" in payload
    )


def _same_model(left: str, right: str) -> bool:
    a = (left or "").strip().lower().rsplit("/", 1)[-1]
    b = (right or "").strip().lower().rsplit("/", 1)[-1]
    return bool(a and b) and (a == b or a in b or b in a)


def _merge_native_tags(rows: list[dict], api_base: str, api_key: str,
                       timeout: float) -> None:
    """有些服务的 ``/v1/models`` 只有 id；窗口、模态、能力都在原生 ``/api/tags``。"""
    root = _root_url(api_base)
    if not root or not rows:
        return
    try:
        payload, _ = _request_json(f"{root}/api/tags", api_key, timeout)
    except ModelProbeError:
        return  # 这个服务没有 /api/tags（或不可用）——保持已有结果
    tags = []
    if isinstance(payload, dict):
        raw_tags = payload.get("models") or payload.get("tags") or []
        if isinstance(raw_tags, list):
            tags = raw_tags
    by_name: dict[str, dict] = {}
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        for key in ("model", "name", "id"):
            value = str(tag.get(key) or "")
            if value:
                by_name.setdefault(value.lower(), tag)
                by_name.setdefault(value.lower().split(":")[0], tag)
    for row in rows:
        tag = by_name.get(row["model_name"].lower())
        if tag is None:
            continue
        details = tag.get("details") if isinstance(tag.get("details"), dict) else {}
        if not row["max_context"]:
            value = _parse_token_count(
                details.get("context_length") or tag.get("context_length"),
            )
            if value:
                row["max_context"] = value
                row["max_context_source"] = "api.tags.details.context_length"
        caps = [str(c).lower() for c in (tag.get("capabilities") or [])]
        if caps:
            inputs, outputs = _modalities({"capabilities": caps})
            if not row["input_modalities"]:
                row["input_modalities"] = inputs
            if not row["output_modalities"]:
                row["output_modalities"] = outputs
            if not row["features"]:
                row["features"] = _features({"capabilities": caps})
        row["details"] = _join_details(
            row["details"],
            _details_text({"details": details, "size": tag.get("size")}),
        )


# ``chat_template_caps`` 直接说明这个模型能不能喂工具——正好对应
# 模型编辑页的「支持工具」勾选框。
_CAPS_FEATURES = (
    ("supports_tools", "tools"),
    ("supports_parallel_tool_calls", "parallel_tools"),
    ("supports_reasoning_effort", "reasoning_effort"),
    ("supports_preserve_reasoning", "preserve_reasoning"),
)
_CAPS_MODALITIES = (("vision", "image"), ("audio", "audio"), ("video", "video"))


def _props_targets(rows: list[dict], alias: str) -> list[dict]:
    """``/props`` 是实例级的（一个 slot 一个模型），只有能确定行↔模型对应
    关系时才套用：只有一个模型时直接套，多个模型时按 ``model_alias`` 匹配。"""
    if len(rows) == 1 or not alias:
        return rows if len(rows) == 1 else []
    return [row for row in rows if _same_model(alias, row["model_name"])]


def _merge_instance_props(rows: list[dict], api_base: str, api_key: str,
                          timeout: float) -> None:
    """本地推理服务的 ``/props`` 往往是信息量最大的地方：

        default_generation_settings.n_ctx   运行时窗口（列表缺时兜底）
        modalities{vision,audio,video}      输入模态——列表里根本没有
        chat_template_caps.supports_tools   能不能调工具
        model_ftype / build_info / size     量化、服务端版本
        total_slots / is_sleeping           并发槽、是否已 sleep

    没有 ``/props`` 的端点会 404，直接返回，不影响已拿到的结果。
    """
    root = _root_url(api_base)
    if not root or not rows:
        return
    try:
        payload, _ = _request_json(f"{root}/props", api_key, timeout)
    except ModelProbeError:
        return
    if not isinstance(payload, dict):
        return
    alias = str(payload.get("model_alias") or payload.get("model_path") or "")
    targets = _props_targets(rows, alias)
    if not targets:
        return

    value, field = _scan_number(payload, ("n_ctx",))
    if not value:
        generation = payload.get("default_generation_settings")
        if isinstance(generation, dict):
            value, field = _scan_number(generation, ("n_ctx",))
            if value:
                field = f"default_generation_settings.{field}"
    if value:
        for row in targets:
            if not row["max_context"]:
                row["max_context"] = value
                row["max_context_source"] = f"props.{field}"

    modalities = payload.get("modalities")
    if isinstance(modalities, dict):
        inputs = ["text"]
        for flag, modality in _CAPS_MODALITIES:
            if modalities.get(flag) and modality not in inputs:
                inputs.append(modality)
        for row in targets:
            if not row["input_modalities"]:
                row["input_modalities"] = inputs
            if not row["output_modalities"]:
                row["output_modalities"] = ["text"]

    caps = payload.get("chat_template_caps")
    if isinstance(caps, dict):
        features = [label for key, label in _CAPS_FEATURES if caps.get(key)]
        for row in targets:
            if not row["features"]:
                row["features"] = features

    extras = {
        "model_ftype": payload.get("model_ftype"),
        "build_info": payload.get("build_info"),
        "total_slots": payload.get("total_slots"),
    }
    for row in targets:
        if not row["details"]:
            row["details"] = _details_text(extras)
        if not row["status"] and payload.get("is_sleeping"):
            row["status"] = "sleeping"


def _merge_health_context(rows: list[dict], api_base: str, api_key: str,
                          timeout: float) -> None:
    """最后一道兜底：部分服务的 ``/health`` 里有 ``max_context``。"""
    root = _root_url(api_base)
    targets = [row for row in rows if not row["max_context"]]
    if not root or not targets:
        return
    try:
        payload, _ = _request_json(f"{root}/health", api_key, timeout)
    except ModelProbeError:
        return
    if not isinstance(payload, dict):
        return
    value, field = _scan_number(payload, ("max_context", "n_ctx"))
    if not value:
        return
    alias = str(payload.get("model_alias") or payload.get("model_path") or "")
    for row in _props_targets(rows, alias):
        if not row["max_context"]:
            row["max_context"] = value
            row["max_context_source"] = f"health.{field}"


def probe_models(api_base: str, api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """探测上游模型列表，返回 ``{"models_url", "count", "models": [...]}``。

    列表本身往往只有 id，所以再各问一次该服务的原生端点补齐：``/api/tags``
    （窗口 + capabilities）、``/props``（模态 + 工具能力 + 量化/版本/并发槽）、
    以及 ``/health``（窗口兜底）。都是 best-effort：404/超时就直接跳过，不影响
    已拿到的结果。每个网络操作最多 ``timeout`` 秒（默认 10s——models.json 里
    确实有已经下线的端点）。

    Raises:
        ModelProbeError: ``invalid_api_base`` / ``missing_api_base`` /
            ``auth_failed`` / ``upstream_error`` / ``invalid_response`` /
            ``unreachable``。
    """
    candidates = models_url_candidates(api_base)
    if not candidates:
        raise ModelProbeError("invalid_api_base", "api_base 为空或无法解析")

    last_error: ModelProbeError | None = None
    for url in candidates:
        try:
            payload, _ = _request_json(url, api_key, timeout)
        except ModelProbeError as exc:
            last_error = exc
            continue
        wrapped_error = _payload_error(payload)
        if wrapped_error:
            raise ModelProbeError(*wrapped_error)
        try:
            models = parse_models_payload(payload)
        except ModelProbeError as exc:
            last_error = exc
            continue
        if not models and not _has_list_field(payload):
            # 连 data/models 字段都没有：这不是模型列表接口（登录页、网关
            # 拦截页之类），和「有列表但为空」要区分开。
            last_error = ModelProbeError(
                "invalid_response", f"{url} 未提供模型列表字段",
            )
            continue
        _merge_native_tags(models, api_base, api_key, timeout)
        _merge_instance_props(models, api_base, api_key, timeout)
        if any(not row["max_context"] for row in models):
            _merge_health_context(models, api_base, api_key, timeout)
        return {"models_url": url, "count": len(models), "models": models}

    if last_error is not None:
        raise last_error
    raise ModelProbeError("unreachable", f"无法访问 {candidates[0]}")

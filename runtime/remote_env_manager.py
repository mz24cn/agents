"""RemoteEnvManager — 远程环境管理模块。

从当前（母）环境的视角管理其派生出的子环境，读写 DATA_DIR/remote_envs.json。
每条记录包含环境地址（URL）以及最近一次状态检查得到的版本 / 推理状态快照
（frontend_build / backend_build / last_config / inference_active 等）；
两类环境的在线状态统一用 online 布尔持久化：直连环境记录最近一次
状态检查/更新的结果（前端回写），隧道环境由隧道 attach/detach 事件驱动
写入（last_seen 记录最后一次被观察到在线的时间）。

更新采用**推送**模型：母环境把增量 tar 直接 POST 到子环境的
``/v1/setup?op=push``，子环境无需知道（更无需能访问）母环境地址——
母环境可以匿名、或只能通过 localhost 访问。

remote_envs.json 只是本环境的本地管理元数据：与 env.json 一样，它不参与
在线更新（build_delta_tar 只打包指定的配置文件）和自解压导出
（build_setup_script 的 _write_runtime_configs 从不写入该文件），也不会被
同步到其他环境。

文件内容是纯 JSON 数组；早期版本写出的 ``{"envs": [...], "source_url": ...}``
对象格式仍然可读（source_url 字段被忽略）。

零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading

import urllib.parse

from runtime.common import now_iso

logger = logging.getLogger("runtime.remote_env_manager")

# /v1/setup 各请求自行决定 / 传递的查询参数。归一化环境 URL 时剔除它们，
# 其余查询参数（如 token=...）原样保留，供后续 hello / update 请求鉴权。
MANAGED_SETUP_QUERY_KEYS = frozenset({
    "op", "source", "frontend_build", "backend_build", "last_config",
})

# 快照字段：从 /v1/setup?op=hello 响应中取值并持久化到 remote_envs.json。
SNAPSHOT_VERSION_KEYS = ("frontend_build", "backend_build", "last_config", "server_instance_id")
SNAPSHOT_FLAG_KEYS = ("inference_active", "api_inference_active", "session_inference_active")
# 应用 / 平台信息字段：同样取自 op=hello 响应（app_title / app_logo / arch / os），
# 在远程环境列表中展示环境的应用标识与运行平台。
# 工作区路径（workspace）：远程执行时供母端 UI 展示子端工作区。
SNAPSHOT_TEXT_KEYS = ("app_title", "app_logo", "arch", "os", "workspace")


def normalize_setup_url(url: str) -> dict:
    """归一化一个 SETUP_SOURCE 风格的环境 URL。

    接受四种形态（与构建版本页的 SETUP_SOURCE 输入框相同）：

    * ``http://172.28.70.13:7988/``
    * ``http://172.28.70.13:7988/v1/setup``
    * ``http://172.28.70.13:7988/v1/setup?token=...``
    * ``https://user:pass@proxy.example.com:27988/`` —— 地址里带 userinfo 凭据。
      凭据被拆到 ``username`` / ``password``，``id`` / ``netloc`` 只留
      host:port：``http.client`` 无法用带 ``user:pass@`` 的 netloc 建连
      （会把整串当主机名去解析），代理与服务端要的是 ``Authorization:
      Basic``，所以拨号前统一换成 header（见 :func:`attach_url_credentials`）。
      这个凭据可能是环境前面那道 Basic 认证反向代理的，也可能是母环境自身的
      访问密码（``AuthManager.verify_password`` 接受它）。

    Returns:
        dict，包含：

        * ``id`` — 稳定标识 ``scheme://host:port``，作为环境记录的去重键；
          **不含凭据**，同一环境带不带凭据都归并成同一条记录；
        * ``scheme`` / ``netloc`` / ``path`` — 归一化后的地址各部分，
          netloc 不含凭据，path 固定为指向 ``/v1/setup`` 的路径；
        * ``query`` — 需要保留的查询参数 (key, value) 列表（剔除 op 等
          由具体请求决定的参数）；
        * ``username`` / ``password`` — URL 携带的凭据（已百分号解码），
          无凭据时为空串；
        * ``userinfo`` — 重新百分号编码后的 ``user:pass@``（拼回规范 URL
          用，凭据要在 remote_envs.json 里留得住）。

    Raises:
        ValueError: 不是合法的 http(s) URL 时。
    """
    raw = str(url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL 必须是 http(s) 地址，例如 http://host:7988/v1/setup")
    try:
        parsed.port  # 非数字端口在这里抛出 ValueError
    except ValueError:
        # 报错里回显地址时遮掉口令：这条消息会进隧道状态、也进界面
        raise ValueError(f"URL 端口无效: {mask_url_credentials(raw)}") from None
    # hostname 已经剥掉 userinfo 并转小写（主机名大小写不敏感，去重更稳）
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                      # IPv6 字面量补回方括号
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    if not netloc:
        raise ValueError(f"URL 缺少主机名: {mask_url_credentials(raw)}")
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    if "/v1/setup" in parsed.path:
        path = parsed.path[: parsed.path.index("/v1/setup") + len("/v1/setup")]
    else:
        path = "/v1/setup"
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in MANAGED_SETUP_QUERY_KEYS
    ]
    return {
        "id": f"{parsed.scheme}://{netloc}",
        "scheme": parsed.scheme,
        "netloc": netloc,
        "path": path,
        "query": query,
        "username": username,
        "password": password,
        "userinfo": _encode_userinfo(username, password),
    }


def _encode_userinfo(username: str, password: str) -> str:
    """``user:pass@``（百分号编码，无凭据时为空串）。"""
    if not username and not password:
        return ""
    userinfo = urllib.parse.quote(str(username), safe="")
    if password:
        userinfo += ":" + urllib.parse.quote(str(password), safe="")
    return userinfo + "@"


def basic_auth_header(username: str, password: str) -> str:
    """``Authorization`` 头的 ``Basic`` 值；无凭据时为空串。"""
    if not username and not password:
        return ""
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# 一次请求只有一个 ``Authorization`` 头。环境 URL 里同时写了凭据（环境前面那道
# 要密码的网关）与 ``?token=``（环境自身的授权）时，凭据占 Authorization，token
# 走这个头 —— 母/子两端都在我们手里，加一个专用头比把 token 塞进 URL 干净
# （URL 会被中间代理的访问日志记下来）。
SECONDARY_TOKEN_HEADER = "X-Agent-Service-Token"


def auth_headers(token: str = "", basic: str = "") -> dict:
    """一份 URL 里可能带着两种凭据：返回要发的鉴权头。

    * 只有 token → ``Authorization: Bearer <token>``；
    * 只有凭据 → ``Authorization: Basic …``；
    * 两者都有 → 凭据占 ``Authorization``（它是过网关的那一把），token 放
      ``X-Agent-Service-Token``（对端在接受 Bearer 的地方同样接受它，见
      ``runtime.handler_base``）。
    """
    token = str(token or "").strip()
    basic = str(basic or "").strip()
    if basic:
        headers = {"Authorization": basic}
        if token:
            headers[SECONDARY_TOKEN_HEADER] = token
        return headers
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def url_credentials(url: str) -> tuple[str, str, str]:
    """环境 URL 里的凭据 → ``(username, password, Basic header)``。

    URL 非法或不含凭据时返回 ``("", "", "")``：调用方大多只是「有就带上」，
    不值得为解析失败再处理一次异常（合法性已在登记处校验）。
    """
    try:
        normalized = normalize_setup_url(url)
    except ValueError:
        return "", "", ""
    username, password = normalized["username"], normalized["password"]
    return username, password, basic_auth_header(username, password)


def strip_url_credentials(url: str) -> str:
    """去掉 URL 的 ``user:pass@`` 段（剩下的部分才能直接建连）。"""
    raw = str(url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if "@" not in parsed.netloc:
        return raw
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc.rsplit("@", 1)[-1],
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def attach_url_credentials(url: str, headers: dict | None = None) -> tuple[str, dict]:
    """把 URL 里的凭据搬进 ``Authorization: Basic``：``(可建连 URL, headers)``。

    每个直连环境 / 更新源的出口都应先过一遍这个函数：带 userinfo 的 netloc
    会直接被当成主机名去 DNS 解析（连不上，报错还看不出原因）。已带有
    ``Authorization`` 的 *headers* 原样保留（调用方明确挑好的凭据优先）。
    """
    merged = dict(headers or {})
    _user, _password, auth = url_credentials(url)
    if auth and not any(str(k).lower() == "authorization" for k in merged):
        merged["Authorization"] = auth
    return strip_url_credentials(url), merged


def mask_url_credentials(url: str) -> str:
    """把 URL 里的密码换成 ``***``（日志 / 界面展示用）。"""
    raw = str(url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if "@" not in parsed.netloc:
        return raw
    userinfo = parsed.netloc.rsplit("@", 1)[0]
    head, _sep, password = userinfo.partition(":")
    shown = f"{head}:***" if password else userinfo
    return urllib.parse.urlunsplit((
        parsed.scheme,
        f"{shown}@{parsed.netloc.rsplit('@', 1)[-1]}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def canonical_setup_url(url: str) -> str:
    """返回归一化后的环境地址（base + 保留的查询参数，不含 op 等受管理参数）。

    userinfo 凭据保留在 URL 里：remote_envs.json 的记录要能重新拨号，
    实际建连时由 :func:`attach_url_credentials` 搬进 header。
    """
    normalized = normalize_setup_url(url)
    url_str = (
        f"{normalized['scheme']}://{normalized['userinfo']}{normalized['netloc']}"
        f"{normalized['path']}"
    )
    if normalized["query"]:
        url_str += "?" + urllib.parse.urlencode(normalized["query"])
    return url_str


def build_setup_request_url(url: str, params: dict) -> str:
    """基于环境 URL 构造带指定查询参数的 /v1/setup 请求 URL。

    保留原 URL 中的鉴权类查询参数（如 token）与 userinfo 凭据（凭据由
    :func:`attach_url_credentials` 在拨号前搬进 header），剔除由 *params*
    重新决定的受管理参数（op / source / 三个版本基线），与后端 op=update
    组装远端 delta 地址的规则一致。
    """
    normalized = normalize_setup_url(url)
    query = list(normalized["query"])
    query.extend((key, str(value)) for key, value in params.items())
    return urllib.parse.urlunsplit((
        normalized["scheme"],
        normalized["userinfo"] + normalized["netloc"],
        normalized["path"],
        urllib.parse.urlencode(query),
        "",
    ))


def snapshot_from_hello(data: dict | None) -> dict:
    """从 ``/v1/setup?op=hello`` 响应中提取可展示的快照字段。"""
    data = data if isinstance(data, dict) else {}
    snapshot: dict = {}
    for key in SNAPSHOT_VERSION_KEYS:
        snapshot[key] = str(data.get(key, "") or "")
    for key in SNAPSHOT_FLAG_KEYS:
        snapshot[key] = bool(data.get(key))
    for key in SNAPSHOT_TEXT_KEYS:
        snapshot[key] = str(data.get(key, "") or "")
    return snapshot


class RemoteEnvManager:
    """管理 remote_envs.json 的读写。

    文件内容是一个 JSON 数组::

        [
          {
            "id": "http://172.28.70.13:7988",
            "url": "http://172.28.70.13:7988/v1/setup?token=...",
            "created_at": "2025-09-08T15:30:00",
            "frontend_build": "250908_143000",
            "backend_build": "250908_143000",
            "last_config": "250908_143000",
            "inference_active": false,
            "api_inference_active": false,
            "session_inference_active": false,
            "server_instance_id": "...",
            "app_title": "...",
            "app_logo": "...",
            "arch": "x86_64",
            "os": "linux",
            "online": true,
            "checked_at": "2025-09-08T15:30:00"
          }
        ]

    早期版本写出的 ``{"envs": [...], "source_url": ...}`` 对象格式仍然可读
    （source_url 字段被忽略）。
    所有写操作都在同一把锁内完成读-改-写，跨线程（HTTP 请求线程）安全。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        # 工具代理论缓存：env_id -> (url, RemoteToolProxy)。代理无每请求状态
        # （地址 + 传输 + 工具清单 TTL 缓存），同一环境的全部调用方（推理、
        # talk_to/delegate、管理调用）共享同一实例，避免每轮推理重新拉取
        # 子端 /v1/tools。
        self._proxy_lock = threading.Lock()
        self._proxies: dict[str, tuple[str, object]] = {}

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def read(self) -> list[dict]:
        """读取全部环境记录；文件不存在时返回空列表。"""
        with self._lock:
            return self._read_locked()

    def get(self, env_id: str) -> dict:
        """按 id 返回单条环境记录（副本）；不存在时抛 KeyError。"""
        with self._lock:
            envs = self._read_locked()
        record = next((e for e in envs if e.get("id") == env_id), None)
        if record is None:
            raise KeyError(env_id)
        return dict(record)

    def upsert(
        self,
        url: str,
        snapshot: dict | None = None,
        online: bool | None = None,
    ) -> list[dict]:
        """新增或更新一条环境记录（按 ``id`` 去重），返回完整列表。

        ``snapshot`` 为前端查询目标环境 ``op=hello`` 的结果；提供时立即
        落盘为版本快照。``online`` 为最近一次状态检查/更新时的可达性
        （前端回写）；提供时落盘为在线状态。
        """
        normalized = normalize_setup_url(url)  # 非法 URL 在这里抛 ValueError
        canonical = canonical_setup_url(url)
        with self._lock:
            envs = self._read_locked()
            record = next((e for e in envs if e.get("id") == normalized["id"]), None)
            if record is None:
                record = {
                    "id": normalized["id"],
                    "url": canonical,
                    "created_at": now_iso(),
                }
                envs.append(record)
            else:
                record["url"] = canonical
            self._apply_online(record, online)
            self._apply_snapshot(record, snapshot)
            self._write_locked(envs)
            return envs

    def update_snapshot(
        self,
        env_id: str,
        snapshot: dict | None = None,
        online: bool | None = None,
    ) -> list[dict]:
        """更新指定环境的版本快照与在线状态（刷新 / 更新完成后由前端回写）。

        ``snapshot`` 为 None / 空时保留已有快照字段；``online`` 为 None 时
        保留已有在线状态；调用方至少提供一项。返回完整列表。
        """
        with self._lock:
            envs = self._read_locked()
            record = next((e for e in envs if e.get("id") == env_id), None)
            if record is None:
                raise KeyError(env_id)
            self._apply_online(record, online)
            self._apply_snapshot(record, snapshot)
            self._write_locked(envs)
            return envs

    def upsert_tunnel(
        self,
        tunnel_id: str,
        snapshot: dict | None = None,
        url_hint: str = "",
    ) -> list[dict]:
        """新增或更新一条**隧道**环境记录（子环境注册到母环境）。

        记录按 ``tunnel:<16hex>`` id 去重；``url`` 字段保存子环境自报的
        直连地址（可空，仅参考——隧道模式不依赖它可达）。
        """
        from runtime.tunnel_protocol import tunnel_env_id
        env_id = tunnel_env_id(tunnel_id)
        with self._lock:
            envs = self._read_locked()
            record = next((e for e in envs if e.get("id") == env_id), None)
            if record is None:
                record = {
                    "id": env_id,
                    "url": url_hint or "",
                    "transport": "ws-tunnel",
                    "tunnel_id": tunnel_id,
                    "created_at": now_iso(),
                    "online": False,
                    "last_seen": "",
                }
                envs.append(record)
            else:
                record["transport"] = "ws-tunnel"
                record["tunnel_id"] = tunnel_id
                if url_hint:
                    record["url"] = url_hint
            self._apply_snapshot(record, snapshot)
            self._write_locked(envs)
            return envs

    def update_tunnel_status(self, env_id: str, online: bool) -> None:
        """更新隧道环境的在线状态（隧道连接建立/断开时调用）。

        与直连环境一样使用 online 布尔；``last_seen`` 记录最后一次被观察到
        在线的时间，仅在上线时刷新（断开不刷新，避免变成"最后事件时间"）。
        """
        with self._lock:
            envs = self._read_locked()
            record = next((e for e in envs if e.get("id") == env_id), None)
            if record is None:
                return
            record["online"] = bool(online)
            if online:
                record["last_seen"] = now_iso()
            self._write_locked(envs)

    def remove(self, env_id: str) -> list[dict]:
        """删除一条环境记录，返回剩余列表。记录不存在时抛 KeyError。"""
        with self._lock:
            envs = self._read_locked()
            kept = [e for e in envs if e.get("id") != env_id]
            if len(kept) == len(envs):
                raise KeyError(env_id)
            self._write_locked(kept)
        with self._proxy_lock:
            self._proxies.pop(env_id, None)
        return kept

    # ------------------------------------------------------------------
    # 工具代理（按环境共享，见 get_proxy）
    # ------------------------------------------------------------------

    def get_proxy(self, env_id: str, tunnel_manager=None):
        """返回该环境的 RemoteToolProxy（按 env_id 缓存，跨请求共享）。

        环境 URL 变化时重建。记录不存在时抛 KeyError。
        """
        from runtime.remote_tool_proxy import RemoteToolProxy
        record = self.get(env_id)
        url = str(record.get("url") or "")
        with self._proxy_lock:
            cached = self._proxies.get(env_id)
            if cached is not None and cached[0] == url:
                return cached[1]
            proxy = RemoteToolProxy(record, tunnel_manager=tunnel_manager)
            self._proxies[env_id] = (url, proxy)
            return proxy

    def invalidate_tools_cache(self, env_id: str) -> None:
        """失效该环境代理的工具清单/技能正文缓存（push-update、隧道重连后调用）。"""
        with self._proxy_lock:
            cached = self._proxies.get(env_id)
        if cached is not None:
            cached[1].invalidate_tools()

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_snapshot(record: dict, snapshot: dict | None) -> None:
        if not isinstance(snapshot, dict) or not snapshot:
            return
        for key in SNAPSHOT_VERSION_KEYS:
            record[key] = str(snapshot.get(key, "") or "")
        for key in SNAPSHOT_FLAG_KEYS:
            record[key] = bool(snapshot.get(key))
        for key in SNAPSHOT_TEXT_KEYS:
            record[key] = str(snapshot.get(key, "") or "")
        record["checked_at"] = now_iso()

    @staticmethod
    def _apply_online(record: dict, online: bool | None) -> None:
        """持久化普通（直连）环境的在线状态。

        隧道环境的在线状态由隧道 attach/detach 事件写入 status / last_seen
        （见 update_tunnel_status），不走这里。
        """
        if online is None:
            return
        record["online"] = bool(online)

    def _read_locked(self) -> list[dict]:
        """读取文件；兼容早期版本写出的 ``{"envs": [...]}`` 对象格式。"""
        if not os.path.isfile(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            # 该文件只是本地管理元数据；损坏时告警并按空列表处理，
            # 不阻塞服务启动或页面加载。
            logger.warning("读取 remote_envs.json 失败，按空列表处理: %s", exc)
            return []
        if isinstance(data, dict):
            raw_envs = data.get("envs", [])
        elif isinstance(data, list):
            raw_envs = data
        else:
            logger.warning("remote_envs.json 内容不是 JSON 对象或数组，按空列表处理")
            return []
        if not isinstance(raw_envs, list):
            logger.warning("remote_envs.json 的 envs 字段不是数组，按空列表处理")
            return []
        records = []
        for e in raw_envs:
            if not isinstance(e, dict) or not e.get("id"):
                continue
            # 早期版本隧道记录用 status 字段记录在线状态；统一为 online
            # 布尔（与直连环境同一字段），下次写入时自然落盘新格式。
            if "status" in e:
                e.setdefault("online", e.get("status") == "online")
                e.pop("status", None)
            e.setdefault("online", False)
            records.append(e)
        return records

    def _write_locked(self, envs: list[dict]) -> None:
        from runtime.common import atomic_write_text
        content = json.dumps(envs, ensure_ascii=False, indent=2)
        atomic_write_text(self._path, content)

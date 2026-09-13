"""RemoteToolProxy — 把子环境（child）的工具暴露给母环境的推理循环。

当一个会话绑定到远程环境时，该会话使用的全部工具都来自子环境：工具 ID /
名称保持子端原样（``exec_shell``、``write_file`` …），与本地执行完全一致，
前端紧凑显示按原名改写。远程工具**不注册进母端全局 ToolRegistry**（远程与
本地工具从不在同一会话混用，无 ID 冲突）：每次推理请求由代理构建
ToolConfig 条目（原名 + 转发 callable），经 ``InferenceRequest.tools`` 直传
推理循环与子代理（group chat / talk_to / delegate）。

每次工具调用被转发到子环境的 ``POST /v1/tools/call``，并携带母端
``session_id`` 与用户消息时间戳，使子端的文件 journal、终端、delegate 子会话
都挂在同一个 session id 下。

设计要点：

* **不做工具过滤**：子环境由母环境 setup 包安装，通常保留母环境的模型、
  工具与 AI 代理配置；skill / delegate / talk_to / exec_cli / undo / MCP
  工具全部代理过去，由子端执行。
* **skill 渐进式披露**：子端 ``/v1/tools/call`` 无法执行 skill（skill 是
  披露型条目），因此 skill 条目保持 ``tool_type="skill"``，由母端推理循环
  触发披露；SKILL.md 正文通过子端 ``GET /v1/tools/skill/{tool_id}``
  获取（带缓存）。
* 零第三方依赖，仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from runtime.models import ToolConfig

logger = logging.getLogger("runtime.remote_tool_proxy")

# 工具清单 TTL 缓存（秒）：子端工具变化后，下一次推理在 TTL 过期时自动刷新。
TOOL_LIST_TTL_SECONDS = 30.0
# 技能正文缓存 TTL（秒）：SKILL.md 在一次会话内不会变。
SKILL_BODY_TTL_SECONDS = 300.0

# 管理/短请求（工具清单、技能正文、revoke 等）的网络层超时（秒）。
_HTTP_TIMEOUT_SECONDS = 180.0
# 工具调用转发在拿不到工具级有效超时时的兜底网络超时（秒）：TOOL_EXEC_TIMEOUT
# 被禁用（None）或非推理路径调用时不会无限阻塞，等待子端自身超时/隧道判死。
_TOOL_CALL_FALLBACK_TIMEOUT = 3600.0


class RemoteToolProxy:
    """单个子环境的工具代理。

    Args:
        env_record: remote_envs.json 中的一条环境记录，至少包含
            ``id``（scheme://netloc 或 tunnel: 记录 id）与 ``url``
            （canonical setup URL，可能带 ``?token=...``）。
        tunnel_manager: 隧道传输（transport == "ws-tunnel"）时的
            母端 TunnelManager；直连环境可为 None。
    """

    def __init__(self, env_record: dict, tunnel_manager: Optional[object] = None) -> None:
        self.env_id = str(env_record.get("id") or "")
        self.env_url = str(env_record.get("url") or self.env_id)
        # Tunnel transport: the env record's transport is "ws-tunnel" and the
        # parent's TunnelManager carries every request over the child's
        # reverse tunnel (the child may be behind NAT / unreachable directly).
        self.is_tunnel = str(env_record.get("transport") or "") == "ws-tunnel"
        self.tunnel_manager = tunnel_manager

        if self.is_tunnel:
            # No direct URL is used; _build_url returns the bare child path.
            self.base = ""
            self.token = ""
        else:
            parsed = urllib.parse.urlsplit(self.env_url)
            # base = scheme://netloc + 部署前缀（/v1/setup 之前的路径）
            path = parsed.path or ""
            marker = "/v1/setup"
            idx = path.find(marker)
            prefix = path[:idx] if idx != -1 else path
            if prefix and not prefix.endswith("/"):
                prefix += "/"
            self.base = f"{parsed.scheme}://{parsed.netloc}{prefix}".rstrip("/")
            # token 保留在登记的 URL 查询参数中（st_/as_）
            self.token = ""
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                if key == "token":
                    self.token = value
                    break

        self._lock = threading.Lock()
        self._tools_cache: Optional[list[dict]] = None
        self._tools_fetched_at: float = 0.0
        # skill 正文缓存：子端 tool_id -> (fetched_at, body, skill_dir)
        self._skill_cache: dict[str, tuple[float, Optional[str], Optional[str]]] = {}

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _build_url(self, path: str, extra_query: Optional[list[tuple[str, str]]] = None) -> str:
        url = f"{self.base}{path}"
        query = []
        if self.token:
            query.append(("token", self.token))
        if extra_query:
            query.extend(extra_query)
        if query:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(query)
        return url

    def _http_request(self, url: str, *, method: str = "GET",
                      body: Optional[bytes] = None,
                      bearer: bool = False,
                      timeout: Optional[float] = None) -> tuple[int, bytes]:
        """执行一次子端 HTTP 请求，返回 (status, body_bytes)。

        GET 优先把 token 放进查询参数（子端 GET 授权支持 token 查询参数）；
        POST 使用 Bearer 头。隧道环境下 *url* 是子端裸路径，改走
        ``_tunnel_request``。*timeout* 为 None 时用默认网络超时。
        """
        effective = timeout if timeout else _HTTP_TIMEOUT_SECONDS
        if self.is_tunnel:
            return self._tunnel_request(url, method=method, body=body,
                                        bearer=bearer, timeout=effective)
        headers = {
            "User-Agent": "agent-service-remote-proxy/1.0",
        }
        if bearer and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=effective) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _tunnel_request(self, path: str, *, method: str = "GET",
                        body: Optional[bytes] = None,
                        bearer: bool = False,
                        timeout: Optional[float] = None) -> tuple[int, bytes]:
        """隧道传输：经母端 TunnelManager 把请求转发到子端本地服务。"""
        from runtime.tunnel_manager import TunnelError, TunnelOfflineError
        if self.tunnel_manager is None:
            raise RemoteToolCallError(
                "tunnel transport unavailable (parent tunnel manager missing)", None)
        # No child-side token is attached: requests over the tunnel are
        # self-authorized by the child when its auth is enabled (the child
        # sees them as local requests). Trust boundary: when the PARENT has
        # no password set, /v1/tunnel/* is open to any client that can
        # reach the port — see tunnel_manager.py.
        headers = {"User-Agent": "agent-service-remote-proxy/1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, _resp_headers, payload = self.tunnel_manager.call_env(
                self.env_id, method, path, headers, body,
                timeout=timeout if timeout else _HTTP_TIMEOUT_SECONDS,
            )
        except TunnelOfflineError as exc:
            raise RemoteToolCallError(
                f"tunnel environment offline: {exc}", None) from exc
        except TunnelError as exc:
            raise RemoteToolCallError(
                f"tunnel call failed: {exc}", None) from exc
        return status, payload

    def http_json(self, path: str, method: str = "GET",
                  payload: Optional[dict] = None,
                  timeout: Optional[float] = None) -> tuple[int, dict]:
        """通用子端 JSON 请求（revoke / 会话清理等管理调用）。

        ``timeout`` 覆盖默认网络超时（秒）：轻量探测类调用（如子端
        log-dir 探测）应传入短超时，避免目标不可达时长时间挂起。

        Returns:
            (status, parsed_json_dict)；响应体不是 JSON 时返回
            ``{"error": <原始文本前 300 字符>}``。
        """
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None else None
        )
        url = self._build_url(path)
        status, payload_bytes = self._http_request(
            url, method=method, body=body,
            bearer=(method != "GET") and bool(self.token),
            timeout=timeout,
        )
        try:
            data = json.loads(payload_bytes.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            data = {"error": payload_bytes[:300].decode("utf-8", errors="replace")}
        if not isinstance(data, dict):
            data = {"data": data}
        return status, data

    # ------------------------------------------------------------------
    # 工具清单
    # ------------------------------------------------------------------

    def list_tools(self, force: bool = False) -> list[dict]:
        """拉取子端 ``GET /v1/tools``（TTL 缓存），返回工具 dict 列表。"""
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._tools_cache is not None
                and now - self._tools_fetched_at < TOOL_LIST_TTL_SECONDS
            ):
                return self._tools_cache
        # 缓存未命中时在线程外拉取，避免持锁期间发生网络 I/O
        url = self._build_url("/v1/tools")
        try:
            status, payload = self._http_request(url, method="GET")
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteToolCallError(
                f"child environment unreachable: {exc}", None
            ) from exc
        tools: list[dict] = []
        if status == 200:
            try:
                data = json.loads(payload.decode("utf-8", errors="replace"))
                raw = data.get("tools") if isinstance(data, dict) else None
                if isinstance(raw, list):
                    tools = [t for t in raw if isinstance(t, dict) and t.get("tool_id")]
            except (ValueError, UnicodeDecodeError) as exc:
                logger.warning("remote env %s: 工具清单解析失败: %s", self.env_id, exc)
        else:
            snippet = payload[:300].decode("utf-8", errors="replace")
            raise RemoteToolCallError(
                f"child tool list HTTP {status}: {snippet}", status
            )
        with self._lock:
            self._tools_cache = tools
            self._tools_fetched_at = time.monotonic()
        return tools

    def list_tool_configs(self) -> list[ToolConfig]:
        """构建子端工具对应的母端 ToolConfig 条目（子端原始 tool_id / name）。

        远程会话与本地工具不混用，不存在 ID 冲突，因此不注册全局 registry。
        非 skill 条目在 config 上携带 ``callable_fn``（推理循环执行 function
        工具时优先使用它，而非查 registry）；skill 条目无 callable，由母端
        推理循环走渐进式披露（正文从子端获取）。
        """
        tools = self.list_tools()
        configs: list[ToolConfig] = []
        for tool in tools:
            original_id = str(tool.get("tool_id") or "")
            original_name = str(tool.get("name") or original_id)
            if not original_id or not original_name:
                continue
            original_type = str(tool.get("tool_type") or "function")
            tool_type = original_type
            if original_type == "mcp":
                # 远程 MCP 工具在子端由子端 MCPClientManager 执行；母端按
                # function 代理转发（保留 mcp_server_name 供 UI 分组显示）。
                # 若保留 mcp 类型，母端 _execute_tool_call 会走本地
                # _execute_mcp_tool 而查不到子端 server。
                tool_type = "function"
            elif original_type not in {"function", "skill"}:
                # 未知类型按 function 代理，避免静默丢弃能力
                tool_type = "function"
            config = ToolConfig(
                tool_id=original_id,
                tool_type=tool_type,
                name=original_name,
                description=str(tool.get("description") or ""),
                parameters=tool.get("parameters") or {"type": "object", "properties": {}},
                mcp_server_name=tool.get("mcp_server_name"),
                tool_name=tool.get("tool_name"),
                skill_dir=tool.get("skill_dir") if tool_type == "skill" else None,
                builtin=True,
                labels=list(tool.get("labels") or []),
            )
            # 标记远程代理工具：超长结果守护据此改为内联截断（子端读不到母端 /tmp）。
            config.is_remote_proxy = True
            if tool_type != "skill":
                child_id = original_id
                proxy = self

                def _proxy_callable(_id=child_id, _p=proxy, **arguments) -> str:
                    return _p.call(_id, arguments)

                config.callable_fn = _proxy_callable
            configs.append(config)
        return configs

    def invalidate_tools(self) -> None:
        """失效工具清单与技能正文缓存（push-update / 隧道重连后调用）。"""
        with self._lock:
            self._tools_cache = None
            self._tools_fetched_at = 0.0
            self._skill_cache.clear()

    # ------------------------------------------------------------------
    # 技能正文（渐进式披露用）
    # ------------------------------------------------------------------

    def _resolve_tool_id(self, name_or_id: str) -> Optional[str]:
        """把技能名称映射到子端 tool_id（查已缓存工具清单，不发额外请求）。"""
        for tool in self.list_tools():
            if tool.get("tool_id") == name_or_id or tool.get("name") == name_or_id:
                return str(tool.get("tool_id"))
        return name_or_id or None

    def fetch_skill_body(self, tool_name: str) -> tuple[Optional[str], Optional[str]]:
        """获取子端技能 SKILL.md 正文与工作目录（TTL 缓存）。

        接受子端原始技能名（模型实际调用的名称，与本地会话一致）或其
        tool_id。

        Returns:
            (body, skill_dir)，任一可能为 None；skill_dir 是子端路径，
            母端不能也不应 chdir 到该目录。
        """
        child_tool_id = self._resolve_tool_id(tool_name)
        if child_tool_id is None:
            return None, None
        with self._lock:
            entry = self._skill_cache.get(child_tool_id)
        now = time.monotonic()
        if entry and now - entry[0] < SKILL_BODY_TTL_SECONDS:
            return entry[1], entry[2]
        url = self._build_url(f"/v1/tools/skill/{urllib.parse.quote(child_tool_id)}")
        try:
            status, payload = self._http_request(url, method="GET")
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteToolCallError(
                f"child environment unreachable: {exc}", None
            ) from exc
        if status != 200:
            snippet = payload[:300].decode("utf-8", errors="replace")
            raise RemoteToolCallError(
                f"child skill body HTTP {status}: {snippet}", status
            )
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        body = data.get("body") if isinstance(data, dict) else None
        skill_dir = data.get("skill_dir") if isinstance(data, dict) else None
        body = str(body) if body else None
        skill_dir = str(skill_dir) if skill_dir else None
        with self._lock:
            self._skill_cache[child_tool_id] = (time.monotonic(), body, skill_dir)
        return body, skill_dir

    # ------------------------------------------------------------------
    # 工具调用转发
    # ------------------------------------------------------------------

    def call(self, child_tool_id: str, arguments: dict) -> str:
        """转发一次工具调用到子端 ``POST /v1/tools/call``，返回结果文本。

        session_id 与 user_message_timestamp 从请求上下文读取（由
        ``_prepare_infer_request`` 设置，工具 worker 线程通过上下文快照
        继承），使子端文件 journal / 终端 / 子会话与母端会话同 id。
        """
        from runtime.common import get_request_context

        session_id = get_request_context("session_id")
        user_message_timestamp = get_request_context("user_message_timestamp")
        # 网络层超时跟随工具级有效超时（TOOL_EXEC_TIMEOUT 基线 / 长执行标签 /
        # 参数 timeout，由母端推理循环经请求上下文传入）：长执行工具
        # （exec_shell 长超时、远程 delegate/talk_to）不会被固定 180s 砍断；
        # 拿不到时退回兜底值。
        raw_timeout = get_request_context("tool_exec_timeout")
        try:
            effective_timeout = float(raw_timeout) if raw_timeout else 0.0
        except (TypeError, ValueError):
            effective_timeout = 0.0
        if effective_timeout <= 0:
            effective_timeout = _TOOL_CALL_FALLBACK_TIMEOUT

        payload: dict = {
            "tool_id": child_tool_id,
            "arguments": arguments if isinstance(arguments, dict) else {},
        }
        if session_id:
            payload["session_id"] = session_id
        if user_message_timestamp:
            payload["user_message_timestamp"] = user_message_timestamp

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self._build_url("/v1/tools/call")
        status, payload_bytes = self._http_request(
            url, method="POST", body=body, bearer=bool(self.token),
            timeout=effective_timeout,
        )
        if status == 200:
            return payload_bytes.decode("utf-8", errors="replace")
        snippet = payload_bytes[:500].decode("utf-8", errors="replace")
        try:
            err_data = json.loads(snippet)
            message = err_data.get("error") or err_data.get("message") or snippet
        except (ValueError, UnicodeDecodeError):
            message = snippet
        return f"Error: remote tool call failed (HTTP {status}): {message}"


class RemoteToolCallError(RuntimeError):
    """远程环境工具清单 / 技能正文拉取失败。"""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status

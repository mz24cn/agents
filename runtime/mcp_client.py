"""MCP Client Manager for the Composable Agent Runtime.

Provides MCPClientManager, a singleton that manages connections to MCP servers
via stdio (subprocess) or HTTP SSE/Streamable HTTP. Communicates using JSON-RPC
2.0 over the appropriate transport. Only uses Python standard library modules.

Idle process reaping:
    stdio connections that have not been used for ``idle_timeout`` seconds are
    automatically terminated by a background reaper thread.  The default
    timeout is 300 s (5 min).  Set ``idle_timeout=0`` to disable reaping.
    When a reaped server is needed again, it is transparently restarted on the
    next call_tool() / get_tools() call.
"""

import asyncio
import json
import os
import signal
import threading
import time
import urllib.request
import urllib.error
from typing import Optional

from runtime.models import ToolConfig

# Default idle timeout in seconds before a stdio process is reaped.
_DEFAULT_IDLE_TIMEOUT = 300


class MCPClientManager:
    """Singleton MCP Client manager supporting stdio and HTTP SSE transports."""

    _instance: Optional["MCPClientManager"] = None
    _initialized: bool = False

    def __new__(cls) -> "MCPClientManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, idle_timeout: int = _DEFAULT_IDLE_TIMEOUT) -> None:
        if MCPClientManager._initialized:
            return
        MCPClientManager._initialized = True
        self._connections: dict[str, dict] = {}
        self._request_id: int = 0
        self._lock = threading.Lock()
        self._idle_timeout = idle_timeout
        # Dedicated event loop running in a background thread.
        # All asyncio subprocess operations are funnelled here so that
        # pipes/Futures are always bound to a single, long-lived loop —
        # regardless of which HTTP worker thread initiates the call.
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._loop_thread.start()
        # Start background reaper thread if timeout is enabled
        if idle_timeout > 0:
            self._start_reaper()

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    def _start_reaper(self) -> None:
        """Start a daemon thread that periodically reaps idle stdio processes."""
        t = threading.Thread(target=self._reaper_loop, daemon=True)
        t.start()

    def _reaper_loop(self) -> None:
        """Check every 60 s and terminate processes idle longer than idle_timeout."""
        while True:
            time.sleep(60)
            now = time.monotonic()
            for server_name, conn in list(self._connections.items()):
                if conn["type"] != "stdio":
                    continue
                if not conn.get("connected"):
                    continue
                last_used = conn.get("last_used", now)
                if now - last_used >= self._idle_timeout:
                    try:
                        self.disconnect(server_name)
                    except Exception:
                        pass

    def _touch(self, conn: dict) -> None:
        """Update the last-used timestamp for a connection."""
        conn["last_used"] = time.monotonic()

    # ------------------------------------------------------------------
    # JSON-RPC helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _build_jsonrpc(self, method: str, params: Optional[dict] = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            msg["params"] = params
        return msg

    # ------------------------------------------------------------------
    # stdio transport helpers
    # ------------------------------------------------------------------

    async def _stdio_send(self, conn: dict, request: dict) -> dict:
        process: asyncio.subprocess.Process = conn["process"]
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("stdio pipes not available")
        payload = json.dumps(request) + "\n"
        process.stdin.write(payload.encode("utf-8"))
        await process.stdin.drain()
        # Some MCP servers emit non-JSON lines or JSON-RPC notifications before
        # the actual response.  Skip non-JSON lines and notifications (no "id").
        while True:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout unexpectedly")
            stripped = line.decode("utf-8").strip()
            if stripped.startswith("{"):
                parsed = json.loads(stripped)
                # Skip JSON-RPC notifications (no "id" field) — e.g. progress messages
                if "id" not in parsed:
                    continue
                return parsed
            # non-JSON line — log to stderr and keep reading
            import sys as _sys
            print(f"[mcp_client] skipping non-JSON stdout: {stripped[:120]}", file=_sys.stderr)

    async def _stdio_initialize(self, conn: dict) -> None:
        request = self._build_jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "runtime", "version": "1.0.0"},
        })
        response = await self._stdio_send(conn, request)
        if "error" in response:
            raise RuntimeError(f"MCP initialize failed: {response['error']}")
        conn["server_info"] = response.get("result", {})
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        payload = json.dumps(notification) + "\n"
        conn["process"].stdin.write(payload.encode("utf-8"))
        await conn["process"].stdin.drain()

    # ------------------------------------------------------------------
    # HTTP SSE transport helpers
    # ------------------------------------------------------------------

    def _http_send(self, conn: dict, request: dict) -> dict:
        url = conn["url"]
        headers = dict(conn.get("headers") or {})
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Accept", "application/json, text/event-stream")
        body = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
                if "text/event-stream" in content_type:
                    return self._parse_sse_response(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"MCP HTTP error {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MCP connection error: {exc.reason}") from exc

    def _parse_sse_response(self, raw: bytes) -> dict:
        text = raw.decode("utf-8")
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[len("data:"):].strip()
                if data_str and data_str != "[DONE]":
                    try:
                        return json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
        raise RuntimeError("No valid JSON-RPC response found in SSE stream")

    def _http_initialize(self, conn: dict) -> None:
        request = self._build_jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "runtime", "version": "1.0.0"},
        })
        response = self._http_send(conn, request)
        if "error" in response:
            raise RuntimeError(f"MCP initialize failed: {response['error']}")
        conn["server_info"] = response.get("result", {})
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            self._http_send(conn, notification)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Public API: connect
    # ------------------------------------------------------------------

    def connect_stdio(
        self,
        server_name: str,
        command: str,
        args: Optional[list] = None,
        env: Optional[dict] = None,
    ) -> None:
        """Connect to an MCP server via stdio subprocess.

        Launches the subprocess and performs the MCP initialize handshake.
        If the server is already connected, this is a no-op.
        """
        if server_name in self._connections:
            if self._connections[server_name].get("connected"):
                return
        self._run_async(self._async_connect_stdio(server_name, command, args, env))

    async def _async_connect_stdio(
        self,
        server_name: str,
        command: str,
        args: Optional[list] = None,
        env: Optional[dict] = None,
    ) -> None:
        cmd_args = [command] + (args or [])
        process_env = dict(os.environ)
        if env:
            process_env.update(env)

        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
            start_new_session=True,  # new process group so killpg cleans up all descendants
        )

        # Raise the StreamReader limit to 100 MB to handle MCP tools that return
        # large payloads (e.g. browser snapshots). The default asyncio limit is
        # 64 KB which causes "ValueError: chunk is longer than limit" errors.
        if process.stdout is not None:
            process.stdout._limit = 100 * 1024 * 1024  # type: ignore[attr-defined]
        conn: dict = {
            "type": "stdio",
            "server_name": server_name,
            "command": command,
            "args": args or [],
            "env": env,
            "process": process,
            "connected": True,
            "server_info": {},
            "tools_cache": None,
            "last_used": time.monotonic(),
        }
        self._connections[server_name] = conn
        try:
            await self._stdio_initialize(conn)
        except Exception:
            await self._async_disconnect_stdio(conn)
            raise

    def connect_url(
        self,
        server_name: str,
        url: str,
        headers: Optional[dict] = None,
    ) -> None:
        """Connect to an MCP server via HTTP SSE / Streamable HTTP.

        Performs the MCP initialize handshake immediately.
        If the server is already connected, this is a no-op.
        """
        if server_name in self._connections:
            if self._connections[server_name].get("connected"):
                return
        conn: dict = {
            "type": "url",
            "server_name": server_name,
            "url": url,
            "headers": headers,
            "connected": True,
            "server_info": {},
            "tools_cache": None,
            "last_used": time.monotonic(),
        }
        self._connections[server_name] = conn
        try:
            self._http_initialize(conn)
        except Exception:
            conn["connected"] = False
            raise

    # ------------------------------------------------------------------
    # Public API: get_tools, call_tool
    # ------------------------------------------------------------------

    def get_tools(self, server_name: str) -> list:
        """Retrieve the list of tools from an MCP server.

        Reconnects automatically if the process was reaped due to idle timeout.
        """
        conn = self._ensure_connected(server_name)
        if conn.get("tools_cache") is not None:
            return list(conn["tools_cache"])
        request = self._build_jsonrpc("tools/list")
        if conn["type"] == "stdio":
            response = self._run_async(self._stdio_send(conn, request))
        else:
            response = self._http_send(conn, request)
        if "error" in response:
            raise RuntimeError(f"MCP tools/list failed: {response['error']}")
        raw_tools = response.get("result", {}).get("tools", [])
        tools = self._convert_tools(server_name, raw_tools)
        conn["tools_cache"] = tools
        self._touch(conn)
        return list(tools)

    def call_tool(self, server_name: str, tool_name: str, arguments: Optional[dict] = None) -> str:
        """Invoke a tool on an MCP server.

        Reconnects automatically if the process was reaped due to idle timeout.
        """
        conn = self._ensure_connected(server_name)
        params: dict = {"name": tool_name}
        if arguments is not None:
            params["arguments"] = arguments
        request = self._build_jsonrpc("tools/call", params)
        if conn["type"] == "stdio":
            response = self._run_async(self._stdio_send(conn, request))
        else:
            response = self._http_send(conn, request)
        self._touch(conn)
        if "error" in response:
            error = response["error"]
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RuntimeError(f"MCP tool call failed: {msg}")
        result = response.get("result", {})
        content_list = result.get("content", [])
        parts = []
        for item in content_list:
            if isinstance(item, dict):
                parts.append(item.get("text", json.dumps(item)))
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else json.dumps(result)

    # ------------------------------------------------------------------
    # Public API: disconnect, is_connected, reconnect
    # ------------------------------------------------------------------

    def disconnect(self, server_name: str) -> None:
        """Disconnect from a specific MCP server, terminating any subprocess."""
        conn = self._connections.get(server_name)
        if conn is None:
            return
        if conn["type"] == "stdio":
            self._run_async(self._async_disconnect_stdio(conn))
        conn["connected"] = False
        conn["tools_cache"] = None

    def disconnect_all(self) -> None:
        for server_name in list(self._connections.keys()):
            self.disconnect(server_name)

    def is_connected(self, server_name: str) -> bool:
        conn = self._connections.get(server_name)
        if conn is None:
            return False
        if conn["type"] == "stdio":
            process = conn.get("process")
            if process is None or process.returncode is not None:
                conn["connected"] = False
                return False
        return bool(conn.get("connected", False))

    def reconnect(self, server_name: str) -> None:
        """Reconnect to a previously registered MCP server."""
        conn = self._connections.get(server_name)
        if conn is None:
            raise RuntimeError(f"No connection info for server '{server_name}', cannot reconnect")
        self.disconnect(server_name)
        if conn["type"] == "stdio":
            self.connect_stdio(server_name, conn["command"], conn.get("args"), conn.get("env"))
        elif conn["type"] == "url":
            self.connect_url(server_name, conn["url"], conn.get("headers"))
        else:
            raise RuntimeError(f"Unknown connection type: {conn['type']}")

    # ------------------------------------------------------------------
    # Convenience: load from config dict
    # ------------------------------------------------------------------

    def load_config(self, config: dict, tool_registry=None) -> list:
        """从 JSON 配置批量连接 MCP Server。

        只注册连接参数，不启动进程也不发现工具。
        如果提供了 tool_registry，工具 schema 已在 tools.json 中持久化，
        无需在此重新发现。

        配置格式::

            {
                "mcpServers": {
                    "time": {"command": "uvx", "args": [...], "env": {}},
                    "fetch": {"url": "http://localhost:8080/mcp"}
                }
            }
        """
        servers = config.get("mcpServers", config)
        if not isinstance(servers, dict):
            raise ValueError("config must be a dict with server definitions")

        for server_name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                continue
            if server_cfg.get("disabled", False):
                continue
            # Only store config — process starts on first actual use
            if "command" in server_cfg:
                self._store_stdio_config(server_name, server_cfg["command"],
                                         server_cfg.get("args"), server_cfg.get("env"))
            elif "url" in server_cfg:
                self._store_url_config(server_name, server_cfg["url"], server_cfg.get("headers"))

        return []

    def _store_stdio_config(self, server_name: str, command: str,
                             args: Optional[list], env: Optional[dict]) -> None:
        """Store stdio config without starting the process (used by load_config)."""
        if server_name in self._connections and self._connections[server_name].get("connected"):
            return
        self._connections[server_name] = {
            "type": "stdio",
            "server_name": server_name,
            "command": command,
            "args": args or [],
            "env": env,
            "process": None,
            "connected": False,
            "server_info": {},
            "tools_cache": None,
            "last_used": 0.0,
        }

    def _store_url_config(self, server_name: str, url: str,
                           headers: Optional[dict]) -> None:
        """Store URL config without performing the handshake (used by load_config)."""
        if server_name in self._connections and self._connections[server_name].get("connected"):
            return
        self._connections[server_name] = {
            "type": "url",
            "server_name": server_name,
            "url": url,
            "headers": headers,
            "connected": False,
            "server_info": {},
            "tools_cache": None,
            "last_used": 0.0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self, server_name: str) -> dict:
        """Return a live connection, reconnecting if the process was reaped."""
        conn = self._connections.get(server_name)
        if conn is None:
            raise RuntimeError(f"MCP server '{server_name}' is not registered")
        if not conn.get("connected"):
            # Process was reaped or never started — reconnect transparently
            self.reconnect(server_name)
            conn = self._connections[server_name]
        # Also check if stdio process has died unexpectedly
        if conn["type"] == "stdio":
            process = conn.get("process")
            if process is None or process.returncode is not None:
                self.reconnect(server_name)
                conn = self._connections[server_name]
        return conn

    def _convert_tools(self, server_name: str, raw_tools: list) -> list:
        tools: list = []
        for raw in raw_tools:
            name = raw.get("name", "")
            description = raw.get("description", "")
            input_schema = raw.get("inputSchema", {"type": "object", "properties": {}})
            tools.append(ToolConfig(
                tool_id=f"mcp-{server_name}-{name}",
                tool_type="mcp",
                name=name,
                description=description,
                parameters=input_schema,
                mcp_server_name=server_name,
                tool_name=name,
            ))
        return tools

    async def _async_disconnect_stdio(self, conn: dict) -> None:
        process = conn.get("process")
        if process is None:
            return
        if process.returncode is None:
            try:
                # Kill the entire process group so child processes spawned by
                # the MCP server (e.g. chrome-devtools-mcp forked by npm exec)
                # are also terminated and don't become orphans.
                pid = process.pid
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
            except (ProcessLookupError, OSError):
                pass

    def _run_async(self, coro) -> object:
        """Submit a coroutine to the dedicated event loop and block until done.

        This is safe to call from any thread (including ThreadingHTTPServer
        worker threads).  The coroutine always executes on ``self._loop``,
        which is the same loop that created the subprocess pipes, so there
        is never a "Future attached to a different loop" mismatch.
        """
        if not self._loop.is_running():
            raise RuntimeError("Dedicated MCP event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()  # blocks the calling thread until done

    @classmethod
    def _reset(cls) -> None:
        """Reset the singleton instance (for testing purposes only)."""
        inst = cls._instance
        if inst is not None:
            # Stop the dedicated event loop so the background thread exits.
            loop = getattr(inst, "_loop", None)
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            thread = getattr(inst, "_loop_thread", None)
            if thread is not None:
                thread.join(timeout=5)
        cls._instance = None
        cls._initialized = False

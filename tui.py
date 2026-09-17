#!/usr/bin/env python3
"""Lianhua Agent Service — local TUI chat client entry point.

Implements the frozen flow of ``docs/tui-contract.md`` §4.4:

    python tui.py [--port N] [--token T] [--login] [--session ID] [--new]
                  [--register <parent-setup-url>] [--plain]

1. parse arguments; resolve the service address (contract §3.2:
   ``--port`` > ``AGENTS_URL`` from ``$AGENTS_RUNTIME_DIR/env.json``
   (default ``~/.agents_runtime/env.json``) > default 7988; an https scheme
   yields an unverified SSL context; the host is always 127.0.0.1);
2. build :class:`tui.chat_client.ServiceClient` (explicit token wins);
3. auth assembly: without a token probe ``GET /v1/auth/config`` —
   disabled → connect directly; enabled → getpass prompt + login (``--login``
   forces the prompt; non-interactive without a token → error exit);
4. service unreachable → clear stderr message (actual base_url + hint),
   exit code 2;
5. ``--register <url>``: synchronous headless ``tunnel_register``;
   failure → stderr + exit code 1; success → one stdout line, continue;
6. ``tui.app.run(client, TuiOptions(...))`` (imported lazily inside
   :func:`main` so ``python tui.py --help`` works before ``tui/app.py``
   exists); returns its exit code.

Standard library only.  The module must not import termios at top level
(raw input belongs to ``tui/term.py``); this entry point never does.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse

from tui.chat_client import ApiError, ServiceClient

DEFAULT_PORT = 7988
EXIT_UNREACHABLE = 2
EXIT_FAILURE = 1


# ---------------------------------------------------------------------------
# Service discovery (contract §3.2)
# ---------------------------------------------------------------------------

def runtime_dir() -> str:
    """``$AGENTS_RUNTIME_DIR`` or ``~/.agents_runtime`` (app.py convention)."""
    directory = os.environ.get("AGENTS_RUNTIME_DIR", "").strip()
    if not directory:
        directory = os.path.join(os.path.expanduser("~"), ".agents_runtime")
    return directory


def agents_url_from_env_json() -> str:
    """Read ``AGENTS_URL`` from the runtime dir's env.json ("" when absent)."""
    path = os.path.join(runtime_dir(), "env.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    if isinstance(data, dict):
        value = data.get("AGENTS_URL")
        if value:
            return str(value).strip()
    return ""


def resolve_base_url(port_arg: "int | None") -> "tuple[str, ssl.SSLContext | None]":
    """Resolve ``(base_url, ssl_context)`` for the local service.

    Priority (contract §3.2): ``--port`` > env.json ``AGENTS_URL`` port >
    default 7988.  The scheme comes from ``AGENTS_URL`` when present (https →
    unverified SSL context); the host is always rewritten to 127.0.0.1.
    """
    scheme = "http"
    ssl_context: "ssl.SSLContext | None" = None
    port = port_arg
    url = agents_url_from_env_json()
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme in ("https", "wss"):
            scheme = "https"
            ssl_context = ssl._create_unverified_context()
        if port is None and parts.port:
            port = parts.port
    if port is None:
        port = DEFAULT_PORT
    return f"{scheme}://127.0.0.1:{int(port)}", ssl_context


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tui.py",
        description=(
            "Agent Service 本地 TUI 对话客户端（仅连接本机 127.0.0.1 服务）。"
            "端口解析：--port > $AGENTS_RUNTIME_DIR/env.json 的 AGENTS_URL > 7988。"
        ),
    )
    parser.add_argument("--port", type=int, default=None, metavar="N",
                        help="服务端口（覆盖 env.json 的 AGENTS_URL；缺省 7988）")
    parser.add_argument("--token", default="", metavar="T",
                        help="鉴权 token（st_/as_）；GET 走 ?token=，写方法走 Bearer")
    parser.add_argument("--login", action="store_true",
                        help="强制密码登录（getpass 提示；保存会话 cookie）")
    parser.add_argument("--session", default=None, metavar="ID",
                        help="挂载历史会话（session_id）")
    parser.add_argument("--new", action="store_true", help="开始新会话")
    parser.add_argument("--register", default=None, metavar="PARENT_URL",
                        help="无头注册到母端（POST /v1/tunnel/parent/register）；"
                             "失败退出码 1，成功打印一行后继续")
    parser.add_argument("--plain", action="store_true",
                        help="纯文本模式（无 ANSI / 无备用屏，便于 tee 日志）")
    return parser


def _login_interactive(client: ServiceClient) -> "int":
    """Prompt for the service password and log in.  Returns a process exit
    code: 0 on success, non-zero on failure (message already on stderr)."""
    try:
        password = getpass.getpass("服务密码: ")
    except (KeyboardInterrupt, EOFError):
        print("未读取到密码（非交互环境）。请使用 --token <token> 或 --login。",
              file=sys.stderr)
        return EXIT_FAILURE
    except OSError:
        print("无法读取密码（没有可用终端）。请使用 --token <token>。",
              file=sys.stderr)
        return EXIT_FAILURE
    try:
        ok = client.login(password)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None) or exc
        print(f"登录请求失败（服务不可达）: {reason}", file=sys.stderr)
        return EXIT_FAILURE
    except ApiError as exc:
        print(f"登录失败: HTTP {exc.status} {exc.message}", file=sys.stderr)
        return EXIT_FAILURE
    if not ok:
        print("登录失败：密码错误（HTTP 401）。", file=sys.stderr)
        return EXIT_FAILURE
    return 0


def _unreachable(base_url: str, reason) -> "int":
    print(
        f"无法连接服务 {base_url}（{reason}）。\n"
        f"请检查 Agent Service 是否已启动、端口是否正确"
        f"（$AGENTS_RUNTIME_DIR/env.json 的 AGENTS_URL 或 --port）。",
        file=sys.stderr,
    )
    return EXIT_UNREACHABLE


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.port is not None and not (1 <= args.port <= 65535):
        parser.error(f"--port 必须在 1-65535 之间: {args.port}")

    # 1) resolve address + 2) build client (explicit token wins)
    base_url, ssl_context = resolve_base_url(args.port)
    client = ServiceClient(base_url, token=args.token or "",
                           ssl_context=ssl_context)

    # 3) auth assembly / 4) reachability — the probe is both
    try:
        status = client.auth_status()
    except urllib.error.URLError as exc:
        return _unreachable(base_url, getattr(exc, "reason", None) or exc)
    except ApiError as exc:
        # Service is up but answered abnormally; warn and carry on.
        print(f"警告: /v1/auth/config 返回 HTTP {exc.status}: {exc.message}",
              file=sys.stderr)
        status = None

    token = args.token or ""
    if token:
        if status is None:
            print("警告: 服务拒绝了 --token（HTTP 401）；token 可能无效或已过期。",
                  file=sys.stderr)
    elif args.login:
        code = _login_interactive(client)
        if code != 0:
            return code
    elif status is None:
        # Auth enabled and unauthenticated.
        if sys.stdin.isatty():
            code = _login_interactive(client)
            if code != 0:
                return code
        else:
            print("服务已启用鉴权且未提供 token：请使用 --token <token> 或 --login。",
                  file=sys.stderr)
            return EXIT_FAILURE
    # else: auth disabled (status dict with auth_enabled False) → direct.

    # 5) --register: synchronous headless tunnel registration
    register_result: "dict | None" = None
    if args.register:
        try:
            register_result = client.tunnel_register(args.register)
        except urllib.error.URLError as exc:
            print(f"母端注册失败（服务不可达）: {getattr(exc, 'reason', None) or exc}",
                  file=sys.stderr)
            return EXIT_FAILURE
        except ApiError as exc:
            print(f"母端注册失败: {exc.message or exc}", file=sys.stderr)
            return EXIT_FAILURE
        print(f"已注册到母端: {args.register}")

    # 6) hand over to the TUI main loop (lazy import on purpose)
    from tui.app import TuiOptions, run

    options = TuiOptions(
        session_id=args.session or None,
        new_session=bool(args.new),
        plain=bool(args.plain),
        register_result=register_result,
    )
    try:
        code = run(client, options)
    except KeyboardInterrupt:
        code = 0
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())

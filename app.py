"""Entry point for the Agent Service server.

Default host/port come from the AGENTS_HOST / AGENTS_PORT env vars
(falling back to 0.0.0.0 / 7988). A positional argument overrides them.

Usage:
    python app.py              # default host/port (env or fallback)
    python app.py 7988         # custom port
    python app.py 0.0.0.0:7988 # custom host and port
"""

import os
import signal
import sys
from runtime.server import RuntimeHTTPServer

PID_FILE_NAME = "server.pid"


def _runtime_dir() -> str:
    runtime_dir = os.environ.get("AGENTS_RUNTIME_DIR", "").strip()
    if not runtime_dir:
        runtime_dir = os.path.join(os.path.expanduser("~"), ".agents_runtime")
    return runtime_dir


def _write_pid_file() -> None:
    """Write the current process PID to AGENTS_RUNTIME_DIR/server.pid.

    PID 的生成由进程自身负责（os.getpid()），与启动方式（脚本、命令行、
    服务管理器等）无关，确保任何方式启动都能得到正确的 PID 文件，
    供 stop 脚本及外部工具查询。
    """
    runtime_dir = _runtime_dir()
    try:
        os.makedirs(runtime_dir, exist_ok=True)
    except OSError:
        pass
    pid_path = os.path.join(runtime_dir, PID_FILE_NAME)
    try:
        with open(pid_path, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def _remove_pid_file() -> None:
    pid_path = os.path.join(_runtime_dir(), PID_FILE_NAME)
    try:
        if os.path.isfile(pid_path):
            os.remove(pid_path)
    except OSError:
        pass


host = os.environ.get("AGENTS_HOST", "").strip() or "0.0.0.0"
port = int(os.environ.get("AGENTS_PORT", "").strip() or "7988")

if len(sys.argv) > 1:
    arg = sys.argv[1]
    parts = arg.split(":", 1)
    if len(parts) == 1:
        port = int(parts[0])
    else:
        host = parts[0]
        port = int(parts[1])

if __name__ == "__main__":
    workspace_dir = os.environ.get("AGENTS_WORKSPACE", "")
    if not workspace_dir or not os.path.exists(workspace_dir):
        workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
        if not os.path.exists(workspace_dir):
            workspace_dir = os.path.dirname(os.path.abspath(__file__))
        os.environ["AGENTS_WORKSPACE"] = workspace_dir
    os.chdir(workspace_dir)

    # 将 SIGTERM / SIGINT 转为 KeyboardInterrupt，走 finally 优雅关闭并清理 PID 文件。
    def _handle_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _write_pid_file()
    server = RuntimeHTTPServer(host=host, port=port)

    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        _remove_pid_file()

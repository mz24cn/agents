"""Entry point for the Agent Service server.

The address the server listens on is resolved by RuntimeHTTPServer.start():

  1. A positional command-line argument (``host:port`` or ``port``) is the
     final override;
  2. otherwise the AGENTS_URL environment variable (e.g.
     ``https://domain:7988/``) is parsed into protocol/domain/port;
 3. otherwise the defaults http + 0.0.0.0 + 7988 are used.

AGENTS_URL may have been written into env.json from the web UI and synced
into the process environment by EnvManager at startup, so changing it there
takes effect after a restart. An https protocol enables TLS; certificates
are loaded from DATA_DIR/certs as {domain}.pem / {domain}.key based on SNI.

Usage:
   python app.py              # AGENTS_URL or default (http://0.0.0.0:7988)
    python app.py 7988         # custom port (overrides AGENTS_URL)
    python app.py 0.0.0.0:7988 # custom host and port (overrides AGENTS_URL)
"""

import os
import signal
import sys
import time
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


# 可选的位置参数（host:port 或 port），作为 start() 的最终重载值；
# 不再读取 AGENTS_HOST / AGENTS_PORT，访问地址由 start() 里的
# AGENTS_URL（或默认值）决定。
host = None
port = None
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
        workspace_dir = os.path.expanduser("~/workspace")
        os.makedirs(workspace_dir, exist_ok=True)
        os.environ["AGENTS_WORKSPACE"] = workspace_dir
    os.chdir(workspace_dir)

    # 将 SIGTERM / SIGINT 转为 KeyboardInterrupt，走 finally 优雅关闭并清理 PID 文件。
    def _handle_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _write_pid_file()
    server = RuntimeHTTPServer()

    try:
        server.start(host=host, port=port)
    except KeyboardInterrupt:
        pass
    finally:
        # 优雅收尾：先通知进行中的推理线程终止并落盘，再关闭服务器。
        # 推理线程是 daemon 线程，直接 stop() 会在进程退出时被强制终止，
        # 可能丢失最后一轮对话输出。这里 set 所有 active_streams 的
        # cancel_event 并短暂等待，让推理线程走正常的最终持久化分支。
        # （配合推理过程中的增量持久化，最多丢失正在执行的最后一轮。）
        try:
            active = getattr(server, "_active_streams", None) or {}
            for ev in list(active.values()):
                try:
                    ev.set()
                except Exception:
                    pass
            if active:
                time.sleep(3)
        except Exception:
            pass
        server.stop()
        _remove_pid_file()

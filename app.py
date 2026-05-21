"""Entry point for the Agent Service server.

Usage:
    python app.py              # default host/port
    python app.py 8080         # custom port
    python app.py 0.0.0.0:8080 # custom host and port
"""

import os
import sys
from runtime.server import RuntimeHTTPServer

host = "0.0.0.0"
port = 8080

if len(sys.argv) > 1:
    arg = sys.argv[1]
    parts = arg.split(":", 1)
    if len(parts) == 1:
        port = int(parts[0])
    else:
        host = parts[0]
        port = int(parts[1])

if __name__ == "__main__":
    server = RuntimeHTTPServer(host=host, port=port)

    # 切换工作目录到当前文件所在目录的 ./workspace
    workspace_dir = os.environ.get("AGENT_WORKSPACE", "")
    if not workspace_dir:
        workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
        os.environ["AGENT_WORKSPACE"] = workspace_dir
    if not os.path.exists(workspace_dir):
        os.makedirs(workspace_dir, exist_ok=True)
    os.chdir(workspace_dir)

    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()

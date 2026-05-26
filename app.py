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
    workspace_dir = os.environ.get("AGENT_WORKSPACE", "")
    if not workspace_dir or not os.path.exists(workspace_dir):
        workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
        if not os.path.exists(workspace_dir):
            workspace_dir = os.path.dirname(os.path.abspath(__file__))
        os.environ["AGENT_WORKSPACE"] = workspace_dir
    os.chdir(workspace_dir)

    server = RuntimeHTTPServer(host=host, port=port)

    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()

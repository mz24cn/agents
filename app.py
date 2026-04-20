"""Entry point for the Composable Agent Runtime server.

Usage:
    python app.py              # default host/port
    python app.py 8080         # custom port
    python app.py 0.0.0.0:8080 # custom host and port
"""

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
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()

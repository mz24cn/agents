"""Build revision detection for display + delta stamping.

Deployed instances do not ship a ``.git`` directory, so the revision of the
code actually running on a machine is carried two ways:

* the parent stamps ``runtime/.build_revision`` into every push-update
  delta (the revision of the code it pushed -- see
  :meth:`runtime.env_manager.EnvManager.build_delta_tar`), and
* a dev checkout falls back to ``git rev-parse`` on the source tree.

The child advertises the result in its tunnel hello snapshot (so the parent
UI can show *which backend a remote environment is really running*) and in
its local ``GET /v1/env`` response.
"""

from __future__ import annotations

import functools
import os
import subprocess

STAMP_FILENAME = ".build_revision"
_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), STAMP_FILENAME)


@functools.lru_cache(maxsize=1)
def build_revision() -> str:
    """Return the short revision of the running code (""unknown" if none)."""
    try:
        with open(_MARKER, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"

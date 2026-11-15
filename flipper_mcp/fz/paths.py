"""Runtime locations for the fz daemon. Override the base directory with FZ_HOME."""
from __future__ import annotations

import os
import socket
from pathlib import Path

# Unix sockets where available; localhost TCP + token elsewhere (Windows).
USE_UNIX = hasattr(socket, "AF_UNIX") and os.name != "nt"


def run_dir() -> Path:
    if os.environ.get("FZ_HOME"):
        base = Path(os.environ["FZ_HOME"])
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "flipper-mcp" / "fz"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "flipper-mcp" / "fz"
    base.mkdir(parents=True, exist_ok=True)
    return base


def socket_path() -> Path:
    p = run_dir() / "fzd.sock"
    if len(str(p)) > 100:  # AF_UNIX paths are limited to ~104 bytes on macOS
        import hashlib
        import tempfile

        tag = hashlib.sha1(str(p).encode()).hexdigest()[:10]
        p = Path(tempfile.gettempdir()) / f"fzd-{tag}.sock"
    return p


def addr_file() -> Path:
    return run_dir() / "fzd.addr"


def endpoint_exists() -> bool:
    return (socket_path() if USE_UNIX else addr_file()).exists()


def font_cache() -> Path:
    p = run_dir() / "fonts"
    p.mkdir(exist_ok=True)
    return p

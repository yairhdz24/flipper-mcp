"""fz client: send one JSON request to fzd, starting the daemon if needed."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from . import paths


class FzError(Exception):
    def __init__(self, msg, gated=False):
        super().__init__(msg)
        self.gated = gated


def _connect(timeout: float):
    if paths.USE_UNIX:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(paths.socket_path()))
        return s, None
    info = json.loads(paths.addr_file().read_text())
    s = socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout)
    return s, info["token"]


def _spawn():
    kw = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    with open(paths.run_dir() / "fzd.stderr.log", "ab") as log:
        subprocess.Popen([sys.executable, "-m", "flipper_mcp.fz.daemon"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, env=os.environ.copy(), **kw)


def call(op: str, timeout: float = 120.0, **args):
    try:
        s, token = _connect(timeout)
    except (OSError, ValueError):
        _spawn()
        deadline = time.monotonic() + 10
        while True:
            try:
                s, token = _connect(timeout)
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise FzError(f"could not start fzd; see {paths.run_dir() / 'fzd.stderr.log'}")
                time.sleep(0.05)
    with s:
        req = {"op": op, "args": args}
        if token:
            req["token"] = token
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise FzError("fzd closed the connection")
    resp = json.loads(buf)
    if not resp.get("ok"):
        raise FzError(resp.get("error", "unknown error"), resp.get("gated", False))
    return resp["result"]

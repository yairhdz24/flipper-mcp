"""fzd: holds the Flipper connection with a LiveSession (screen stream always on)
and serves JSON-line requests from the `fz` CLI.

Started on demand by the client; exits after FZ_IDLE seconds (default 600) without
requests. Listens on a unix socket, or on localhost TCP with a token on Windows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import socket
import socketserver
import threading
import time
import traceback
from pathlib import Path

from ..device import FlipperDevice
from ..engine import Engine
from ..live import LiveSession, RpcError
from ..transport.base import TransportError
from . import paths

log = logging.getLogger("fzd")

# CLI commands that transmit, write, delete or reboot need --force.
GATED_CLI = re.compile(
    r"^\s*(subghz\s+(tx|tx_from_file|chat)|ir\s+(tx|universal)|rfid\s+(emulate|write)|"
    r"nfc\s+(emulate|write)|ikey\s+(emulate|write)|power\s+(off|reboot)|update|"
    r"storage\s+(remove|format|rename)|factory_reset|crypto)", re.IGNORECASE)


class Gate(Exception):
    pass


class State:
    def __init__(self):
        self.fd: FlipperDevice | None = None
        self.sess: LiveSession | None = None
        self.eng: Engine | None = None
        self.pack: str | None = None
        self.info: dict = {}
        self.last_req = time.monotonic()
        self.lock = threading.RLock()

    def ensure(self) -> Engine:
        if self.sess is not None and self.sess.mode == "rpc":
            return self.eng
        self.close()
        fd = FlipperDevice(os.environ.get("FLIPPER_TRANSPORT", "auto"),
                           os.environ.get("FLIPPER_COM_PORT") or None,
                           exclusive=os.name == "posix").connect()
        sess = LiveSession(fd.transport).start()
        self.fd, self.sess = fd, sess
        self.eng = Engine(sess, paths.font_cache())
        try:
            self.info = sess.device_info()
        except Exception:  # noqa: BLE001
            self.info = {}
        try:
            self.pack = self.eng.load_asset_pack_fonts()
        except Exception as e:  # noqa: BLE001
            log.warning("asset pack font load failed: %s", e)
        log.info("connected %s pack=%s", fd.transport.description, self.pack)
        return self.eng

    def close(self):
        try:
            if self.sess is not None:
                self.sess.stop()
        except Exception as e:  # noqa: BLE001
            log.debug("session stop failed: %s", e)
        try:
            if self.fd is not None:
                self.fd.disconnect()
        except Exception as e:  # noqa: BLE001
            log.debug("disconnect failed: %s", e)
        self.fd = self.sess = self.eng = None

    def cli(self, command: str, timeout: float, stream_for: float | None) -> str:
        """Leave RPC, run a text-CLI command, re-enter RPC."""
        self.sess.stop()
        try:
            self.fd.cli.ensure_prompt()  # the CLI needs a moment after RPC ends
            if stream_for:
                return self.fd.stream_cli(command, duration=stream_for)
            return self.fd.run_cli(command, timeout=timeout)
        finally:
            self.sess = LiveSession(self.fd.transport).start()
            self.eng.dev = self.sess
            self.eng._read_cache = None


S = State()


def _need_force(args, what):
    if not args.get("force"):
        raise Gate(f"{what} changes the device or transmits; re-run with --force")


def dispatch(op: str, a: dict):
    if op == "ping":
        return {"pong": True, "pid": os.getpid()}
    if op == "shutdown":
        threading.Thread(target=lambda: (time.sleep(0.1), os.kill(os.getpid(), signal.SIGTERM)),
                         daemon=True).start()
        return {"bye": True}
    eng = S.ensure()
    dev = eng.dev
    if op == "status":
        out = {"transport": S.fd.transport.description, "asset_pack": S.pack or "default",
               "firmware": f"{S.info.get('firmware_origin_fork', '')} "
                           f"{S.info.get('firmware_version', '')}".strip(),
               "name": S.info.get("hardware_name"), "locked": dev.locked(),
               "app_running": dev.app_running()}
        f = dev.latest()
        out["frame_age_ms"] = int((time.monotonic() - f.t) * 1000) if f else None
        if a.get("app"):
            out["loader"] = S.cli("loader info", 5.0, None)
        return out
    if op == "info":
        return {**S.info, **({"power": dev.power_info()} if a.get("power") else {})}
    if op == "screen":
        return eng.snapshot(a.get("png"), a.get("ascii", False))
    if op == "png":
        return {"png_b64": eng.png_b64()}
    if op == "keys":
        res = eng.keys(a["keys"], a.get("until"), a.get("gone"), a.get("stable"),
                       a.get("timeout", 5.0))
        return {**res, "screen": eng.snapshot(a.get("png"))}
    if op == "wait":
        res = eng.wait(a.get("until"), a.get("gone"), a.get("stable"), a.get("timeout", 5.0))
        return {**res, "screen": eng.snapshot()}
    if op == "select":
        res = eng.select(a["item"], not a.get("no_ok", False))
        return {**res, "screen": eng.snapshot()}
    if op == "goto":
        res = eng.goto(a["path"])
        return {**res, "screen": eng.snapshot()}
    if op == "back":
        res = eng.back(a.get("home", False))
        return {**res, "screen": eng.snapshot()}
    if op == "unlock":
        dev.unlock()
        return {"locked": dev.locked()}
    if op == "app_open":
        eng.launch(a["name"], a.get("args", ""))
        return {"screen": eng.snapshot()}
    if op == "app_load":
        dev.app_load_file(a["path"])
        return {"screen": eng.snapshot()}
    if op == "app_exit":
        dev.app_exit()
        return {"screen": eng.snapshot()}
    if op == "app_button":
        dev.app_button(a.get("args", ""), a.get("index", 0), a.get("action", "press_release"))
        return {"screen": eng.snapshot()}
    if op == "app_error":
        return dev.app_error()
    if op == "ls":
        return {"entries": dev.ls(a["path"], a.get("md5", False))}
    if op == "stat":
        return dev.stat(a["path"])
    if op == "get":
        data = dev.read_file(a["path"])
        if a.get("out"):
            Path(a["out"]).write_bytes(data)
            return {"bytes": len(data), "out": a["out"]}
        try:
            return {"bytes": len(data), "text": data.decode()}
        except UnicodeDecodeError:
            import base64
            return {"bytes": len(data), "b64": base64.b64encode(data).decode()}
    if op == "put":
        data = Path(a["local"]).read_bytes()
        dev.write_file(a["remote"], data)
        return {"bytes": len(data), "md5_ok": dev.md5(a["remote"]) == hashlib.md5(data).hexdigest()}
    if op == "mkdir":
        dev.mkdir(a["path"])
        return {"ok": True}
    if op == "rm":
        _need_force(a, "fs rm")
        dev.delete(a["path"], a.get("recursive", False))
        return {"ok": True}
    if op == "mv":
        dev.rename(a["old"], a["new"])
        return {"ok": True}
    if op == "md5":
        return {"md5": dev.md5(a["path"])}
    if op == "cli":
        cmd = a["command"]
        if "\n" in cmd or "\r" in cmd:
            raise ValueError("command must be one line")
        if GATED_CLI.search(cmd):
            _need_force(a, f"cli '{cmd}'")
        return {"output": S.cli(cmd, a.get("timeout", 5.0), a.get("stream_for"))}
    if op == "rec":
        return eng.record(a.get("seconds", 3.0), a["out"], a.get("keys"))
    if op == "save_frame":
        return {"path": eng.save_frame(a["name"], Path(a["dir"]))}
    raise ValueError(f"unknown op {op}")


TOKEN: str | None = None


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            S.last_req = time.monotonic()
            try:
                req = json.loads(line)
                if TOKEN is not None and req.get("token") != TOKEN:
                    raise PermissionError("bad token")
                op, args = req.get("op"), req.get("args", {})
                t0 = time.monotonic()
                with S.lock:
                    try:
                        result = dispatch(op, args)
                    except (TransportError, OSError) as e:
                        # one reconnect attempt for link failures (unplug, reboot)
                        log.warning("link error on %s: %s; reconnecting", op, e)
                        S.close()
                        result = dispatch(op, args)
                resp = {"ok": True, "result": result, "ms": int((time.monotonic() - t0) * 1000)}
            except Gate as e:
                resp = {"ok": False, "error": str(e), "gated": True}
            except RpcError as e:
                resp = {"ok": False, "error": f"device refused: {e}"}
            except Exception as e:  # noqa: BLE001
                log.error("op failed: %s", traceback.format_exc())
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            S.last_req = time.monotonic()
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


def _make_server():
    global TOKEN
    if paths.USE_UNIX:
        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        sock = paths.socket_path()
        if sock.exists():
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(str(sock))
                s.close()
                return None  # another fzd is running
            except OSError:
                sock.unlink()
        srv = UnixServer(str(sock), Handler)
        os.chmod(sock, 0o600)
        return srv

    class TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True

    TOKEN = secrets.token_hex(16)
    srv = TcpServer(("127.0.0.1", 0), Handler)
    paths.addr_file().write_text(json.dumps({"port": srv.server_address[1], "token": TOKEN}))
    return srv


def main():
    run = paths.run_dir()
    h = logging.handlers.RotatingFileHandler(run / "fzd.log", maxBytes=1_000_000, backupCount=2)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %I:%M:%S %p"))
    logging.basicConfig(level=logging.INFO, handlers=[h])
    srv = _make_server()
    if srv is None:
        log.info("another fzd is running; exiting")
        return
    (run / "fzd.pid").write_text(str(os.getpid()))
    idle = float(os.environ.get("FZ_IDLE", "600"))

    def cleanup():
        try:
            S.close()
        finally:
            for p in (paths.socket_path(), paths.addr_file()):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    def reaper():
        while True:
            time.sleep(5)
            if time.monotonic() - S.last_req > idle:
                log.info("idle %ss; exiting", idle)
                cleanup()
                os._exit(0)

    threading.Thread(target=reaper, daemon=True).start()

    def term(*_):
        cleanup()
        os._exit(0)

    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    log.info("fzd up pid=%s", os.getpid())
    srv.serve_forever()


if __name__ == "__main__":
    main()

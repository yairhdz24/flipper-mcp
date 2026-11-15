"""High-level Flipper Zero facade used by the MCP server and scripts.

Owns one transport + a text-CLI driver, and switches into a protobuf RPC session
on demand (for binary file transfer, screenshots, input injection, reboot, apps).
All access is serialized with a re-entrant lock so one operation runs at a time.
"""

from __future__ import annotations

import contextlib
import threading
from contextlib import contextmanager

from .cli import FlipperCLI
from .rpc.session import RpcSession, framebuffer_to_png
from .transport import describe_available, open_transport


def _parse_kv(text: str) -> dict:
    out = {}
    for line in text.split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


class FlipperDevice:
    def __init__(self, prefer: str = "auto", port: str | None = None, exclusive: bool = False):
        self.prefer = prefer
        self.port = port
        self.exclusive = exclusive
        self._fonts: list | None = None
        self.transport = None
        self.cli: FlipperCLI | None = None
        self._lock = threading.RLock()

    # -- connection ----------------------------------------------------------

    def connect(self) -> "FlipperDevice":
        with self._lock:
            if self.cli is not None:
                return self
            self.transport = open_transport(self.prefer, self.port, self.exclusive)
            self.cli = FlipperCLI(self.transport).open()
            return self

    def disconnect(self) -> None:
        with self._lock:
            if self.cli is not None:
                try:
                    self.cli.close()
                finally:
                    self.cli = None
                    self.transport = None

    @property
    def connected(self) -> bool:
        return self.cli is not None

    def _ensure(self) -> None:
        if self.cli is None:
            self.connect()

    def status(self) -> dict:
        avail = describe_available()
        avail["connected"] = self.connected
        if self.connected:
            avail["transport"] = self.transport.description
        return avail

    # -- text CLI ------------------------------------------------------------

    def run_cli(self, command: str, timeout: float = 5.0) -> str:
        with self._lock:
            self._ensure()
            return self.cli.command(command, timeout_s=timeout)

    def stream_cli(self, command: str, duration: float = 3.0) -> str:
        with self._lock:
            self._ensure()
            return self.cli.stream(command, duration_s=duration)

    def device_info(self) -> dict:
        return _parse_kv(self.run_cli("device_info", timeout=6.0))

    def power_info(self) -> dict:
        return _parse_kv(self.run_cli("info power"))

    def uptime(self) -> str:
        return self.run_cli("uptime").replace("Uptime:", "").strip()

    # -- RPC session context -------------------------------------------------

    @contextmanager
    def rpc(self):
        with self._lock:
            self._ensure()
            self.cli.ensure_prompt()
            sess = RpcSession(self.transport).start()
            try:
                yield sess
            finally:
                if sess.active:
                    sess.stop()
                    self.cli.ensure_prompt()

    @contextmanager
    def live(self):
        """Streaming RPC session wrapped in a navigation ``Engine`` (screen as text,
        key sequences with waits, select/goto). Returns to the text CLI on exit."""
        from .engine import Engine
        from .live import LiveSession

        with self._lock:
            self._ensure()
            self.cli.ensure_prompt()
            sess = LiveSession(self.transport).start()
            try:
                eng = Engine(sess)
                if self._fonts is None:
                    # custom asset-pack fonts are optional; fall back to stock fonts
                    with contextlib.suppress(Exception):
                        eng.load_asset_pack_fonts()
                    self._fonts = eng.extra_fonts
                eng.extra_fonts = self._fonts
                yield eng
            finally:
                sess.stop()
                self.cli.ensure_prompt()

    # -- RPC-backed operations ----------------------------------------------

    def ping(self, data: bytes = b"flipper-mcp") -> bytes:
        with self.rpc() as r:
            return r.ping(data)

    def list_dir(self, path: str = "/ext", include_md5: bool = False) -> list:
        with self.rpc() as r:
            return r.storage_list(path, include_md5=include_md5)

    def stat(self, path: str) -> dict:
        with self.rpc() as r:
            return r.storage_stat(path)

    def storage_space(self, path: str = "/ext") -> dict:
        with self.rpc() as r:
            return r.storage_info(path)

    def read_file(self, path: str) -> bytes:
        with self.rpc() as r:
            return r.storage_read(path)

    def write_file(self, path: str, content: bytes) -> str:
        import hashlib
        with self.rpc() as r:
            r.storage_write(path, content)
            dev_md5 = r.storage_md5(path)
        ok = dev_md5 == hashlib.md5(content).hexdigest()
        return dev_md5 if ok else f"{dev_md5} (WARNING: md5 mismatch)"

    def mkdir(self, path: str) -> None:
        with self.rpc() as r:
            r.storage_mkdir(path)

    def delete(self, path: str, recursive: bool = False) -> None:
        with self.rpc() as r:
            r.storage_delete(path, recursive=recursive)

    def rename(self, old: str, new: str) -> None:
        with self.rpc() as r:
            r.storage_rename(old, new)

    def md5(self, path: str) -> str:
        with self.rpc() as r:
            return r.storage_md5(path)

    def screenshot_png(self, scale: int = 4) -> bytes:
        with self.rpc() as r:
            data, orient = r.screen_frame()
        return framebuffer_to_png(data, orient, scale=scale)

    def press(self, key: str, itype: str = "SHORT") -> None:
        with self.rpc() as r:
            self._send_press(r, key, itype)

    @staticmethod
    def _send_press(rpc, key: str, itype: str) -> None:
        # A reliable tap is the full PRESS -> SHORT/LONG -> RELEASE sequence
        # (what qFlipper sends); a lone SHORT event is unreliable.
        t = itype.upper()
        if t in ("SHORT", "LONG"):
            rpc.send_input(key, "PRESS")
            rpc.send_input(key, t)
            rpc.send_input(key, "RELEASE")
        else:
            rpc.send_input(key, t)

    def reboot(self, mode: str = "OS") -> None:
        with self.rpc() as r:
            r.reboot(mode)

    def play_alert(self) -> None:
        with self.rpc() as r:
            r.play_alert()

    def app_start(self, name: str, args: str = "") -> None:
        with self.rpc() as r:
            r.app_start(name, args)

    def app_load_file(self, path: str) -> None:
        with self.rpc() as r:
            r.app_load_file(path)

    def app_exit(self) -> None:
        with self.rpc() as r:
            r.app_exit()

    def upload_and_launch(self, local_bytes: bytes, dest_path: str) -> dict:
        """Upload a .fap to the SD card and try to start it (over any transport)."""
        import hashlib
        import time
        parent = dest_path.rsplit("/", 1)[0]
        with self.rpc() as r:
            try:
                r.storage_mkdir(parent)
            except Exception:
                pass
            r.storage_write(dest_path, local_bytes)
            dev_md5 = r.storage_md5(dest_path)
            launched = True
            launch_err = None
            try:
                r.app_start(dest_path, "")
            except Exception:  # noqa: BLE001
                # The loader is blocked if a menu/app is in the foreground
                # (ERROR_APP_SYSTEM_LOCKED). Back out to the desktop and retry.
                try:
                    for _ in range(4):
                        self._send_press(r, "BACK", "SHORT")
                        time.sleep(0.15)
                    time.sleep(0.3)
                    r.app_start(dest_path, "")
                    launch_err = None
                except Exception as e2:  # noqa: BLE001
                    launched = False
                    launch_err = str(e2)
        ok = dev_md5 == hashlib.md5(local_bytes).hexdigest()
        return {"path": dest_path, "md5_ok": ok, "launched": launched, "launch_error": launch_err}

    # -- subsystem convenience (text CLI) -----------------------------------

    def led(self, channel: str, value: int) -> str:
        if channel not in ("r", "g", "b", "bl"):
            raise ValueError("channel must be r, g, b or bl")
        return self.run_cli(f"led {channel} {int(value)}")

    def vibro(self, on: bool) -> str:
        return self.run_cli(f"vibro {1 if on else 0}")

    def gpio_mode(self, pin: str, output: bool) -> str:
        return self.run_cli(f"gpio mode {pin} {1 if output else 0}")

    def gpio_set(self, pin: str, value: int) -> str:
        return self.run_cli(f"gpio set {pin} {1 if value else 0}")

    def gpio_read(self, pin: str) -> str:
        return self.run_cli(f"gpio read {pin}")

    def ir_send(self, protocol: str, address: str, command: str) -> str:
        return self.run_cli(f"ir tx {protocol} {address} {command}", timeout=6.0)

    def ir_universal(self, remote: str, signal: str) -> str:
        return self.run_cli(f"ir universal {remote} {signal}", timeout=8.0)

    def subghz_tx_from_file(self, path: str, repeat: int = 1, device: int = 0) -> str:
        return self.run_cli(f"subghz tx_from_file {path} {repeat} {device}", timeout=10.0)

    def subghz_rx(self, frequency: int, duration: float = 5.0, device: int = 0) -> str:
        return self.stream_cli(f"subghz rx {frequency} {device}", duration=duration)

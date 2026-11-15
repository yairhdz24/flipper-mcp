"""Long-lived RPC session over an already-open transport.

Unlike ``RpcSession`` (one request at a time, screen frames polled on demand),
``LiveSession`` runs a background reader thread, keeps the screen stream on and
stores frames in a ring buffer. That lets callers press a key and wait for the
screen to settle locally, without a round trip per frame.
"""
from __future__ import annotations

import collections
import contextlib
import threading
import time

import numpy as np
from google.protobuf.message import DecodeError

from .rpc.framing import encode_varint
from .rpc.proto import flipper_pb2, gui_pb2
from .screen import frame as screen
from .transport.base import Transport, TransportError

_STATUS = {v.number: v.name for v in flipper_pb2.CommandStatus.DESCRIPTOR.values}
KEYS = ("UP", "DOWN", "LEFT", "RIGHT", "OK", "BACK")


class RpcError(Exception):
    def __init__(self, status: int, ctx: str = ""):
        self.status = status
        self.name = _STATUS.get(status, str(status))
        super().__init__(f"{self.name}" + (f" ({ctx})" if ctx else ""))


class Frame:
    __slots__ = ("_arr", "data", "orientation", "seq", "t")

    def __init__(self, seq, t, data, orientation):
        self.seq, self.t, self.data, self.orientation = seq, t, data, orientation
        self._arr = None

    @property
    def arr(self) -> np.ndarray:
        if self._arr is None:
            self._arr = screen.frame_to_array(self.data, self.orientation)
        return self._arr


class LiveSession:
    def __init__(self, transport: Transport, ring: int = 256):
        self.t = transport
        self._buf = bytearray()
        self._wlock = threading.Lock()
        self._cid = 0
        self._pending: dict[int, list] = {}
        self._events: dict[int, threading.Event] = {}
        self._frames: collections.deque[Frame] = collections.deque(maxlen=ring)
        self._fcond = threading.Condition()
        self._seq = 0
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.mode = "closed"   # closed | rpc | cli
        self.error: str | None = None
        self.op_lock = threading.RLock()  # serializes high-level operations

    # ------------------------------------------------------------ lifecycle
    def start(self) -> LiveSession:
        """Switch the transport from the text CLI into RPC and start the screen stream."""
        self._enter_rpc()
        return self

    def stop(self) -> None:
        """Stop streaming and hand the transport back to the text CLI."""
        if self.mode == "rpc":
            self._leave_rpc()
        self.mode = "closed"

    def _enter_rpc(self, attempts: int = 3):
        t = self.t
        t.set_control_lines(True, True)
        for i in range(attempts):
            t.write(b"\r\n")
            self._drain_quiet(0.25, 1.5)   # eat the CLI banner / prompt
            t.write(b"start_rpc_session\r")
            self._drain_quiet(0.2, 0.6)
            self._buf.clear()
            self._stop.clear()
            self.mode = "rpc"
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            try:
                self._simple("system_ping_request", 1.5)
                self.stream_on()
                return
            except (TransportError, RpcError):
                # not in RPC yet (CLI busy or stale session); reset and retry
                self._stop.set()
                self._reader.join(1.0)
                self.mode = "cli"
                m = flipper_pb2.Main()
                m.stop_session.SetInParent()
                payload = m.SerializeToString()
                t.write(encode_varint(len(payload)) + payload)
                t.write(b"\x03")
                self._drain_quiet(0.3, 1.0)
        raise TransportError("could not start an RPC session (is another program using the Flipper?)")

    def _drain_quiet(self, quiet: float, cap: float):
        end = time.monotonic() + cap
        last = time.monotonic()
        while time.monotonic() < end:
            if self.t.read(4096, 50):
                last = time.monotonic()
            elif time.monotonic() - last >= quiet:
                return

    def _leave_rpc(self):
        with contextlib.suppress(TransportError, RpcError):
            self.stream_off()
        m = flipper_pb2.Main()
        m.stop_session.SetInParent()
        self._send(m)
        time.sleep(0.15)
        self._stop.set()
        if self._reader:
            self._reader.join(1.0)
        self._reader = None
        self._drain(0.2)
        self.mode = "cli"

    def _drain(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.t.read(4096, 50)

    # ------------------------------------------------------------ framing
    def _send(self, m):
        payload = m.SerializeToString()
        with self._wlock:
            self.t.write(encode_varint(len(payload)) + payload)

    def _read_varint(self):
        result = shift = 0
        i = 0
        while True:
            while i >= len(self._buf):
                if self._stop.is_set():
                    return None
                chunk = self.t.read(4096, 50)
                if chunk:
                    self._buf += chunk
            b = self._buf[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                del self._buf[:i]
                return result
            shift += 7

    def _read_exact(self, n):
        while len(self._buf) < n:
            if self._stop.is_set():
                return None
            chunk = self.t.read(4096, 50)
            if chunk:
                self._buf += chunk
        d = bytes(self._buf[:n])
        del self._buf[:n]
        return d

    def _read_loop(self):
        try:
            while not self._stop.is_set():
                n = self._read_varint()
                if n is None:
                    return
                data = self._read_exact(n)
                if data is None:
                    return
                m = flipper_pb2.Main()
                try:
                    m.ParseFromString(data)
                except DecodeError:
                    self.error = "rpc desync"
                    continue
                kind = m.WhichOneof("content")
                if kind == "gui_screen_frame":
                    with self._fcond:
                        self._seq += 1
                        self._frames.append(Frame(self._seq, time.monotonic(),
                                                  bytes(m.gui_screen_frame.data),
                                                  int(m.gui_screen_frame.orientation)))
                        self._fcond.notify_all()
                    continue
                cid = m.command_id
                if cid in self._pending:
                    self._pending[cid].append(m)
                    if not m.has_next or m.command_status != 0:
                        self._events[cid].set()
        except Exception as e:  # noqa: BLE001
            self.error = f"reader: {e}"
            self.mode = "closed"
            with self._fcond:
                self._fcond.notify_all()
            for ev in list(self._events.values()):
                ev.set()

    # ------------------------------------------------------------ requests
    def request(self, m, timeout: float = 6.0, ctx: str = "") -> list:
        if self.mode != "rpc":
            raise TransportError(f"device not in rpc mode ({self.mode}): {self.error or ''}")
        self._cid += 1
        cid = self._cid
        m.command_id = cid
        self._pending[cid] = []
        ev = self._events[cid] = threading.Event()
        try:
            self._send(m)
            if not ev.wait(timeout):
                raise TransportError(f"timeout waiting for {ctx or m.WhichOneof('content')}")
            out = self._pending[cid]
            for r in out:
                if r.command_status != 0:
                    raise RpcError(r.command_status, ctx or m.WhichOneof("content"))
            return out
        finally:
            self._pending.pop(cid, None)
            self._events.pop(cid, None)

    def _simple(self, field, timeout=6.0, **kw):
        m = flipper_pb2.Main()
        sub = getattr(m, field)
        sub.SetInParent()
        for k, v in kw.items():
            setattr(sub, k, v)
        return self.request(m, timeout, field)

    # ------------------------------------------------------------ screen
    def stream_on(self):
        self._simple("gui_start_screen_stream_request")

    def stream_off(self):
        self._simple("gui_stop_screen_stream_request")

    def latest(self) -> Frame | None:
        with self._fcond:
            return self._frames[-1] if self._frames else None

    def wait_frame(self, after_seq: int = 0, timeout: float = 1.0) -> Frame | None:
        """Next frame with seq > after_seq. The firmware only sends frames when the screen changes."""
        end = time.monotonic() + timeout
        with self._fcond:
            while True:
                if self._frames and self._frames[-1].seq > after_seq:
                    return self._frames[-1]
                rem = end - time.monotonic()
                if rem <= 0 or self.mode != "rpc":
                    return None
                self._fcond.wait(rem)

    def current(self) -> Frame:
        f = self.latest()
        if f is None:
            f = self.wait_frame(0, 2.0)
        if f is None:
            # stream silent (no frame since start); restart it to force a frame
            self.stream_off()
            self.stream_on()
            f = self.wait_frame(0, 2.0)
        if f is None:
            raise TransportError("no screen frame (is the Flipper locked or asleep?)")
        return f

    def frames_since(self, seq: int) -> list:
        with self._fcond:
            return [f for f in self._frames if f.seq > seq]

    # ------------------------------------------------------------ input
    def key_event(self, key: str, itype: str):
        m = flipper_pb2.Main()
        m.gui_send_input_event_request.key = gui_pb2.InputKey.Value(key)
        m.gui_send_input_event_request.type = gui_pb2.InputType.Value(itype)
        self.request(m, 3.0, f"input {key} {itype}")

    def press(self, key: str, kind: str = "short"):
        key = key.upper()
        if key not in KEYS:
            raise ValueError(f"unknown key {key}; use one of {', '.join(KEYS)}")
        kind = kind.lower()
        if kind == "short":
            seq = ("PRESS", "SHORT", "RELEASE")
        elif kind == "long":
            seq = ("PRESS", "LONG", "RELEASE")
        elif kind == "press":
            seq = ("PRESS",)
        elif kind == "release":
            seq = ("RELEASE",)
        elif kind == "repeat":
            seq = ("REPEAT",)
        else:
            raise ValueError("kind must be short|long|press|release|repeat")
        for it in seq:
            self.key_event(key, it)

    # ------------------------------------------------------------ system / apps
    def device_info(self) -> dict:
        return {r.system_device_info_response.key: r.system_device_info_response.value
                for r in self._simple("system_device_info_request", 8.0)}

    def power_info(self) -> dict:
        return {r.system_power_info_response.key: r.system_power_info_response.value
                for r in self._simple("system_power_info_request", 8.0)}

    def locked(self) -> bool:
        try:
            self._simple("desktop_is_locked_request")
            return True
        except RpcError as e:
            if e.name == "ERROR_APP_NOT_RUNNING" or e.status != 0:
                return False
            raise

    def unlock(self):
        self._simple("desktop_unlock_request")

    def app_running(self) -> bool:
        r = self._simple("app_lock_status_request")
        return bool(r[0].app_lock_status_response.locked)

    def app_start(self, name: str, args: str = ""):
        self._simple("app_start_request", 10.0, name=name, args=args)

    def app_exit(self):
        self._simple("app_exit_request")

    def app_load_file(self, path: str):
        self._simple("app_load_file_request", 10.0, path=path)

    def app_button(self, args: str = "", index: int = 0, action: str = "press_release"):
        field = {"press": "app_button_press_request", "release": "app_button_release_request",
                 "press_release": "app_button_press_release_request"}[action]
        self._simple(field, 6.0, args=args, index=index)

    def app_error(self) -> dict:
        r = self._simple("app_get_error_request")[0].app_get_error_response
        return {"code": r.code, "text": r.text}

    def property_get(self, key: str) -> dict:
        return {r.property_get_response.key: r.property_get_response.value
                for r in self._simple("property_get_request", 8.0, key=key)}

    # ------------------------------------------------------------ storage
    def ls(self, path: str, md5: bool = False) -> list:
        out = []
        for r in self._simple("storage_list_request", 15.0, path=path, include_md5=md5):
            for f in r.storage_list_response.file:
                out.append({"type": "dir" if f.type == 1 else "file", "name": f.name,
                            "size": f.size, **({"md5": f.md5sum} if md5 else {})})
        return out

    def stat(self, path: str) -> dict:
        f = self._simple("storage_stat_request", 6.0, path=path)[0].storage_stat_response.file
        return {"type": "dir" if f.type == 1 else "file", "size": f.size}

    def read_file(self, path: str) -> bytes:
        data = bytearray()
        for r in self._simple("storage_read_request", 60.0, path=path):
            data += r.storage_read_response.file.data
        return bytes(data)

    def write_file(self, path: str, content: bytes, chunk: int = 512):
        if self.mode != "rpc":
            raise TransportError("device not in rpc mode")
        self._cid += 1
        cid = self._cid
        self._pending[cid] = []
        ev = self._events[cid] = threading.Event()
        try:
            parts = [content[i:i + chunk] for i in range(0, len(content), chunk)] or [b""]
            for i, part in enumerate(parts):
                m = flipper_pb2.Main()
                m.command_id = cid
                m.has_next = i < len(parts) - 1
                m.storage_write_request.path = path
                m.storage_write_request.file.data = part
                self._send(m)
            if not ev.wait(60):
                raise TransportError("timeout writing file")
            for r in self._pending[cid]:
                if r.command_status != 0:
                    raise RpcError(r.command_status, "storage_write")
        finally:
            self._pending.pop(cid, None)
            self._events.pop(cid, None)

    def mkdir(self, path: str):
        self._simple("storage_mkdir_request", path=path)

    def delete(self, path: str, recursive: bool = False):
        self._simple("storage_delete_request", 15.0, path=path, recursive=recursive)

    def rename(self, old: str, new: str):
        self._simple("storage_rename_request", old_path=old, new_path=new)

    def md5(self, path: str) -> str:
        return self._simple("storage_md5sum_request", 30.0, path=path)[0].storage_md5sum_response.md5sum

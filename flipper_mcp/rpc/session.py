"""High-level Flipper Zero protobuf RPC session.

Enter with ``start()`` (sends ``start_rpc_session`` over the text CLI), issue
commands, then ``stop()`` to return to the text CLI. One command is in flight at
a time; responses are matched by ``command_id`` and accumulated across ``has_next``.
"""

from __future__ import annotations

import time

from ..transport.base import Transport, TransportError
from .framing import FrameReader, encode_varint
from .proto import (
    flipper_pb2,
    gui_pb2,
    system_pb2,
)

# Screen geometry.
SCREEN_W, SCREEN_H = 128, 64

_STATUS_NAMES = {v.number: v.name for v in flipper_pb2.CommandStatus.DESCRIPTOR.values}


class RpcError(Exception):
    def __init__(self, status: int, context: str = ""):
        self.status = status
        self.name = _STATUS_NAMES.get(status, str(status))
        super().__init__(f"RPC error {self.name}" + (f" ({context})" if context else ""))


class RpcSession:
    def __init__(self, transport: Transport):
        self._t = transport
        self._reader = FrameReader(transport)
        self._cid = 0
        self.active = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "RpcSession":
        self._t.set_control_lines(dtr=True, rts=True)
        # Nudge to a clean CLI prompt, then hand the stream to the RPC worker.
        self._t.write(b"\r\n")
        self._reader.discard(0.2)
        self._t.write(b"start_rpc_session\r")
        self._reader.discard(0.35)  # eat command echo; stream is protobuf after this
        self.active = True
        return self

    def stop(self) -> None:
        if not self.active:
            return
        try:
            m = flipper_pb2.Main()
            m.stop_session.SetInParent()
            self._send(m)
            self._reader.discard(0.2)
        except Exception:
            pass
        finally:
            self.active = False

    def __enter__(self) -> "RpcSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- framing -------------------------------------------------------------

    def _next_cid(self) -> int:
        self._cid += 1
        return self._cid

    def _send(self, main: flipper_pb2.Main) -> None:
        payload = main.SerializeToString()
        self._t.write(encode_varint(len(payload)) + payload)

    def _recv(self, timeout_s: float) -> flipper_pb2.Main:
        length = self._reader.read_varint(timeout_s)
        data = self._reader.read_exact(length, timeout_s)
        m = flipper_pb2.Main()
        m.ParseFromString(data)
        return m

    def _transact(self, request: flipper_pb2.Main, timeout_s: float = 6.0) -> list:
        """Send one request; collect all responses sharing its command_id."""
        cid = self._next_cid()
        request.command_id = cid
        self._send(request)
        responses = []
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = max(0.2, deadline - time.monotonic())
            m = self._recv(remaining)
            if m.command_id == 0:
                if m.command_status == flipper_pb2.CommandStatus.ERROR_DECODE:
                    self.active = False
                    raise RpcError(m.command_status, "session desync")
                continue  # async screen frame / notification
            if m.command_id != cid:
                continue
            if m.command_status != flipper_pb2.CommandStatus.OK:
                raise RpcError(m.command_status, request.WhichOneof("content"))
            responses.append(m)
            if not m.has_next:
                return responses

    # -- system --------------------------------------------------------------

    def ping(self, data: bytes = b"flipper-mcp") -> bytes:
        m = flipper_pb2.Main()
        m.system_ping_request.data = data
        resp = self._transact(m)
        return bytes(resp[0].system_ping_response.data)

    def device_info(self) -> dict:
        m = flipper_pb2.Main()
        m.system_device_info_request.SetInParent()
        info = {}
        for r in self._transact(m, timeout_s=8.0):
            info[r.system_device_info_response.key] = r.system_device_info_response.value
        return info

    def power_info(self) -> dict:
        m = flipper_pb2.Main()
        m.system_power_info_request.SetInParent()
        info = {}
        for r in self._transact(m, timeout_s=8.0):
            info[r.system_power_info_response.key] = r.system_power_info_response.value
        return info

    def reboot(self, mode: str = "OS") -> None:
        m = flipper_pb2.Main()
        m.system_reboot_request.mode = system_pb2.RebootRequest.RebootMode.Value(mode.upper())
        # The device reboots; no response is expected.
        self._send(m)
        self.active = False

    def play_alert(self) -> None:
        m = flipper_pb2.Main()
        m.system_play_audiovisual_alert_request.SetInParent()
        self._transact(m)

    # -- storage -------------------------------------------------------------

    def storage_info(self, path: str = "/ext") -> dict:
        m = flipper_pb2.Main()
        m.storage_info_request.path = path
        r = self._transact(m)[0]
        return {
            "total_space": r.storage_info_response.total_space,
            "free_space": r.storage_info_response.free_space,
        }

    def storage_stat(self, path: str) -> dict:
        m = flipper_pb2.Main()
        m.storage_stat_request.path = path
        r = self._transact(m)[0]
        f = r.storage_stat_response.file
        return {"type": "dir" if f.type == 1 else "file", "size": f.size, "name": f.name}

    def storage_list(self, path: str = "/ext", include_md5: bool = False) -> list:
        m = flipper_pb2.Main()
        m.storage_list_request.path = path
        m.storage_list_request.include_md5 = include_md5
        entries = []
        for r in self._transact(m, timeout_s=10.0):
            for f in r.storage_list_response.file:
                entries.append({
                    "type": "dir" if f.type == 1 else "file",
                    "name": f.name,
                    "size": f.size,
                    "md5": f.md5sum or None,
                })
        return entries

    def storage_read(self, path: str, timeout_s: float = 30.0) -> bytes:
        m = flipper_pb2.Main()
        m.storage_read_request.path = path
        data = bytearray()
        for r in self._transact(m, timeout_s=timeout_s):
            data += r.storage_read_response.file.data
        return bytes(data)

    def storage_write(self, path: str, content: bytes, chunk: int = 512,
                      timeout_s: float = 60.0) -> None:
        cid = self._next_cid()
        chunks = [content[i:i + chunk] for i in range(0, len(content), chunk)] or [b""]
        for i, part in enumerate(chunks):
            m = flipper_pb2.Main()
            m.command_id = cid
            m.has_next = i < len(chunks) - 1
            m.storage_write_request.path = path
            m.storage_write_request.file.data = part
            self._send(m)
        # Single ack after the final chunk.
        deadline = time.monotonic() + timeout_s
        while True:
            m = self._recv(max(0.2, deadline - time.monotonic()))
            if m.command_id != cid:
                continue
            if m.command_status != flipper_pb2.CommandStatus.OK:
                raise RpcError(m.command_status, "storage_write")
            if not m.has_next:
                return

    def storage_mkdir(self, path: str) -> None:
        m = flipper_pb2.Main()
        m.storage_mkdir_request.path = path
        self._transact(m)

    def storage_delete(self, path: str, recursive: bool = False) -> None:
        m = flipper_pb2.Main()
        m.storage_delete_request.path = path
        m.storage_delete_request.recursive = recursive
        self._transact(m)

    def storage_md5(self, path: str) -> str:
        m = flipper_pb2.Main()
        m.storage_md5sum_request.path = path
        return self._transact(m)[0].storage_md5sum_response.md5sum

    def storage_rename(self, old: str, new: str) -> None:
        m = flipper_pb2.Main()
        m.storage_rename_request.old_path = old
        m.storage_rename_request.new_path = new
        self._transact(m)

    # -- apps ----------------------------------------------------------------

    def app_start(self, name: str, args: str = "") -> None:
        m = flipper_pb2.Main()
        m.app_start_request.name = name
        m.app_start_request.args = args
        self._transact(m, timeout_s=10.0)

    def app_load_file(self, path: str) -> None:
        m = flipper_pb2.Main()
        m.app_load_file_request.path = path
        self._transact(m)

    def app_exit(self) -> None:
        m = flipper_pb2.Main()
        m.app_exit_request.SetInParent()
        self._transact(m)

    def app_button_press(self, args: str = "", index: int = 0) -> None:
        m = flipper_pb2.Main()
        m.app_button_press_request.args = args
        m.app_button_press_request.index = index
        self._transact(m)

    # -- gui -----------------------------------------------------------------

    def send_input(self, key: str, itype: str = "SHORT") -> None:
        m = flipper_pb2.Main()
        m.gui_send_input_event_request.key = gui_pb2.InputKey.Value(key.upper())
        m.gui_send_input_event_request.type = gui_pb2.InputType.Value(itype.upper())
        self._transact(m)

    def screen_frame(self, timeout_s: float = 5.0) -> tuple:
        """Capture one framebuffer: returns (1024 bytes, orientation int)."""
        start = flipper_pb2.Main()
        start.gui_start_screen_stream_request.SetInParent()
        start.command_id = self._next_cid()
        self._send(start)
        frame = None
        deadline = time.monotonic() + timeout_s
        try:
            while frame is None and time.monotonic() < deadline:
                m = self._recv(max(0.2, deadline - time.monotonic()))
                if m.WhichOneof("content") == "gui_screen_frame":
                    frame = (bytes(m.gui_screen_frame.data), int(m.gui_screen_frame.orientation))
        finally:
            stop = flipper_pb2.Main()
            stop.gui_stop_screen_stream_request.SetInParent()
            stop.command_id = self._next_cid()
            self._send(stop)
            self._reader.discard(0.2)
        if frame is None:
            raise TransportError("no screen frame received")
        return frame


def decode_framebuffer(data: bytes, orientation: int = 0):
    """Decode a 1024-byte Flipper framebuffer into a Pillow 1-bit image.

    Buffer is column-major, page-packed (SSD1306 layout):
    pixel(x, y) = (data[(y >> 3) * 128 + x] >> (y & 7)) & 1  (1 = lit/black).
    """
    from PIL import Image

    if len(data) < 1024:
        data = data + b"\x00" * (1024 - len(data))
    img = Image.new("1", (SCREEN_W, SCREEN_H), 1)  # 1 = white background
    px = img.load()
    for y in range(SCREEN_H):
        base = (y >> 3) * SCREEN_W
        shift = y & 7
        for x in range(SCREEN_W):
            if (data[base + x] >> shift) & 1:
                px[x, y] = 0  # lit pixel -> black
    if orientation == 1:      # HORIZONTAL_FLIP
        img = img.transpose(Image.ROTATE_180)
    elif orientation == 2:    # VERTICAL (e.g. Infrared universal remotes): turn clockwise
        img = img.transpose(Image.ROTATE_270)
    elif orientation == 3:    # VERTICAL_FLIP
        img = img.transpose(Image.ROTATE_90)
    return img


def framebuffer_to_png(data: bytes, orientation: int = 0, scale: int = 4) -> bytes:
    import io

    img = decode_framebuffer(data, orientation)
    if scale > 1:
        from PIL import Image
        img = img.convert("L").resize((SCREEN_W * scale, SCREEN_H * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

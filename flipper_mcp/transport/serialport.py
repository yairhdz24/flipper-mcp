"""pyserial transport over the Flipper's USB virtual COM port.

Used when the CDC interface is bound to usbser.sys and a COMx port exists
(the normal state, or after running the driver fix). Baud rate is irrelevant
for USB CDC; pyserial asserts DTR on open, which starts the Flipper CLI.
"""

from __future__ import annotations

import serial

from .base import Transport, TransportError


class SerialTransport(Transport):
    kind = "serial"

    def __init__(self, port: str, baudrate: int = 230400, exclusive: bool = False):
        self._portname = port
        try:
            # timeout=0 -> non-blocking reads; we set a per-read timeout in read().
            # exclusive=True takes an advisory lock (POSIX) so a second program
            # can't open the port and corrupt the RPC stream.
            self._ser = serial.Serial(port, baudrate=baudrate, timeout=0,
                                      write_timeout=3, dsrdtr=False,
                                      exclusive=True if exclusive else None)
        except Exception as e:  # noqa: BLE001
            raise TransportError(
                f"could not open {port}: {e}. Is qFlipper or another program holding it?"
            )
        self.set_control_lines(True, True)

    def write(self, data: bytes) -> None:
        if data:
            self._ser.write(data)

    def read(self, size: int = 4096, timeout_ms: int = 200) -> bytes:
        self._ser.timeout = max(timeout_ms, 0) / 1000.0
        try:
            return self._ser.read(size) or b""
        except serial.SerialException as e:
            raise TransportError(f"serial read failed: {e}")

    def set_control_lines(self, dtr: bool = True, rts: bool = True) -> None:
        try:
            self._ser.dtr = dtr
            self._ser.rts = rts
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    @property
    def description(self) -> str:
        return f"Serial {self._portname}"

"""Find and open a transport to a connected Flipper Zero.

Strategy (auto): prefer a real serial COM port if one exists (works with every
other tool too); otherwise fall back to the raw WinUSB/libusb transport.
"""

from __future__ import annotations

from .base import Transport, TransportError
from .serialport import SerialTransport
from .usb import FLIPPER_PID, FLIPPER_VID, UsbTransport, find_device


def find_serial_ports() -> list[str]:
    """Return COM port device names that belong to a Flipper (by VID:PID)."""
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    ports = []
    for p in list_ports.comports():
        if p.vid == FLIPPER_VID and p.pid == FLIPPER_PID:
            ports.append(p.device)
    return ports


def usb_present() -> bool:
    """True if a Flipper is reachable over raw USB (WinUSB/libusb)."""
    try:
        return find_device() is not None
    except Exception:
        return False


def describe_available() -> dict:
    """Summarize how the Flipper can currently be reached (for diagnostics)."""
    ports = find_serial_ports()
    return {
        "serial_ports": ports,
        "usb_winusb": usb_present(),
        "recommended": (
            f"serial:{ports[0]}" if ports else ("usb" if usb_present() else "none")
        ),
    }


def open_transport(prefer: str = "auto", port: str | None = None,
                   exclusive: bool = False) -> Transport:
    """Open the best available transport.

    prefer: "auto" (serial then usb), "serial", or "usb".
    port:   explicit COM port to use (implies serial).
    """
    if port:
        return SerialTransport(port, exclusive=exclusive)

    ports = find_serial_ports()
    if prefer in ("auto", "serial") and ports:
        return SerialTransport(ports[0], exclusive=exclusive)
    if prefer in ("auto", "usb") and usb_present():
        return UsbTransport()
    if prefer == "serial" and not ports:
        raise TransportError("No Flipper COM port found. Run the driver fix, or use prefer='usb'.")
    if ports:  # prefer was usb but none present -> last resort
        return SerialTransport(ports[0], exclusive=exclusive)
    raise TransportError(
        "No Flipper found: no COM port (VID:PID 0483:5740) and no WinUSB device. "
        "Check the cable, that the Flipper is on, and that qFlipper isn't holding the port."
    )

"""MCP server exposing a Flipper Zero over its serial CLI + protobuf RPC.

Run with:  python -m flipper_mcp
The device connection is opened lazily on first use, so the server starts even
when the Flipper is unplugged. Transport is auto-selected (COM port if present,
otherwise raw WinUSB). Override with env FLIPPER_TRANSPORT / FLIPPER_COM_PORT.
"""

from __future__ import annotations

import base64
import os

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from .device import FlipperDevice
from .fap import build_app, create_app, read_manifest
from .transport.base import TransportError

mcp = FastMCP(
    "flipper",
    instructions=(
        "Control a Flipper Zero over USB. Tools cover device info, battery, SD-card "
        "storage, screenshots, virtual button presses, GPIO/LED/IR/Sub-GHz subsystems, "
        "app control, and building/deploying custom apps (FAPs) with ufbt. Radio "
        "transmit tools affect real hardware and RF spectrum — only use on authorized "
        "frequencies/hardware."
    ),
)

_PREFER = os.environ.get("FLIPPER_TRANSPORT", "auto")
_PORT = os.environ.get("FLIPPER_COM_PORT") or None
_APPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")

_device: FlipperDevice | None = None


def get_device(require_connection: bool = True) -> FlipperDevice:
    global _device
    if _device is None:
        _device = FlipperDevice(prefer=_PREFER, port=_PORT)
    if require_connection and not _device.connected:
        try:
            _device.connect()
        except TransportError as e:
            raise ToolError(str(e))
    return _device


# ---------------------------------------------------------------- connection

@mcp.tool()
def flipper_status() -> dict:
    """Report how the Flipper can be reached (serial ports on macOS/Linux/Windows, plus the WinUSB fallback) and whether it is connected. Does not open a connection."""
    return get_device(require_connection=False).status()


@mcp.tool()
def flipper_connect() -> dict:
    """Open the connection to the Flipper (auto-selects a serial/COM port on any OS, or the raw WinUSB path on Windows when no port exists). Returns connection status."""
    dev = get_device(require_connection=True)
    return dev.status()


@mcp.tool()
def flipper_disconnect() -> str:
    """Close the connection to the Flipper, releasing the USB/serial handle."""
    get_device(require_connection=False).disconnect()
    return "disconnected"


# ---------------------------------------------------------------------- info

@mcp.tool()
def flipper_device_info() -> dict:
    """Get hardware/firmware details (model, name, region, firmware version, radio stack, BLE MAC, ...)."""
    return get_device().device_info()


@mcp.tool()
def flipper_power_info() -> dict:
    """Get battery/charging telemetry: charge level %, voltage, current, temperature, health, capacity."""
    return get_device().power_info()


@mcp.tool()
def flipper_uptime() -> str:
    """Get the Flipper's uptime since last boot."""
    return get_device().uptime()


@mcp.tool()
def flipper_ping() -> str:
    """Round-trip liveness check over protobuf RPC. Returns the echoed payload."""
    return get_device().ping(b"flipper-mcp-ping").decode(errors="replace")


# ----------------------------------------------------------------- raw CLI

@mcp.tool()
def flipper_cli(command: str, timeout_seconds: float = 5.0) -> str:
    """Run any Flipper CLI command and return its text output.

    Examples: "help", "info device", "top", "free", "loader list", "storage list /ext",
    "gpio read PC0", "led g 255". Streaming commands (log, subghz rx, ir rx) will be
    cut off at the timeout — use flipper_cli_stream for those instead.
    """
    return get_device().run_cli(command, timeout=timeout_seconds)


@mcp.tool()
def flipper_cli_stream(command: str, seconds: float = 3.0) -> str:
    """Run a streaming CLI command for a fixed duration, then stop it (Ctrl+C) and return what it printed.

    Use for commands that stream until interrupted: "log", "subghz rx 433920000 0", "ir rx", "input dump".
    """
    return get_device().stream_cli(command, duration=seconds)


# ------------------------------------------------------------------- storage

@mcp.tool()
def flipper_list_dir(path: str = "/ext", include_md5: bool = False) -> list:
    """List files and directories at a path (use /ext for SD card, /int for internal flash)."""
    return get_device().list_dir(path, include_md5=include_md5)


@mcp.tool()
def flipper_stat(path: str) -> dict:
    """Get type and size of a file or directory on the Flipper."""
    return get_device().stat(path)


@mcp.tool()
def flipper_storage_space(path: str = "/ext") -> dict:
    """Get total and free bytes for the /ext (SD) or /int (flash) filesystem."""
    return get_device().storage_space(path)


@mcp.tool()
def flipper_read_file(path: str, binary: bool = False) -> str:
    """Read a file from the Flipper. Text is returned decoded; set binary=true to get base64."""
    data = get_device().read_file(path)
    if binary:
        return base64.b64encode(data).decode()
    return data.decode("utf-8", errors="replace")


@mcp.tool()
def flipper_write_file(path: str, content: str, is_base64: bool = False) -> str:
    """Write content to a file on the Flipper (overwrites). Returns the device-side md5. Set is_base64=true for binary content."""
    raw = base64.b64decode(content) if is_base64 else content.encode("utf-8")
    dev = get_device()
    try:
        dev.delete(path)  # storage_write appends, so clear an existing file first
    except Exception:
        pass
    return dev.write_file(path, raw)


@mcp.tool()
def flipper_upload_file(local_path: str, dest_path: str) -> dict:
    """Upload a local file from this computer to a path on the Flipper's SD card; verifies md5."""
    if not os.path.exists(local_path):
        raise ToolError(f"local file not found: {local_path}")
    data = open(local_path, "rb").read()
    dev = get_device()
    try:
        dev.delete(dest_path)
    except Exception:
        pass
    md5 = dev.write_file(dest_path, data)
    return {"dest": dest_path, "bytes": len(data), "md5": md5}


@mcp.tool()
def flipper_mkdir(path: str) -> str:
    """Create a directory on the Flipper."""
    get_device().mkdir(path)
    return f"created {path}"


@mcp.tool()
def flipper_delete(path: str, recursive: bool = False) -> str:
    """Delete a file or directory on the Flipper. Use recursive=true for non-empty directories."""
    get_device().delete(path, recursive=recursive)
    return f"deleted {path}"


@mcp.tool()
def flipper_rename(old_path: str, new_path: str) -> str:
    """Rename or move a file/directory on the Flipper."""
    get_device().rename(old_path, new_path)
    return f"{old_path} -> {new_path}"


# --------------------------------------------------------------------- gui

@mcp.tool()
def flipper_screenshot() -> Image:
    """Capture the Flipper's current screen (128x64) as a PNG image."""
    png = get_device().screenshot_png(scale=4)
    return Image(data=png, format="png")


@mcp.tool()
def flipper_press(key: str, type: str = "SHORT") -> str:
    """Send a virtual button press. key: UP, DOWN, LEFT, RIGHT, OK, BACK. type: SHORT, LONG, PRESS, RELEASE."""
    get_device().press(key, type)
    return f"pressed {key} ({type})"


# ------------------------------------------------- screen as text + navigation
# These run the press->look loop next to the device and return the screen as text
# (read by exact matching against the firmware's own bitmap fonts), so an agent
# can do multi-step navigation in one call instead of screenshot round trips.


def _run_live(op: str, fn) -> str:
    from .fz.cli import render

    with get_device().live() as eng:
        res = dict(fn(eng))
        res.setdefault("screen", eng.snapshot())
    return render(op, res)


@mcp.tool()
def flipper_read_screen() -> str:
    """Read the Flipper screen as text ('>' marks the highlighted row). Much cheaper than flipper_screenshot; use the screenshot only for icons/graphics."""
    from .fz.cli import fmt_screen

    with get_device().live() as eng:
        return fmt_screen(eng.snapshot())


@mcp.tool()
def flipper_keys(keys: list[str], until: str | None = None, gone: str | None = None,
                 stable_ms: int | None = None, timeout: float = 5.0) -> str:
    """Press a key sequence in one call and return the resulting screen as text.

    keys: up down left right ok back; 'long:ok', 'hold:ok:2' (seconds), 'wait:0.5', 'down*3'.
    until / gone: wait until this text appears / disappears; stable_ms: wait until the screen stops changing.
    """
    return _run_live("keys", lambda e: e.keys(keys, until, gone, stable_ms, timeout))


@mcp.tool()
def flipper_select(item: str, press_ok: bool = True) -> str:
    """Highlight a named item in the current on-screen list (scrolling as needed) and press OK. Works on Momentum's carousel main menu too."""
    return _run_live("select", lambda e: e.select(item, press_ok))


@mcp.tool()
def flipper_goto(path: str) -> str:
    """Open an app and walk its menus by name in one call, e.g. 'Sub-GHz/Frequency Analyzer' or 'Infrared/Universal Remotes/TVs'. App names: `loader list`."""
    return _run_live("goto", lambda e: e.goto(path))


@mcp.tool()
def flipper_back(home: bool = False) -> str:
    """Press BACK once, or home=True to back out of every app to the desktop (stops and says so if a screen ignores BACK)."""
    return _run_live("back", lambda e: e.back(home))


@mcp.tool()
def flipper_wait(until: str | None = None, gone: str | None = None, stable_ms: int | None = None,
                 timeout: float = 10.0) -> str:
    """Wait until text appears / disappears on screen, or until the screen stops changing; returns the screen as text."""
    return _run_live("wait", lambda e: e.wait(until, gone, stable_ms, timeout))


# ------------------------------------------------------------------- system

@mcp.tool()
def flipper_reboot(mode: str = "OS") -> str:
    """Reboot the Flipper. mode: OS (normal), DFU (bootloader), UPDATE. The connection will drop."""
    get_device().reboot(mode)
    return f"rebooting ({mode})"


@mcp.tool()
def flipper_find_my_flipper() -> str:
    """Play an audiovisual alert (sound + backlight + vibro) to locate the device."""
    get_device().play_alert()
    return "alert played"


# --------------------------------------------------------------- subsystems

@mcp.tool()
def flipper_led(channel: str, value: int) -> str:
    """Set an onboard LED channel. channel: r, g, b (RGB) or bl (display backlight). value: 0-255."""
    return get_device().led(channel, value) or f"led {channel}={value}"


@mcp.tool()
def flipper_vibro(on: bool) -> str:
    """Turn the vibration motor on or off."""
    return get_device().vibro(on) or ("vibro on" if on else "vibro off")


@mcp.tool()
def flipper_gpio(action: str, pin: str, value: int = 0) -> str:
    """Control a GPIO pin. action: mode|set|read. pin: e.g. PC0, PC1, PC3, PB2, PB3, PA4, PA6, PA7. For mode/set, value 0/1."""
    dev = get_device()
    if action == "mode":
        return dev.gpio_mode(pin, bool(value))
    if action == "set":
        return dev.gpio_set(pin, value)
    if action == "read":
        return dev.gpio_read(pin)
    raise ToolError("action must be one of: mode, set, read")


@mcp.tool()
def flipper_ir_send(protocol: str, address: str, command: str) -> str:
    """Transmit an infrared signal. protocol e.g. NEC, NECext, Samsung32, RC5, RC6, SIRC. address and command are hex (e.g. 0x04, 0x08)."""
    return get_device().ir_send(protocol, address, command) or "ir sent"


@mcp.tool()
def flipper_ir_universal(remote: str, signal: str) -> str:
    """Send a signal from a built-in universal IR remote. remote: tv, ac, audio, projector. Use flipper_cli('ir universal list tv') to list signals (e.g. Power, Vol_up, Mute)."""
    return get_device().ir_universal(remote, signal) or "ir universal sent"


@mcp.tool()
def flipper_subghz_tx_file(path: str, repeat: int = 1) -> str:
    """Transmit a saved Sub-GHz capture (.sub file on the SD card). WARNING: transmits on real RF — only use legal frequencies you are authorized to transmit on."""
    return get_device().subghz_tx_from_file(path, repeat=repeat) or "subghz tx complete"


@mcp.tool()
def flipper_subghz_rx(frequency: int = 433920000, seconds: float = 5.0) -> str:
    """Listen for Sub-GHz signals on a frequency (Hz) for a few seconds and return decoded output. Common: 433920000, 315000000, 868350000."""
    return get_device().subghz_rx(frequency, duration=seconds)


# ----------------------------------------------------------------- apps

@mcp.tool()
def flipper_list_apps() -> str:
    """List apps available to the loader (installed FAPs and built-in settings apps)."""
    return get_device().run_cli("loader list", timeout=6.0)


@mcp.tool()
def flipper_app_start(name: str, args: str = "") -> str:
    """Launch an app by name (e.g. 'Sub-GHz', 'NFC', 'Storage') or by full .fap path. args is optional (e.g. a file to open)."""
    get_device().app_start(name, args)
    return f"started {name}"


@mcp.tool()
def flipper_app_exit() -> str:
    """Ask the currently running app to exit, returning to the desktop."""
    get_device().app_exit()
    return "app exit requested"


# ------------------------------------------------------- FAP development

@mcp.tool()
def fap_scaffold(appid: str) -> dict:
    """Scaffold a new Flipper app (FAP) with ufbt into the project's apps/ folder. appid must be lowercase letters/digits/underscore."""
    return create_app(_APPS_DIR, appid)


@mcp.tool()
def fap_build(appid: str) -> dict:
    """Build a FAP in apps/<appid> with ufbt. Returns build success, log tail, and the produced .fap path."""
    app_dir = os.path.join(_APPS_DIR, appid)
    if not os.path.isdir(app_dir):
        raise ToolError(f"no app folder at {app_dir}; scaffold it first with fap_scaffold")
    result = build_app(app_dir)
    tail = "\n".join((result["stdout"] + result["stderr"]).strip().split("\n")[-15:])
    return {"ok": result["ok"], "fap": result["fap"], "log_tail": tail}


@mcp.tool()
def fap_deploy(appid: str) -> dict:
    """Build a FAP and deploy it to the Flipper over USB (upload to /ext/apps/<category>/ and launch it). No COM port required."""
    app_dir = os.path.join(_APPS_DIR, appid)
    if not os.path.isdir(app_dir):
        raise ToolError(f"no app folder at {app_dir}; scaffold it first with fap_scaffold")
    build = build_app(app_dir)
    if not build["ok"] or not build["fap"]:
        tail = "\n".join((build["stdout"] + build["stderr"]).strip().split("\n")[-15:])
        raise ToolError(f"build failed:\n{tail}")
    manifest = build["manifest"] or read_manifest(app_dir)
    category = manifest.get("fap_category", "Examples")
    dest = f"/ext/apps/{category}/{appid}.fap"
    data = open(build["fap"], "rb").read()
    result = get_device().upload_and_launch(data, dest)
    result["fap_bytes"] = len(data)
    return result


def main() -> None:
    """Console-script / module entry point: run the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

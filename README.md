# flipper-mcp 🐬

> Control your **Flipper Zero** from **Claude** (or any MCP client) over USB —
> **no COM port needed**. Screenshots, RF / NFC / IR / GPIO, and **build & deploy
> your own apps (FAPs)** — all over USB. Works on **macOS, Linux, and Windows**.

[![PyPI](https://img.shields.io/pypi/v/flipper-mcp)](https://pypi.org/project/flipper-mcp/)
![Python](https://img.shields.io/pypi/pyversions/flipper-mcp)
[![License: MIT](https://img.shields.io/github/license/yairhdz24/flipper-mcp)](LICENSE)
[![CI](https://github.com/yairhdz24/flipper-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/yairhdz24/flipper-mcp/actions/workflows/ci.yml)

![flipper-mcp demo](assets/demo.gif)

---

## Why it's different

Most Flipper MCP servers just wrap the serial CLI. `flipper-mcp` goes further:

- 🔌 **Works with no COM port.** On Windows, when the USB serial interface is bound to
  WinUSB (Google's ADB driver) and **no `COMx` exists**, it talks to the Flipper
  **directly over the raw USB bulk endpoints** via `pyusb`/`libusb` — no admin, no
  driver change. On macOS/Linux it uses the native serial device automatically.
  Transport is **auto-selected**: real serial port first, raw USB fallback.
- 🛠️ **Build & deploy your own apps (FAPs) over USB.** Scaffold → build with `ufbt`
  → **upload to the SD card and launch** — all over USB, no COM port required.
- 📸 **Screenshots + reliable virtual buttons.** Capture the 128×64 framebuffer as a
  PNG, and send `PRESS → SHORT/LONG → RELEASE` button sequences that actually register.
- 📝 **Screen as text + navigation by name.** `flipper_read_screen` reads the display
  as text by exact matching against the firmware's own bitmap fonts (`>` marks the
  highlighted row). `flipper_goto("Sub-GHz/Frequency Analyzer")`, `flipper_select`,
  and `flipper_keys(..., until="Read")` run the press→look loop next to the device,
  so multi-step navigation is one tool call instead of screenshot round trips. Also
  available as a shell CLI, `fz` ([`docs/fz.md`](docs/fz.md)).
- 🧰 **41 tools** covering device info, power telemetry, SD-card storage (md5-verified
  binary transfer), Sub-GHz, NFC, RFID, Infrared (+ universal remotes), GPIO, I²C,
  iButton, LED, vibro, app control, and a raw CLI passthrough for everything else.

Built and verified live against a real Flipper Zero (firmware 1.4.3) on macOS and Windows.

## Install (30 seconds)

```bash
# Run instantly, no install (recommended, once published to PyPI):
uvx flipper-mcp

# or with pip:
pip install flipper-mcp
```

## Register with Claude Code

After `pip install flipper-mcp` (or using `uvx`), register the server once at user scope.

**macOS / Linux:**

```bash
claude mcp add --transport stdio --scope user \
  --env FLIPPER_TRANSPORT=auto \
  flipper -- flipper-mcp
```

**Windows (PowerShell):**

```powershell
claude mcp add --transport stdio --scope user `
  --env FLIPPER_TRANSPORT=auto `
  flipper -- flipper-mcp
```

Then check `/mcp` inside Claude Code. Environment overrides:

| Variable | Values | Meaning |
|---|---|---|
| `FLIPPER_TRANSPORT` | `auto` (default), `serial`, `usb` | Force a transport, or auto-select serial → raw USB |
| `FLIPPER_COM_PORT` | e.g. `COM5`, `/dev/tty.usbmodemflip_xxxx` | Force a specific serial port |

> **macOS/Linux:** the Flipper appears automatically as `/dev/tty.usbmodemflip_*`
> (macOS) or `/dev/ttyACM*` (Linux). Close **qFlipper** first — only one program can
> hold the port at a time.
> **Windows:** if no `COMx` exists, the raw WinUSB transport is used automatically.
> See [`docs/setup.md`](docs/setup.md) for the full per-OS guide.

## Tools (41)

| Domain | Tools |
|---|---|
| **Connection** | `flipper_status`, `flipper_connect`, `flipper_disconnect` |
| **System / info** | `flipper_device_info`, `flipper_power_info`, `flipper_uptime`, `flipper_ping`, `flipper_reboot`, `flipper_find_my_flipper` |
| **CLI** | `flipper_cli`, `flipper_cli_stream` |
| **Storage (SD / flash)** | `flipper_list_dir`, `flipper_stat`, `flipper_storage_space`, `flipper_read_file`, `flipper_write_file`, `flipper_upload_file`, `flipper_mkdir`, `flipper_delete`, `flipper_rename` |
| **GUI** | `flipper_screenshot`, `flipper_press` |
| **Screen as text / navigation** | `flipper_read_screen`, `flipper_keys`, `flipper_select`, `flipper_goto`, `flipper_back`, `flipper_wait` |
| **Radio / subsystems** | `flipper_led`, `flipper_vibro`, `flipper_gpio`, `flipper_ir_send`, `flipper_ir_universal`, `flipper_subghz_tx_file`, `flipper_subghz_rx` |
| **Apps** | `flipper_list_apps`, `flipper_app_start`, `flipper_app_exit` |
| **FAP development** | `fap_scaffold`, `fap_build`, `fap_deploy` |

## Build your own app (FAP)

```
fap_scaffold("my_app")   # ufbt create -> apps/my_app/
# edit apps/my_app/my_app.c + application.fam
fap_deploy("my_app")     # ufbt build -> upload to /ext/apps/<category>/ -> launch
```

`ufbt` (the build) needs no device; deploying goes over USB via the protobuf RPC
storage upload, so **no COM port is required**. A worked example lives in
[`examples/mcp_demo/`](examples/mcp_demo/) — a small counter app built and launched
on-device during development.

## How it works

`flipper-mcp` drives the Flipper's text **serial CLI** and its **protobuf RPC** over a
single USB byte stream. It **auto-selects** the transport (real serial port → raw
WinUSB), decodes the column-major framebuffer into PNGs, and chains length-delimited
RPC frames for binary-safe, md5-verified file transfer. Verified protocol notes live in
[`docs/research/`](docs/research/).

## Safety & legality

⚠️ Radio-transmit tools (`flipper_subghz_tx_file`, `flipper_ir_send`, `flipper_ir_universal`)
affect real RF hardware and spectrum. **Only transmit on frequencies and hardware you
are legally authorized to use.** You are responsible for complying with your local
regulations.

## Author

Made by **[Yair Hernández (@yairhdz24)](https://github.com/yairhdz24)**.

Licensed under the [MIT License](LICENSE) © 2026 Yair Hernández.

Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md).

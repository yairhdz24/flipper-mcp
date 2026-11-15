# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Screen as text**: `flipper_mcp/screen/` decodes the firmware's u8g2 fonts and reads
  the 128×64 frame by exact glyph matching (normal + inverted text, highlighted row,
  scrollbar). Momentum asset-pack fonts are picked up automatically.
- **Navigation tools** (6): `flipper_read_screen`, `flipper_keys` (sequences with
  `until`/`gone`/`stable_ms` waits), `flipper_select`, `flipper_goto`, `flipper_back`,
  `flipper_wait`. They run on `LiveSession`, a streaming RPC session with a background
  reader and a frame ring buffer, so waits happen next to the device.
- **`fz` CLI + `fzd` daemon** for shell-driven agents: holds the connection between
  commands, starts on demand, exits when idle. Transmit/write/delete commands need `--force`.
- Optional exclusive serial-port lock (`SerialTransport(exclusive=True)`), used by `fzd` on POSIX.

### Fixed
- VERTICAL / VERTICAL_FLIP screenshots were rotated the wrong way (upside down).

## [0.1.0] - 2026-07-19 — Initial public release

### Added
- MCP server exposing **35 tools** to control a Flipper Zero over USB (FastMCP).
- **Auto transport selection**: native serial port (macOS/Linux/Windows) with a raw
  **WinUSB/libusb** fallback so it works on Windows even when no `COMx` exists.
- Device info, power/battery telemetry, uptime, ping.
- SD-card storage: list, stat, read, write, upload, mkdir, delete, rename, free space —
  binary-safe chunked transfer over protobuf RPC, md5-verified.
- **Screenshots** (128×64 framebuffer → PNG) and reliable **virtual button presses**.
- Subsystems via CLI: Sub-GHz, NFC, RFID, Infrared (+ universal remotes), GPIO, LED,
  vibro, plus a raw CLI passthrough.
- App control (start/exit/list) and **FAP development**: `fap_scaffold` → `fap_build`
  → `fap_deploy` (build with `ufbt`, upload over USB, launch) — no COM port required.
- Cross-platform verification script and a demo captured from a real device.
- PyPI packaging, CI matrix (macOS/Linux/Windows × Python 3.10–3.13), MIT license.

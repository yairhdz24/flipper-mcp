# Screen as text, navigation, and the `fz` CLI

Agents driving a Flipper with screenshots spend a full model turn per step: press,
screenshot, look, decide. The Flipper itself answers in milliseconds. These tools
move the press→look loop next to the device and return the screen as **text**.

## MCP tools

| Tool | What it does |
|---|---|
| `flipper_read_screen` | Screen as text; `>` marks the highlighted row |
| `flipper_keys(keys, until, gone, stable_ms)` | Key sequence in one call, then wait for a condition |
| `flipper_select(item)` | Highlight a named list item (scrolls), press OK |
| `flipper_goto("App/Item/Sub item")` | Start the app over RPC, then `select` each level |
| `flipper_back(home)` | BACK once, or out of every app to the desktop |
| `flipper_wait(until, gone, stable_ms)` | Wait for text / stillness |

Key syntax: `up down left right ok back`, `long:ok`, `hold:ok:2` (seconds), `wait:0.5`, `down*3`.

```
flipper_goto("System")
→ steps: System
  --- screen ---
  > Hand Orient     Righty
    Units      Metric
    Time Format     24h
    Date Format   D/M/Y
  [scroll 0.00]
```

Each MCP call enters RPC, does its work and returns to the text CLI (about +0.5 s).
The `fz` daemon below keeps the session open between calls.

## `fz` CLI (for shell-driven agents)

`fz` talks to `fzd`, a daemon that holds the connection, keeps the screen stream on,
and runs the same engine. It starts on the first call and exits after `FZ_IDLE`
seconds (default 600) without requests.

```
fz screen [--png out.png] [--ascii]
fz goto "Sub-GHz/Frequency Analyzer"
fz select "Saved Remotes" [--no-ok]
fz keys down down ok --until "Saved" [--gone TEXT] [--stable MS] [--timeout S]
fz back [--home]
fz wait --until TEXT
fz app open NAME [ARGS] | app load PATH | app exit | app button | app error
fz fs ls|stat|get|put|mkdir|mv|md5 ... ; fz fs rm PATH --force
fz cli "loader list" [--for SECONDS] [--force]
fz status [--app] | fz info [--power] | fz unlock
fz rec 3 out.gif --keys ok down          # record frames
fz save-frame NAME DIR                   # raw frame (.npy + .png) for tests
fz daemon stop|ping|log
fz --json <command>
```

Exit codes: 0 ok, 1 wait/navigation not met (screen still printed), 2 error,
3 refused without `--force`. `fz fs rm` and transmit / write / delete / reboot CLI
commands (`subghz tx`, `ir tx|universal`, `nfc|rfid|ikey emulate|write`,
`storage remove|format|rename`, `power off|reboot`, `update`, `factory_reset`) need `--force`.

Runtime files: `~/.cache/flipper-mcp/fz` (`%LOCALAPPDATA%\flipper-mcp\fz` on Windows,
or `FZ_HOME`): `fzd.log` (rotating 1 MB × 3), `fzd.stderr.log`, the unix socket
(`fzd.sock`, mode 600), or `fzd.addr` (localhost port + token) on Windows.
`FLIPPER_TRANSPORT` / `FLIPPER_COM_PORT` work as for the MCP server. On POSIX `fzd`
locks the serial port so another program can't open it at the same time.

Measured on Momentum mntm-012 (macOS): daemon start + connect ≈ 1.4 s (first call
only); keys + settle + read 0.1–0.9 s; `goto` to an item 5 rows down ≈ 1.5 s;
text-CLI command ≈ 1.8 s (leaves and re-enters RPC); screen read ≈ 40 ms.

## How the screen is read (`flipper_mcp/screen/`)

1. **Fonts.** The firmware draws text with u8g2 bitmap fonts. `u8g2.py` is a port of
   u8g2's glyph decoder. The stock fonts (`FontPrimary` = helvB08, `FontSecondary` =
   haxrcorp4089, `FontKeyboard` = profont11, `FontBigNumbers` = profont22, battery =
   5x7) ship in `screen/fonts/`, extracted from the firmware's `lib/u8g2/u8g2_fonts.c`
   with `scripts/extract_fonts.py`. If a Momentum asset pack is active
   (`/int/.momentum_settings.txt` → `asset_pack:`), its `Fonts/*.u8f` are downloaded and
   tried first. `FZ_FONTS=all` adds five more common u8g2 fonts.
2. **Exact matching.** Every glyph is matched pixel-exactly against the frame, normal
   and inverted. Glyphs whose ink is one solid rectangle (`l I | - _ .`) must also have
   background on all four sides, or they would match any filled area.
3. **Chaining.** Matches sharing a font and baseline are chained by each glyph's pen
   advance (gaps of one space allowed); the best chain per baseline wins.
4. **Verification.** Each glyph's cell, over the font's full line height, is compared
   with a re-render of the chain; mismatching glyphs split the chain.
5. **Clean-up.** Overlapping candidates are resolved by ink coverage. Lone letters are
   kept only next to a real word in the same font and baseline (artwork produces lone
   `L`/`I` hits). `I` and `l` share one bitmap in the stock fonts and are picked by context.
6. **Structure.** Inverted text inside a black box ≥ 48 px wide = the highlighted row.
   A dotted track + solid thumb on the right edge = the scrollbar position.

Icons and image-asset labels (e.g. universal-remote button captions) are not text;
use `flipper_screenshot` / `fz screen --png` for those. Pixels not explained by text
are reported as `graphics Npx`.

## Navigation details (`flipper_mcp/engine.py`)

- **settle**: after each key, wait for the next frame (the firmware sends one per
  redraw), then until the content stops changing (150 ms, 1.5 s cap).
- **select**: find the target row by name, press UP/DOWN the exact count, re-read,
  confirm the highlight, press OK. If the item isn't visible, scroll and detect
  wrap-around. Momentum's carousel main menu has no highlighted row; there it presses
  RIGHT until the title matches.
- **goto**: `app_start` over RPC, wait until the app's first screen is drawn (screen
  text changes), then `select` per level. App names come from `loader list`.
- **back --home**: BACK until no app runs; stops with an error if a screen ignores BACK
  (some confirmation dialogs do; in Flipper dialogs LEFT/RIGHT are the two buttons).

## Tests

`tests/test_screen_text.py` runs without a device: synthetic menus rendered with the
real fonts, golden frames captured on hardware (`tests/frames/*.npy` + expected
`.txt`), and `select`/`goto` against a simulated device (`tests/sim.py`). To add a
golden frame: `fz save-frame NAME tests/frames`, check the PNG, write `NAME.txt`.

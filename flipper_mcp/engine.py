"""Navigation engine on top of a LiveSession: read the screen as text, wait for
conditions, key sequences, select/goto by name, back out, record. Runs next to the
device (in the MCP server or the fz daemon) so press->look loops cost no model turns.
"""
from __future__ import annotations

import base64
import io
import itertools
import re
import time
from pathlib import Path

import numpy as np

from .live import LiveSession, RpcError
from .screen import frame as screen
from .screen import reader

DEFAULT_SETTLE_MS = 150
DEFAULT_SETTLE_CAP = 1.5


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class Engine:
    def __init__(self, dev: LiveSession, font_dir: Path | None = None):
        self.dev = dev
        self.font_dir = font_dir
        self.extra_fonts: list[str] = []
        self._read_cache: tuple[int, reader.Reading] | None = None

    # ------------------------------------------------------------ setup
    def load_asset_pack_fonts(self) -> str | None:
        """If a Momentum asset pack with custom fonts is active, fetch its .u8f files."""
        try:
            txt = self.dev.read_file("/int/.momentum_settings.txt").decode(errors="replace")
        except RpcError:
            return None
        m = re.search(r"^asset_pack:\s*(.+)$", txt, re.MULTILINE)
        pack = m.group(1).strip() if m else ""
        if not pack:
            return None
        base = f"/ext/asset_packs/{pack}/Fonts"
        try:
            files = [f for f in self.dev.ls(base) if f["name"].endswith(".u8f")]
        except RpcError:
            return pack
        from .fz.paths import font_cache

        dest = (self.font_dir or font_cache()) / pack
        dest.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in files:
            p = dest / f["name"]
            if not p.exists() or p.stat().st_size != f["size"]:
                p.write_bytes(self.dev.read_file(f"{base}/{f['name']}"))
            paths.append(str(p))
        self.extra_fonts = paths
        return pack

    # ------------------------------------------------------------ reading
    def read_frame(self, f) -> reader.Reading:
        if self._read_cache and self._read_cache[0] == f.seq:
            return self._read_cache[1]
        r = reader.read(f.arr, self.extra_fonts or None)
        self._read_cache = (f.seq, r)
        return r

    def snapshot(self, png: str | None = None, ascii_art: bool = False) -> dict:
        f = self.dev.current()
        r = self.read_frame(f)
        out = {"seq": f.seq, "age_ms": int((time.monotonic() - f.t) * 1000), **r.as_dict(),
               "text": r.text()}
        if png:
            Path(png).write_bytes(screen.array_to_png(f.arr))
            out["png"] = png
        if ascii_art:
            out["ascii"] = screen.array_to_ascii(f.arr)
        return out

    def png_b64(self) -> str:
        return base64.b64encode(screen.array_to_png(self.dev.current().arr)).decode()

    # ------------------------------------------------------------ waiting
    def settle(self, after_seq: int, first_ms: int = 300, stable_ms: int = DEFAULT_SETTLE_MS,
               cap_s: float = DEFAULT_SETTLE_CAP):
        """Wait for the screen to react to an input and then stop changing."""
        dev = self.dev
        f = dev.wait_frame(after_seq, first_ms / 1000)
        if f is None:
            return dev.current()
        end = time.monotonic() + cap_s
        last_arr, last_change = f.arr, time.monotonic()
        seq = f.seq
        while time.monotonic() < end:
            nf = dev.wait_frame(seq, stable_ms / 1000)
            if nf is None:
                break
            seq = nf.seq
            if screen.changed(nf.arr, last_arr):
                last_arr, last_change = nf.arr, time.monotonic()
            elif (time.monotonic() - last_change) * 1000 >= stable_ms:
                break
        return dev.latest()

    def wait(self, until_text: str | None = None, gone_text: str | None = None,
             stable_ms: int | None = None, timeout: float = 5.0) -> dict:
        dev = self.dev
        start = time.monotonic()
        end = start + timeout
        f = dev.current()
        last_arr, last_change = f.arr, time.monotonic()
        while True:
            r = self.read_frame(f)
            txt = norm(r.all_text())
            ok = True
            if until_text is not None and norm(until_text) not in txt:
                ok = False
            if gone_text is not None and norm(gone_text) in txt:
                ok = False
            if stable_ms is not None and (time.monotonic() - last_change) * 1000 < stable_ms:
                ok = False
            if ok:
                return {"met": True, "waited_ms": int((time.monotonic() - start) * 1000)}
            rem = end - time.monotonic()
            if rem <= 0:
                return {"met": False, "waited_ms": int((time.monotonic() - start) * 1000)}
            nf = dev.wait_frame(f.seq, min(rem, (stable_ms or 100) / 1000))
            if nf is not None:
                if screen.changed(nf.arr, last_arr):
                    last_arr, last_change = nf.arr, time.monotonic()
                f = nf

    # ------------------------------------------------------------ keys
    def keys(self, seq: list[str], until_text=None, gone_text=None, stable_ms=None,
             timeout: float = 5.0, settle: bool = True) -> dict:
        """seq items: 'down', 'ok', 'long:back', 'hold:ok:1.5', 'wait:0.3', 'down*3'."""
        pressed = 0
        for raw in seq:
            item = raw.strip().lower()
            if not item:
                continue
            rep = 1
            m = re.match(r"^(.*)\*(\d+)$", item)
            if m:
                item, rep = m.group(1), int(m.group(2))
            for _ in range(rep):
                if item.startswith("wait:"):
                    time.sleep(float(item.split(":", 1)[1]))
                    continue
                before = (self.dev.latest().seq if self.dev.latest() else 0)
                parts = item.split(":")
                if parts[0] == "hold":
                    key, secs = parts[1], float(parts[2]) if len(parts) > 2 else 1.0
                    self.dev.press(key, "press")
                    time.sleep(secs)
                    self.dev.press(key, "release")
                elif len(parts) == 2:
                    self.dev.press(parts[1], parts[0])
                else:
                    self.dev.press(parts[0], "short")
                pressed += 1
                if settle:
                    self.settle(before)
        out = {"pressed": pressed}
        if until_text or gone_text or stable_ms:
            out["wait"] = self.wait(until_text, gone_text, stable_ms, timeout)
        return out

    # ------------------------------------------------------------ navigation
    @staticmethod
    def _menu_rows(r: reader.Reading):
        """Rows as (label, is_selected). Label = leftmost text item of the row."""
        rows = []
        for line in r.lines:
            label = line[0].text
            rows.append((label, any(i.inverted for i in line), reader.join_line(line)))
        return rows

    @staticmethod
    def _find(rows, target):
        t = norm(target)
        for i, (label, _, full) in enumerate(rows):
            if norm(label) == t:
                return i
        for i, (label, _, full) in enumerate(rows):
            if norm(full).startswith(t) or norm(label).startswith(t):
                return i
        for i, (label, _, full) in enumerate(rows):
            if t in norm(full):
                return i
        return None

    def select(self, target: str, press_ok: bool = True, max_scroll: int = 60) -> dict:
        """Move the highlight to `target` in the current list, then press OK."""
        dev = self.dev
        presses = 0
        seen = set()
        direction = "down"
        for _ in range(max_scroll + 8):
            f = dev.current()
            r = self.read_frame(f)
            rows = self._menu_rows(r)
            sel = next((i for i, row in enumerate(rows) if row[1]), None)
            idx = self._find(rows, target)
            if sel is None:
                return self._select_by_title(target, press_ok, presses, max_scroll)
            if idx is not None and idx == sel:
                if press_ok:
                    before = f.seq
                    dev.press("OK")
                    presses += 1
                    self.settle(before)
                return {"ok": True, "presses": presses, "selected": rows[sel][2]}
            if idx is not None:
                key = "DOWN" if idx > sel else "UP"
                steps = abs(idx - sel)
            else:
                # not visible: scroll; detect wrap-around by screen identity
                h = hash(f.arr.tobytes())
                if h in seen:
                    if direction == "down":
                        direction = "up"
                        seen.clear()
                    else:
                        return {"ok": False, "error": f"'{target}' not found in list",
                                "presses": presses, "screen": r.text()}
                seen.add(h)
                key, steps = direction.upper(), 1
            for _ in range(steps):
                before = dev.latest().seq
                dev.press(key)
                presses += 1
                self.settle(before, first_ms=250, stable_ms=60, cap_s=0.6)
            if presses > max_scroll:
                break
        return {"ok": False, "error": "gave up", "presses": presses}

    def _select_by_title(self, target, press_ok, presses, max_steps):
        """Carousel menus (Momentum main menu): the selected entry's name is the top line.
        Press RIGHT until the title matches."""
        dev = self.dev
        t = norm(target)
        seen = set()
        for _ in range(max_steps):
            f = dev.current()
            r = self.read_frame(f)
            title = norm(reader.join_line(r.lines[0])) if r.lines else ""
            if title == t or (title and title.startswith(t)):
                if press_ok:
                    before = f.seq
                    dev.press("OK")
                    presses += 1
                    self.settle(before)
                return {"ok": True, "presses": presses, "selected": title, "mode": "title"}
            if title in seen:
                return {"ok": False, "error": f"'{target}' not found (no highlighted list; cycled titles)",
                        "presses": presses, "screen": r.text()}
            seen.add(title)
            before = f.seq
            dev.press("RIGHT")
            presses += 1
            self.settle(before, first_ms=250, stable_ms=80, cap_s=0.8)
        return {"ok": False, "error": "gave up", "presses": presses}

    def launch(self, name: str, args: str = "", timeout: float = 5.0):
        """Start an app and wait until its first screen is drawn (text differs from before)."""
        dev = self.dev
        f0 = dev.current()
        before_text = norm(self.read_frame(f0).all_text())
        before_png = f0.arr
        dev.app_start(name, args)
        end = time.monotonic() + timeout
        seq = f0.seq
        while time.monotonic() < end:
            f = dev.wait_frame(seq, 0.25)
            if f is None:
                f = dev.current()
            seq = f.seq
            r = self.read_frame(f)
            if norm(r.all_text()) != before_text or (not before_text and screen.changed(f.arr, before_png) > 200
                                                    and dev.app_running()):
                break
        return self.settle(seq, first_ms=200, stable_ms=200, cap_s=2.0)

    def goto(self, path: str) -> dict:
        """'App/Item/Sub item': open App over RPC, then select each item."""
        parts = [p for p in path.split("/") if p.strip()]
        if not parts:
            return {"ok": False, "error": "empty path"}
        steps = []
        try:
            self.launch(parts[0])
        except RpcError as e:
            return {"ok": False, "error": f"app_start {parts[0]!r}: {e.name} (see `fz cli 'loader list'`)",
                    "steps": steps}
        steps.append({"open": parts[0]})
        for p in parts[1:]:
            res = self.select(p)
            steps.append({"select": p, **{k: v for k, v in res.items() if k != "screen"}})
            if not res.get("ok"):
                return {"ok": False, "error": res.get("error"), "steps": steps,
                        "screen": res.get("screen")}
        return {"ok": True, "steps": steps}

    def back(self, home: bool = False, max_presses: int = 12) -> dict:
        dev = self.dev
        presses = 0
        if not home:
            before = dev.latest().seq
            dev.press("BACK")
            self.settle(before)
            return {"presses": 1}
        ignored = 0
        for _ in range(max_presses):
            if not dev.app_running():
                break
            f = dev.current()
            txt = norm(self.read_frame(f).all_text())
            dev.press("BACK")
            presses += 1
            f2 = self.settle(f.seq, stable_ms=200)
            if norm(self.read_frame(f2).all_text()) == txt and not screen.changed(f2.arr, f.arr):
                ignored += 1
                if ignored >= 2:
                    return {"ok": False, "presses": presses, "app_running": True,
                            "error": "this screen ignores BACK; use its own buttons (read the screen)"}
            else:
                ignored = 0
        if dev.app_running():
            return {"ok": False, "presses": presses, "app_running": True, "error": "app still running"}
        # Momentum's carousel main menu is not an app; one more BACK reaches the desktop
        r = self.read_frame(dev.current())
        if any(norm(reader.join_line(l)) == "start" for l in r.lines):
            before = dev.latest().seq
            dev.press("BACK")
            presses += 1
            self.settle(before, stable_ms=200)
        return {"ok": True, "presses": presses, "app_running": False}

    # ------------------------------------------------------------ recording
    def record(self, seconds: float, out: str, keys: list[str] | None = None, scale: int = 3) -> dict:
        from PIL import Image

        start_seq = self.dev.current().seq - 1
        t0 = time.monotonic()
        if keys:
            self.keys(keys, settle=False)
        rem = seconds - (time.monotonic() - t0)
        if rem > 0:
            time.sleep(rem)
        frames = self.dev.frames_since(start_seq)
        imgs, durs, last = [], [], None
        for i, f in enumerate(frames):
            if last is not None and not screen.changed(f.arr, last.arr):
                continue
            imgs.append(Image.open(io.BytesIO(screen.array_to_png(f.arr, scale))).convert("P"))
            durs.append(f.t)
            last = f
        if not imgs:
            return {"frames": 0}
        ms = [max(20, int((b - a) * 1000)) for a, b in itertools.pairwise(durs)] + [500]
        if out.endswith(".gif"):
            imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=ms, loop=0)
        else:
            w, h = imgs[0].size
            strip = Image.new("RGB", (w, (h + 4) * len(imgs)), "white")
            for i, im in enumerate(imgs):
                strip.paste(im.convert("RGB"), (0, i * (h + 4)))
            strip.save(out)
        return {"frames": len(imgs), "out": out}

    def save_frame(self, name: str, directory: Path) -> str:
        f = self.dev.current()
        directory.mkdir(parents=True, exist_ok=True)
        p = directory / f"{name}.npy"
        np.save(p, f.arr)
        (directory / f"{name}.png").write_bytes(screen.array_to_png(f.arr))
        return str(p)

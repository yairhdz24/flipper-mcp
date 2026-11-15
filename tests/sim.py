"""Simulated Flipper for offline tests: renders a stock-style submenu with the real fonts."""
import threading
import time

import numpy as np

from flipper_mcp.live import Frame
from flipper_mcp.screen import u8g2

FONT = "haxrcorp4089_tr"


def draw_text(arr, font, text, x, base, color=1):
    f = u8g2.load(font); pen = x
    for ch in text:
        g = f.glyphs[ch]
        if g.w and g.h:
            t, l = base - (g.h + g.y), pen + g.x
            sub = arr[t:t + g.h, l:l + g.w]
            sub[g.bitmap == 1] = color
        pen += g.adv


class Menu:
    def __init__(self, title, items, children=None):
        self.title, self.items, self.children = title, items, children or {}
        self.sel, self.top = 0, 0

    def render(self):
        a = np.zeros((64, 128), np.uint8)
        draw_text(a, "helvB08_tr", self.title, 2, 9)
        vis = 4
        for row in range(vis):
            i = self.top + row
            if i >= len(self.items):
                break
            base = 22 + row * 12
            if i == self.sel:
                a[base - 9:base + 3, 0:122] = 1
                draw_text(a, FONT, self.items[i], 4, base, 0)
            else:
                draw_text(a, FONT, self.items[i], 4, base, 1)
        # scrollbar: dotted track + thumb
        for y in range(12, 64, 2):
            a[y, 125] = 1
        n = len(self.items)
        th = max(3, 52 * vis // max(n, vis))
        ty = 12 + (52 - th) * self.sel // max(n - 1, 1)
        a[ty:ty + th, 124:127] = 1
        return a

    def key(self, k):
        n = len(self.items)
        if k == "DOWN":
            self.sel = (self.sel + 1) % n
        elif k == "UP":
            self.sel = (self.sel - 1) % n
        self.top = min(self.top, self.sel)
        if self.sel >= self.top + 4:
            self.top = self.sel - 3
        if self.sel == 0:
            self.top = 0


class SimDevice:
    def __init__(self, apps):
        self.apps = apps
        self.stack = []
        self.home = Menu("Desktop", ["(idle)"])
        self._seq = 0
        self._frames = []
        self._cond = threading.Condition()
        self.port, self.mode = "sim", "rpc"
        self.presses = 0
        self._push()

    def _top(self):
        return self.stack[-1] if self.stack else self.home

    def _push(self):
        with self._cond:
            self._seq += 1
            self._frames.append(Frame(self._seq, time.monotonic(), b"", 0))
            self._frames[-1]._arr = self._top().render()
            self._cond.notify_all()

    def latest(self):
        return self._frames[-1]

    def current(self):
        return self._frames[-1]

    def wait_frame(self, after_seq=0, timeout=1.0):
        end = time.monotonic() + timeout
        with self._cond:
            while self._frames[-1].seq <= after_seq:
                rem = end - time.monotonic()
                if rem <= 0:
                    return None
                self._cond.wait(rem)
            return self._frames[-1]

    def frames_since(self, seq):
        return [f for f in self._frames if f.seq > seq]

    def press(self, key, kind="short"):
        self.presses += 1
        key = key.upper(); m = self._top()
        def later():
            time.sleep(0.01)
            if key in ("UP", "DOWN"):
                m.key(key)
            elif key == "OK" and m.items[m.sel] in m.children:
                self.stack.append(m.children[m.items[m.sel]])
            elif key == "BACK" and self.stack:
                self.stack.pop()
            self._push()
        threading.Thread(target=later).start()

    def app_start(self, name, args=""):
        self.stack = [self.apps[name]]
        self._push()

    def app_running(self):
        return bool(self.stack)

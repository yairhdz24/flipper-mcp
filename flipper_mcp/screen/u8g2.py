"""Decoder for u8g2 compressed fonts (the format the Flipper firmware and Momentum
asset packs use). Port of lib/u8g2/u8g2_font.c glyph decoding, ASCII range only.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).parent / "fonts"


@dataclass
class Glyph:
    char: str
    w: int
    h: int
    x: int      # offset from pen x to bbox left
    y: int      # offset of bbox bottom above baseline (u8g2: top = baseline - (h + y))
    adv: int    # pen advance
    bitmap: np.ndarray  # (h, w) uint8, 1 = ink


@dataclass
class Font:
    name: str
    glyphs: dict
    ascent: int
    descent: int
    max_h: int

    @property
    def space_adv(self) -> int:
        g = self.glyphs.get(" ")
        return g.adv if g else 3


class _Bits:
    def __init__(self, data: bytes, pos: int):
        self.d = data
        self.p = pos
        self.bit = 0

    def u(self, cnt: int) -> int:
        val = self.d[self.p] >> self.bit
        end = self.bit + cnt
        if end >= 8:
            self.p += 1
            nxt = self.d[self.p] if self.p < len(self.d) else 0
            val |= nxt << (8 - self.bit)
            end -= 8
        self.bit = end
        return val & ((1 << cnt) - 1)

    def s(self, cnt: int) -> int:
        return self.u(cnt) - (1 << (cnt - 1))


class _Pen:
    """Run-length writer: fills a glyph bitmap left-to-right, top-to-bottom."""

    def __init__(self, bmp: np.ndarray):
        self.bmp = bmp
        self.h, self.w = bmp.shape
        self.lx = self.ly = 0

    def run(self, n: int, fg: bool) -> None:
        while n > 0 and self.ly < self.h:
            take = min(n, self.w - self.lx)
            if fg:
                self.bmp[self.ly, self.lx:self.lx + take] = 1
            self.lx += take
            n -= take
            if self.lx >= self.w:
                self.lx = 0
                self.ly += 1


def _s8(b: int) -> int:
    return b - 256 if b > 127 else b


def parse_font(data: bytes, name: str) -> Font:
    (_cnt, _mode, b0, b1, bw, bh, bx, by, bd, _maxw, maxh) = data[:11]
    ascent_A, descent_g = _s8(data[13]), _s8(data[14])
    glyphs = {}
    pos = 23
    while pos + 1 < len(data):
        enc, jump = data[pos], data[pos + 1]
        if jump == 0:
            break
        bits = _Bits(data, pos + 2)
        w, h = bits.u(bw), bits.u(bh)
        x, y, d = bits.s(bx), bits.s(by), bits.s(bd)
        bmp = np.zeros((h, w), dtype=np.uint8)
        if w > 0 and h > 0:
            pen = _Pen(bmp)
            while True:
                a = bits.u(b0)
                b = bits.u(b1)
                while True:
                    pen.run(a, False)
                    pen.run(b, True)
                    if bits.u(1) == 0:
                        break
                if pen.ly >= h:
                    break
        if 32 <= enc < 127:
            glyphs[chr(enc)] = Glyph(chr(enc), w, h, x, y, d, bmp)
        pos += jump
    return Font(name, glyphs, ascent_A, descent_g, maxh)


@cache
def load(name: str, path: str | None = None) -> Font:
    p = Path(path) if path else DATA_DIR / f"{name}.u8f"
    return parse_font(p.read_bytes(), name)


def render(font: Font, text: str, width: int = 128, height: int = 16) -> np.ndarray:
    """Render text the way u8g2 does (baseline at font ascent). Used by tests."""
    img = np.zeros((height, width), dtype=np.uint8)
    pen, base = 0, font.ascent + 1
    for ch in text:
        g = font.glyphs.get(ch)
        if g is None:
            continue
        top, left = base - (g.h + g.y), pen + g.x
        if g.w and g.h:
            img[top:top + g.h, left:left + g.w] |= g.bitmap
        pen += g.adv
    return img

"""Screen reader: turn a 128x64 Flipper frame into text by exact glyph matching.

The firmware draws text with a few known u8g2 bitmap fonts, so instead of OCR we
look for exact glyph bitmaps, chain them along shared baselines using each
glyph's pen advance, then verify every glyph's cell against a re-render.
Inverted text (white on a black highlight box) is read too and marks the
selected item.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import u8g2

# Order = preference when two fonts explain the same pixels equally well.
STOCK_FONTS = ["haxrcorp4089_tr", "helvB08_tr", "profont11_mr", "profont22_tn", "5x7_tr"]
EXTRA_FONTS = ["4x6_tr", "5x8_tr", "6x10_tr", "profont12_tr", "helvR08_tr"]
PUNCT_ONLY_MIN = 3


@dataclass
class Item:
    text: str
    x: int
    y: int          # top of text bbox
    w: int
    h: int
    base: int       # baseline
    font: str
    inverted: bool
    ink: int = 0

    def as_dict(self):
        return {k: getattr(self, k) for k in ("text", "x", "y", "w", "h", "font", "inverted")}


@dataclass
class Reading:
    lines: list = field(default_factory=list)      # list[list[Item]] grouped by baseline
    selected: str | None = None
    scroll: dict | None = None
    unread_px: int = 0                             # ink pixels not explained by text

    def text(self) -> str:
        out = []
        for line in self.lines:
            out.append(("> " if any(i.inverted for i in line) else "  ") + join_line(line))
        return "\n".join(out)

    def all_text(self) -> str:
        return "\n".join(join_line(l) for l in self.lines)

    def as_dict(self):
        return {
            "lines": [join_line(l) for l in self.lines],
            "selected": self.selected,
            "scroll": self.scroll,
            "items": [i.as_dict() for l in self.lines for i in l],
            "unread_px": self.unread_px,
        }


def _strong(it) -> bool:
    al = [c for c in it.text if c.isalnum()]
    if len(set(al)) >= 2:
        return True
    return len(al) == 1 and al[0].isdigit() and it.ink >= 8


def _fix_il(text: str) -> str:
    """'I' and 'l' share one bitmap in the stock fonts; pick by context."""
    if "I" not in text and "l" not in text:
        return text
    out = list(text.replace("l", "I"))
    for i, c in enumerate(out):
        if c != "I":
            continue
        prev = out[i - 1] if i > 0 else " "
        nxt = out[i + 1] if i + 1 < len(out) else " "
        if prev.isalpha():
            out[i] = "l" if prev.islower() or (nxt.isalpha() and nxt.islower()) else "I"
        elif nxt.isalpha() and nxt.isupper():
            out[i] = "I"
        elif prev == " " and nxt.isalpha():
            words_before = "".join(out[:i]).split()
            out[i] = "l" if words_before and words_before[-1][:1].islower() else "I"
        else:
            out[i] = "I"
    return "".join(out)


def join_line(items) -> str:
    out, end = "", None
    for it in items:
        if end is not None:
            gap = it.x - end
            out += " " * max(1, min(6, round(gap / 6))) if gap > 3 else ("" if gap <= 2 else " ")
        out += it.text
        end = it.x + it.w
    return out


# ---------------------------------------------------------------- font bank

class FontBank:
    def __init__(self, names=None, extra_paths=None):
        names = list(names or STOCK_FONTS)
        if os.environ.get("FZ_FONTS") == "all":
            names += EXTRA_FONTS
        self.fonts = [u8g2.load(n) for n in names]
        for p in extra_paths or []:
            self.fonts.insert(0, u8g2.load(Path(p).stem + "@" + str(Path(p).parent.parent.name), str(p)))
        # row bit patterns per glyph: tuple of ints, LSB = leftmost pixel
        self.rows = {}
        self.solid = {}
        for fi, f in enumerate(self.fonts):
            for ch, g in f.glyphs.items():
                if g.w and g.h and g.bitmap.any():
                    self.rows[(fi, ch)] = [int(sum(int(v) << k for k, v in enumerate(r))) for r in g.bitmap]
                    ys, xs = np.nonzero(g.bitmap)
                    iy, ix = int(ys.min()), int(xs.min())
                    ih, iw = int(ys.max()) - iy + 1, int(xs.max()) - ix + 1
                    # glyphs whose ink is one solid rectangle (l I | - _ .) match any
                    # filled area; they must be isolated around that rectangle
                    if g.bitmap[iy:iy + ih, ix:ix + iw].all():
                        self.solid[(fi, ch)] = (iy, ix, ih, iw)
                    else:
                        self.solid[(fi, ch)] = None
            tops = [g.h + g.y for g in f.glyphs.values() if g.h]
            bots = [-g.y for g in f.glyphs.values() if g.h]
            f.band_top = max(tops) if tops else f.max_h
            f.band_bot = max(max(bots), 0) if bots else 0


_bank_cache: dict = {}


def bank(extra_paths=None) -> FontBank:
    key = tuple(extra_paths or ())
    if key not in _bank_cache:
        _bank_cache[key] = FontBank(extra_paths=list(key))
    return _bank_cache[key]


# ---------------------------------------------------------------- matching

def _windows(arr: np.ndarray, w: int) -> np.ndarray:
    """W[y, x] = bits arr[y, x:x+w] packed LSB-first."""
    H, Wd = arr.shape
    out = np.zeros((H, Wd - w + 1), dtype=np.uint32)
    for k in range(w):
        out |= arr[:, k:k + Wd - w + 1].astype(np.uint32) << k
    return out


def _find_matches(arr: np.ndarray, fb: FontBank):
    """Yield (font_idx, char, bx, by, inverted) for exact bbox matches."""
    H, Wd = arr.shape
    win_cache = {}
    res = []
    for (fi, ch), rows in fb.rows.items():
        g = fb.fonts[fi].glyphs[ch]
        w, h = g.w, g.h
        if w > Wd or h > H:
            continue
        if w not in win_cache:
            W = _windows(arr, w)
            win_cache[w] = (W, (~W) & np.uint32((1 << w) - 1))
        W, Winv = win_cache[w]
        ny = H - h + 1
        for inv, src in ((False, W), (True, Winv)):
            m = src[0:ny] == rows[0]
            for r in range(1, h):
                if not m.any():
                    break
                m &= src[r:r + ny] == rows[r]
            if not m.any():
                continue
            sol = fb.solid[(fi, ch)]
            if sol:
                iy, ix, ih, iw = sol
                iso = _isolated(arr if not inv else 1 - arr, iw, ih, (H - ih + 1, Wd - iw + 1))
                m = m & iso[iy:iy + m.shape[0], ix:ix + m.shape[1]]
            ys, xs = np.nonzero(m)
            for by, bx in zip(ys.tolist(), xs.tolist()):
                res.append((fi, ch, bx, by, inv))
    return res


def _isolated(a: np.ndarray, w: int, h: int, shape) -> np.ndarray:
    """True where a w x h solid box at (x, y) has background on all four sides."""
    H, Wd = a.shape
    p = np.pad(a, 1, constant_values=0).astype(np.int32)
    ii = np.zeros((H + 3, Wd + 3), dtype=np.int32)
    ii[1:, 1:] = p.cumsum(0).cumsum(1)

    def box(y0, x0, hh, ww):  # sum over padded coords
        ny, nx = shape
        ys = np.arange(ny)[:, None] + y0
        xs = np.arange(nx)[None, :] + x0
        return ii[ys + hh, xs + ww] - ii[ys, xs + ww] - ii[ys + hh, xs] + ii[ys, xs]

    ring = box(0, 0, h + 2, w + 2) - box(1, 1, h, w)
    return ring == 0


def _render_chain(fb, fi, chain, base, x0, x1, y0, y1):
    """Render glyphs of chain into the box [y0,y1) x [x0,x1)."""
    out = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    f = fb.fonts[fi]
    for pen, ch in chain:
        g = f.glyphs[ch]
        if not (g.w and g.h):
            continue
        top, left = base - (g.h + g.y) - y0, pen + g.x - x0
        t0, l0 = max(top, 0), max(left, 0)
        t1, l1 = min(top + g.h, out.shape[0]), min(left + g.w, out.shape[1])
        if t1 > t0 and l1 > l0:
            out[t0:t1, l0:l1] |= g.bitmap[t0 - top:t1 - top, l0 - left:l1 - left]
    return out


def read(arr: np.ndarray, extra_fonts=None) -> Reading:
    fb = bank(extra_fonts)
    H, Wd = arr.shape
    matches = _find_matches(arr, fb)

    # group by (font, inverted, baseline) keyed by pen x
    groups: dict = {}
    for fi, ch, bx, by, inv in matches:
        g = fb.fonts[fi].glyphs[ch]
        pen, base = bx - g.x, by + g.h + g.y
        groups.setdefault((fi, inv, base), {}).setdefault(pen, []).append(ch)

    candidates = []
    for (fi, inv, base), by_pen in groups.items():
        f = fb.fonts[fi]
        sp = f.space_adv
        pens = sorted(by_pen)
        # DP over (pen, char) nodes: best chain ending at node
        best = {}
        end_at: dict = {}
        for pen in pens:
            for ch in by_pen[pen]:
                g = f.glyphs[ch]
                ink = int(g.bitmap.sum())
                score, prev = ink + 1, None
                for gap in (0, sp):
                    for pnode in end_at.get(pen - gap, ()):
                        pscore = best[pnode][0]
                        if pscore + ink + 1 > score:
                            score, prev = pscore + ink + 1, pnode
                best[(pen, ch)] = (score, prev)
                if g.adv > 0:
                    end_at.setdefault(pen + g.adv, []).append((pen, ch))
        used = set()
        for node, (score, _) in sorted(best.items(), key=lambda kv: -kv[1][0]):
            chain, n = [], node
            while n is not None:
                chain.append(n)
                n = best[n][1]
            chain.reverse()
            if any(c in used for c in chain):
                # keep the unused tail only if it is long enough to matter
                continue
            used.update(chain)
            candidates.append((score, fi, inv, base, chain))

    # verify each glyph against a re-render of the chain; split on failures
    verified = []
    for score, fi, inv, base, chain in candidates:
        f = fb.fonts[fi]
        tops = [base - (f.glyphs[c].h + f.glyphs[c].y) for _, c in chain if f.glyphs[c].h]
        if not tops:
            continue
        y0, y1 = max(base - f.band_top, 0), min(base + f.band_bot, H)
        x0 = max(chain[0][0], 0)
        last = f.glyphs[chain[-1][1]]
        x1 = min(max(chain[-1][0] + last.x + last.w, chain[-1][0] + 1), Wd)
        if x1 <= x0 or y1 <= y0:
            continue
        want = _render_chain(fb, fi, chain, base, x0, x1, y0, y1)
        have = arr[y0:y1, x0:x1]
        if inv:
            have = 1 - have
        seg = []
        segs = []
        for i, (pen, ch) in enumerate(chain):
            g = f.glyphs[ch]
            c0 = max(pen, x0) - x0
            c1 = min(pen + max(g.adv, g.x + g.w), x1) - x0
            if i == len(chain) - 1:
                c1 = min(pen + g.x + g.w, x1) - x0
            ok = c1 <= c0 or np.array_equal(want[:, c0:c1], have[:, c0:c1])
            if ok:
                seg.append((pen, ch))
            else:
                if seg:
                    segs.append(seg)
                seg = []
        if seg:
            segs.append(seg)
        for s in segs:
            verified.append((fi, inv, base, s))

    # build items, enforce minimum plausibility, resolve pixel conflicts greedily
    items = []
    for fi, inv, base, seg in verified:
        f = fb.fonts[fi]
        text, prev_end = "", None
        for pen, ch in seg:
            if prev_end is not None and pen > prev_end:
                text += " "
            text += ch
            prev_end = pen + f.glyphs[ch].adv
        text = text.strip()
        if not text:
            continue
        alnum = sum(c.isalnum() for c in text)
        if alnum == 0 and len(text) < PUNCT_ONLY_MIN and text not in (">", "<"):
            continue
        glyphs = [(pen, f.glyphs[ch]) for pen, ch in seg if f.glyphs[ch].w]
        ink_mask = np.zeros_like(arr, dtype=bool)
        for pen, g in glyphs:
            top, left = base - (g.h + g.y), pen + g.x
            sub = g.bitmap.astype(bool)
            ink_mask[top:top + g.h, left:left + g.w] |= sub
        ink = int(ink_mask.sum())
        if len(text) == 1 and text not in "<>" and (text.islower() or ink < 6):
            continue
        text = _fix_il(text)
        xs = [pen + g.x for pen, g in glyphs]
        xe = [pen + g.x + g.w for pen, g in glyphs]
        ys = [base - (g.h + g.y) for _, g in glyphs]
        ye = [base - g.y for _, g in glyphs]
        it = Item(text, min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys), base,
                  f.name, inv, ink)
        items.append((ink + 3 * len(text) - fi * 0.01, it, ink_mask))

    items.sort(key=lambda t: -t[0])
    claimed = np.zeros_like(arr, dtype=bool)
    kept = []
    for _, it, mask in items:
        if (claimed & mask).any():
            continue
        claimed |= mask
        kept.append((it, mask))

    # weak items (one letter, or one letter repeated) are common false hits in
    # artwork; keep them only next to a strong item in the same font and baseline
    strong = [it for it, _ in kept if _strong(it)]
    claimed = np.zeros_like(arr, dtype=bool)
    final = []
    for it, mask in kept:
        if _strong(it) or any(s.font == it.font and abs(s.base - it.base) <= 1 for s in strong):
            final.append(it)
            claimed |= mask
    kept = final

    # lines: group by baseline (tolerance 2px), sort by x
    kept.sort(key=lambda i: (i.base, i.x))
    lines = []
    for it in kept:
        if lines and abs(lines[-1][0].base - it.base) <= 2:
            lines[-1].append(it)
        else:
            lines.append([it])
    for l in lines:
        l.sort(key=lambda i: i.x)

    r = Reading(lines=lines)
    boxes = np.zeros_like(arr, dtype=bool)

    def wide_box(it):
        y = max(it.y - 1, 0)
        row = arr[y]
        l = it.x
        while l > 0 and row[l - 1]:
            l -= 1
        r_ = it.x
        while r_ < Wd and row[r_]:
            r_ += 1
        if r_ - l < 48:
            return False
        top, bot = y, it.y + it.h
        while top > 0 and arr[top - 1, l + 2:r_ - 2].all():
            top -= 1
        while bot < H and arr[bot, l + 2:r_ - 2].all():
            bot += 1
        boxes[top:bot, l:r_] = True
        return True

    for l in lines:
        for i in l:
            if i.inverted and not wide_box(i):
                i.inverted = False
                i.boxed = True
    sel = [l for l in lines if any(i.inverted for i in l)]
    if sel:
        r.selected = join_line([i for i in sel[0] if i.inverted]) or join_line(sel[0])
    r.scroll = _scrollbar(arr)
    a = arr.astype(bool)
    solid = a.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            solid &= np.roll(np.roll(a, dy, 0), dx, 1)
    r.unread_px = int((a & ~claimed & ~solid & ~boxes).sum())
    return r


def _scrollbar(arr: np.ndarray):
    """Detect the standard Flipper scrollbar on the right edge: dotted track + solid thumb."""
    H, Wd = arr.shape
    for x in range(Wd - 1, Wd - 8, -1):
        col = arr[:, x]
        # thumb: longest solid vertical run, track: dotted pixels above/below it
        best, cur, start, bstart = 0, 0, 0, 0
        for y in range(H):
            if col[y]:
                if cur == 0:
                    start = y
                cur += 1
                if cur > best:
                    best, bstart = cur, start
            else:
                cur = 0
        if best < 3 or best > H - 4:
            continue
        neigh = arr[:, x - 1] if x > 0 else col
        dotted = sum(1 for y in range(1, H - 1) if neigh[y] and not neigh[y - 1] and not neigh[y + 1])
        dots_self = sum(1 for y in range(1, H - 1) if col[y] and not col[y - 1] and not col[y + 1])
        if dotted + dots_self < 8:
            continue
        span = H - best
        frac = round(bstart / span, 2) if span > 0 else 0.0
        return {"x": x, "thumb_top": bstart, "thumb_len": best, "pos": frac}
    return None

"""Framebuffer helpers: 1024-byte Flipper frame <-> numpy (64, 128), PNG, ASCII."""
from __future__ import annotations

import io

import numpy as np

W, H = 128, 64


def frame_to_array(data: bytes, orientation: int = 0) -> np.ndarray:
    """Flipper buffer is page-major (SSD1306): pixel(x,y) = data[(y//8)*128+x] >> (y%8) & 1."""
    buf = np.frombuffer(bytes(data).ljust(1024, b"\0")[:1024], dtype=np.uint8).reshape(8, W)
    arr = ((buf[:, None, :] >> np.arange(8, dtype=np.uint8)[None, :, None]) & 1).reshape(H, W)
    if orientation == 1:
        arr = arr[::-1, ::-1]
    elif orientation == 2:      # VERTICAL
        arr = np.rot90(arr, 3)
    elif orientation == 3:      # VERTICAL_FLIP
        arr = np.rot90(arr, 1)
    return np.ascontiguousarray(arr, dtype=np.uint8)


def array_to_png(arr: np.ndarray, scale: int = 4, fg=(0x1e, 0x1e, 0x1e), bg=(0xff, 0x8c, 0x29)) -> bytes:
    from PIL import Image

    rgb = np.where(arr[..., None] == 1, np.array(fg, np.uint8), np.array(bg, np.uint8)).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB")
    if scale > 1:
        img = img.resize((arr.shape[1] * scale, arr.shape[0] * scale), Image.NEAREST)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def png_to_array(path: str) -> np.ndarray:
    """Load a PNG written by array_to_png (any scale) back into a (64,128) bit array."""
    from PIL import Image

    img = Image.open(path).convert("L")
    sw, _ = img.size
    s = sw // W
    a = np.asarray(img)[s // 2::s, s // 2::s][:H, :W]
    return (a < 100).astype(np.uint8)


def array_to_ascii(arr: np.ndarray) -> str:
    return "\n".join("".join("#" if v else "." for v in row) for row in arr)


def changed(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.count_nonzero(a != b))

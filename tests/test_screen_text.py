"""Screen-to-text reader and navigation engine, without a Flipper attached.

- synthetic menus rendered with the firmware fonts
- golden frames captured from a real device (Momentum mntm-012): tests/frames/*.npy + .txt
- select/goto against a simulated device (tests/sim.py)
"""
from pathlib import Path

import numpy as np
import pytest

from flipper_mcp.engine import Engine
from flipper_mcp.screen import reader, u8g2

from .sim import Menu, SimDevice

FRAMES = Path(__file__).parent / "frames"
ITEMS = ["Read", "Read RAW", "Saved", "Add Manually", "Frequency Analyzer", "Radio Settings", "Info"]


def test_fonts_decode():
    for name in reader.STOCK_FONTS:
        f = u8g2.load(name)
        assert len(f.glyphs) >= 10


def test_reads_synthetic_menu():
    m = Menu("Sub-GHz", ITEMS)
    m.sel = 1
    r = reader.read(m.render())
    assert r.lines[0][0].text == "Sub-GHz"
    assert r.selected == "Read RAW"
    assert [line[0].text for line in r.lines[1:]] == ITEMS[:4]
    assert r.scroll is not None


@pytest.mark.parametrize("npy", sorted(FRAMES.glob("*.npy")), ids=lambda p: p.stem)
def test_golden_frames(npy):
    want = npy.with_suffix(".txt").read_text().strip()
    assert reader.read(np.load(npy)).text().strip() == want


def _sim():
    remotes = Menu("Universal", ["TVs", "Audio Players", "Projectors", "Fans", "Air Conditioners", "LEDs"])
    ir = Menu("Infrared", ["Universal Remotes", "Learn New Remote", "Saved Remotes", "GPIO Settings", "Debug"],
              {"Universal Remotes": remotes})
    dev = SimDevice({"Sub-GHz": Menu("Sub-GHz", list(ITEMS)), "Infrared": ir})
    return dev, Engine(dev), remotes


def test_goto_scrolls_to_item():
    dev, eng, _ = _sim()
    res = eng.goto("Sub-GHz/Radio Settings")
    top = dev._top()
    assert res["ok"] and top.items[top.sel] == "Radio Settings"


def test_select_upwards_and_nested_goto():
    dev, eng, remotes = _sim()
    eng.goto("Sub-GHz/Radio Settings")
    assert eng.select("Read", press_ok=False)["ok"] and dev._top().sel == 0
    res = eng.goto("Infrared/Universal Remotes/Air Conditioners")
    assert res["ok"] and dev._top() is remotes and remotes.items[remotes.sel] == "Air Conditioners"


def test_select_missing_item_fails_cleanly():
    _, eng, _ = _sim()
    eng.goto("Infrared")
    assert not eng.select("Nonexistent Thing", press_ok=False)["ok"]

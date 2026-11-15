"""fz: fast Flipper Zero control for agents. Every command returns the resulting screen as text."""
from __future__ import annotations

import argparse
import json
import sys

from . import paths
from .client import FzError, call


def fmt_screen(s: dict) -> str:
    body = s.get("text") or "(no text on screen)"
    meta = []
    if s.get("scroll"):
        meta.append(f"scroll {s['scroll']['pos']:.2f}")
    if s.get("unread_px", 0) > 40:
        meta.append(f"graphics {s['unread_px']}px (use --png to see)")
    out = body + ("\n[" + " | ".join(meta) + "]" if meta else "")
    if s.get("png"):
        out += f"\npng: {s['png']}"
    if s.get("ascii"):
        out += "\n" + s["ascii"]
    return out


def render(op: str, res: dict) -> str:
    if op in ("screen",):
        return fmt_screen(res)
    lines = []
    scr = res.pop("screen", None)
    if op in ("keys", "wait", "select", "goto", "back"):
        w = res.get("wait")
        if w is not None:
            lines.append(f"wait: {'met' if w['met'] else 'NOT met'} after {w['waited_ms']}ms")
        if "met" in res:
            lines.append(f"wait: {'met' if res['met'] else 'NOT met'} after {res['waited_ms']}ms")
        if res.get("ok") is False:
            lines.append(f"FAILED: {res.get('error')}")
        if op == "goto" and res.get("steps"):
            lines.append("steps: " + " → ".join(
                (st.get("open") or st.get("select")) + ("" if st.get("ok", True) else " ✗")
                for st in res["steps"]))
        if "presses" in res and op != "goto":
            lines.append(f"presses: {res['presses']}")
    elif op == "ls":
        return "\n".join(f"{'d' if e['type'] == 'dir' else '-'} {e['size']:>8} {e['name']}"
                         + (f"  {e['md5']}" if e.get("md5") else "") for e in res["entries"])
    elif op == "get" and "text" in res:
        return res["text"]
    elif op == "cli":
        return res["output"]
    elif op == "status":
        return "\n".join(f"{k}: {v}" for k, v in res.items())
    else:
        lines.append(json.dumps(res, indent=None))
    if scr:
        lines.append("--- screen ---")
        lines.append(fmt_screen(scr))
    return "\n".join(lines)


def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fz", description=__doc__)
    p.add_argument("--json", action="store_true", help="raw JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="connection, lock, app state")
    s.add_argument("--app", action="store_true", help="also ask the loader which app is open (slower)")
    s = sub.add_parser("info", help="device info")
    s.add_argument("--power", action="store_true")

    s = sub.add_parser("screen", help="read the screen as text")
    s.add_argument("--png", help="also save a PNG here")
    s.add_argument("--ascii", action="store_true", help="also print pixels as ASCII")

    def waits(sp):
        sp.add_argument("--until", help="wait until this text is on screen")
        sp.add_argument("--gone", help="wait until this text is gone")
        sp.add_argument("--stable", type=int, help="wait until no change for N ms")
        sp.add_argument("--timeout", type=float, default=5.0)

    s = sub.add_parser("keys", help="press keys: up down left right ok back; long:ok hold:ok:2 wait:0.5 down*3")
    s.add_argument("keys", nargs="+")
    s.add_argument("--png")
    waits(s)
    s = sub.add_parser("wait", help="wait for a screen condition")
    waits(s)
    s = sub.add_parser("select", help="highlight an item in the current list by name and press OK")
    s.add_argument("item")
    s.add_argument("--no-ok", action="store_true", help="only highlight it")
    s = sub.add_parser("goto", help="open an app and walk menus: 'Sub-GHz/Read'")
    s.add_argument("path")
    s = sub.add_parser("back", help="press BACK (or --home: back out of all apps)")
    s.add_argument("--home", action="store_true")
    sub.add_parser("unlock", help="unlock the desktop keypad lock")

    a = sub.add_parser("app", help="apps over RPC").add_subparsers(dest="sub", required=True)
    x = a.add_parser("open"); x.add_argument("name"); x.add_argument("args", nargs="?", default="")
    x = a.add_parser("load"); x.add_argument("path")
    a.add_parser("exit")
    x = a.add_parser("button"); x.add_argument("args", nargs="?", default=""); x.add_argument("--index", type=int, default=0)
    x.add_argument("--action", choices=["press", "release", "press_release"], default="press_release")
    a.add_parser("error")

    f = sub.add_parser("fs", help="storage").add_subparsers(dest="sub", required=True)
    x = f.add_parser("ls"); x.add_argument("path", nargs="?", default="/ext"); x.add_argument("--md5", action="store_true")
    x = f.add_parser("stat"); x.add_argument("path")
    x = f.add_parser("get"); x.add_argument("path"); x.add_argument("out", nargs="?")
    x = f.add_parser("put"); x.add_argument("local"); x.add_argument("remote")
    x = f.add_parser("mkdir"); x.add_argument("path")
    x = f.add_parser("rm"); x.add_argument("path"); x.add_argument("-r", "--recursive", action="store_true")
    x.add_argument("--force", action="store_true")
    x = f.add_parser("mv"); x.add_argument("old"); x.add_argument("new")
    x = f.add_parser("md5"); x.add_argument("path")

    s = sub.add_parser("cli", help="run a Flipper text-CLI command")
    s.add_argument("command")
    s.add_argument("--for", dest="stream_for", type=float, help="streaming command: run N seconds then Ctrl+C")
    s.add_argument("--timeout", type=float, default=5.0)
    s.add_argument("--force", action="store_true", help="allow transmit / write / delete commands")

    s = sub.add_parser("rec", help="record the screen to .gif or .png strip")
    s.add_argument("seconds", type=float); s.add_argument("out")
    s.add_argument("--keys", nargs="*")
    s = sub.add_parser("save-frame", help="save the current raw frame (.npy + .png) for tests")
    s.add_argument("name"); s.add_argument("dir")

    d = sub.add_parser("daemon", help="fzd control").add_subparsers(dest="sub", required=True)
    d.add_parser("stop"); d.add_parser("ping"); d.add_parser("log")
    return p


def main(argv=None):
    import os

    args = build().parse_args(argv)
    c = args.cmd
    try:
        if c == "daemon":
            if args.sub == "log":
                print((paths.run_dir() / "fzd.log").read_text()[-4000:])
                return 0
            if args.sub == "stop":
                if not paths.endpoint_exists():
                    print("fzd not running")
                    return 0
                res = call("shutdown")
                import time
                end = time.monotonic() + 5
                while paths.endpoint_exists() and time.monotonic() < end:
                    time.sleep(0.05)
            else:
                res = call("ping")
            op = "daemon"
        elif c == "app":
            op = {"open": "app_open", "load": "app_load", "exit": "app_exit",
                  "button": "app_button", "error": "app_error"}[args.sub]
            kw = {k: v for k, v in vars(args).items() if k not in ("cmd", "sub", "json")}
            res = call(op, **kw)
        elif c == "fs":
            op = args.sub
            kw = {k: v for k, v in vars(args).items() if k not in ("cmd", "sub", "json") and v is not None}
            if op in ("get",) and kw.get("out"):
                kw["out"] = os.path.abspath(kw["out"])
            if op == "put":
                kw["local"] = os.path.abspath(kw["local"])
            res = call(op, **kw)
        else:
            op = c.replace("-", "_")
            kw = {k: v for k, v in vars(args).items() if k not in ("cmd", "json") and v is not None}
            for k in ("png", "out", "dir"):
                if kw.get(k):
                    kw[k] = os.path.abspath(kw[k])
            res = call(op, **kw)
    except FzError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3 if e.gated else 2
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print(render(op, dict(res)) if isinstance(res, dict) else res)
    if isinstance(res, dict) and (res.get("ok") is False or (res.get("wait") or {}).get("met") is False
                                  or res.get("met") is False):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

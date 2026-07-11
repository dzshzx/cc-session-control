"""Terminal-theme seam: background detection + the dark/light palettes.

Top-level helper like `clipboard.py` — it touches only the controlling tty
(an OSC 11 background-color query) and the process env; `app._make_screen`
is the sole consumer. Views keep referencing the ONE semantic attr set; this
module only decides which fg/bg pair each name resolves to.

Detection order (`detect_mode`): explicit `CSCTL_THEME` (dark/light) →
OSC 11 tty query → `$COLORFGBG` → dark. The query MUST run before urwid
takes over the tty — `App.__init__` builds the screen before `loop.run()`,
so stdin is still in normal mode there.
"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import time

from .config import cfg

Mode = str  # "light" | anything else = dark

# (name, mono, dark (fg16, bg16, fg256, bg256), light (fg16, bg16, fg256, bg256))
# ONE semantic set, table-driven like `_colspec`/`_keytable`: both palettes are
# generated from this single spec so their attr names can never diverge.
# Body-content attrs use bg "default" (inherit the terminal's own background —
# the adaptive part); only the structural bands (header/footer/tabs/status/
# notify) and the selection keep an explicit bg. 256-color foregrounds stay
# ≥ 4.5:1 (WCAG-AA-ish, relative luminance) against the assumed backdrop —
# dark: near-black (#6d6/#d66 ≈ 5.5+, #aaa ≈ 8.1); light: near-white
# (#070 ≈ 5.7, #a00 ≈ 7.7, #666 ≈ 5.7, #850/#ddd ≈ 4.7).
_SPEC: list[tuple[str, str | None, tuple[str, str, str, str], tuple[str, str, str, str]]] = [
    ("header", "bold",
     ("white,bold", "black", "#fff,bold", "#111"),
     ("black,bold", "light gray", "#000,bold", "#ddd")),
    ("footer", None,
     ("light gray", "black", "#aaa", "#111"),
     ("dark gray", "light gray", "#555", "#ddd")),
    ("tab_on", "bold,standout",
     ("white,bold", "dark cyan", "#fff,bold", "#068"),
     ("white,bold", "dark cyan", "#fff,bold", "#068")),
    ("tab_off", None,
     ("light cyan", "black", "#7ab", "#111"),
     ("dark cyan", "light gray", "#067", "#ddd")),
    ("alive", None,
     ("light green", "default", "#6d6", "default"),
     ("dark green", "default", "#070", "default")),
    ("status_busy", "bold",
     ("light green,bold", "default", "#6d6,bold", "default"),
     ("dark green,bold", "default", "#070,bold", "default")),
    ("status_err", None,
     ("light red", "default", "#d66", "default"),
     ("dark red", "default", "#a00", "default")),
    ("dead", None,
     ("light gray", "default", "#ccc", "default"),
     ("dark gray", "default", "#666", "default")),
    ("selected", "standout",
     ("white,bold", "dark cyan", "#fff,bold", "#068"),
     ("white,bold", "dark cyan", "#fff,bold", "#068")),
    ("notify", "bold",
     ("yellow,bold", "black", "#ff0,bold", "#111"),
     ("brown,bold", "light gray", "#850,bold", "#ddd")),
    ("status", None,
     ("light gray", "black", "#bbb", "#111"),
     ("dark gray", "light gray", "#444", "#ddd")),
    ("body", None,
     ("light gray", "default", "#ccc", "default"),
     ("black", "default", "#222", "default")),
    ("col_header", None,
     ("dark cyan", "default", "#9cc", "default"),
     ("dark cyan", "default", "#067", "default")),
]


def palette(mode: Mode) -> list[tuple[str, str, str, str | None, str, str]]:
    """The urwid 6-tuple palette (name, fg16, bg16, mono, fg256, bg256)."""
    out = []
    for name, mono, dark, light in _SPEC:
        fg16, bg16, fg256, bg256 = light if mode == "light" else dark
        out.append((name, fg16, bg16, mono, fg256, bg256))
    return out


def detect_mode() -> Mode:
    """`CSCTL_THEME` override → OSC 11 query → `$COLORFGBG` → dark."""
    forced = cfg.theme.strip().lower()
    if forced in ("dark", "light"):
        return forced
    rgb = _query_bg_rgb()
    if rgb is not None:
        return _mode_from_rgb(*rgb)
    return _parse_colorfgbg(os.environ.get("COLORFGBG", "")) or "dark"


# Reply looks like `ESC]11;rgb:1111/1111/1111 BEL` (xterm also emits `rgba:`
# and 1-4 hex digits per channel; terminator is BEL or ST `ESC \`).
_OSC11_RGB = re.compile(r"rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")
# OSC 11 query + DA1 (Primary Device Attributes) as a SENTINEL: terminals
# answer queries in order and virtually every terminal answers DA1, so its
# reply (`ESC[?...c`) means "the OSC 11 answer, if any, has already arrived".
# Terminals that ignore OSC 11 (tmux 3.7 without an explicit bg — measured
# 0.3ms — the common csctl case) return in one round-trip instead of eating
# the full timeout on EVERY startup. Reading through to the DA1 reply also
# keeps the tty buffer clean: no reply bytes are left over to leak into urwid
# as phantom keys.
_QUERY = b"\x1b]11;?\x07\x1b[c"
_DA1_REPLY = re.compile(r"\x1b\[\?[0-9;]*c")
# Hard cap only — with the sentinel, any real terminal answers in one RTT;
# this bounds fake ptys that answer neither query.
_REPLY_TIMEOUT = 0.25


def _parse_osc11_reply(buf: str) -> tuple[float, float, float] | None:
    m = _OSC11_RGB.search(buf)
    if not m:
        return None

    def chan(h: str) -> float:
        return int(h, 16) / (16 ** len(h) - 1)

    return chan(m.group(1)), chan(m.group(2)), chan(m.group(3))


def _mode_from_rgb(r: float, g: float, b: float) -> Mode:
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "light" if lum > 0.5 else "dark"


def _parse_colorfgbg(value: str) -> Mode | None:
    """rxvt/konsole convention: "fg;bg" or "fg;default;bg".

    vim's rule for the bg code: 0-6 and 8 mean a dark background."""
    parts = value.split(";")
    if len(parts) < 2:
        return None
    try:
        bg = int(parts[-1])
    except ValueError:
        return None
    return "dark" if bg in (0, 1, 2, 3, 4, 5, 6, 8) else "light"


def _query_bg_rgb(timeout: float = _REPLY_TIMEOUT) -> tuple[float, float, float] | None:
    """Ask the terminal its background color via OSC 11; None on any failure."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        return _query_bg_rgb_on(sys.stdin.fileno(), sys.stdout.fileno(), timeout)
    except Exception:
        return None


def _query_bg_rgb_on(
    in_fd: int, out_fd: int, timeout: float
) -> tuple[float, float, float] | None:
    """The fd-level query — separated so tests can drive a bare pty pair
    (pytest's capture owns `sys.stdin`/`sys.stdout` during a test, so the
    wrapper above cannot be exercised end-to-end). May raise; the wrapper
    swallows."""
    old = termios.tcgetattr(in_fd)
    try:
        # Raw enough to read the reply: no line buffering, no echo (the reply
        # bytes must not be painted onto the screen). Set directly instead of
        # via tty.setcbreak, whose ECHO handling changed across 3.12.x.
        new = termios.tcgetattr(in_fd)
        new[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(in_fd, termios.TCSANOW, new)
        os.write(out_fd, _QUERY)
        buf = ""
        deadline = time.monotonic() + timeout
        # Stop on the DA1 sentinel, NOT on the OSC terminator — see _QUERY.
        while not _DA1_REPLY.search(buf):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([in_fd], [], [], remaining)
            if not ready:
                break
            buf += os.read(in_fd, 128).decode("ascii", "ignore")
        return _parse_osc11_reply(buf)
    finally:
        try:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, old)
        except Exception:
            pass

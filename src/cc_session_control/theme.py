"""Terminal-theme seam: background detection + the dark/light palettes.

Top-level helper like `clipboard.py` — it touches only the process env;
`app._make_screen` is the sole consumer. Views keep referencing the ONE
semantic attr set; this module only decides which fg/bg pair each name
resolves to.

Detection order (`detect_mode`): explicit `CSCTL_THEME` (dark/light) →
`$COLORFGBG` → dark.
"""

from __future__ import annotations

import os

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
_SPEC: list[
    tuple[str, str | None, tuple[str, str, str, str], tuple[str, str, str, str]]
] = [
    (
        "header",
        "bold",
        ("white,bold", "black", "#fff,bold", "#111"),
        ("black,bold", "light gray", "#000,bold", "#ddd"),
    ),
    (
        "footer",
        None,
        ("light gray", "black", "#aaa", "#111"),
        ("dark gray", "light gray", "#555", "#ddd"),
    ),
    (
        "tab_on",
        "bold,standout",
        ("white,bold", "dark cyan", "#fff,bold", "#068"),
        ("white,bold", "dark cyan", "#fff,bold", "#068"),
    ),
    (
        "tab_off",
        None,
        ("light cyan", "black", "#7ab", "#111"),
        ("dark cyan", "light gray", "#067", "#ddd"),
    ),
    (
        "alive",
        None,
        ("light green", "default", "#6d6", "default"),
        ("dark green", "default", "#070", "default"),
    ),
    (
        "status_busy",
        "bold",
        ("light green,bold", "default", "#6d6,bold", "default"),
        ("dark green,bold", "default", "#070,bold", "default"),
    ),
    (
        "status_err",
        None,
        ("light red", "default", "#d66", "default"),
        ("dark red", "default", "#a00", "default"),
    ),
    (
        "dead",
        None,
        ("light gray", "default", "#ccc", "default"),
        ("dark gray", "default", "#666", "default"),
    ),
    (
        "selected",
        "standout",
        ("white,bold", "dark cyan", "#fff,bold", "#068"),
        ("white,bold", "dark cyan", "#fff,bold", "#068"),
    ),
    (
        "notify",
        "bold",
        ("yellow,bold", "black", "#ff0,bold", "#111"),
        ("brown,bold", "light gray", "#850,bold", "#ddd"),
    ),
    (
        "status",
        None,
        ("light gray", "black", "#bbb", "#111"),
        ("dark gray", "light gray", "#444", "#ddd"),
    ),
    (
        "body",
        None,
        ("light gray", "default", "#ccc", "default"),
        ("black", "default", "#222", "default"),
    ),
    (
        "col_header",
        None,
        ("dark cyan", "default", "#9cc", "default"),
        ("dark cyan", "default", "#067", "default"),
    ),
]


def palette(mode: Mode) -> list[tuple[str, str, str, str | None, str, str]]:
    """The urwid 6-tuple palette (name, fg16, bg16, mono, fg256, bg256)."""
    out = []
    for name, mono, dark, light in _SPEC:
        fg16, bg16, fg256, bg256 = light if mode == "light" else dark
        out.append((name, fg16, bg16, mono, fg256, bg256))
    return out


def detect_mode() -> Mode:
    """`CSCTL_THEME` override → `$COLORFGBG` → dark."""
    forced = cfg.theme.strip().lower()
    if forced in ("dark", "light"):
        return forced
    return _parse_colorfgbg(os.environ.get("COLORFGBG", "")) or "dark"


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

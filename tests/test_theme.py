"""Adaptive terminal theme: palette generation + background detection."""

from __future__ import annotations

import os
import threading
import time

import pytest

from cc_session_control import theme
from cc_session_control.config import cfg

# The ONE semantic attr set views reference (see views/*).
EXPECTED_NAMES = {
    "header", "footer", "tab_on", "tab_off", "alive", "status_busy",
    "status_err", "dead", "selected", "notify", "status", "body", "col_header",
}


def test_palettes_share_one_semantic_set() -> None:
    dark = theme.palette("dark")
    light = theme.palette("light")
    assert {e[0] for e in dark} == EXPECTED_NAMES
    assert [e[0] for e in dark] == [e[0] for e in light]


def test_palette_entries_are_urwid_6_tuples() -> None:
    for entry in theme.palette("dark") + theme.palette("light"):
        assert len(entry) == 6
        name, fg16, bg16, _mono, fg256, bg256 = entry
        for field in (fg16, bg16, fg256, bg256):
            assert isinstance(field, str) and field, (name, field)


def test_unknown_mode_falls_back_to_dark() -> None:
    assert theme.palette("weird") == theme.palette("dark")


@pytest.mark.parametrize(
    ("rgb", "expected"),
    [
        ((1.0, 1.0, 1.0), "light"),   # white
        ((0.0, 0.0, 0.0), "dark"),    # black
        ((0.99, 0.96, 0.89), "light"),  # solarized light #fdf6e3
        ((0.0, 0.17, 0.21), "dark"),    # solarized dark #002b36
    ],
)
def test_mode_from_rgb(rgb: tuple[float, float, float], expected: str) -> None:
    assert theme._mode_from_rgb(*rgb) == expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("\x1b]11;rgb:ffff/ffff/ffff\x07", (1.0, 1.0, 1.0)),
        ("\x1b]11;rgb:0000/0000/0000\x1b\\", (0.0, 0.0, 0.0)),
        ("\x1b]11;rgb:ff/00/00\x07", (1.0, 0.0, 0.0)),  # 2-digit channels
        ("\x1b]11;rgba:ffff/ffff/ffff/ffff\x07", (1.0, 1.0, 1.0)),
        ("", None),
        ("\x1b]11;?\x07", None),          # our own query echoed back
        ("garbage without rgb", None),
    ],
)
def test_parse_osc11_reply(reply: str, expected: tuple | None) -> None:
    got = theme._parse_osc11_reply(reply)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15;0", "dark"),
        ("0;15", "light"),
        ("12;8", "dark"),
        ("0;default;15", "light"),  # 3-field rxvt form: bg is the LAST field
        ("0;7", "light"),
        ("", None),
        ("15", None),          # single field — no bg
        ("15;default", None),  # non-numeric bg
    ],
)
def test_parse_colorfgbg(value: str, expected: str | None) -> None:
    assert theme._parse_colorfgbg(value) == expected


def test_detect_mode_env_override_skips_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "LIGHT")

    def boom() -> None:
        raise AssertionError("forced theme must not probe the tty")

    monkeypatch.setattr(theme, "_query_bg_rgb", boom)
    assert theme.detect_mode() == "light"


def test_detect_mode_uses_osc_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    monkeypatch.setattr(theme, "_query_bg_rgb", lambda: (1.0, 1.0, 1.0))
    assert theme.detect_mode() == "light"


def test_detect_mode_colorfgbg_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    monkeypatch.setattr(theme, "_query_bg_rgb", lambda: None)
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.detect_mode() == "light"


def test_detect_mode_defaults_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    monkeypatch.setattr(theme, "_query_bg_rgb", lambda: None)
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_mode() == "dark"


def test_query_bg_rgb_refuses_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(theme.sys, "stdin", NotATty())
    assert theme._query_bg_rgb() is None


# --- end-to-end against a real pty. Tests drive the fd-level
# `_query_bg_rgb_on` directly: pytest's capture plugin owns
# `sys.stdin`/`sys.stdout` during a test, so the isatty wrapper can't be
# exercised here (it is covered by the non-tty refusal test above). The fake
# terminal answers on the master side. ---


@pytest.fixture
def pty_pair():
    master, slave = os.openpty()
    yield master, slave
    os.close(master)
    os.close(slave)


def _answer(master: int, reply: bytes) -> None:
    os.read(master, 64)  # wait until the query arrives
    os.write(master, reply)


def test_query_bg_rgb_end_to_end(pty_pair: tuple[int, int]) -> None:
    master, slave = pty_pair
    # xterm-style: OSC 11 answer, then the DA1 sentinel reply.
    t = threading.Thread(
        target=_answer, args=(master, b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?1;2c")
    )
    t.start()
    got = theme._query_bg_rgb_on(slave, slave, timeout=5.0)
    t.join()
    assert got == pytest.approx((1.0, 1.0, 1.0))


def test_query_bg_rgb_da1_sentinel_short_circuits(pty_pair: tuple[int, int]) -> None:
    master, slave = pty_pair
    # tmux-style: OSC 11 ignored, only DA1 answered — must return on the
    # sentinel immediately, NOT sit out the timeout (given generously here so
    # a cap-hit fails loudly even on a slow CI box).
    t = threading.Thread(target=_answer, args=(master, b"\x1b[?1;2;4c"))
    t.start()
    t0 = time.monotonic()
    got = theme._query_bg_rgb_on(slave, slave, timeout=5.0)
    elapsed = time.monotonic() - t0
    t.join()
    assert got is None
    assert elapsed < 2.0


def test_query_bg_rgb_times_out_on_silent_pty(pty_pair: tuple[int, int]) -> None:
    _master, slave = pty_pair
    assert theme._query_bg_rgb_on(slave, slave, timeout=0.05) is None

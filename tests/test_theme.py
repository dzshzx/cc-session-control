"""Adaptive terminal theme: palette generation + background detection."""

from __future__ import annotations

import pytest

from cc_session_control import theme
from cc_session_control.config import cfg

# The ONE semantic attr set views reference (see views/*).
EXPECTED_NAMES = {
    "header",
    "footer",
    "tab_on",
    "tab_off",
    "alive",
    "status_busy",
    "status_err",
    "dead",
    "selected",
    "notify",
    "status",
    "body",
    "col_header",
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
    ("value", "expected"),
    [
        ("15;0", "dark"),
        ("0;15", "light"),
        ("12;8", "dark"),
        ("0;default;15", "light"),  # 3-field rxvt form: bg is the LAST field
        ("0;7", "light"),
        ("", None),
        ("15", None),  # single field — no bg
        ("15;default", None),  # non-numeric bg
    ],
)
def test_parse_colorfgbg(value: str, expected: str | None) -> None:
    assert theme._parse_colorfgbg(value) == expected


def test_detect_mode_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "LIGHT")
    assert theme.detect_mode() == "light"


def test_detect_mode_colorfgbg_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert theme.detect_mode() == "light"


def test_detect_mode_defaults_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_mode() == "dark"

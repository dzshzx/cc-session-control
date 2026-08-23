"""Clipboard backend detection (`_detect`) and `copy()` invocation."""

from __future__ import annotations

import subprocess

import pytest

from cc_session_control import clipboard


@pytest.fixture(autouse=True)
def _reset_backend_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # `copy()` memoizes `_backend`/`_encoding` at module scope after the first
    # `_detect()` call, so every test must start from an undetected state or
    # a patched `_detect` in an earlier test would leak into a later one.
    monkeypatch.setattr(clipboard, "_backend", None)
    monkeypatch.setattr(clipboard, "_encoding", "utf-8")


def _no_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: no platform backend is discoverable."""
    monkeypatch.setattr(clipboard.os.path, "isfile", lambda _path: False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda _name: None)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)


# --- _detect(): one test per platform branch, in the function's own priority order ---


def test_detect_prefers_wsl_clip_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_backends(monkeypatch)
    monkeypatch.setattr(
        clipboard.os.path,
        "isfile",
        lambda path: path == "/mnt/c/Windows/System32/clip.exe",
    )

    backend, encoding = clipboard._detect()

    assert backend == ["/mnt/c/Windows/System32/clip.exe"]
    assert encoding == "utf-16-le"


def test_detect_falls_back_to_pbcopy(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_backends(monkeypatch)
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None,
    )

    backend, encoding = clipboard._detect()

    assert backend == ["pbcopy"]
    assert encoding == "utf-8"


def test_detect_falls_back_to_wlcopy_under_wayland(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_backends(monkeypatch)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
    )

    backend, encoding = clipboard._detect()

    assert backend == ["wl-copy"]
    assert encoding == "utf-8"


def test_detect_ignores_wlcopy_without_wayland_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # wl-copy is installed but $WAYLAND_DISPLAY is unset — not a Wayland session.
    _no_backends(monkeypatch)
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
    )

    backend, _encoding = clipboard._detect()

    assert backend == []


def test_detect_falls_back_to_xclip_under_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_backends(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )

    backend, encoding = clipboard._detect()

    assert backend == ["xclip", "-selection", "clipboard"]
    assert encoding == "utf-8"


def test_detect_ignores_xclip_without_display(monkeypatch: pytest.MonkeyPatch) -> None:
    # xclip is installed but $DISPLAY is unset — not an X11 session.
    _no_backends(monkeypatch)
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )

    backend, _encoding = clipboard._detect()

    assert backend == []


def test_detect_returns_empty_backend_when_nothing_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_backends(monkeypatch)

    backend, encoding = clipboard._detect()

    assert backend == []
    assert encoding == "utf-8"


# --- copy() ---


def test_copy_returns_false_when_no_backend_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_backends(monkeypatch)

    assert clipboard.copy("hello") is False


def test_copy_invokes_detected_backend_with_encoded_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard, "_backend", ["xclip", "-selection", "clipboard"])
    monkeypatch.setattr(clipboard, "_encoding", "utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(clipboard.subprocess, "run", run)

    assert clipboard.copy("hello") is True
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["xclip", "-selection", "clipboard"]
    assert kwargs["input"] == b"hello"
    assert kwargs["timeout"] == 5
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True


def test_copy_encodes_utf16_le_for_wsl_clip_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard, "_backend", ["/mnt/c/Windows/System32/clip.exe"])
    monkeypatch.setattr(clipboard, "_encoding", "utf-16-le")
    calls: list[dict[str, object]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(clipboard.subprocess, "run", run)

    assert clipboard.copy("你好") is True
    # UTF-16-LE, no BOM: 2 bytes per BMP codepoint, low byte first.
    assert calls[0]["input"] == "你好".encode("utf-16-le")
    assert calls[0]["input"] == bytes([0x60, 0x4F, 0x7D, 0x59])


def test_copy_returns_false_on_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard, "_backend", ["xclip", "-selection", "clipboard"])
    monkeypatch.setattr(clipboard, "_encoding", "utf-8")

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(clipboard.subprocess, "run", run)

    assert clipboard.copy("hello") is False


def test_copy_returns_false_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard, "_backend", ["xclip", "-selection", "clipboard"])
    monkeypatch.setattr(clipboard, "_encoding", "utf-8")

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(clipboard.subprocess, "run", run)

    assert clipboard.copy("hello") is False

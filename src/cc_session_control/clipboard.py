"""Cross-platform clipboard support."""

from __future__ import annotations

import os
import shutil
import subprocess

_backend: list[str] | None = None
_encoding: str = "utf-8"


def _detect() -> tuple[list[str], str]:
    if os.path.isfile("/mnt/c/Windows/System32/clip.exe"):
        return ["/mnt/c/Windows/System32/clip.exe"], "utf-16-le"

    if shutil.which("pbcopy"):
        return ["pbcopy"], "utf-8"

    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"], "utf-8"

    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"], "utf-8"

    return [], "utf-8"


def copy(text: str) -> bool:
    global _backend, _encoding
    if _backend is None:
        _backend, _encoding = _detect()

    if not _backend:
        return False

    try:
        subprocess.run(
            _backend, input=text.encode(_encoding),
            timeout=5, check=True, capture_output=True,
        )
        return True
    except Exception:
        return False

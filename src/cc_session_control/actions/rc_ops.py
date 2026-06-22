"""RC operations: start/stop/toggle autostart."""

from __future__ import annotations

from ..data import rc


def toggle_autostart(proj: str) -> bool:
    """Toggle project in the autostart list. Returns new state."""
    if rc.list_has(proj):
        rc.list_rm(proj)
        return False
    else:
        rc.list_add(proj)
        return True


def start_project(proj: str) -> bool:
    return rc.start_one(proj)


def stop_project(proj: str) -> bool:
    return rc.stop_one(proj)


def start_all_listed() -> int:
    return rc.start_many(rc.list_enabled())


def stop_all_rc() -> bool:
    return rc.stop_all()

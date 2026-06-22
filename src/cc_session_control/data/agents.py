"""Claude agents integration — the single authority for session liveness."""

from __future__ import annotations

import json
import subprocess
import time

_cache: dict[str, int | None] | None = None
_cache_time: float = 0


def alive_map(max_age: float = 5.0) -> dict[str, int | None]:
    """Return {session_id: pid} for all known agents. Cached for max_age seconds."""
    global _cache, _cache_time
    now = time.monotonic()
    if _cache is not None and (now - _cache_time) < max_age:
        return _cache
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        result = {
            a.get("sessionId"): a.get("pid")
            for a in json.loads(out or "[]")
            if a.get("sessionId")
        }
    except Exception:
        result = {}
    _cache = result
    _cache_time = now
    return result


def invalidate_cache() -> None:
    global _cache
    _cache = None

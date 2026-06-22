"""Claude agents integration — the single authority for session liveness."""

from __future__ import annotations

import json
import subprocess


def alive_map() -> dict[str, int | None]:
    """Return {session_id: pid} for all known agents."""
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return {
            a.get("sessionId"): a.get("pid")
            for a in json.loads(out or "[]")
            if a.get("sessionId")
        }
    except Exception:
        return {}

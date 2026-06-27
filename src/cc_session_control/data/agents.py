"""Backward-compatible re-export shim — liveness lives in `liveness.py` now.

Kept zero-logic on purpose: re-exporting the *same* callables means terminate's
`invalidate_cache()` and scan's `alive_map()` share the ONE cache in
`liveness` (`liveness._cache`). New code should import from `.liveness`.
"""

from __future__ import annotations

from .liveness import alive_map, invalidate_cache

__all__ = ["alive_map", "invalidate_cache"]

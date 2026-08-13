"""Codex trust-store reader — membership evidence (ADR-0007).

Split out of `codex.py` for the 600-line budget (same sidecar discipline as
`codex_source.py`). The store is `$CODEX_HOME/config.toml`'s
`[projects."<abs path>"]` tables with `trust_level = "trusted"`.
"""

from __future__ import annotations

import os
import tomllib

from ...config import cfg
from ...models import InventoryIssue
from .base import TrustScan


def read_trusted_dirs() -> TrustScan:
    """Exact-match `trust_level = "trusted"` keys of `config.toml [projects]`.

    Whether codex's own runtime trust inherits down the directory tree is
    UNVERIFIED upstream (ADR-0007) — so only a recorded key itself counts,
    never its descendants. A missing config is a fresh install (no issue);
    an unreadable/malformed one narrows only this source.
    """
    path = cfg.codex_home / "config.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TrustScan()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return TrustScan(
            issues=(InventoryIssue("codex trust", os.fspath(path), str(exc)),)
        )
    projects = document.get("projects", {})
    if not isinstance(projects, dict):
        return TrustScan(
            issues=(
                InventoryIssue(
                    "codex trust",
                    os.fspath(path),
                    "'projects' is not a table",
                ),
            )
        )
    return TrustScan(
        directories=tuple(
            key
            for key, value in projects.items()
            if key.startswith("/")
            and isinstance(value, dict)
            and value.get("trust_level") == "trusted"
        )
    )

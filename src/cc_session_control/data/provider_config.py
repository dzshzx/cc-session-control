"""Operator-declared CLI instances — the machine-level provider inventory.

`providers.json` (`cfg.provider_config_file`, XDG config home) answers a
question the CLIs' own relocation variables CANNOT: *which agent-CLI state
homes exist on this machine*. `CODEX_HOME` answers a different one — which
home THIS codex process uses — and it is inherited by everything a codex
session spawns, csctl included. Reading it as the machine inventory made
csctl's world drift with its launch environment: started from inside a
second-identity codex session, csctl saw that identity's sessions and none
of the default home's (ADR-0008).

So when this file declares `codex_homes`, that list is the COMPLETE set of
codex instances and `CODEX_HOME` stops participating entirely. Without the
file — the common single-identity case — behavior is exactly as before: one
codex instance following `cfg.codex_home` (i.e. `CODEX_HOME` or `~/.codex`).

    {
      "codex_homes": [
        {"label": "cx",  "home": "~/.codex"},
        {"label": "cx2", "home": "~/.codex-eva02"}
      ]
    }

The FIRST entry is the default instance and keeps provider key `codex`, so
existing `Session.provider` values and dispatched windows' `@csctl_provider`
metadata stay valid; every later entry gets `codex:<label>`. Labels are the
CLI column tag (max `_LABEL_MAX_CHARS` cells, matching that column's width).
csctl never writes this file — it is operator-authored, read once per
process, and a broken one degrades to the single-instance default with
visible detail rather than a blanked codex view.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Longest instance label — the Sessions CLI column is 3 cells wide
#: (`_session_row.SESSION_COLS`), and a label also prefixes tmux window names.
_LABEL_MAX_CHARS = 3

#: The default instance's provider key. Keeping the FIRST declared instance on
#: the pre-multi-home key means existing rows, curation entries, and dispatch
#: metadata never need migration.
DEFAULT_CODEX_KEY = "codex"
DEFAULT_CODEX_LABEL = "cx"


class ProviderConfigState(Enum):
    """Availability of the operator provider-instance declaration."""

    AVAILABLE = "available"
    MISSING = "missing"  # no file — single-instance default, NOT a failure
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    INVALID = "invalid"


@dataclass(frozen=True)
class CodexInstanceSpec:
    """One declared codex state home: its provider identity plus that home."""

    key: str
    label: str
    home: Path


@dataclass(frozen=True)
class ProviderConfigResult:
    """A provider-config read whose external failure remains observable.

    `codex_instances` is empty when the file is absent or unusable; the
    registry then builds its single default instance. `detail` carries the
    reason a present file was rejected, so degradation is never silent.
    """

    state: ProviderConfigState
    codex_instances: tuple[CodexInstanceSpec, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "codex_instances", tuple(self.codex_instances))

    @property
    def available(self) -> bool:
        return self.state in {
            ProviderConfigState.AVAILABLE,
            ProviderConfigState.MISSING,
        }


def _invalid_label(label: object) -> str | None:
    if not isinstance(label, str) or not label:
        return "'label' must be a non-empty string"
    if len(label) > _LABEL_MAX_CHARS:
        return f"label {label!r} exceeds {_LABEL_MAX_CHARS} characters"
    # ASCII only: the label sits in a fixed-width column and inside tmux
    # window names, where wide glyphs would break alignment and matching.
    if not label.isascii() or not label.isalnum():
        return f"label {label!r} must be ASCII alphanumeric"
    return None


def _instance_home(home: object) -> tuple[Path | None, str | None]:
    """Validate one declared home into an absolute path (`~` expanded).

    Existence is deliberately NOT required: an absent home simply leaves that
    instance inactive (`provider.available()`), exactly like a machine
    without codex installed — a config file must not fail because one
    identity's home was cleaned up.
    """
    if not isinstance(home, str) or not home:
        return None, "'home' must be a non-empty string"
    expanded = Path(home).expanduser()
    if not expanded.is_absolute():
        return None, f"home {home!r} is not an absolute path"
    return Path(os.path.normpath(expanded)), None


def _parse_codex_homes(entries: object) -> tuple[tuple[CodexInstanceSpec, ...], str]:
    """The `codex_homes` schema; returns (instances, invalidity detail)."""
    if not isinstance(entries, list):
        return (), "'codex_homes' is not a list"
    if not entries:
        # An explicitly EMPTY list contradicts itself: the operator wrote the
        # key but declared no instance. Refusing keeps it distinguishable
        # from an absent key (which legitimately means "use the default").
        return (), "'codex_homes' is empty"
    instances: list[CodexInstanceSpec] = []
    labels: set[str] = set()
    homes: set[Path] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return (), f"codex_homes[{index}] is not an object"
        label = entry.get("label")
        invalid = _invalid_label(label)
        if invalid is not None:
            return (), f"codex_homes[{index}]: {invalid}"
        assert isinstance(label, str)  # narrowed by _invalid_label
        home, invalid = _instance_home(entry.get("home"))
        if invalid is not None:
            return (), f"codex_homes[{index}]: {invalid}"
        assert home is not None
        if label in labels:
            return (), f"codex_homes[{index}]: duplicate label {label!r}"
        if home in homes:
            return (), f"codex_homes[{index}]: duplicate home {os.fspath(home)!r}"
        labels.add(label)
        homes.add(home)
        instances.append(
            CodexInstanceSpec(
                key=DEFAULT_CODEX_KEY if index == 0 else f"codex:{label}",
                label=label,
                home=home,
            )
        )
    return tuple(instances), ""


def read_provider_config(path: Path) -> ProviderConfigResult:
    """Read the instance declaration without conflating absence with failure."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ProviderConfigResult(ProviderConfigState.MISSING)
    except OSError as exc:
        return ProviderConfigResult(ProviderConfigState.UNREADABLE, detail=str(exc))
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return ProviderConfigResult(ProviderConfigState.MALFORMED, detail=str(exc))
    if not isinstance(document, dict):
        return ProviderConfigResult(
            ProviderConfigState.INVALID,
            detail="top-level JSON value is not an object",
        )
    if "codex_homes" not in document:
        # A file that configures something else entirely (or nothing yet) is
        # valid and leaves the codex default alone.
        return ProviderConfigResult(ProviderConfigState.AVAILABLE)
    instances, invalid = _parse_codex_homes(document["codex_homes"])
    if invalid:
        return ProviderConfigResult(ProviderConfigState.INVALID, detail=invalid)
    return ProviderConfigResult(ProviderConfigState.AVAILABLE, instances)

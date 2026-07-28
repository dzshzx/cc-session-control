"""Preview-time removal anchors for cleanup policy targets."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..models import Session
from .removal import RemovalAnchor, anchor_path


@dataclass(frozen=True)
class PlanAnchors:
    sessions: dict[str, tuple[RemovalAnchor, ...]]
    orphans: dict[str, RemovalAnchor]
    zombies: dict[int, RemovalAnchor]
    aged: dict[str, RemovalAnchor]


def entry_anchors(
    entries: Sequence[str],
    bases: Mapping[str, str],
) -> dict[str, RemovalAnchor]:
    anchors: dict[str, RemovalAnchor] = {}
    for entry in entries:
        label, _, name = entry.partition("/")
        base = bases.get(label)
        if base is not None:
            anchors[entry] = anchor_path(base, os.path.join(base, name))
    return anchors


def _sid_targets(sid: str, sid_roots: Sequence[str]) -> list[tuple[str, str]]:
    return [(root, os.path.join(root, sid)) for root in sid_roots]


def agent_removal_anchors(
    short: str,
    sid: str,
    sid_roots: Sequence[str],
    jobs_root: str,
) -> tuple[RemovalAnchor, ...]:
    targets = [
        (jobs_root, os.path.join(jobs_root, short)),
        *_sid_targets(sid, sid_roots),
        (jobs_root, os.path.join(jobs_root, sid[:8])),
    ]
    return _anchors(list(dict.fromkeys(targets)))


def _session_anchors(
    session: Session,
    sid_roots: Sequence[str],
    jobs_root: str,
) -> tuple[RemovalAnchor, ...]:
    targets: list[tuple[str, str]] = []
    if session.file:
        transcript_root = os.fspath(Path(session.file).parent)
        targets.append((transcript_root, session.file))
        if session.file.endswith(".jsonl"):
            targets.append((transcript_root, session.file[:-6]))
    targets.extend(_sid_targets(session.sid, sid_roots))
    targets.append((jobs_root, os.path.join(jobs_root, session.sid[:8])))
    return _anchors(list(dict.fromkeys(targets)))


def session_removal_anchors(
    sessions: Sequence[Session],
    sid_roots: Sequence[str],
    jobs_root: str,
) -> dict[str, tuple[RemovalAnchor, ...]]:
    return {
        session.sid: _session_anchors(session, sid_roots, jobs_root)
        for session in sessions
    }


def pin_plan_targets(
    sessions: Sequence[Session],
    orphan_entries: Sequence[str],
    orphan_bases: Mapping[str, str],
    zombie_pids: Sequence[int],
    sessions_root: str,
    aged_entries: Sequence[str],
    aged_bases: Mapping[str, str],
    sid_roots: Sequence[str],
    jobs_root: str,
) -> PlanAnchors:
    return PlanAnchors(
        sessions=session_removal_anchors(sessions, sid_roots, jobs_root),
        orphans=entry_anchors(orphan_entries, orphan_bases),
        zombies={
            pid: anchor_path(sessions_root, os.path.join(sessions_root, f"{pid}.json"))
            for pid in zombie_pids
        },
        aged=entry_anchors(aged_entries, aged_bases),
    )


def _anchors(targets: Sequence[tuple[str, str]]) -> tuple[RemovalAnchor, ...]:
    return tuple(anchor_path(root, target) for root, target in targets)

"""Bridge-environment ledger — a PASSIVE, observe-and-forget store (R6, D4).

Claude Code keeps only the *current* bridge binding for a session/agent (a
single overwritten field), so toggled-away or historically minted cloud
environments vanish from on-disk state. csctl maintains its own append-only
ledger so those environments stay traceable enough to be deleted by hand on
claude.ai/code — there is NO local deregister, this module never deletes a
cloud environment.

Design invariants:
  - **Passive store.** Callers push observations in (`upsert(records)`); this
    module never reaches up to collect them. It must NOT import `rc`
    (`environments` is below `rc` in the import DAG; `rc` calls `upsert` one-way
    in Phase 5 for the `env_*` namespace). `observe()` is a convenience builder
    that reads the lower-level `registry` only.
  - **Three namespaces, namespace-scoped dedup.** The merge key is
    `(prefix, key)`: within `cse_*` a resume pair shares one suffix → one env;
    `session_*` and `cse_*` never merge (their suffixes never coincide in
    practice); `env_*` ids are opaque and each unique. Dedup is WITHIN a
    namespace, never cross-view.
  - **Write-on-change + atomic + single-writer.** The read-modify-write runs
    under an advisory `flock` (degrades gracefully where unavailable), the
    resulting ledger is serialized canonically and only rewritten when it
    differs from disk, and the write is `tmp + os.replace` atomic.
  - **Retention/compaction.** Entries older than `_RETENTION_SECONDS` (90d) are
    dropped; if still over `_MAX_ENTRIES`, the most-recently-seen are kept. A
    re-observed env always carries `last_seen == now` so it survives compaction.

Everything swallows errors → safe empties: a missing or corrupt ledger never
crashes (returns `[]` / no-ops).

Known limitation (capability red line): the ledger cannot back-fill
environments minted while csctl was not running — there is no `null`/history on
disk to recover them — so the orphan / manual-delete list is inherently
incomplete.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from typing import Any

from ..config import cfg
from ..models import BridgeEnv, EnvRecord
from . import registry

try:  # POSIX advisory locking; absent on Windows → degrade to no lock.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

# Drop entries unseen for longer than this; cap total entries beyond that.
_RETENTION_SECONDS = 90 * 86400
_MAX_ENTRIES = 500


# --- bridge id parsing -----------------------------------------------------

def _split_bridge(bridge: str) -> tuple[str, str]:
    """`cse_abc` -> (`cse`, `abc`); the suffix is the namespace-local env id."""
    prefix, sep, suffix = bridge.partition("_")
    if not sep:
        return "", ""
    return prefix, suffix


# --- observation builder (reads registry only, never rc) -------------------

def observe(max_age: float = 5.0) -> list[EnvRecord]:
    """Build env records from the currently observable registries (R6).

    `session_*` from `sessions/<pid>.json` bridges (truthy), `cse_*` from job
    env suffixes. The `env_*` namespace (project RC server) has no state file
    and is pushed in by `rc` in Phase 5 — never collected here. This is a coarse
    "bridge present" view; stricter liveness (pid alive) is the caller's job (it
    holds `live_index`), so it may pass a filtered `observed` to the queries.
    """
    records: list[EnvRecord] = []
    try:
        for sp in registry.read_session_procs(max_age=max_age):
            if not sp.bridge:
                continue
            prefix, key = _split_bridge(sp.bridge)
            if prefix and key:
                records.append(EnvRecord(prefix=prefix, key=key, bound_sid=sp.sid))
        for job in registry.read_agent_jobs(max_age=max_age):
            if job.env_suffix:
                records.append(
                    EnvRecord(prefix="cse", key=job.env_suffix, bound_sid=job.sid)
                )
    except Exception:
        return []
    return records


# --- ledger IO (parse / serialize / atomic write / lock) -------------------

def _read_raw() -> str:
    try:
        with open(cfg.environments_ledger, errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def _parse_ledger(text: str) -> dict[tuple[str, str], BridgeEnv]:
    """Parse ledger text into a (prefix, key) -> BridgeEnv map. Skips bad lines.

    Persisted `status` is ignored (recomputed against the live observation), so
    only the five durable fields are read back.
    """
    out: dict[tuple[str, str], BridgeEnv] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        prefix = d.get("prefix")
        key = d.get("key")
        if not prefix or not key:
            continue
        out[(prefix, key)] = BridgeEnv(
            prefix=str(prefix),
            key=str(key),
            bound_sid=d.get("bound_sid"),
            first_seen=_as_float(d.get("first_seen")),
            last_seen=_as_float(d.get("last_seen")),
        )
    return out


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _serialize(entries: list[BridgeEnv]) -> str:
    """Canonical JSONL (sorted by id, sorted keys) so write-on-change is exact."""
    lines: list[str] = []
    for e in sorted(entries, key=lambda e: (e.prefix, e.key)):
        d = {
            "prefix": e.prefix,
            "key": e.key,
            "bound_sid": e.bound_sid,
            "first_seen": e.first_seen,
            "last_seen": e.last_seen,
        }
        lines.append(json.dumps(d, sort_keys=True, ensure_ascii=False))
    return ("\n".join(lines) + "\n") if lines else ""


def _atomic_write(text: str) -> None:
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    path = str(cfg.environments_ledger)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def _write_lock() -> Iterator[None]:
    """Advisory single-writer lock around a read-modify-write.

    Uses a dedicated `.lock` file (never replaced) so the lock survives the
    ledger's atomic `os.replace`. Degrades to no-op locking where `fcntl` is
    unavailable (Windows) — the atomic rename still prevents a torn file.
    """
    fh = None
    try:
        cfg.config_dir.mkdir(parents=True, exist_ok=True)
        fh = open(str(cfg.environments_ledger) + ".lock", "w")
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
        yield
    except Exception:
        yield
    finally:
        if fh is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            fh.close()


# --- merge / compaction ----------------------------------------------------

def _dedup_records(records: list[EnvRecord]) -> list[EnvRecord]:
    """Collapse records by (prefix, key) — within-namespace dedup (resume pairs).

    Picks a deterministic canonical `bound_sid` (the smallest non-empty sid) so
    the merge result is stable regardless of the observation order (registry
    glob order is not sorted), keeping write-on-change from flapping.
    """
    grouped: dict[tuple[str, str], list[EnvRecord]] = {}
    for r in records:
        if not r.prefix or not r.key:
            continue
        grouped.setdefault((r.prefix, r.key), []).append(r)
    out: list[EnvRecord] = []
    for (prefix, key), recs in grouped.items():
        sids = sorted({r.bound_sid for r in recs if r.bound_sid})
        out.append(EnvRecord(prefix=prefix, key=key, bound_sid=sids[0] if sids else None))
    return out


def _merge(
    ledger: dict[tuple[str, str], BridgeEnv],
    records: list[EnvRecord],
    now: float,
) -> dict[tuple[str, str], BridgeEnv]:
    for rec in _dedup_records(records):
        k = (rec.prefix, rec.key)
        existing = ledger.get(k)
        if existing is None:
            ledger[k] = BridgeEnv(
                prefix=rec.prefix,
                key=rec.key,
                bound_sid=rec.bound_sid,
                first_seen=now,
                last_seen=now,
            )
        else:
            existing.bound_sid = rec.bound_sid
            existing.last_seen = now
    return ledger


def _compact(
    ledger: dict[tuple[str, str], BridgeEnv], now: float
) -> dict[tuple[str, str], BridgeEnv]:
    cutoff = now - _RETENTION_SECONDS
    kept = {k: e for k, e in ledger.items() if e.last_seen >= cutoff}
    if len(kept) > _MAX_ENTRIES:
        newest = sorted(kept.values(), key=lambda e: e.last_seen, reverse=True)
        kept = {(e.prefix, e.key): e for e in newest[:_MAX_ENTRIES]}
    return kept


# --- public API ------------------------------------------------------------

def upsert(records: list[EnvRecord], now: float | None = None) -> None:
    """Merge observed env records into the ledger (passive store, R6/D4).

    Sets `first_seen` on insert, advances `last_seen` to `now` on re-observation
    (`now` injectable for deterministic tests), dedups within a namespace,
    compacts, and writes ONLY if the canonical serialization changed — under an
    advisory lock and via an atomic `tmp + replace`. Swallows all errors.
    """
    import time

    ts = time.time() if now is None else now
    try:
        with _write_lock():
            old_text = _read_raw()
            ledger = _parse_ledger(old_text)
            ledger = _merge(ledger, records, ts)
            ledger = _compact(ledger, ts)
            new_text = _serialize(list(ledger.values()))
            if new_text != old_text:
                _atomic_write(new_text)
    except Exception:
        return


def _read_ledger() -> dict[tuple[str, str], BridgeEnv]:
    return _parse_ledger(_read_raw())


def current_envs(observed: list[EnvRecord]) -> list[BridgeEnv]:
    """Envs bound to something observed right now (status='current').

    Classifies the ledger against the observation. An observed env not yet in
    the ledger (caller queried before `upsert`) is still reported current so the
    result is correct regardless of call order. Sorted newest-seen first.
    """
    obs = {(r.prefix, r.key): r for r in observed if r.prefix and r.key}
    ledger = _read_ledger()
    out: list[BridgeEnv] = []
    seen: set[tuple[str, str]] = set()
    for k, env in ledger.items():
        if k in obs:
            env.status = "current"
            out.append(env)
            seen.add(k)
    for k, rec in obs.items():
        if k not in seen:
            out.append(BridgeEnv(prefix=rec.prefix, key=rec.key,
                                 bound_sid=rec.bound_sid, status="current"))
    return sorted(out, key=lambda e: e.last_seen, reverse=True)


def orphan_envs(observed: list[EnvRecord]) -> list[BridgeEnv]:
    """Ledger entries NOT currently observed (status='orphan').

    These are the manual-delete candidates: csctl cannot deregister a cloud
    environment, so the user removes them on claude.ai/code. Sorted newest-seen
    first. (Inherently incomplete — see the module docstring's red line.)
    """
    obs_keys = {(r.prefix, r.key) for r in observed if r.prefix and r.key}
    out: list[BridgeEnv] = []
    for k, env in _read_ledger().items():
        if k not in obs_keys:
            env.status = "orphan"
            out.append(env)
    return sorted(out, key=lambda e: e.last_seen, reverse=True)


def manual_delete_list(observed: list[EnvRecord] | None = None) -> list[dict[str, Any]]:
    """Orphans formatted for manual deletion on claude.ai/code (R6).

    Each row carries the full namespaced `env_id` (incl. `env_*`), `prefix`,
    `key`, last-known `bound_sid`, and `last_seen`. With no observation passed,
    every ledger entry is treated as a candidate. There is NO deregister action
    — this is a checklist for the human, observe-and-forget only.
    """
    orphans = orphan_envs(observed or [])
    return [
        {
            "env_id": e.env_id,
            "prefix": e.prefix,
            "key": e.key,
            "bound_sid": e.bound_sid,
            "last_seen": e.last_seen,
        }
        for e in orphans
    ]

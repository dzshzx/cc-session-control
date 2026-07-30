"""Bridge-environment ledger — a PASSIVE, observe-and-forget store (R6, D4).

Claude Code keeps only the *current* bridge binding for a session/agent (a
single overwritten field), so toggled-away or historically minted cloud
environments vanish from on-disk state. csctl maintains its own append-only
ledger so those environments stay traceable enough to be deleted by hand on
claude.ai/code — there is NO local deregister, this module never deletes a
cloud environment.

Design invariants:
  - **Passive store.** `reconcile()` is the sole production ledger writer; this
    module never reaches up to collect observations. It must NOT import `rc`
    (`environments` is below `rc` in the import DAG). `observe()` takes its
    `session_procs`/`agent_jobs` from the caller's typed liveness snapshot and
    accepts `env_*` records passed in, never collected here.
  - **Two observation tiers.** `observe()` is the bridge-truthy FILE-REFERENCED
    set — what defines ledger MEMBERSHIP (an env exists in the cloud while any
    on-disk file references it, alive or zombie). `observe_live()` alive-gates the
    same sources for the CURRENT/bound DISPLAY. Orphans (manual-delete
    candidates) are `ledger − file-referenced`: an env the ledger remembers but no
    file references anymore (RC toggled off, job removed, server stopped).
  - **Three namespaces, namespace-scoped dedup.** The merge key is
    `(prefix, key)`: within `cse_*` a resume pair shares one suffix → one env;
    `session_*` and `cse_*` never merge (their suffixes never coincide in
    practice); `env_*` ids are opaque and each unique. Dedup is WITHIN a
    namespace, never cross-view.
  - **Typed persistence.** `environment_ledger` owns the locked atomic
    read-modify-write. Missing is a legal empty ledger; unreadable history,
    malformed rows, and write failures remain distinct and operator-visible.
    Programming errors are never converted into empty observations.

Known limitation (capability red line): the ledger cannot back-fill
environments minted while csctl was not running — there is no `null`/history on
disk to recover them — so the orphan / manual-delete list is inherently
incomplete.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from ..models import (
    AgentJob,
    BridgeEnv,
    EnvRecord,
    InventoryIssue,
    RCServer,
    SessionProc,
    split_env_id,
)
from . import environment_ledger, liveness
from .environment_ledger import (
    LedgerRead,
    LedgerReadState,
    LedgerUpdate,
    LedgerUpdateState,
)


@dataclass(frozen=True)
class Reconciliation:
    """One observation and ledger reconciliation for CLI/TUI consumers."""

    current: tuple[BridgeEnv, ...] = ()
    orphans: tuple[BridgeEnv, ...] = ()
    observed: tuple[EnvRecord, ...] = ()
    file_referenced: tuple[EnvRecord, ...] = ()
    ledger: LedgerUpdate = field(
        default_factory=lambda: LedgerUpdate(
            LedgerUpdateState.UNCHANGED,
            read=LedgerRead(LedgerReadState.MISSING),
        ),
    )
    ledger_history_complete: bool = True
    liveness_issues: tuple[liveness.LivenessIssue, ...] = ()
    inventory_issues: tuple[InventoryIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "current", tuple(self.current))
        object.__setattr__(self, "orphans", tuple(self.orphans))
        object.__setattr__(self, "observed", tuple(self.observed))
        object.__setattr__(
            self,
            "file_referenced",
            tuple(self.file_referenced),
        )
        object.__setattr__(
            self,
            "liveness_issues",
            tuple(self.liveness_issues),
        )
        object.__setattr__(
            self,
            "inventory_issues",
            tuple(self.inventory_issues),
        )

    @property
    def evidence_complete(self) -> bool:
        """Whether every liveness source was available for this inventory."""

        return not self.liveness_issues and not self.inventory_issues

    @property
    def success(self) -> bool:
        """Whether the result is safe to present as a complete inventory."""

        return self.evidence_complete and self.ledger.success


# --- observation builder (reads registry + liveness, never rc) -------------


def _collect(
    session_procs: Sequence[SessionProc],
    agent_jobs: Sequence[AgentJob],
    rc_servers: Sequence[RCServer] | None,
    *,
    alive_gated: bool,
) -> list[EnvRecord]:
    """The ONE observation walk over the three env sources (R6).

    `alive_gated=False` → the FILE-REFERENCED membership set (any env a file
    references, alive or zombie). `alive_gated=True` → the CURRENT set, each
    source gated by its own liveness rule: `session_*` by the single
    `liveness.is_rc_exposed` predicate, `cse_*` by a host-alive job or a
    proc-alive session sharing the job sid, `env_*` by a running RC server.
    """
    alive_sids = {sp.sid for sp in session_procs if sp.proc_alive}
    records: list[EnvRecord] = []
    for sp in session_procs:
        if not sp.bridge:
            continue
        if alive_gated and not liveness.is_rc_exposed(
            sp.bridge,
            sp.proc_alive is True,
        ):
            continue
        prefix, key = split_env_id(sp.bridge)
        if prefix and key:
            records.append(EnvRecord(prefix=prefix, key=key, bound_sid=sp.sid))
    for job in agent_jobs:
        if not job.env_suffix:
            continue
        if alive_gated and not (job.host_alive or job.sid in alive_sids):
            continue
        records.append(EnvRecord(prefix="cse", key=job.env_suffix, bound_sid=job.sid))
    for srv in rc_servers or []:
        if not srv.env_id:
            continue
        if alive_gated and srv.status != "running":
            continue
        prefix, key = split_env_id(srv.env_id)
        if prefix and key:
            records.append(EnvRecord(prefix=prefix, key=key, bound_sid=None))
    return records


def observe(
    session_procs: Sequence[SessionProc],
    agent_jobs: Sequence[AgentJob],
    rc_servers: Sequence[RCServer] | None = None,
) -> list[EnvRecord]:
    """FILE-REFERENCED bridge envs — the ledger MEMBERSHIP set (R6).

    Every env an on-disk file references right now: a session's truthy
    `bridgeSessionId` (`session_*`, alive OR zombie), a job's `cse_*` env suffix,
    plus any `env_*` captured from the `rc_servers` passed in. This is the set
    that DEFINES ledger membership — an env exists in the cloud as long as a file
    references it, regardless of liveness. Contrast `observe_live()`, which
    alive-gates the same sources for the CURRENT/bound display; orphans are
    `ledger − file-referenced`.

    Pure: `session_procs`/`agent_jobs` are always supplied by the caller (the
    snapshot path and `csctl env` both source them from one typed
    `liveness.LivenessSnapshot`). `env_*` is only ever passed in (this module
    never imports rc).
    """
    return _collect(session_procs, agent_jobs, rc_servers, alive_gated=False)


def observe_live(
    session_procs: Sequence[SessionProc],
    agent_jobs: Sequence[AgentJob],
    rc_servers: Sequence[RCServer] | None = None,
) -> list[EnvRecord]:
    """Alive-gated "currently exposed" bridge envs (R3/R6) — the CURRENT set.

    Where `observe()` is a bridge-truthy passive collector, this applies the
    single `liveness.is_rc_exposed` predicate (and the per-source gates in
    `_collect`): a bridge is CURRENT only when its owner is alive — `session_*`
    gated by proc-alive, `cse_*` by a proc-alive session sharing the job sid
    (host-alive), `env_*` by a running RC server. So a zombie session's stale
    `bridgeSessionId` is NOT reported as current — it falls through to
    `orphan` classification (a manual-delete candidate) instead of overstating
    the bound count.

    Pure: `session_procs`/`agent_jobs` are always supplied by the caller (the
    snapshot path passes its already-liveness-resolved data — DI for tests).
    """
    return _collect(session_procs, agent_jobs, rc_servers, alive_gated=True)


# --- public API ------------------------------------------------------------


def reconcile(
    evidence: liveness.LivenessSnapshot,
    rc_servers: Sequence[RCServer] | None = None,
    *,
    inventory_issues: Sequence[InventoryIssue] = (),
    now: float | None = None,
) -> Reconciliation:
    """THE R6 pipeline, in one place: observe (file-referenced) → `upsert` →
    observe_live → classify (`current` / `orphan = ledger − file-referenced`).

    The load-bearing ordering — upsert the file-referenced set BEFORE
    classifying, and compute orphans against the FILE-REFERENCED tier (never
    the alive-gated one, which would report every current env as an orphan) —
    used to be re-established by hand at each call site; here it is an
    implementation detail. Both consumers (`build_world_snapshot` every cycle
    and `csctl env`) call this instead of re-wiring the pieces. Sources are
    supplied as one typed liveness snapshot; this seam never falls back to the
    compatibility readers that discard source issues. `rc_servers` is passed in
    separately because this module never imports rc (the data DAG stays intact).

    Incomplete liveness is fail-closed: partial current observations remain
    available for explicitly partial display, but ledger persistence and orphan
    classification are skipped. A partial membership set can neither add a
    durable record nor prove that a remembered environment became an orphan.
    Partial ledger history is handled the same way: its salvaged entries and
    precise warnings remain visible, but it is neither rewritten nor classified.
    """
    file_referenced = observe(
        evidence.session_procs,
        evidence.agent_jobs,
        rc_servers,
    )
    observed = observe_live(
        evidence.session_procs,
        evidence.agent_jobs,
        rc_servers,
    )
    if not evidence.complete or inventory_issues:
        return Reconciliation(
            current=tuple(_current_envs(observed, {})),
            observed=tuple(observed),
            file_referenced=tuple(file_referenced),
            ledger_history_complete=False,
            liveness_issues=evidence.issues,
            inventory_issues=tuple(inventory_issues),
        )

    update = upsert(file_referenced, now=now)
    entries = update.entries if update.history_available else {}
    ledger_history_complete = (
        update.success and update.history_available and not update.warnings
    )
    return Reconciliation(
        current=tuple(_current_envs(observed, entries)),
        orphans=(
            tuple(_orphan_envs(file_referenced, entries))
            if update.history_available
            else ()
        ),
        observed=tuple(observed),
        file_referenced=tuple(file_referenced),
        ledger=update,
        ledger_history_complete=ledger_history_complete,
        liveness_issues=evidence.issues,
    )


def upsert(
    records: Sequence[EnvRecord],
    now: float | None = None,
) -> LedgerUpdate:
    """Merge observed env records into the ledger (passive store, R6/D4).

    Sets `first_seen` on insert, advances `last_seen` to `now` on re-observation
    (`now` injectable for deterministic tests), dedups within a namespace, and
    compacts — all under an advisory lock, via an atomic `tmp + replace`.

    Write-on-change ignores `last_seen` (M2): the file is rewritten only when the
    MEMBERSHIP changes (an env added/dropped, a re-bind, a new `first_seen`) or
    when valid on-disk text is not already canonical (legacy line cleanup).
    Malformed rows block the entire update and preserve the original bytes. A
    pure clock advance on otherwise-identical membership does NOT rewrite, so a
    steady-state refresh cycle leaves the file (and its mtime) untouched. The
    persisted copy still carries the advanced `last_seen` whenever a real write
    happens. Expected I/O failures are returned as `LedgerUpdateState.FAILED`;
    unsafe partial history is `LedgerUpdateState.BLOCKED`; programming errors
    propagate.
    """
    return environment_ledger.update(records, now=now)


def _current_envs(
    observed: Sequence[EnvRecord],
    entries: Mapping[tuple[str, str], BridgeEnv],
) -> list[BridgeEnv]:
    obs = {(r.prefix, r.key): r for r in observed if r.prefix and r.key}
    out: list[BridgeEnv] = []
    seen: set[tuple[str, str]] = set()
    for k, env in entries.items():
        if k in obs:
            out.append(replace(env, status="current"))
            seen.add(k)
    for k, rec in obs.items():
        if k not in seen:
            out.append(
                BridgeEnv(
                    prefix=rec.prefix,
                    key=rec.key,
                    bound_sid=rec.bound_sid,
                    status="current",
                )
            )
    return sorted(out, key=lambda e: e.last_seen, reverse=True)


def _orphan_envs(
    observed: Sequence[EnvRecord],
    entries: Mapping[tuple[str, str], BridgeEnv],
) -> list[BridgeEnv]:
    obs_keys = {(r.prefix, r.key) for r in observed if r.prefix and r.key}
    out: list[BridgeEnv] = []
    for k, env in entries.items():
        if k not in obs_keys:
            out.append(replace(env, status="orphan"))
    return sorted(out, key=lambda e: e.last_seen, reverse=True)

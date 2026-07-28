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
    (`environments` is below `rc` in the import DAG). `observe()` is a
    convenience builder that reads the lower-level `registry` and accepts
    `env_*` records passed in, never collected here.
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

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..models import (
    AgentJob,
    BridgeEnv,
    EnvRecord,
    RCServer,
    SessionProc,
    split_env_id,
)
from . import environment_ledger, liveness, registry
from .environment_ledger import (
    LedgerRead,
    LedgerReadState,
    LedgerUpdate,
    LedgerUpdateState,
)


@dataclass
class Reconciliation:
    """One observation and ledger reconciliation for CLI/TUI consumers."""

    current: list[BridgeEnv] = field(default_factory=list)
    orphans: list[BridgeEnv] = field(default_factory=list)
    observed: list[EnvRecord] = field(default_factory=list)
    file_referenced: list[EnvRecord] = field(default_factory=list)
    ledger: LedgerUpdate = field(
        default_factory=lambda: LedgerUpdate(
            LedgerUpdateState.UNCHANGED,
            read=LedgerRead(LedgerReadState.MISSING),
        ),
    )
    ledger_history_complete: bool = True
    liveness_issues: tuple[liveness.LivenessIssue, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def evidence_complete(self) -> bool:
        """Whether every liveness source was available for this inventory."""

        return not self.liveness_issues

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
    session_procs: Sequence[SessionProc] | None = None,
    agent_jobs: Sequence[AgentJob] | None = None,
    rc_servers: Sequence[RCServer] | None = None,
    max_age: float = 5.0,
) -> list[EnvRecord]:
    """FILE-REFERENCED bridge envs — the ledger MEMBERSHIP set (R6).

    Every env an on-disk file references right now: a session's truthy
    `bridgeSessionId` (`session_*`, alive OR zombie), a job's `cse_*` env suffix,
    plus any `env_*` captured from the `rc_servers` passed in. This is the set
    that DEFINES ledger membership — an env exists in the cloud as long as a file
    references it, regardless of liveness. Contrast `observe_live()`, which
    alive-gates the same sources for the CURRENT/bound display; orphans are
    `ledger − file-referenced`.

    Pure when the sources are supplied (snapshot path / tests); self-reads the
    registry when they are None (CLI / no-snapshot fallback). `env_*` is only ever
    passed in (this module never imports rc). Lower-level registry readers own
    their expected external-I/O degradation; programming errors propagate.
    """
    if session_procs is None:
        session_procs = registry.read_session_procs(max_age=max_age)
    if agent_jobs is None:
        agent_jobs = registry.read_agent_jobs(max_age=max_age)
    return _collect(session_procs, agent_jobs, rc_servers, alive_gated=False)


def observe_live(
    session_procs: Sequence[SessionProc] | None = None,
    agent_jobs: Sequence[AgentJob] | None = None,
    rc_servers: Sequence[RCServer] | None = None,
    max_age: float = 5.0,
) -> list[EnvRecord]:
    """Alive-gated "currently exposed" bridge envs (R3/R6) — the CURRENT set.

    Where `observe()` is a bridge-truthy passive collector, this applies the
    single `liveness.is_rc_exposed` predicate (and the per-source gates in
    `_collect`): a bridge is CURRENT only when its owner is alive — `session_*`
    gated by proc-alive, `cse_*` by a proc-alive session sharing the job sid
    (host-alive), `env_*` by a running RC server. So a zombie session's stale
    `bridgeSessionId` is NOT reported as current — it falls through to
    `orphan_envs` (a manual-delete candidate) instead of overstating the bound
    count.

    Pure when `session_procs`/`agent_jobs` are supplied (the snapshot path passes
    its already-liveness-resolved data — DI for tests); reads the registry +
    `/proc` itself when they are None (CLI / no-snapshot view fallback).
    """
    if session_procs is None:
        session_procs = liveness.live_session_procs(max_age=max_age)
    if agent_jobs is None:
        agent_jobs = registry.read_agent_jobs(max_age=max_age)
    return _collect(session_procs, agent_jobs, rc_servers, alive_gated=True)


# --- public API ------------------------------------------------------------


def reconcile(
    evidence: liveness.LivenessSnapshot,
    rc_servers: Sequence[RCServer] | None = None,
    *,
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
    if not evidence.complete:
        return Reconciliation(
            current=_current_envs(observed, {}),
            observed=observed,
            file_referenced=file_referenced,
            ledger_history_complete=False,
            liveness_issues=evidence.issues,
        )

    update = upsert(file_referenced, now=now)
    entries = update.entries if update.history_available else {}
    ledger_history_complete = (
        update.success and update.history_available and not update.warnings
    )
    return Reconciliation(
        current=_current_envs(observed, entries),
        orphans=_orphan_envs(file_referenced, entries),
        observed=observed,
        file_referenced=file_referenced,
        ledger=update,
        ledger_history_complete=ledger_history_complete,
        liveness_issues=evidence.issues,
        warnings=_ledger_warnings(update),
    )


def upsert(
    records: list[EnvRecord],
    now: float | None = None,
) -> LedgerUpdate:
    """Merge observed env records into the ledger (passive store, R6/D4).

    Sets `first_seen` on insert, advances `last_seen` to `now` on re-observation
    (`now` injectable for deterministic tests), dedups within a namespace, and
    compacts — all under an advisory lock, via an atomic `tmp + replace`.

    Write-on-change ignores `last_seen` (M2): the file is rewritten only when the
    MEMBERSHIP changes (an env added/dropped, a re-bind, a new `first_seen`) or
    when the on-disk text is not already canonical (corrupt / legacy line cleanup).
    A pure clock advance on otherwise-identical membership does NOT rewrite, so a
    steady-state refresh cycle leaves the file (and its mtime) untouched. The
    persisted copy still carries the advanced `last_seen` whenever a real write
    happens. Expected I/O failures are returned as `LedgerUpdateState.FAILED`;
    programming errors propagate.
    """
    return environment_ledger.update(records, now=now)


def read_ledger() -> LedgerRead:
    """Public typed read for consumers that do not need reconciliation."""

    return environment_ledger.read()


def current_envs(observed: list[EnvRecord]) -> list[BridgeEnv]:
    """Envs bound to something observed right now (status='current').

    Classifies the ledger against the observation. An observed env not yet in
    the ledger is still reported current — inside `reconcile` (which upserts
    first) that branch covers a typed ledger failure (for example a read-only
    file), so a write failure never hides a bound env. Sorted newest-seen first.
    """
    result = read_ledger()
    entries = result.entries if result.usable else {}
    return _current_envs(observed, entries)


def _current_envs(
    observed: list[EnvRecord],
    entries: dict[tuple[str, str], BridgeEnv],
) -> list[BridgeEnv]:
    obs = {(r.prefix, r.key): r for r in observed if r.prefix and r.key}
    out: list[BridgeEnv] = []
    seen: set[tuple[str, str]] = set()
    for k, env in entries.items():
        if k in obs:
            env.status = "current"
            out.append(env)
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


def orphan_envs(observed: list[EnvRecord]) -> list[BridgeEnv]:
    """Ledger entries NOT in the current observation (status='orphan').

    Pass the FILE-REFERENCED set (`observe()`) here so orphans are precisely
    `ledger − file-referenced`: envs the ledger remembers but no on-disk file
    references anymore (RC toggled off, job removed, server stopped). These are
    the manual-delete candidates: csctl cannot deregister a cloud environment, so
    the user removes them on claude.ai/code. Sorted newest-seen first.
    (Inherently incomplete — see the module docstring's red line.)
    """
    result = read_ledger()
    if not result.usable:
        return []
    return _orphan_envs(observed, result.entries)


def _orphan_envs(
    observed: list[EnvRecord],
    entries: dict[tuple[str, str], BridgeEnv],
) -> list[BridgeEnv]:
    obs_keys = {(r.prefix, r.key) for r in observed if r.prefix and r.key}
    out: list[BridgeEnv] = []
    for k, env in entries.items():
        if k not in obs_keys:
            env.status = "orphan"
            out.append(env)
    return sorted(out, key=lambda e: e.last_seen, reverse=True)


def _ledger_warnings(update: LedgerUpdate) -> tuple[str, ...]:
    warnings = [
        f"环境台账第 {warning.line} 行损坏，已跳过：{warning.detail}；"
        "孤儿历史可能不完整"
        for warning in update.warnings
    ]
    if update.failure is not None:
        warnings.append(
            f"环境台账操作失败（{update.failure.value}）：{update.detail}；"
            "当前环境仍显示，孤儿历史不完整",
        )
    return tuple(warnings)

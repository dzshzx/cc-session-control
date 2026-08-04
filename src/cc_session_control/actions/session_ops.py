"""Session operations: resume, terminate, delete, clipboard."""

from __future__ import annotations

import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from enum import StrEnum

from .. import clipboard
from ..data import liveness, proc, providers, sessions, tmux
from ..data.liveness import invalidate_cache
from ..models import Session, issue_detail


class TakeOverState(StrEnum):
    """Typed kill outcome used by every public destructive action."""

    KILLED = "killed"
    GONE = "gone"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class TakeOverOutcome:
    state: TakeOverState
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state in {TakeOverState.KILLED, TakeOverState.GONE}


class ExecutionSessionState(StrEnum):
    """Execution-time exact-SID resolution states.

    RESOLVED is discriminated by `.success`; LIVENESS_INCOMPLETE and
    TRANSCRIPT_INCOMPLETE are discriminated by `do_resume_sid_result` to
    prefix the detail. Every other rejection reason (missing sid, ambiguous
    match, current-session guard, unusable cwd, incomplete live identity) is
    never discriminated by any caller — only the detail string is — so they
    collapse into REFUSED, which still carries the specific reason in
    `detail`.
    """

    RESOLVED = "resolved"
    LIVENESS_INCOMPLETE = "liveness_incomplete"
    TRANSCRIPT_INCOMPLETE = "transcript_incomplete"
    REFUSED = "refused"


@dataclass(frozen=True)
class ExecutionSessionResolution:
    state: ExecutionSessionState
    session: Session | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is ExecutionSessionState.RESOLVED


def take_over_result(pid: int, proc_start: str = "") -> TakeOverOutcome:
    """THE kill primitive behind every takeover/stop: R10 gate → kill-time
    liveness recheck → SIGTERM → settle → invalidate the liveness cache.

    One implementation so the gate order and the kill semantics cannot fork
    across the resume/terminate/stop variants (they had already started to:
    only terminate/stop skipped the settle sleep for an already-gone pid).
    The recheck (`proc.probe_pid` against `proc_start`; mere existence when the
    start is unknown) closes the pid-reuse window — a confirm modal can sit
    open for minutes, and a recycled pid must never be SIGTERMed.

    Results: "killed" (signalled + settled), "gone" (already dead / recycled —
    nothing to kill), "refused" (R10: current undeterminable), "failed"
    (signal error, e.g. permissions). A required takeover may continue only
    after "killed" or "gone"; "refused" and "failed" both fail closed.
    """
    ancestors = proc.probe_current_ancestors()
    if not ancestors.complete:
        return TakeOverOutcome(
            TakeOverState.REFUSED,
            issue_detail(ancestors.issues),
        )
    if pid in ancestors.pids:
        return TakeOverOutcome(
            TakeOverState.REFUSED,
            f"pid {pid} belongs to the current session ancestor chain",
        )
    probe = proc.probe_pid(pid, proc_start)
    if probe.alive is None:
        issue = probe.issue
        if issue is None:
            raise AssertionError("unknown pid probe must carry an issue")
        return TakeOverOutcome(
            TakeOverState.REFUSED,
            issue_detail((issue,)),
        )
    if not probe.alive:
        invalidate_cache()  # already gone — liveness may have changed
        return TakeOverOutcome(TakeOverState.GONE)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        invalidate_cache()
        return TakeOverOutcome(TakeOverState.GONE)
    except OSError as exc:
        return TakeOverOutcome(TakeOverState.FAILED, str(exc))
    time.sleep(1)
    invalidate_cache()
    return TakeOverOutcome(TakeOverState.KILLED)


def _resume_plan(s: Session, fork: bool = False) -> tuple[str, list[str], bool]:
    """Shared resume recipe: the cwd to enter, the resume argv, and whether
    to kill the old session first.

    Returns (cwd, args, should_kill). The argv comes from the session's
    provider (ADR-0005) — never inline a CLI command here. Unified kill
    semantics stay provider-neutral: a fork is a copy and leaves the original
    running, while a plain resume takes the session over — so we kill only
    when it is alive, not the current session, and we are NOT forking.
    Runtime resume operations and `would_take_over` obey this single
    decision; copied live commands defer the whole decision to the
    execution-time `csctl resume --take-over` scan.
    """
    args = providers.get(s.provider).resume_argv(s.sid, fork)
    should_kill = s.alive and not s.current and not fork
    return s.cwd, args, should_kill


def would_take_over(s: Session, fork: bool = False) -> bool:
    """Whether resuming/relaunching `s` would first kill a live process (takeover).

    The single source of the "needs confirmation" decision for the UI: it reads
    `_resume_plan`'s `should_kill` so views never re-derive `s.alive and not
    s.current` themselves (CLAUDE.md: should_kill is single-point — re-derivation
    was the old divergence). The typed resume operations and the confirm gate
    thus agree by construction.
    """
    return _resume_plan(s, fork)[2]


def resume_cmd(s: Session, fork: bool = False) -> str:
    """Return a ready-to-copy command without serializing destructive state.

    A live session can change pid, process generation, cwd, or current-session
    status while a copied command waits in a clipboard. Carry only its durable
    sid and make ``csctl resume --take-over`` reacquire that evidence at
    execution time. Dead resumes and forks are non-destructive, so their
    direct commands remain useful. The take-over deferral is Claude-only
    (ADR-0005: the headless resolver reads registry + transcripts); a
    non-Claude command is always the direct provider resume — it never
    serializes a kill, so the destructive-state argument does not apply.
    """
    if s.provider == "claude" and s.alive and not fork:
        return shlex.join(["csctl", "resume", "--take-over", s.sid])

    cwd, args, _ = _resume_plan(s, fork)
    parts: list[str] = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    parts.append(shlex.join(args))
    return " && ".join(parts)


@dataclass(frozen=True)
class ResumeOutcome:
    success: bool
    detail: str = ""


@dataclass(frozen=True)
class TmuxResumeOutcome:
    target: str | None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.target is not None


def resolve_execution_session(sid: str) -> ExecutionSessionResolution:
    """Resolve one stable SID against one fresh liveness/transcript generation."""
    evidence = liveness.liveness_inputs()
    if not evidence.complete:
        return ExecutionSessionResolution(
            ExecutionSessionState.LIVENESS_INCOMPLETE,
            detail=issue_detail(evidence.issues),
        )
    transcript_scan = sessions.scan_result(evidence)
    if not transcript_scan.complete:
        return ExecutionSessionResolution(
            ExecutionSessionState.TRANSCRIPT_INCOMPLETE,
            detail=issue_detail(transcript_scan.issues),
        )
    matches = tuple(
        session for session in transcript_scan.sessions if session.sid == sid
    )
    if not matches:
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"missing session id {sid!r}",
        )
    if len(matches) != 1:
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"ambiguous session id {sid!r}; found {len(matches)} exact matches",
        )
    target = matches[0]
    if target.current:
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"session {sid!r} is the current session",
        )
    if not target.cwd or not os.path.isdir(target.cwd):
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"session {sid!r} has no usable execution-time cwd: {target.cwd!r}",
        )
    if target.alive:
        missing = []
        if target.pid is None:
            missing.append("pid")
        if not target.proc_start:
            missing.append("proc_start")
        if missing:
            return ExecutionSessionResolution(
                ExecutionSessionState.REFUSED,
                detail=(
                    f"live session {sid!r} has incomplete execution-time identity "
                    f"({', '.join(missing)})"
                ),
            )
    return ExecutionSessionResolution(
        ExecutionSessionState.RESOLVED,
        session=target,
    )


def _session_for_execution(
    session: Session,
    fork: bool,
) -> ExecutionSessionResolution:
    if not session.alive or fork:
        return ExecutionSessionResolution(
            ExecutionSessionState.RESOLVED,
            session=session,
        )
    if session.provider != "claude":
        # The Claude resolver below reads registry + transcripts; a live
        # non-Claude takeover re-resolves through its provider's argv scan
        # instead — same guarantee, different evidence: kill only on a fresh
        # whole Session, never on snapshot identity (ADR-0005).
        argv_resolution = providers.resolve_argv_execution(
            session.provider,
            session.sid,
        )
        if not argv_resolution.success:
            return ExecutionSessionResolution(
                ExecutionSessionState.REFUSED,
                detail=argv_resolution.detail,
            )
        return ExecutionSessionResolution(
            ExecutionSessionState.RESOLVED,
            session=argv_resolution.session,
        )
    return resolve_execution_session(session.sid)


def _required_takeover_failure(s: Session) -> str:
    """Return empty only when a required live takeover is proven successful."""
    if s.pid is None:
        return "live session takeover requires a pid"
    outcome = take_over_result(s.pid, s.proc_start)
    if outcome.success:
        return ""
    return outcome.detail or f"takeover {outcome.state.value}"


def _do_resume_resolved_result(s: Session, fork: bool = False) -> ResumeOutcome:
    """Execute a Session already selected for this execution generation."""
    cwd, args, should_kill = _resume_plan(s, fork)
    if should_kill:
        takeover_failure = _required_takeover_failure(s)
        if takeover_failure:
            return ResumeOutcome(False, takeover_failure)
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    # argv[0] IS the provider's binary (ADR-0005) — never a hardcoded CLI.
    os.execvp(args[0], args)
    return ResumeOutcome(True)


def do_resume_result(s: Session, fork: bool = False) -> ResumeOutcome:
    """Resolve live takeover identity, then kill-if-needed, chdir, and exec."""
    resolution = _session_for_execution(s, fork)
    if not resolution.success:
        return ResumeOutcome(False, resolution.detail)
    if resolution.session is None:
        raise AssertionError("successful session resolution must carry a Session")
    return _do_resume_resolved_result(resolution.session, fork)


def do_resume_sid_result(sid: str) -> ResumeOutcome:
    """Resolve an exact SID once, then perform a terminal takeover."""
    resolution = resolve_execution_session(sid)
    if not resolution.success:
        detail = resolution.detail
        if resolution.state is ExecutionSessionState.LIVENESS_INCOMPLETE:
            detail = f"liveness evidence is incomplete: {detail}"
        elif resolution.state is ExecutionSessionState.TRANSCRIPT_INCOMPLETE:
            detail = f"transcript inventory is incomplete: {detail}"
        return ResumeOutcome(False, detail)
    if resolution.session is None:
        raise AssertionError("successful session resolution must carry a Session")
    return _do_resume_resolved_result(resolution.session)


def _spawn_in_tmux_result(
    s: Session,
    fork: bool = False,
) -> TmuxResumeOutcome:
    """Resolve and kill-if-needed, then spawn in the per-project tmux session."""
    resolution = _session_for_execution(s, fork)
    if not resolution.success:
        return TmuxResumeOutcome(None, resolution.detail)
    if resolution.session is None:
        raise AssertionError("successful session resolution must carry a Session")
    target_session = resolution.session
    _, _, should_kill = _resume_plan(target_session, fork)
    if should_kill and not target_session.tmux_inventory_complete:
        detail = (
            target_session.tmux_inventory_detail
            or "tmux residency inventory incomplete"
        )
        return TmuxResumeOutcome(None, detail)
    if not fork:
        resident_target = attach_target(target_session)
        if resident_target is not None:
            return TmuxResumeOutcome(resident_target)
    if should_kill:
        takeover_failure = _required_takeover_failure(target_session)
        if takeover_failure:
            return TmuxResumeOutcome(None, takeover_failure)
    window = providers.get(target_session.provider).window_name(
        target_session.sid,
        fork,
    )
    cmd = tmux_foreground_cmd(target_session, fork)
    result = tmux.run_in_tmux_result(
        tmux.session_name_for(target_session.cwd),
        window,
        cmd,
    )
    target = result.target if result.success else None
    return TmuxResumeOutcome(target, result.diagnostic)


def attach_target(s: Session) -> str | None:
    """The tmux window ("session:index") already hosting this live session.

    Non-None means the session is tmux-resident and can be entered in place —
    no kill, no respawn. Reads the snapshot-computed `Session.tmux_target`
    (the SAME data the ⧉ badge renders — one source, no per-action
    re-detection), guarded on `alive` so a stale target on a dead session
    never answers."""
    if not s.alive:
        return None
    return s.tmux_target


def tmux_foreground_cmd(s: Session, fork: bool = False) -> str:
    """Shell command for a tmux-dispatch window: plain resume, NO
    --remote-control.

    Deliberate (ADR-0001): tmux residency is the anti-disconnect mechanism;
    every --remote-control process mints a new cloud environment entry, which
    piles up with frequent use — the Sessions tab never mints cloud envs."""
    cwd, args, _ = _resume_plan(s, fork)
    line = shlex.join(args)
    return f"cd {shlex.quote(cwd)} && {line}" if cwd else line


def do_tmux_resume_result(s: Session, fork: bool = False) -> TmuxResumeOutcome:
    """Typed tmux resume outcome retaining probe or spawn failure detail."""
    return _spawn_in_tmux_result(s, fork=fork)


def do_tmux_new_result(
    directory: str,
    provider_key: str = "claude",
) -> tmux.TmuxWriteResult:
    """Start a NEW session of `provider_key`'s CLI in `directory`, inside
    that project's own tmux session, retaining the exact create stage and
    failure detail.

    The 项目-tab launcher keys: same skeleton as `do_tmux_resume_result` but
    nothing exists yet — no kill, no confirm, no R10 gate (no process is
    terminated). The argv comes from the provider (ADR-0005); for claude it
    stays plain `claude` with NO --remote-control (same tradeoff as
    `tmux_foreground_cmd`: every RC process mints a new cloud environment
    entry). No trust gate either: the user lands inside the window, so each
    CLI's own trust/onboarding dialog shows interactively."""
    provider = providers.get(provider_key)
    line = shlex.join(provider.new_session_argv())
    cmd = f"cd {shlex.quote(directory)} && {line}"
    return tmux.run_in_tmux_result(
        tmux.session_name_for(directory),
        provider.key,
        cmd,
    )


# --- exit intents (the payload crossing the exit-then-exec seam) ------------
#
# The TUI cannot run `claude` (or replace itself with tmux) inside the urwid
# loop, so a resume-family action exits the MainLoop carrying ONE of these
# intents and the CLI TUI handler calls `intent.run()` afterwards. Each
# intent owns its own finalizer + failure messages, so adding a variant means
# adding one class here + one view call — app.py and cli.py stay untouched.


class ExitIntent:
    """What a view asks csctl to do AFTER the MainLoop exits."""

    def run(self) -> int:
        """Finalize outside the loop and return a process exit status."""
        raise NotImplementedError


@dataclass(frozen=True)
class ResumeIntent(ExitIntent):
    """`t` 终端接回: bare-terminal resume (execvp; takeover kill inside
    do_resume_result) — the fallback when tmux is unavailable or unwanted."""

    session: Session
    fork: bool = False

    def run(self) -> int:
        try:
            outcome = do_resume_result(self.session, fork=self.fork)
        except OSError as exc:
            print(
                f"Failed to resume session {self.session.sid} in the terminal: {exc}",
                file=sys.stderr,
            )
            return 1
        if not outcome.success:
            detail = f": {outcome.detail}" if outcome.detail else ""
            print(
                f"Terminal resume did not occur for session {self.session.sid}"
                f"{detail}.",
                file=sys.stderr,
            )
            return 1
        return 0


@dataclass(frozen=True)
class AttachIntent(ExitIntent):
    """Enter on a tmux-resident session: enter its window in place (no kill)."""

    target: str

    def run(self) -> int:
        try:
            entered = enter_window(self.target)
        except OSError as exc:
            print(
                f"Failed to enter tmux window {self.target}: {exc}",
                file=sys.stderr,
            )
            return 1
        if not entered:
            print(
                f"Failed to enter tmux window {self.target} (is tmux running?)",
                file=sys.stderr,
            )
            return 1
        return 0


@dataclass(frozen=True)
class TmuxResumeIntent(ExitIntent):
    """Enter/f on a dead / bare-terminal session: resume (or fork) inside
    tmux, then enter — the primary tmux-first dispatch (ADR-0001)."""

    session: Session
    fork: bool = False

    def run(self) -> int:
        outcome = do_tmux_resume_result(self.session, fork=self.fork)
        if outcome.target is None:
            detail = f": {outcome.detail}" if outcome.detail else ""
            print(
                f"Failed to resume the session inside tmux{detail}.",
                file=sys.stderr,
            )
            return 1
        target = outcome.target
        try:
            entered = enter_window(target)
        except OSError as exc:
            print(
                f"Session resumed in tmux window {target}, but attaching failed: {exc}",
                file=sys.stderr,
            )
            return 1
        if not entered:
            print(
                f"Session resumed in tmux window {target}, but attaching failed.",
                file=sys.stderr,
            )
            return 1
        return 0


@dataclass(frozen=True)
class TmuxNewIntent(ExitIntent):
    """项目-tab launcher: start a NEW session of one provider's CLI in tmux,
    then enter (pure spawn)."""

    directory: str
    provider: str = "claude"

    def run(self) -> int:
        result = do_tmux_new_result(self.directory, self.provider)
        if not result.success:
            detail = f": {result.diagnostic}" if result.diagnostic else ""
            print(
                f"Failed to start a new session inside tmux{detail}.",
                file=sys.stderr,
            )
            return 1
        target = result.target
        if target is None:
            raise AssertionError("successful tmux create must carry a target")
        try:
            entered = enter_window(target)
        except OSError as exc:
            print(
                f"Session started in tmux window {target}, but attaching failed: {exc}",
                file=sys.stderr,
            )
            return 1
        if not entered:
            print(
                f"Session started in tmux window {target}, but attaching failed.",
                file=sys.stderr,
            )
            return 1
        return 0


def enter_window(target: str) -> bool:
    """Bring `target` ("session:window") to the user's terminal foreground.

    Outside tmux: exec `tmux attach-session` — replaces the csctl process (does
    not return on success). Inside tmux ($TMUX set): `switch-client`, then
    return so the caller exits csctl normally — both paths end csctl, keeping
    "接回 = 离开 csctl" uniform. select-window failure is non-fatal (the user
    lands in the session and can pick the window by hand)."""
    tmux.select_window(target)
    session = target.split(":", 1)[0]
    if os.environ.get("TMUX"):
        return tmux.switch_client(target)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    return True  # unreachable after a successful exec; keeps type-checkers calm


def to_clipboard(text: str) -> bool:
    return clipboard.copy(text)

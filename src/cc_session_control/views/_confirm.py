"""Kill-confirm policy — ONE home for the gate order and the confirm 文案.

Every op that terminates a live process confirms via the app-level modal
(`App.confirm`); a takeover additionally consumes a prepared `/proc` probe and
refuses incomplete evidence (R10) BEFORE confirming. The 文案 follows the one
template `{动词}{对象}「name」？{后果}`
(接管类 "将先终止原进程。" / 停止类 "将终止其进程。"). Views call these
helpers instead of re-inlining the degrade-gate → confirm → act sequence.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..actions.session_ops import (
    AttachIntent,
    TmuxResumeIntent,
    attach_target,
    resume_cmd,
    would_take_over,
)
from ..data import proc
from ._rows import CONFIRM_NAME_CELLS, truncate_cells

if TYPE_CHECKING:
    from ..app import App
    from ..models import Session

# R10/D7: refusal shown when the "current" session can't be determined (no
# /proc) — session-keyed destructive ops are disabled rather than silently
# doing nothing.
DEGRADED = "liveness 降级：破坏性操作已禁用"


def stop_message(verb: str, name: str) -> str:
    """Confirm 文案 for a plain stop (kills the named thing's own process)."""
    return f"{verb}「{truncate_cells(name, CONFIRM_NAME_CELLS)}」？将终止其进程。"


def archived_notice(s: Session) -> str:
    """归档 refusal 文案: the provider-official un-archive command with a
    display-shortened sid — `y` copies the full command (`resume_cmd`'s
    archived branch, the same synthesis this reads, so they cannot drift)."""
    short_cmd = resume_cmd(s).replace(s.sid, f"{s.sid[:8]}…")
    return f"该会话已归档：先 {short_cmd} 恢复后再接回。"


def accept_ancestor_probe(app: App, evidence: proc.AncestorProbe) -> bool:
    """Consume prepared current-session protection evidence on the main loop."""
    if evidence.complete:
        return True
    app.notify(DEGRADED)
    return False


def confirm_stop(
    app: App,
    noun: str,
    name: str,
    on_yes: Callable[[], None],
    *,
    alive: bool,
    current: bool = False,
    gated: bool = True,
    evidence: proc.AncestorProbe | None = None,
) -> None:
    """The plain-stop twin of `confirm_takeover`: degrade-gate → alive →
    current(self-protect) → confirm, with the 文案 derived from one `noun`
    (停止{noun} / {noun}未在运行 / 不能停止当前{noun}).

    `gated=False` is reserved for a submitted mutation whose worker owns fresh
    typed process validation, or for a stop that does not signal a pid.
    Otherwise the caller must supply the probe prepared off-loop.
    """
    if gated:
        if evidence is None:
            raise RuntimeError("protected stop confirmation requires prepared evidence")
        if not accept_ancestor_probe(app, evidence):
            return
    if not alive:
        # 中西文混排: a latin-ending noun gets a space before 未.
        sep = " " if noun and noun[-1].isascii() else ""
        app.notify(f"{noun}{sep}未在运行")
        return
    if current:
        app.notify(f"不能停止当前{noun}")
        return
    app.confirm(stop_message(f"停止{noun}", name), on_yes)


def confirm_takeover(
    app: App,
    s: Session,
    verb: str,
    on_yes: Callable[[], None],
    *,
    name: str | None = None,
    fork: bool = False,
    gated: bool = True,
    evidence: proc.AncestorProbe | None = None,
) -> None:
    """Run `on_yes` now, or degrade-gate + confirm first on a live takeover.

    Reads `would_take_over` (= should_kill, the single source) so the confirm
    gate never re-derives the takeover condition. Gate order is fixed: the R10
    degrade refusal fires BEFORE the confirm modal — off `/proc` a live
    takeover cannot safely kill the old pid, and refusing here beats exiting
    the TUI only to have `do_resume_result` print its refusal. Resuming/relaunching a
    DEAD session kills nothing: no gate, no confirm (B3) — EXCEPT the honest
    unbound-live-hint confirm: a row flagged `Session.unbound_live_hint` may
    already be held by an unbindable bare TUI in its directory, so Enter/t/R
    confirm the double-attach risk first (still no kill, hence no R10 gate;
    a fork is a fresh copy and keeps falling straight through). ``gated=False``
    is reserved for callers carrying a complete typed liveness preparation, so
    confirmation does not mix generations. Otherwise the caller supplies the
    current-ancestor probe prepared off-loop.

    An archived row refuses FIRST — before the hint confirm, the R10 gate and
    the modal: every path from here would synthesize a resume/fork argv
    against the provider's archived store, which is unverified upstream
    semantics, so the notice hands back the official un-archive step instead.
    Only the in-place tmux attach in `confirm_tmux_takeover` (no resume argv
    at all) stays available for a live resident row.
    """
    if s.archived:
        app.notify(archived_notice(s))
        return
    shown = truncate_cells(s.label if name is None else name, CONFIRM_NAME_CELLS)
    if not would_take_over(s, fork):
        if s.unbound_live_hint and not fork:
            app.confirm(
                f"{verb}「{shown}」？该目录存在未绑定的 {s.provider} "
                "运行进程，恢复可能双开同一会话。",
                on_yes,
            )
            return
        on_yes()
        return
    if gated:
        if evidence is None:
            raise RuntimeError(
                "protected takeover confirmation requires prepared evidence"
            )
        if not accept_ancestor_probe(app, evidence):
            return
    app.confirm(f"{verb}「{shown}」？将先终止原进程。", on_yes)


def confirm_tmux_takeover(
    app: App,
    s: Session,
    verb: str,
    *,
    fork: bool = False,
    name: str | None = None,
    gated: bool = True,
    evidence: proc.AncestorProbe | None = None,
) -> None:
    """The tmux-first Enter/f body (ADR-0001).

    A tmux-resident session is entered IN PLACE (`AttachIntent` — no kill, no
    confirm, no R10 gate: nothing destructive happens). Anything else goes
    through the standard takeover gate (`confirm_takeover`) into
    `TmuxResumeIntent` — resume (or fork) inside a project-labelled window in
    the shared csctl tmux session, then enter. A fork is a copy: it never
    enters the original's window in place, it always spawns its own (and never
    kills, so the confirm path falls straight through). ``gated`` has the same
    typed-preparation contract as :func:`confirm_takeover`.
    """
    if not fork:
        target = attach_target(s)
        if target:
            app.exit_with(AttachIntent(target))
            return
    confirm_takeover(
        app,
        s,
        verb,
        lambda: app.exit_with(TmuxResumeIntent(s, fork=fork)),
        name=name,
        fork=fork,
        gated=gated,
        evidence=evidence,
    )

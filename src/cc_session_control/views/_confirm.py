"""Kill-confirm policy — ONE home for the gate order and the confirm 文案.

Every op that terminates a live process confirms via the app-level modal
(`App.confirm`); a takeover additionally refuses off `/proc` (R10) BEFORE
confirming. The 文案 follows the one template `{动词}{对象}「name」？{后果}`
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


def confirm_stop(
    app: App,
    noun: str,
    name: str,
    on_yes: Callable[[], None],
    *,
    alive: bool,
    current: bool = False,
    gated: bool = True,
) -> None:
    """The plain-stop twin of `confirm_takeover`: degrade-gate → alive →
    current(self-protect) → confirm, with the 文案 derived from one `noun`
    (停止{noun} / {noun}未在运行 / 不能停止当前{noun}).

    `gated=False` skips the R10 degrade gate for stops that don't signal a
    pid — the RC tab's stop kills a tmux window, not a process, so refusing
    it off `/proc` would be a gate it never needed.
    """
    if gated and not proc.probe_current_ancestors().complete:
        app.notify(DEGRADED)
        return
    if not alive:
        # 中西文混排: a latin-ending noun ("后台 agent") gets a space before 未.
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
) -> None:
    """Run `on_yes` now, or degrade-gate + confirm first on a live takeover.

    Reads `would_take_over` (= should_kill, the single source) so the confirm
    gate never re-derives the takeover condition. Gate order is fixed: the R10
    degrade refusal fires BEFORE the confirm modal — off `/proc` a live
    takeover cannot safely kill the old pid, and refusing here beats exiting
    the TUI only to have `do_resume` print its refusal. Resuming/relaunching a
    DEAD session kills nothing: no gate, no confirm (B3).
    """
    if not would_take_over(s, fork):
        on_yes()
        return
    if not proc.probe_current_ancestors().complete:
        app.notify(DEGRADED)
        return
    shown = truncate_cells(s.label if name is None else name, CONFIRM_NAME_CELLS)
    app.confirm(f"{verb}「{shown}」？将先终止原进程。", on_yes)


def confirm_tmux_takeover(
    app: App,
    s: Session,
    verb: str,
    *,
    fork: bool = False,
    name: str | None = None,
) -> None:
    """The tmux-first Enter/f body, shared by the 会话/后台 tabs (ADR-0001).

    A tmux-resident session is entered IN PLACE (`AttachIntent` — no kill, no
    confirm, no R10 gate: nothing destructive happens). Anything else goes
    through the standard takeover gate (`confirm_takeover`) into
    `TmuxResumeIntent` — resume (or fork) inside its per-project tmux window,
    then enter. A fork is a copy: it never enters the original's window in
    place, it always spawns its own (and never kills, so the confirm path
    falls straight through).
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
    )

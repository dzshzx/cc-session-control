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

from ..actions.session_ops import would_take_over
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
    if not proc.current_determinable():
        app.notify(DEGRADED)
        return
    shown = truncate_cells(s.label if name is None else name, CONFIRM_NAME_CELLS)
    app.confirm(f"{verb}「{shown}」？将先终止原进程。", on_yes)

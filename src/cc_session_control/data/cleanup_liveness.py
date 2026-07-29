"""Liveness assembly used by cleanup preview and destructive revalidation."""

from __future__ import annotations

from collections.abc import Sequence

from . import liveness
from .removal import CleanupExecution, CleanupIssue


def fresh_liveness_inputs() -> liveness.LivenessSnapshot:
    """Read every cleanup protection source with its cache disabled."""
    return liveness.liveness_inputs()


def refuse_incomplete_liveness(
    result: CleanupExecution,
    targets: Sequence[object],
    evidence: liveness.LivenessSnapshot,
) -> CleanupExecution:
    """Fail closed while retaining every unavailable protection source."""
    result.issues.extend(
        CleanupIssue(
            source=issue.source,
            error=issue.detail,
            path=issue.path,
        )
        for issue in evidence.issues
    )
    result.refuse(
        list(targets) or ["liveness evidence"],
        "liveness evidence incomplete; nothing deleted",
    )
    return result

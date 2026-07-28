"""Headless resume listing (`csctl resume`).

Filters, paginates, and renders scanned sessions as ready-to-copy resume
commands. Command synthesis and kill semantics stay in
`session_ops.resume_cmd` / `_resume_plan` — this module only selects and
formats; it must not re-derive takeover decisions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..models import Session
from .session_ops import resume_cmd


@dataclass(frozen=True)
class ResumeRenderIssue:
    """One transcript body failure encountered during keyword selection."""

    source: str
    path: str
    detail: str


@dataclass(frozen=True)
class ResumeRenderResult:
    """Complete rendered output, or typed body-read failures and no output."""

    text: str = ""
    issues: tuple[ResumeRenderIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


def keyword_matches(s: Session, keyword: str) -> bool:
    """Metadata first (sid / cwd / label), then fall back to transcript body.

    The body fallback is what lets "discussed in the conversation but not in
    the title" sessions be found; common words will over-match, which is the
    documented trade-off.
    """
    kw = keyword.strip().lower()
    if not kw:
        return True
    hay = " ".join([s.sid, s.cwd, s.label]).lower()
    if kw in hay:
        return True
    if not s.file:
        return False
    with open(s.file, errors="ignore") as fh:
        return any(kw in line.lower() for line in fh)


def paginate(
    rows: list[Session], page: int, limit: int, all_pages: bool
) -> tuple[list[Session], int, int]:
    """Return (page_rows, effective_page, total_pages). Page is clamped."""
    if all_pages or not rows:
        return rows, 1, 1
    limit = max(1, limit)
    pages = (len(rows) + limit - 1) // limit
    page = min(max(1, page), pages)
    return rows[(page - 1) * limit : page * limit], page, pages


def format_session(s: Session) -> list[str]:
    """Render one session as display lines: status header, label, command."""
    state = "live" if s.alive else "dead"
    flags = f"  [hidden:{','.join(sorted(s.hidden))}]" if s.hidden else ""
    when = time.strftime("%m-%d %H:%M", time.localtime(s.mtime))
    lines = [f"[{state}] {when}  {s.sid}{flags}", f"    {s.label}"]
    if s.current:
        lines.append(
            "    <- you are IN this session (no resume needed; "
            "kill it only to take over from elsewhere)"
        )
    else:
        lines.append(f"    {resume_cmd(s)}")
        if s.alive:
            lines.append(
                "    ^ live session: the kill stops its running process first "
                "(single timeline, no fork); to attach without interrupting, "
                "use the fleet view instead"
            )
    return lines


def render(
    rows: list[Session],
    keyword: str = "",
    page: int = 1,
    limit: int = 20,
    all_pages: bool = False,
) -> ResumeRenderResult:
    """Full `csctl resume` output for an already-scanned session list."""
    matched: list[Session] = []
    issues: list[ResumeRenderIssue] = []
    for session in rows:
        try:
            if keyword_matches(session, keyword):
                matched.append(session)
        except OSError as exc:
            issues.append(
                ResumeRenderIssue(
                    "session transcript body",
                    session.file or "",
                    str(exc),
                )
            )
    if issues:
        return ResumeRenderResult(issues=tuple(issues))
    if not matched:
        return ResumeRenderResult(
            f"No matching sessions{f' (keyword: {keyword})' if keyword else ''}."
        )

    page_rows, page, pages = paginate(matched, page, limit, all_pages)
    out: list[str] = []
    for s in page_rows:
        out.extend(format_session(s))
        out.append("")

    total = len(matched)
    if all_pages or pages == 1:
        out.append(f"-- {total} session(s) --")
    else:
        kw_part = f"{keyword} " if keyword else ""
        hints = []
        if page < pages:
            hints.append(f"next: csctl resume {kw_part}--page {page + 1}")
        hints.append(f"all: csctl resume {kw_part}--all")
        out.append(
            f"-- page {page}/{pages}, {total} session(s) --    " + " | ".join(hints)
        )
    return ResumeRenderResult("\n".join(out))

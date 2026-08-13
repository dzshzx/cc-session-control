"""Codex rollout file reading — bounded parses of one NDJSON session file.

Split out of `codex.py` for the 600-line budget (same sidecar discipline as
`codex_source.py` / `codex_trust.py`). Everything here reads ONE rollout path
and is shared by every codex instance (ADR-0008): the caps, the first-line
`session_meta` parse, and the bounded label fallback are identity-agnostic.

No function here raises on external state or contributes completeness
evidence beyond its return value — the caller (`CodexProvider._scan_root`)
owns issue reporting.
"""

from __future__ import annotations

import json

# First-line pre-check (same cheap-substring-guard pattern as transcripts.py).
_META_MARK = b'"session_meta"'
# 256KB — real-machine session_meta lines have been observed at ~35KB, so the
# old 64KB cap left under 2x headroom; a line that reaches this cap without a
# trailing newline is truncated, not malformed (see `read_meta`).
FIRST_LINE_CAP = 256 * 1024

# Label-fallback continuation read past the session_meta line: bounded by
# BOTH line count and byte count so a huge rollout is never read in full —
# same cheap-substring-guard discipline as `read_meta`/transcripts.py.
_USER_MSG_MARK = b'"user_message"'
_BODY_SCAN_MAX_LINES = 64
_BODY_SCAN_MAX_BYTES = 128 * 1024


def read_meta(path: str) -> tuple[dict | None, bool, bool]:
    """(parsed `session_meta` payload or None, first-line-was-empty,
    first-line-was-truncated-by-the-cap).

    A line that reads out to exactly `FIRST_LINE_CAP` bytes without a
    trailing newline was cut off by the bounded read, not malformed by
    upstream — that's a distinct, honest cause from a genuine parse failure
    and must not be blamed on "upstream format change?" (see `discover`).
    """
    with open(path, "rb") as fh:
        raw = fh.readline(FIRST_LINE_CAP)
    if not raw.strip():
        return None, True, False
    if len(raw) == FIRST_LINE_CAP and not raw.endswith(b"\n"):
        return None, False, True
    if _META_MARK not in raw:
        return None, False, False
    try:
        record = json.loads(raw)
    except ValueError:
        return None, False, False
    payload = record.get("payload")
    return (
        (payload, False, False) if isinstance(payload, dict) else (None, False, False)
    )


def _clean_label(text: str) -> str:
    """Collapse whitespace/newlines the same way transcripts.py cleans prompts."""
    return " ".join(text.split()).strip()


def _is_wrapper_block(text: str) -> bool:
    """A leading `<...>` block (`<user_instructions>`, `<environment_context>`,
    …) is injected context, not something the operator typed — skip it."""
    return text.startswith("<")


def first_user_message(path: str) -> str | None:
    """Bounded continuation read past the session_meta first line: the first
    real `user_message` event body, cleaned — or None if nothing usable
    turns up within the line/byte caps. Malformed lines are skipped
    silently; this is a best-effort label fallback, not a completeness
    signal, so it never raises and never contributes an `InventoryIssue`.
    """
    try:
        with open(path, "rb") as fh:
            fh.readline(FIRST_LINE_CAP)  # skip the already-parsed session_meta line
            total_bytes = 0
            for line_number, raw in enumerate(fh, start=1):
                total_bytes += len(raw)
                if (
                    line_number > _BODY_SCAN_MAX_LINES
                    or total_bytes > _BODY_SCAN_MAX_BYTES
                ):
                    return None
                if _USER_MSG_MARK not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue  # malformed body line — keep scanning
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                event_payload = record.get("payload")
                if not isinstance(event_payload, dict):
                    continue
                if event_payload.get("type") != "user_message":
                    continue
                message = event_payload.get("message")
                if not isinstance(message, str):
                    continue
                cleaned = _clean_label(message)
                if not cleaned or _is_wrapper_block(cleaned):
                    continue
                return cleaned
    except OSError:
        return None
    return None

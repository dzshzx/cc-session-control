"""Codex launch-surface classification (pure), split out of `codex.py` so
that file stays under the 600-line budget.

One PURE mapping: a `session_meta` payload → the coarse `Session.source`
bucket the Sessions tab badges. Unknown originator/source shapes fall through
to the honest "cli" default — exact matches only, never prefix/substring
guesses (the same discipline as the sid extractor).
"""

from __future__ import annotations

# The only ChatGPT-mobile-remote originator observed on this machine (real
# rollout `session_meta`, 2026-08-04).
_ANDROID_REMOTE_ORIGINATOR = "codex_chatgpt_android_remote"

# Codex Desktop writes `source: "vscode"` for its main threads (it reuses the
# IDE extension's session pipeline) — only `originator` tells it apart from a
# real VS Code session (real rollouts sampled 2026-08-12: 14 desktop rows
# with source "vscode", cli_version 0.130.0–0.147.0).
_DESKTOP_ORIGINATOR = "Codex Desktop"


def classify_source(payload: dict) -> str:
    """PURE: coarse `Session.source` bucket from a codex session_meta payload.

    Priority (highest first, first match wins):
      1. `originator == "codex_exec"` — the pre-existing bridge/SDK signal
         (`Session.bridge_or_sdk` keys off `source == "sdk"`); a headless
         exec run must stay hidden-by-default no matter what else is set.
      2. `originator == _DESKTOP_ORIGINATOR` (exact) — Codex Desktop launches;
         checked BEFORE the vscode source because Desktop main threads reuse
         the IDE pipeline and carry `source == "vscode"`.
      3. session_meta `source == "vscode"` — reuses the IDE badge Claude
         rows already use for `entrypoint == "claude-vscode"`, so
         IDE-launched codex sessions get an honest badge instead of "CLI".
      4. `originator == _ANDROID_REMOTE_ORIGINATOR` (exact) — ChatGPT mobile/
         remote launches, which are typically app-server-hosted and often
         show dead in `/proc`; the "CLI" badge would wrongly imply a direct
         terminal takeover is available.
      5. Otherwise "cli" — the honest default for a real interactive CLI
         session and for any originator/source this machine hasn't observed.
    """
    if payload.get("originator") == "codex_exec":
        return "sdk"
    if payload.get("originator") == _DESKTOP_ORIGINATOR:
        return "desktop"
    if payload.get("source") == "vscode":
        return "vscode"
    if payload.get("originator") == _ANDROID_REMOTE_ORIGINATOR:
        return "remote"
    return "cli"

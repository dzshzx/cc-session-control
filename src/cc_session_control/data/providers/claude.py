"""Claude Code provider — argv synthesis and capabilities (ADR-0005).

Discovery/liveness stay in the `data/` engine (`sessions.scan_result`,
`liveness.*`); this module owns only what the provider protocol needs
uniformly: identity, caps, and command synthesis. `actions.session_ops`
routes every resume/new argv through here — never inline `["claude", ...]`
elsewhere.
"""

from __future__ import annotations

from ...config import cfg
from .base import LivenessGrade, ProviderCaps


class ClaudeProvider:
    key = "claude"
    label = "cc"
    caps = ProviderCaps(
        fork=True,
        takeover=True,
        background_agents=True,
        remote_control=True,
        cleanup=True,
        liveness=LivenessGrade.FULL,
    )

    def available(self) -> bool:
        return cfg.claude_home.is_dir()

    def resume_argv(self, sid: str, fork: bool = False) -> list[str]:
        args = ["claude", "--resume", sid]
        if fork:
            args.append("--fork-session")
        return args

    def new_session_argv(self) -> list[str]:
        return ["claude"]

    def window_name(self, sid: str, fork: bool = False) -> str:
        # Bare <sid8> keeps continuity with pre-provider window names.
        return f"{sid[:8]}-fork" if fork else sid[:8]

"""Suite-wide fixtures.

Unit tests must not depend on which agent CLIs happen to exist on the
machine running them (`~/.claude`, `~/.codex`, `~/.kimi-code` — absent on
CI, present on dev boxes), so provider activation is made deterministic:
the allowlist defaults to Claude-only and the Claude provider reads as
locally present. Multi-provider tests opt in by re-setting `cfg.providers`
/ stubbing `active_providers` and monkeypatching the provider homes
themselves.
"""

from __future__ import annotations

import pytest

from cc_session_control.config import cfg
from cc_session_control.data.providers.claude import ClaudeProvider


@pytest.fixture(autouse=True)
def _claude_only_providers(monkeypatch):
    monkeypatch.setattr(cfg, "providers", ("claude",))
    monkeypatch.setattr(ClaudeProvider, "available", lambda self: True)

"""Suite-wide fixtures.

Unit tests must not depend on which agent CLIs happen to exist on the
machine running them (`~/.claude`, `~/.codex`, `~/.kimi-code` — absent on
CI, present on dev boxes), so provider activation is made deterministic:
the allowlist defaults to Claude-only and the Claude provider reads as
locally present. Multi-provider tests opt in by re-setting `cfg.providers`
/ stubbing `active_providers` and monkeypatching the provider homes
themselves.

The provider registry (ADR-0008) adds two more machine dependencies to
neutralize: it CACHES its instance table process-wide, and it builds that
table from the operator's `providers.json`. Both are reset per test — a
dev box's real declaration must not leak in, and one test's declaration
must not leak forward.
"""

from __future__ import annotations

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.providers.claude import ClaudeProvider


@pytest.fixture(autouse=True)
def _claude_only_providers(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "providers", ("claude",))
    monkeypatch.setattr(ClaudeProvider, "available", lambda self: True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-default"))
    providers.reset()
    yield
    providers.reset()

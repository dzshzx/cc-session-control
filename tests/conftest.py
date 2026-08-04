"""Suite-wide fixtures.

Unit tests must not depend on which agent CLIs happen to exist on the dev
machine (`~/.codex`, `~/.kimi-code`), so provider discovery defaults to
Claude-only; multi-provider tests opt in by re-setting `cfg.providers`
(and monkeypatching the provider homes) themselves.
"""

from __future__ import annotations

import pytest

from cc_session_control.config import cfg


@pytest.fixture(autouse=True)
def _claude_only_providers(monkeypatch):
    monkeypatch.setattr(cfg, "providers", ("claude",))

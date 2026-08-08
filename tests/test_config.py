"""Configuration invariants for tmux session ownership."""

import pytest

from cc_session_control.config import Config


def test_tmux_sessions_are_distinct_by_default(monkeypatch):
    monkeypatch.delenv("CSCTL_RC_SESSION", raising=False)

    config = Config()

    assert config.tmux_session == "csctl"
    assert config.rc_session == "rc"


def test_rc_session_cannot_reuse_the_workbench_session(monkeypatch):
    monkeypatch.setenv("CSCTL_RC_SESSION", "csctl")

    with pytest.raises(ValueError, match="must differ from the reserved"):
        Config()


@pytest.mark.parametrize(
    "value",
    ["", "cs*", "rc?", "rc[1]", "=csctl", "rc:0", "$1", "@legacy", "%3"],
)
def test_rc_session_rejects_tmux_target_expressions(monkeypatch, value):
    monkeypatch.setenv("CSCTL_RC_SESSION", value)

    with pytest.raises(ValueError, match="literal tmux session name"):
        Config()

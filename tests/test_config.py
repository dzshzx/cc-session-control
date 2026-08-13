"""Configuration invariants for tmux session ownership."""

from cc_session_control.config import Config


def test_workbench_tmux_session_is_fixed():
    # The unified workbench session is a fixed name (ADR-0006), not env-driven.
    assert Config().tmux_session == "csctl"

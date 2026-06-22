"""Smoke tests — basic sanity checks that don't require Claude or tmux."""

import subprocess
import sys
from pathlib import Path


def test_version_flag():
    result = subprocess.run(
        [sys.executable, "-m", "cc_session_control", "--version"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "csctl" in result.stdout


def test_help_flag():
    result = subprocess.run(
        [sys.executable, "-m", "cc_session_control", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Remote Control" in result.stdout or "rc" in result.stdout


def test_config_detect_workspace():
    from cc_session_control.config import cfg
    ws = cfg.workspace
    assert isinstance(ws, Path)


def test_clipboard_importable():
    from cc_session_control import clipboard
    assert hasattr(clipboard, "copy")


def test_models_importable():
    from cc_session_control.models import RCProject, Session
    s = Session(sid="test", cwd="/tmp", label="test", mtime=0.0,
                prompts=0, pid=None, alive=False, current=False)
    assert s.sid == "test"
    p = RCProject(name="proj", directory="/tmp/proj", trusted=True,
                  in_list=False, status="stopped", auto_start=False)
    assert p.name == "proj"

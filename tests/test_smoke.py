"""Smoke tests — basic sanity checks that don't require Claude or tmux."""

import os
import subprocess
import sys

import pytest


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
    assert "rc" in result.stdout


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


def test_urwid_importable():
    import urwid
    assert hasattr(urwid, "MainLoop")


def test_app_instantiation():
    from cc_session_control.app import App
    app = App()
    assert app.result is None
    assert len(app.views) == 3


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("CSCTL_RC_STAGGER", "not-a-number"),
        ("CSCTL_RC_STAGGER", "-1"),
        ("CSCTL_CLEANUP_AGE_DAYS", "0"),
    ],
)
def test_invalid_integer_environment_exits_two_without_traceback(name, raw):
    env = os.environ.copy()
    env[name] = raw

    result = subprocess.run(
        [
            sys.executable, "-m", "cc_session_control",
            "rc", "add", "/definitely/not/a/csctl/project",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert f"{name}={raw!r}" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_help_and_version_ignore_invalid_runtime_environment(flag):
    env = os.environ.copy()
    env["CSCTL_RC_STAGGER"] = "broken"

    result = subprocess.run(
        [sys.executable, "-m", "cc_session_control", flag],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "Traceback" not in result.stderr


def test_tui_startup_rejects_invalid_environment_before_opening_ui():
    env = os.environ.copy()
    env["CSCTL_CLEANUP_AGE_DAYS"] = "-1"

    result = subprocess.run(
        [sys.executable, "-m", "cc_session_control"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert "CSCTL_CLEANUP_AGE_DAYS='-1'" in result.stderr
    assert "Traceback" not in result.stderr

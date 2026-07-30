"""RC pane env-id caching at its public data-module seam."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module

import pytest

from cc_session_control.data import rc_environment, tmux
from cc_session_control.data.tmux import TmuxWindow


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _window(wid: str = "@1", pid: int | None = 101) -> TmuxWindow:
    return TmuxWindow(wid, "same-name", False, pid, "/same/path")


def _replace_tmux_with_python(monkeypatch, source: str):
    real_popen = tmux.subprocess.Popen
    calls = []
    processes = []

    def popen(argv, **kwargs):
        calls.append(argv)
        process = real_popen([sys.executable, "-c", source], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)
    return calls, processes


def test_tmux_reexports_pure_outcome_types_from_the_split_module():
    outcomes = import_module("cc_session_control.data.tmux_outcomes")

    for name in (
        "TmuxWindow",
        "TmuxIssue",
        "WindowInventory",
        "PaneCaptureIssue",
        "PaneCaptureResult",
        "KillState",
        "KillResult",
        "TmuxPane",
        "ResidencyIssue",
        "PaneInventory",
        "ResidencyInventory",
        "SessionWindowResult",
    ):
        assert getattr(tmux, name) is getattr(outcomes, name)


def _text_capture(fn):
    """Wrap a legacy `target -> str` capture as the typed `resolve_result`
    seam expects (the composition the removed `EnvironmentIdCache.resolve`
    compatibility wrapper used to perform)."""

    def capture(target: str) -> tmux.PaneCaptureResult:
        return tmux.PaneCaptureResult(target, fn(target))

    return capture


def test_successful_capture_is_reused_for_the_same_window_and_pid():
    calls: list[str] = []
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        calls.append(target)
        return "env_STABLE"

    capture = _text_capture(capture)

    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_STABLE"
    }
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_STABLE"
    }
    assert calls == ["@1"]


def test_pid_change_invalidates_immediately_even_when_name_and_path_match():
    calls: list[str] = []
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        calls.append(target)
        return f"env_{len(calls)}"

    capture = _text_capture(capture)

    assert dict(cache.resolve_result([_window(pid=101)], capture).environment_ids) == {
        "@1": "env_1"
    }
    assert dict(cache.resolve_result([_window(pid=202)], capture).environment_ids) == {
        "@1": "env_2"
    }
    assert calls == ["@1", "@1"]
    assert len(cache) == 1


def test_disappeared_windows_are_pruned_and_do_not_reuse_old_ids():
    calls: list[str] = []
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        calls.append(target)
        return f"env_{len(calls)}"

    capture = _text_capture(capture)

    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_1"
    }
    assert dict(cache.resolve_result([], capture).environment_ids) == {}
    assert len(cache) == 0
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_2"
    }


def test_negative_capture_retries_only_after_bounded_exponential_backoff():
    clock = Clock()
    calls: list[float] = []
    cache = rc_environment.EnvironmentIdCache(
        clock=clock,
        initial_backoff=2.0,
        maximum_backoff=8.0,
    )

    def capture(target: str) -> str:
        calls.append(clock.now)
        return ""

    capture = _text_capture(capture)

    cache.resolve_result([_window()], capture)  # miss; next at 2
    clock.advance(1.0)
    cache.resolve_result([_window()], capture)  # too early
    clock.advance(1.0)
    cache.resolve_result([_window()], capture)  # miss; next at 6
    clock.advance(3.0)
    cache.resolve_result([_window()], capture)  # too early
    clock.advance(1.0)
    cache.resolve_result([_window()], capture)  # miss; next at 14
    clock.advance(8.0)
    cache.resolve_result([_window()], capture)  # miss; cap stays 8
    clock.advance(8.0)
    cache.resolve_result([_window()], capture)

    assert calls == [0.0, 2.0, 6.0, 14.0, 22.0]


def test_negative_capture_eventually_picks_up_a_delayed_environment_id():
    clock = Clock()
    responses = iter(("", "", "env_LATE"))
    calls = 0
    cache = rc_environment.EnvironmentIdCache(
        clock=clock,
        initial_backoff=1.0,
        maximum_backoff=4.0,
    )

    def capture(target: str) -> str:
        nonlocal calls
        calls += 1
        return next(responses)

    capture = _text_capture(capture)

    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {}
    clock.advance(0.5)
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {}
    clock.advance(0.5)
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {}
    clock.advance(2.0)
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_LATE"
    }
    clock.advance(100.0)
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_LATE"
    }
    assert calls == 3


def test_capture_failure_stays_visible_during_backoff_then_success_clears_it():
    clock = Clock()
    calls = 0
    cache = rc_environment.EnvironmentIdCache(
        clock=clock,
        initial_backoff=1.0,
        maximum_backoff=4.0,
    )

    def capture(target: str) -> tmux.PaneCaptureResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return tmux.PaneCaptureResult(
                target,
                issue=tmux.PaneCaptureIssue(
                    "tmux capture-pane",
                    target,
                    "timed out after 5 seconds",
                ),
            )
        return tmux.PaneCaptureResult(target, "server starting")

    failed = cache.resolve_result([_window()], capture)
    clock.advance(0.5)
    backed_off = cache.resolve_result([_window()], capture)
    clock.advance(0.5)
    recovered = cache.resolve_result([_window()], capture)

    assert failed.environment_ids == {}
    assert failed.issues == backed_off.issues
    assert failed.issues[0].source == "tmux capture-pane"
    assert failed.issues[0].target == "@1"
    assert failed.issues[0].path == "/same/path"
    assert failed.issues[0].detail == "timed out after 5 seconds"
    assert recovered.environment_ids == {}
    assert recovered.issues == ()
    assert calls == 2


def test_disappeared_window_prunes_cached_capture_issue():
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> tmux.PaneCaptureResult:
        return tmux.PaneCaptureResult(
            target,
            issue=tmux.PaneCaptureIssue(
                "tmux capture-pane",
                target,
                "timed out after 5 seconds",
            ),
        )

    assert cache.resolve_result([_window()], capture).issues

    disappeared = cache.resolve_result([], capture)

    assert disappeared.issues == ()
    assert len(cache) == 0


def test_explicit_window_and_global_invalidation_force_recapture():
    calls = 0
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        nonlocal calls
        calls += 1
        return f"env_{calls}"

    capture = _text_capture(capture)

    cache.resolve_result([_window()], capture)
    cache.invalidate_window("@1")
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_2"
    }
    cache.invalidate_all()
    assert dict(cache.resolve_result([_window()], capture).environment_ids) == {
        "@1": "env_3"
    }


def test_tmux_capture_keeps_only_the_first_two_thousand_lines(monkeypatch):
    calls, _processes = _replace_tmux_with_python(
        monkeypatch,
        "for number in range(2025): print(f'line-{number}')",
    )

    captured = tmux.capture_pane_result("@1").text

    assert len(captured.splitlines()) == 2_000
    assert captured.splitlines()[0] == "line-0"
    assert captured.splitlines()[-1] == "line-1999"
    assert calls == [
        ["tmux", "capture-pane", "-p", "-S", "-2000", "-E", "-", "-t", "@1"]
    ]


def test_tmux_capture_caps_utf8_bytes_without_splitting_a_character(monkeypatch):
    calls, _processes = _replace_tmux_with_python(
        monkeypatch,
        "import sys; sys.stdout.write('environment=env_KEPT\\n' + '界' * 400_000)",
    )

    result = tmux.capture_pane_result("@1")
    captured = result.text

    assert result.success is True
    assert result.truncated is True
    assert len(captured.encode("utf-8")) == 1_048_575
    assert captured.endswith("界")
    assert rc_environment.extract_env_id(captured) == "env_KEPT"
    assert calls == [
        ["tmux", "capture-pane", "-p", "-S", "-2000", "-E", "-", "-t", "@1"]
    ]


def test_tmux_capture_stops_and_reaps_a_producer_at_the_byte_limit(monkeypatch):
    real_popen = tmux.subprocess.Popen
    processes = []

    class TrackedProcess:
        def __init__(self, process):
            self._process = process
            self.communicate_called = False
            self.terminate_called = False
            self.kill_called = False
            self.wait_calls = 0

        def __getattr__(self, name):
            return getattr(self._process, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._process.__exit__(*args)

        def communicate(self, *args, **kwargs):
            self.communicate_called = True
            raise AssertionError("capture must not buffer stdout via communicate()")

        def terminate(self):
            self.terminate_called = True
            return self._process.terminate()

        def kill(self):
            self.kill_called = True
            return self._process.kill()

        def wait(self, *args, **kwargs):
            self.wait_calls += 1
            return self._process.wait(*args, **kwargs)

    def popen(_argv, **kwargs):
        tracked = TrackedProcess(
            real_popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 4_194_304)",
                ],
                **kwargs,
            )
        )
        processes.append(tracked)
        return tracked

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    result = tmux.capture_pane_result("@1")
    captured = result.text

    assert result.success is True
    assert result.truncated is True
    assert len(captured.encode("utf-8")) == 1_048_576
    assert processes[0].communicate_called is False
    assert processes[0].terminate_called or processes[0].kill_called
    assert processes[0].wait_calls >= 1
    assert processes[0].returncode is not None


def test_tmux_capture_drains_large_stderr_and_returns_empty_on_nonzero(monkeypatch):
    _calls, processes = _replace_tmux_with_python(
        monkeypatch,
        (
            "import sys; "
            "sys.stdout.write('environment=env_MUST_NOT_ESCAPE'); "
            "sys.stdout.flush(); "
            "sys.stderr.buffer.write(b'e' * 4_194_304); "
            "raise SystemExit(7)"
        ),
    )

    assert tmux.capture_pane_result("@1").text == ""
    assert processes[0].returncode == 7


def test_tmux_capture_result_reports_nonzero_exit_without_returning_stdout(
    monkeypatch,
):
    _calls, processes = _replace_tmux_with_python(
        monkeypatch,
        (
            "import sys; "
            "sys.stdout.write('environment=env_MUST_NOT_ESCAPE'); "
            "sys.stdout.flush(); "
            "raise SystemExit(7)"
        ),
    )

    result = tmux.capture_pane_result("@1")

    assert result.text == ""
    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@1"
    assert result.issue.detail == "exited with status 7"
    assert processes[0].returncode == 7


def test_tmux_capture_timeout_terminates_and_reaps_without_sleep(monkeypatch):
    _calls, processes = _replace_tmux_with_python(
        monkeypatch,
        "import signal; signal.pause()",
    )
    monkeypatch.setattr(tmux, "_TMUX_TIMEOUT_SECONDS", 0.05)

    result = tmux.capture_pane_result("@1")

    assert result.text == ""
    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@1"
    assert result.issue.detail == "timed out after 0.05 seconds"
    assert processes[0].returncode is not None


def test_tmux_capture_spawn_oserror_returns_empty(monkeypatch):
    def popen(*_args, **_kwargs):
        raise FileNotFoundError("tmux missing")

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    assert tmux.capture_pane_result("@1").text == ""


def test_tmux_capture_result_bounds_spawn_failure_detail(monkeypatch):
    def popen(*_args, **_kwargs):
        raise FileNotFoundError("x" * 2_000)

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    result = tmux.capture_pane_result("@spawn")

    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@spawn"
    assert result.issue.detail.startswith("spawn failed: ")
    assert len(result.issue.detail) <= 512


def test_tmux_capture_result_does_not_hide_programming_errors(monkeypatch):
    def popen(*_args, **_kwargs):
        raise RuntimeError("capture invariant broken")

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    with pytest.raises(RuntimeError, match="capture invariant broken"):
        tmux.capture_pane_result("@bug")


def test_tmux_capture_result_reports_selector_failure(monkeypatch):
    def selector():
        raise OSError("selector unavailable")

    monkeypatch.setattr(tmux.selectors, "DefaultSelector", selector)

    result = tmux.capture_pane_result("@selector")

    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@selector"
    assert result.issue.detail == "selector failed: selector unavailable"


def test_tmux_capture_result_reports_selector_close_failure_and_reaps_child(
    monkeypatch,
):
    real_selector = tmux.selectors.DefaultSelector
    _calls, processes = _replace_tmux_with_python(monkeypatch, "print('ready')")

    class FailClose:
        def __init__(self):
            self._selector = real_selector()

        def __getattr__(self, name):
            return getattr(self._selector, name)

        def close(self):
            self._selector.close()
            raise OSError("selector close denied")

    monkeypatch.setattr(tmux.selectors, "DefaultSelector", FailClose)

    result = tmux.capture_pane_result("@selector-close")

    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@selector-close"
    assert result.issue.detail == "selector close failed: selector close denied"
    assert processes[0].returncode is not None


def test_tmux_capture_result_reports_wait_failure_and_reaps_child(monkeypatch):
    real_popen = tmux.subprocess.Popen
    processes = []

    class FailFirstWait:
        def __init__(self, process):
            self._process = process
            self.wait_calls = 0

        def __getattr__(self, name):
            return getattr(self._process, name)

        def wait(self, *args, **kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise OSError("w" * 2_000)
            return self._process.wait(*args, **kwargs)

    def popen(_argv, **kwargs):
        process = FailFirstWait(
            real_popen(
                [sys.executable, "-c", "print('ready')"],
                **kwargs,
            ),
        )
        processes.append(process)
        return process

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    result = tmux.capture_pane_result("@wait")

    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@wait"
    assert result.issue.detail.startswith("wait failed: ")
    assert len(result.issue.detail) <= 512
    assert processes[0].wait_calls >= 2
    assert processes[0].returncode is not None


def test_tmux_capture_result_reports_read_failure_and_reaps_child(monkeypatch):
    real_popen = tmux.subprocess.Popen
    processes = []

    def fail_read(_fd, _size):
        raise OSError("read denied")

    def popen(_argv, **kwargs):
        process = real_popen(
            [
                sys.executable,
                "-c",
                (
                    "import signal, sys; "
                    "sys.stdout.write('ready'); "
                    "sys.stdout.flush(); "
                    "signal.pause()"
                ),
            ],
            **kwargs,
        )
        processes.append(process)
        monkeypatch.setattr(tmux.os, "read", fail_read)
        return process

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    result = tmux.capture_pane_result("@read")

    assert result.success is False
    assert result.issue is not None
    assert result.issue.source == "tmux capture-pane"
    assert result.issue.target == "@read"
    assert result.issue.detail == "read failed: read denied"
    assert processes[0].returncode is not None

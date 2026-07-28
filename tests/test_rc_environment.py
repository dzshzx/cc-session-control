"""RC pane env-id caching at its public data-module seam."""

from __future__ import annotations

import sys
from dataclasses import dataclass

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


def test_successful_capture_is_reused_for_the_same_window_and_pid():
    calls: list[str] = []
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        calls.append(target)
        return "env_STABLE"

    assert cache.resolve([_window()], capture) == {"@1": "env_STABLE"}
    assert cache.resolve([_window()], capture) == {"@1": "env_STABLE"}
    assert calls == ["@1"]


def test_pid_change_invalidates_immediately_even_when_name_and_path_match():
    calls: list[str] = []
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        calls.append(target)
        return f"env_{len(calls)}"

    assert cache.resolve([_window(pid=101)], capture) == {"@1": "env_1"}
    assert cache.resolve([_window(pid=202)], capture) == {"@1": "env_2"}
    assert calls == ["@1", "@1"]
    assert len(cache) == 1


def test_disappeared_windows_are_pruned_and_do_not_reuse_old_ids():
    calls: list[str] = []
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        calls.append(target)
        return f"env_{len(calls)}"

    assert cache.resolve([_window()], capture) == {"@1": "env_1"}
    assert cache.resolve([], capture) == {}
    assert len(cache) == 0
    assert cache.resolve([_window()], capture) == {"@1": "env_2"}


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

    cache.resolve([_window()], capture)  # miss; next at 2
    clock.advance(1.0)
    cache.resolve([_window()], capture)  # too early
    clock.advance(1.0)
    cache.resolve([_window()], capture)  # miss; next at 6
    clock.advance(3.0)
    cache.resolve([_window()], capture)  # too early
    clock.advance(1.0)
    cache.resolve([_window()], capture)  # miss; next at 14
    clock.advance(8.0)
    cache.resolve([_window()], capture)  # miss; cap stays 8
    clock.advance(8.0)
    cache.resolve([_window()], capture)

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

    assert cache.resolve([_window()], capture) == {}
    clock.advance(0.5)
    assert cache.resolve([_window()], capture) == {}
    clock.advance(0.5)
    assert cache.resolve([_window()], capture) == {}
    clock.advance(2.0)
    assert cache.resolve([_window()], capture) == {"@1": "env_LATE"}
    clock.advance(100.0)
    assert cache.resolve([_window()], capture) == {"@1": "env_LATE"}
    assert calls == 3


def test_explicit_window_and_global_invalidation_force_recapture():
    calls = 0
    cache = rc_environment.EnvironmentIdCache(clock=Clock())

    def capture(target: str) -> str:
        nonlocal calls
        calls += 1
        return f"env_{calls}"

    cache.resolve([_window()], capture)
    cache.invalidate_window("@1")
    assert cache.resolve([_window()], capture) == {"@1": "env_2"}
    cache.invalidate_all()
    assert cache.resolve([_window()], capture) == {"@1": "env_3"}


def test_tmux_capture_keeps_only_the_first_two_thousand_lines(monkeypatch):
    calls, _processes = _replace_tmux_with_python(
        monkeypatch,
        "for number in range(2025): print(f'line-{number}')",
    )

    captured = tmux.capture_pane("@1")

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

    captured = tmux.capture_pane("@1")

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

    captured = tmux.capture_pane("@1")

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

    assert tmux.capture_pane("@1") == ""
    assert processes[0].returncode == 7


def test_tmux_capture_timeout_terminates_and_reaps_without_sleep(monkeypatch):
    _calls, processes = _replace_tmux_with_python(
        monkeypatch,
        "import signal; signal.pause()",
    )
    monkeypatch.setattr(tmux, "_TMUX_TIMEOUT_SECONDS", 0.05)

    assert tmux.capture_pane("@1") == ""
    assert processes[0].returncode is not None


def test_tmux_capture_spawn_oserror_returns_empty(monkeypatch):
    def popen(*_args, **_kwargs):
        raise FileNotFoundError("tmux missing")

    monkeypatch.setattr(tmux.subprocess, "Popen", popen)

    assert tmux.capture_pane("@1") == ""

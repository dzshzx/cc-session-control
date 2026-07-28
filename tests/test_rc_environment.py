"""RC pane env-id caching at its public data-module seam."""

from __future__ import annotations

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


def test_tmux_capture_uses_finite_range_and_caps_returned_text(monkeypatch):
    calls: list[list[str]] = []

    class Captured:
        returncode = 0
        stdout = "environment=env_KEPT\n" + "x" * 1_048_576

    def run(args: list[str]) -> Captured:
        calls.append(args)
        return Captured()

    monkeypatch.setattr(tmux, "_tmux_run", run)

    captured = tmux.capture_pane("@1")

    assert calls == [["capture-pane", "-p", "-S", "-2000", "-E", "-", "-t", "@1"]]
    assert len(captured) == 1_048_576
    assert rc_environment.extract_env_id(captured) == "env_KEPT"


def test_tmux_capture_failure_and_nonzero_return_safe_empty(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux_run", lambda args: None)
    assert tmux.capture_pane("@1") == ""

    class Failed:
        returncode = 1
        stdout = "environment=env_MUST_NOT_ESCAPE"

    monkeypatch.setattr(tmux, "_tmux_run", lambda args: Failed())
    assert tmux.capture_pane("@1") == ""

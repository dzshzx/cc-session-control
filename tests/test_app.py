"""App orchestration tests without a real MainLoop or live data sources."""

import threading
from queue import Queue
import urwid
import pytest

import cc_session_control.app as app_mod
from cc_session_control.actions.runner import ActionResult, Accepted, Busy
from cc_session_control.app import App
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.data.refresh import RefreshBatch, RefreshFailure


class _RecorderView:
    """A minimal TabView that records main-loop batch application."""

    def __init__(self):
        self.widget = urwid.Text("body")
        self._loaded = False
        self.applied = []
        self.apply_threads = []

    def apply_refresh(self, batch):
        self.applied.append(batch)
        self.apply_threads.append(threading.get_ident())
        self._loaded = True

    def keyhints(self):
        return ""

    def handle_key(self, key):
        pass

    def captures_text(self):
        return False


def _app_with_recorders(n=3):
    app = App()
    views = [_RecorderView() for _ in range(n)]
    app.views = views
    return app, views


def _batch(generation=1, snapshot=None):
    return RefreshBatch(
        generation=generation,
        snapshot=snapshot or WorldSnapshot(),
        cleanup_plan=CleanupPlan(),
        cleanup_counts={},
        session_stats={"total": 0, "empty": 0, "short": 0, "orphans": 0},
        ordered_projects=(),
    )


def test_worker_publishes_then_main_loop_applies_same_batch_to_all_views():
    built = Queue()
    batch = _batch()

    def build(generation):
        built.put(threading.get_ident())
        return batch

    app = App(refresh_builder=build)
    views = [_RecorderView() for _ in range(3)]
    app.views = views
    main_thread = threading.get_ident()

    app.trigger_async_refresh()
    worker_thread = built.get(timeout=1)
    assert worker_thread != main_thread
    assert all(not view.applied for view in views)

    assert app._on_pipe(b"1") is True
    assert all(view.applied == [batch] for view in views)
    assert all(view.apply_threads == [main_thread] for view in views)


def test_failed_generation_keeps_last_good_views_and_notifies():
    completed = Queue()
    results = [
        _batch(1),
        RefreshFailure(2, "/tmp/runtime", "permission denied"),
    ]

    def build(generation):
        result = results[generation - 1]
        completed.put(generation)
        return result

    app = App(refresh_builder=build)
    views = [_RecorderView() for _ in range(3)]
    app.views = views
    notifications = []
    app.notify = notifications.append

    app.trigger_async_refresh()
    assert completed.get(timeout=1) == 1
    app._on_pipe(b"1")
    app.trigger_async_refresh()
    assert completed.get(timeout=1) == 2
    app._on_pipe(b"1")

    assert all(view.applied == [results[0]] for view in views)
    assert notifications == [
        "刷新失败（/tmp/runtime）：permission denied",
    ]


def test_on_pipe_noop_while_exiting():
    app, views = _app_with_recorders()
    app._exiting = True
    app._on_pipe(b"1")
    assert all(not view.applied for view in views)


def test_exit_drops_late_completion_without_second_apply():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    batch = _batch()

    def build(generation):
        started.set()
        assert release.wait(1)
        finished.set()
        return batch

    app = App(refresh_builder=build)
    views = [_RecorderView() for _ in range(3)]
    app.views = views
    app.trigger_async_refresh()
    assert started.wait(1)

    try:
        app._exit()
    except urwid.ExitMainLoop:
        pass
    release.set()
    assert finished.wait(1)

    assert app.trigger_async_refresh() is app_mod.RequestResult.CLOSED
    app._on_pipe(b"stale")
    assert all(not view.applied for view in views)


def test_complete_batch_drives_real_views():
    from cc_session_control.models import RCProject, Session

    sess = [Session(sid="s1", cwd="/tmp/p", label="t", mtime=0.0, prompts=1,
                    pid=None, alive=False, current=False)]
    proj = [RCProject(name="p1", directory="/tmp/p1", trusted=True,
                      in_list=True, status="stopped", auto_start=True)]
    snap = WorldSnapshot(sessions=sess, rc_projects=proj)
    batch = RefreshBatch(
        generation=1,
        snapshot=snap,
        cleanup_plan=CleanupPlan(),
        cleanup_counts={},
        session_stats={"total": 1, "empty": 0, "short": 0, "orphans": 0},
        ordered_projects=tuple(proj),
    )

    app = App()
    ready = threading.Event()
    app._refresh = app_mod.RefreshCoordinator(
        lambda _generation: batch,
        ready.set,
    )
    app.trigger_async_refresh()
    assert ready.wait(1)

    app._on_pipe(b"1")
    assert len(app.views[1].walker) == len(sess)
    assert app.views[0]._projects == proj


def test_tab_order_launcher_first():
    # ADR-0001: 项目 → 会话 → 后台, startup on 项目; TAB_NAMES and self.views
    # index in lockstep.
    from cc_session_control.views.agents import AgentsView
    from cc_session_control.views.rc import RCView
    from cc_session_control.views.sessions import SessionsView

    assert app_mod.TAB_NAMES == ["项目", "会话", "后台"]
    app = App()
    assert app._active == 0
    assert [type(v) for v in app.views] == [RCView, SessionsView, AgentsView]
    assert len(app.views) == len(app_mod.TAB_NAMES)


# --- Confirm modal: App-level y/n routing shared by all tabs ---

def test_confirm_y_or_enter_runs_callback_and_closes():
    # Enter = 确认 alongside y (universal dialog muscle memory).
    for confirm_key in ("y", "enter"):
        app, _views = _app_with_recorders()
        ran = {"n": 0}
        app.confirm("终止？", lambda: ran.__setitem__("n", ran["n"] + 1))

        assert app._confirm_yes is not None
        assert isinstance(app.body.original_widget, urwid.Overlay)  # modal is up

        app._input(confirm_key)
        assert ran["n"] == 1                       # callback fired
        assert app._confirm_yes is None            # modal closed
        assert not isinstance(app.body.original_widget, urwid.Overlay)


def test_confirm_n_and_esc_cancel_without_callback():
    for cancel_key in ("n", "esc"):
        app, _views = _app_with_recorders()
        ran = {"n": 0}
        app.confirm("停止全部？", lambda: ran.__setitem__("n", ran["n"] + 1))
        app._input(cancel_key)
        assert ran["n"] == 0                    # callback NOT fired
        assert app._confirm_yes is None         # modal closed


def test_confirm_swallows_other_keys():
    app, _views = _app_with_recorders()
    app.confirm("终止？", lambda: None)
    before = app._active
    app._input("tab")                          # tab must NOT switch while modal up
    assert app._active == before
    assert app._confirm_yes is not None        # still modal


def test_confirm_modal_fits_long_message_on_narrow_terminal(monkeypatch):
    # The old fixed 50%×7 overlay clipped the message tail and the n/Esc line
    # on narrow terminals; the modal now sizes to its wrapped content.
    app, _views = _app_with_recorders()
    monkeypatch.setattr(app._screen, "get_cols_rows", lambda: (40, 24))
    msg = "接回会话「" + "长" * 30 + "」？将先终止原进程。"
    app.confirm(msg, lambda: None)
    canvas = app.body.original_widget.render((40, 24), focus=False)
    blob = b"\n".join(canvas.text).decode()
    assert "取消" in blob   # the control line is visible (was clipped at 7 rows)
    assert "确认" in blob


# --- Notifications: the newest message owns the footer ---

def test_second_notify_cancels_first_restore_alarm():
    # Without cancelling, the FIRST notification's timer fires and clears the
    # SECOND message early.
    app, _views = _app_with_recorders()
    removed = []
    app.loop.remove_alarm = lambda handle: removed.append(handle)
    app.notify("first")
    first_handle = app._notify_alarm
    app.notify("second")
    assert removed == [first_handle]
    blob = b"\n".join(app.frame.footer.render((80,)).text).decode()
    assert "second" in blob


# --- Filter mode owns its keys (captures_text): q/tab must not act globally ---

def test_filter_mode_captures_q_into_edit():
    # `q` typed into the filter (e.g. the q in "sql") used to quit csctl:
    # _input consumed it before the view ever saw it.
    app = App()
    app.trigger_async_refresh = lambda: None  # keep the test IO-free
    app._input("tab")               # 项目 → 会话 (项目/会话/后台 order)
    sessions_view = app.views[1]
    app._input("/")                 # enter filter mode
    app._input("q")                 # must land in the Edit, not exit
    assert sessions_view._mode == "filter"
    assert sessions_view._filter_edit.get_edit_text() == "q"


def test_tab_is_captured_during_filter_mode():
    app = App()
    app.trigger_async_refresh = lambda: None
    app._input("tab")               # 项目 → 会话
    sessions_view = app.views[1]
    app._input("/")
    app._input("x")
    app._input("tab")               # captured: must NOT switch tabs mid-typing
    assert app._active == 1
    assert sessions_view._mode == "filter"

    app._input("enter")             # commit the filter
    assert sessions_view._filter_text == "x"
    app._input("tab")               # back in list mode: tab switches again
    assert app._active == 2


def test_notify_restore_does_not_evict_filter_edit():
    # A notify fired ≤3s before entering filter leaves a restore alarm pending;
    # when it fires, the Edit must stay visible AND keep receiving keys (it used
    # to live in the App footer, where the restore silently replaced it while
    # _mode stayed "filter").
    app = App()
    app.trigger_async_refresh = lambda: None
    app._input("tab")               # 项目 → 会话
    sessions_view = app.views[1]
    app.notify("已复制")
    app._input("/")                 # enter filter with the alarm still pending
    app._restore_footer()           # the leftover alarm fires
    app._input("x")
    assert sessions_view._filter_edit.get_edit_text() == "x"
    canvas = app.frame.render((100, 30), focus=False)
    blob = b"\n".join(canvas.text).decode()
    assert "过滤" in blob           # the Edit is still on screen


# --- Fix 2b: degraded-mode header banner (D7/R10) ---

def test_degraded_banner_in_header_when_no_proc(monkeypatch):
    monkeypatch.setattr(app_mod.proc, "has_proc", lambda: False)
    app = App()
    # title + tab_bar + banner == 3 header rows; banner carries the warning.
    rows = [w for (w, _opts) in app.header.contents]
    assert len(rows) == 3
    blob = "\n".join(
        b"\n".join(r.render((120,)).text).decode() for r in rows
    )
    assert "liveness 降级" in blob
    assert "已受限" in blob


def test_no_degraded_banner_when_proc_present(monkeypatch):
    monkeypatch.setattr(app_mod.proc, "has_proc", lambda: True)
    app = App()
    rows = [w for (w, _opts) in app.header.contents]
    assert len(rows) == 2  # title + tab_bar only


# --- Stay-in-TUI actions: worker publish -> main-loop apply -----------------

def test_action_worker_never_updates_widgets_and_completion_refreshes_once():
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()
    worker_threads = []
    notify_threads = []
    notifications = []
    refreshes = []
    main_thread = threading.get_ident()

    def action():
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(1)
        return ActionResult.success("已完成", needs_refresh=True)

    app = App()
    app._action_pipe_fd = 99
    app._signal_action_ready = ready.set
    app._actions = app_mod.ActionRunner(ready.set)

    def notify(message, seconds=3):
        notifications.append(message)
        notify_threads.append(threading.get_ident())

    app.notify = notify
    app.trigger_async_refresh = lambda: refreshes.append("refresh")

    assert isinstance(app.submit_action("test.action", action), Accepted)
    assert notifications == ["处理中…"]
    assert started.wait(1)
    assert worker_threads == [worker_threads[0]]
    assert worker_threads[0] != main_thread

    release.set()
    assert ready.wait(1)
    assert notifications == ["处理中…"]
    assert app._on_action_pipe(b"1") is True
    assert notifications == ["处理中…", "已完成"]
    assert notify_threads == [main_thread, main_thread]
    assert refreshes == ["refresh"]
    assert app._on_action_pipe(b"duplicate") is True
    assert refreshes == ["refresh"]


def test_blocking_action_keeps_navigation_refresh_and_quit_responsive():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    refreshes = []
    notifications = []

    def action():
        started.set()
        assert release.wait(1)
        finished.set()
        return ActionResult.success("late", needs_refresh=True)

    app = App()
    app.notify = lambda message, seconds=3: notifications.append(message)
    app.trigger_async_refresh = lambda: refreshes.append("refresh")
    assert isinstance(app.submit_action("blocking", action), Accepted)
    assert started.wait(1)

    app._input("tab")
    assert app._active == 1
    app._input("r")
    assert refreshes == ["refresh", "refresh"]

    with pytest.raises(urwid.ExitMainLoop):
        app._input("q")
    release.set()
    assert finished.wait(1)
    app._on_action_pipe(b"late")

    assert notifications == ["处理中…", "刷新中…"]
    assert refreshes == ["refresh", "refresh"]


def test_second_app_submission_reports_busy_without_running():
    release = threading.Event()
    started = threading.Event()
    calls = []
    notifications = []

    def action():
        calls.append("first")
        started.set()
        assert release.wait(1)
        return ActionResult.success("done")

    app = App()
    app.notify = lambda message, seconds=3: notifications.append(message)
    assert isinstance(app.submit_action("first", action), Accepted)
    assert started.wait(1)
    outcome = app.submit_action(
        "second",
        lambda: calls.append("second") or ActionResult.success("bad"),
    )
    assert isinstance(outcome, Busy)
    assert calls == ["first"]
    assert notifications == ["处理中…", "已有操作处理中"]
    release.set()

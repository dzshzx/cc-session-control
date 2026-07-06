"""App orchestration tests — drive the refresh seam WITHOUT a real MainLoop.

`App._run_fetch_cycle` is the synchronous worker-phase seam (R11/D8): it builds
ONE shared `WorldSnapshot` and projects it into every view's `_pending` via
`fetch_pending(snapshot)`, degrading to per-view self-fetch (`snapshot=None`)
when the build raises. `_on_pipe` is the main-loop phase that swaps each view's
pending into its walker via `apply_data()`. These tests exercise both phases with
recorder views (so no real disk/`/proc`/`claude` IO) and assert the
"worker never touches widgets, main loop applies" contract holds. The degraded
header banner (D7) is covered too.
"""

import urwid

import cc_session_control.app as app_mod
from cc_session_control.app import App
from cc_session_control.data.snapshot import WorldSnapshot


class _RecorderView:
    """A minimal TabView that records the snapshot it was handed (no widgets)."""

    def __init__(self):
        self.widget = urwid.Text("body")
        self._loaded = False
        self.fetched = []      # snapshots passed to fetch_pending (worker phase)
        self.applied = 0       # apply_data calls (main-loop phase)
        self._pending = None

    def load(self):
        self._loaded = True

    def fetch_pending(self, snapshot=None):
        # Worker-thread phase: only stash, never touch widgets.
        self.fetched.append(snapshot)
        self._pending = snapshot

    def apply_data(self):
        # Main-loop phase: swap pending in.
        self.applied += 1
        self._pending = None

    def keyhints(self):
        return ""

    def handle_key(self, key):
        pass

    def deactivate(self):
        pass


def _app_with_recorders(n=3):
    app = App()
    views = [_RecorderView() for _ in range(n)]
    app.views = views
    return app, views


# --- Fix 6: the worker-phase seam ---

def test_run_fetch_cycle_dispatches_shared_snapshot(monkeypatch):
    app, views = _app_with_recorders()
    snap = WorldSnapshot()
    monkeypatch.setattr(app_mod, "build_world_snapshot", lambda: snap)

    app._run_fetch_cycle()

    # Every view received the SAME snapshot instance (one scan, projected).
    for v in views:
        assert v.fetched == [snap]
        assert v._pending is snap


def test_run_fetch_cycle_degrades_to_self_fetch_when_build_raises(monkeypatch):
    app, views = _app_with_recorders()

    def boom():
        raise RuntimeError("no world")

    monkeypatch.setattr(app_mod, "build_world_snapshot", boom)

    app._run_fetch_cycle()

    # A failed build -> each view is asked to self-fetch (snapshot=None).
    for v in views:
        assert v.fetched == [None]


# --- Fix 6: the main-loop phase ---

def test_on_pipe_applies_data_on_all_views():
    app, views = _app_with_recorders()
    # _on_pipe runs on the main loop; with _exiting False it applies every view.
    handled = app._on_pipe(b"1")
    assert handled is True
    for v in views:
        assert v.applied == 1


def test_on_pipe_noop_while_exiting():
    app, views = _app_with_recorders()
    app._exiting = True
    app._on_pipe(b"1")
    for v in views:
        assert v.applied == 0


def test_full_cycle_worker_stashes_then_main_loop_swaps(monkeypatch):
    # End-to-end without a MainLoop: worker dispatch stashes _pending, then the
    # main-loop apply swaps it in (clearing _pending).
    app, views = _app_with_recorders()
    snap = WorldSnapshot()
    monkeypatch.setattr(app_mod, "build_world_snapshot", lambda: snap)

    app._run_fetch_cycle()
    for v in views:
        assert v._pending is snap   # worker stashed
        assert v.applied == 0       # widgets untouched yet

    app._on_pipe(b"1")
    for v in views:
        assert v.applied == 1       # main loop applied
        assert v._pending is None   # swapped in


def test_full_cycle_drives_real_views(monkeypatch):
    # Same path with the THREE real views: a controlled snapshot is projected
    # into each _pending then swapped into each walker by the main-loop phase.
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.models import RCProject, Session

    sess = [Session(sid="s1", cwd="/tmp/p", label="t", mtime=0.0, prompts=1,
                    pid=None, alive=False, current=False)]
    proj = [RCProject(name="p1", directory="/tmp/p1", trusted=True,
                      in_list=True, status="stopped", auto_start=True)]
    snap = WorldSnapshot(sessions=sess, rc_projects=proj)

    monkeypatch.setattr(app_mod, "build_world_snapshot", lambda: snap)
    # Keep the views' projection IO-free / deterministic.
    monkeypatch.setattr(sv_mod, "cleanup_classified", lambda *a, **k: {
        "empty": 0, "short": 0, "orphan_dirs": 0, "zombie_procs": 0, "aged_entries": 0})

    app = App()
    app._run_fetch_cycle()
    assert app.views[0]._pending == sess          # SessionsView stashed
    assert app.views[1]._pending == proj          # RCView stashed (tab order: 会话/项目/后台)

    app._on_pipe(b"1")
    assert len(app.views[0].walker) == len(sess)  # swapped into the walker
    assert app.views[0]._pending is None


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


# --- Tab switch closes transient footer modes (filter-mode leak) ---

def test_switch_tab_closes_sessions_filter_mode(monkeypatch):
    app = App()
    app.trigger_async_refresh = lambda: None  # keep the test IO-free
    sessions_view = app.views[0]
    app._input("/")                 # enter filter mode
    app._input("x")                 # type into the (footer) filter edit
    assert sessions_view._mode == "filter"

    app._input("tab")               # switch away — the filter commits + closes

    assert sessions_view._mode == "list"
    assert sessions_view._filter_text == "x"
    assert app.frame.footer is app.footer  # the hidden Edit left the footer


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

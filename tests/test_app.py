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
    import cc_session_control.views.rc as rc_view_mod
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.models import BridgeEnv, RCProject, Session

    sess = [Session(sid="s1", cwd="/tmp/p", label="t", mtime=0.0, prompts=1,
                    pid=None, alive=False, current=False)]
    proj = [RCProject(name="p1", directory="/tmp/p1", trusted=True,
                      in_list=True, status="stopped", auto_start=True)]
    snap = WorldSnapshot(sessions=sess, rc_projects=proj)

    monkeypatch.setattr(app_mod, "build_world_snapshot", lambda: snap)
    # Keep the views' projection IO-free / deterministic.
    monkeypatch.setattr(sv_mod, "cleanup_classified", lambda *a, **k: {
        "empty": 0, "short": 0, "orphan_dirs": 0, "zombie_procs": 0, "aged_entries": 0})
    monkeypatch.setattr(rc_view_mod.environments, "current_envs", lambda obs: [])
    monkeypatch.setattr(rc_view_mod.environments, "orphan_envs", lambda obs: [])

    app = App()
    app._run_fetch_cycle()
    assert app.views[0]._pending == sess          # SessionsView stashed
    assert app.views[2]._pending == proj          # RCView stashed (projects)

    app._on_pipe(b"1")
    assert len(app.views[0].walker) == len(sess)  # swapped into the walker
    assert app.views[0]._pending is None


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

"""Tests for data/proc.py — the /proc seam and its non-Linux degradation."""

from cc_session_control.data import proc


# --- proc_starttime: comm-with-parens-and-spaces parsing ---

def test_proc_starttime_parses_field_22_with_spaced_parens_comm(tmp_path, monkeypatch):
    # comm field "(weird (cmd) name)" contains spaces AND nested parens — a naive
    # split()[21] would mis-index. The parser must slice after the LAST ')'.
    fake_pid = 4242
    procdir = tmp_path / str(fake_pid)
    procdir.mkdir()
    # Build a /proc/<pid>/stat: field1=pid, field2=comm, field3=state, ...
    # We need field 22 (starttime). After the comm, fields 3..N are simple.
    # tail: state(3) ppid(4) ... up to starttime(22). Index of starttime in the
    # post-')' split is 22-3 = 19.
    tail_fields = [str(i) for i in range(3, 100)]  # field3=='3' ... but we override
    # Put a recognizable starttime at field 22 -> tail index 19.
    tail_fields[22 - 3] = "987654"
    stat = f"{fake_pid} (weird (cmd) name) " + " ".join(tail_fields)
    (procdir / "stat").write_text(stat + "\n")

    monkeypatch.setattr(proc, "_PROC", str(tmp_path))
    monkeypatch.setattr(proc, "has_proc", lambda: True)
    assert proc.proc_starttime(fake_pid) == "987654"


def test_proc_starttime_missing_pid_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "_PROC", str(tmp_path))
    monkeypatch.setattr(proc, "has_proc", lambda: True)
    assert proc.proc_starttime(999999) is None


# --- pid_alive: zombie vs alive vs reuse ---

def test_pid_alive_zombie_no_proc(monkeypatch):
    # No /proc entry -> proc_starttime None -> not alive.
    monkeypatch.setattr(proc, "proc_starttime", lambda pid: None)
    assert proc.pid_alive(4242, "123") is False


def test_pid_alive_matches_proc_start(monkeypatch):
    monkeypatch.setattr(proc, "proc_starttime", lambda pid: "123")
    assert proc.pid_alive(4242, "123") is True


def test_pid_alive_reuse_mismatch_is_dead(monkeypatch):
    # pid exists but starttime differs -> a recycled pid, treated as dead.
    monkeypatch.setattr(proc, "proc_starttime", lambda pid: "999")
    assert proc.pid_alive(4242, "123") is False


def test_pid_alive_unknown_procstart_falls_back_to_existence(monkeypatch):
    monkeypatch.setattr(proc, "proc_starttime", lambda pid: "123")
    assert proc.pid_alive(4242, None) is True
    assert proc.pid_alive(4242, "") is True


def test_pid_alive_none_pid_is_false():
    assert proc.pid_alive(None, "123") is False
    assert proc.pid_alive(0, "123") is False


# --- non-Linux degradation ---

def test_non_linux_degrades(monkeypatch):
    monkeypatch.setattr(proc, "has_proc", lambda: False)
    assert proc.proc_starttime(4242) is None
    assert proc.pid_alive(4242, "123") is False
    # ancestor_pids returns only self, so "current" can't be determined.
    import os
    assert proc.ancestor_pids() == {os.getpid()}


def test_ancestor_pids_includes_self_on_linux():
    import os
    pids = proc.ancestor_pids()
    assert os.getpid() in pids
    assert all(isinstance(p, int) for p in pids)

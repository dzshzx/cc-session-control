"""Tests for data/proc.py — the /proc seam and its non-Linux degradation."""

import errno
import os

import pytest

from cc_session_control.data import proc


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(errno.ENOENT, "missing proc"),
        PermissionError(errno.EACCES, "permission denied"),
        OSError(errno.EIO, "input/output error"),
    ],
)
def test_rc_inventory_preserves_proc_root_failure(monkeypatch, error):
    def fail_listdir(_path):
        raise error

    monkeypatch.setattr(proc.os, "listdir", fail_listdir)

    result = proc.scan_rc_server_inventory()

    assert result.records == ()
    assert result.complete is False
    assert result.issues[0].source == "RC process inventory"
    assert result.issues[0].path == proc._PROC
    assert str(error) in result.issues[0].detail


def test_rc_inventory_preserves_per_pid_permission_failure(monkeypatch):
    monkeypatch.setattr(proc.os, "listdir", lambda _path: ["4242"])

    def fail_open(path, *_args, **_kwargs):
        raise PermissionError(errno.EACCES, "permission denied", path)

    monkeypatch.setattr("builtins.open", fail_open)

    result = proc.scan_rc_server_inventory()

    assert result.records == ()
    assert result.complete is False
    assert result.issues[0].source == "RC process inventory"
    assert result.issues[0].path == f"{proc._PROC}/4242/comm"
    assert "permission denied" in result.issues[0].detail


def test_rc_inventory_skips_per_pid_enoent_race(monkeypatch):
    monkeypatch.setattr(proc.os, "listdir", lambda _path: ["4242"])

    def gone(path, *_args, **_kwargs):
        raise FileNotFoundError(errno.ENOENT, "process exited", path)

    monkeypatch.setattr("builtins.open", gone)

    result = proc.scan_rc_server_inventory()

    assert result.records == ()
    assert result.complete is True
    assert result.issues == ()


def test_rc_inventory_flags_malformed_rc_cmdline(tmp_path, monkeypatch):
    pid_dir = tmp_path / "4242"
    pid_dir.mkdir()
    (pid_dir / "comm").write_text("claude\n")
    (pid_dir / "cmdline").write_bytes(b"claude\0remote-control\0--name\0")
    os.symlink(tmp_path, pid_dir / "cwd")
    monkeypatch.setattr(proc, "_PROC", str(tmp_path))

    result = proc.scan_rc_server_inventory()

    assert result.records == ()
    assert result.complete is False
    assert result.issues[0].path == str(pid_dir / "cmdline")
    assert "missing --name value" in result.issues[0].detail


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "permission denied"),
        OSError(errno.EIO, "input/output error"),
    ],
)
def test_probe_pid_preserves_unavailable_stat_evidence(monkeypatch, error):
    monkeypatch.setattr(proc, "has_proc", lambda: True)

    def fail_open(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("builtins.open", fail_open)

    result = proc.probe_pid(4242, "123")

    assert result.alive is None
    assert result.issue is not None
    assert result.issue.source == "process stat"
    assert result.issue.path == f"{proc._PROC}/4242/stat"
    assert str(error) in result.issue.detail


def test_probe_pid_preserves_malformed_stat_evidence(tmp_path, monkeypatch):
    procdir = tmp_path / "4242"
    procdir.mkdir()
    (procdir / "stat").write_text("4242 (truncated) R 1\n")
    monkeypatch.setattr(proc, "_PROC", str(tmp_path))

    result = proc.probe_pid(4242, "123")

    assert result.alive is None
    assert result.stat is not None
    assert result.stat.state is proc.ProcReadState.MALFORMED
    assert result.issue is not None
    assert result.issue.path == str(procdir / "stat")
    assert "truncated" in result.issue.detail


def test_probe_pid_rejects_invalid_stat_state_field(tmp_path, monkeypatch):
    procdir = tmp_path / "4242"
    procdir.mkdir()
    fields = ["not-a-state", "1", *("0" for _ in range(17)), "123"]
    (procdir / "stat").write_text(f"4242 (worker) {' '.join(fields)}\n")
    monkeypatch.setattr(proc, "_PROC", str(tmp_path))

    result = proc.probe_pid(4242, "123")

    assert result.alive is None
    assert result.stat is not None
    assert result.stat.state is proc.ProcReadState.MALFORMED
    assert result.issue is not None
    assert "state field" in result.issue.detail


def test_probe_pid_enoent_race_is_gone_without_issue(monkeypatch):
    monkeypatch.setattr(proc, "has_proc", lambda: True)

    def gone(*_args, **_kwargs):
        raise OSError(errno.ENOENT, "process exited")

    monkeypatch.setattr("builtins.open", gone)

    result = proc.probe_pid(4242, "123")

    assert result.alive is False
    assert result.stat is not None
    assert result.stat.state is proc.ProcReadState.GONE
    assert result.issue is None


# --- read_proc_stat: comm-with-parens-and-spaces parsing ---


def test_read_proc_stat_parses_field_22_with_spaced_parens_comm(tmp_path, monkeypatch):
    # comm field "(weird (cmd) name)" contains spaces AND nested parens — a naive
    # split()[21] would mis-index. The parser must slice after the LAST ')'.
    fake_pid = 4242
    procdir = tmp_path / str(fake_pid)
    procdir.mkdir()
    # Build a /proc/<pid>/stat: field1=pid, field2=comm, field3=state, ...
    # We need field 22 (starttime). After the comm, fields 3..N are simple.
    # tail: state(3) ppid(4) ... up to starttime(22). Index of starttime in the
    # post-')' split is 22-3 = 19.
    tail_fields = [str(i) for i in range(3, 100)]
    tail_fields[0] = "R"
    # Put a recognizable starttime at field 22 -> tail index 19.
    tail_fields[22 - 3] = "987654"
    stat = f"{fake_pid} (weird (cmd) name) " + " ".join(tail_fields)
    (procdir / "stat").write_text(stat + "\n")

    monkeypatch.setattr(proc, "_PROC", str(tmp_path))
    monkeypatch.setattr(proc, "has_proc", lambda: True)
    assert proc.read_proc_stat(fake_pid).starttime == "987654"


def test_read_proc_stat_missing_pid_returns_none_starttime(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "_PROC", str(tmp_path))
    monkeypatch.setattr(proc, "has_proc", lambda: True)
    assert proc.read_proc_stat(999999).starttime is None


# --- non-Linux degradation ---


def test_non_linux_degrades(monkeypatch):
    monkeypatch.setattr(proc, "has_proc", lambda: False)
    assert proc.read_proc_stat(4242).starttime is None
    assert proc.probe_pid(4242, "123").alive is None
    # ancestor_pids returns only self, so "current" can't be determined.
    import os

    assert proc.ancestor_pids() == {os.getpid()}


def test_ancestor_pids_includes_self_on_linux():
    import os

    pids = proc.ancestor_pids()
    assert os.getpid() in pids
    assert all(isinstance(p, int) for p in pids)


def test_probe_ancestors_preserves_partial_chain_on_mid_chain_failure(
    tmp_path,
    monkeypatch,
):
    child = tmp_path / "10"
    child.mkdir()
    fields = ["R", "20", *("0" for _ in range(17)), "123"]
    (child / "stat").write_text(f"10 (child) {' '.join(fields)}\n")
    monkeypatch.setattr(proc, "_PROC", str(tmp_path))
    real_open = open

    def fail_parent(path, *args, **kwargs):
        if path == str(tmp_path / "20" / "stat"):
            raise PermissionError(errno.EACCES, "permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_parent)

    result = proc.probe_ancestors(10)

    assert result.pids == frozenset({10, 20})
    assert result.complete is False
    assert len(result.issues) == 1
    assert result.issues[0].path == str(tmp_path / "20" / "stat")

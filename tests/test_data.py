"""Data-layer unit tests — pure functions, transcript parsing, rc toggles."""

import time

import json

from cc_session_control.actions.session_ops import resume_cmd
from cc_session_control.config import cfg
from cc_session_control.data.sessions import (
    _parse_transcript,
    cleanup_stats,
    prune_sessions,
)
from cc_session_control.models import Session


def _make_session(**overrides):
    defaults = dict(
        sid="abc123", cwd="/tmp/proj", label="test", mtime=0.0,
        prompts=0, pid=None, alive=False, current=False,
        hidden=set(), file="/tmp/abc123.jsonl",
    )
    defaults.update(overrides)
    return Session(**defaults)


# --- D1: prune_sessions ---

def test_prune_sessions_excludes_alive():
    now = time.time()
    old = now - 700
    sessions = [
        _make_session(sid="dead", prompts=0, mtime=old, alive=False),
        _make_session(sid="alive", prompts=0, mtime=old, alive=True, pid=999),
    ]
    pruned = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert "dead" in pruned
    assert "alive" not in pruned


def test_prune_sessions_excludes_current():
    now = time.time()
    old = now - 700
    sessions = [
        _make_session(sid="normal", prompts=0, mtime=old, current=False),
        _make_session(sid="cur", prompts=0, mtime=old, current=True),
    ]
    pruned = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert "normal" in pruned
    assert "cur" not in pruned


def test_prune_sessions_excludes_recent():
    now = time.time()
    sessions = [
        _make_session(sid="old", prompts=0, mtime=now - 700),
        _make_session(sid="recent", prompts=0, mtime=now - 100),
    ]
    pruned = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert "old" in pruned
    assert "recent" not in pruned


def test_prune_sessions_threshold():
    now = time.time()
    old = now - 700
    sessions = [
        _make_session(sid="p0", prompts=0, mtime=old),
        _make_session(sid="p2", prompts=2, mtime=old),
        _make_session(sid="p3", prompts=3, mtime=old),
    ]
    empties = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert empties == {"p0"}
    shorts = {s.sid for s in prune_sessions(sessions, max_prompts=2)}
    assert shorts == {"p0", "p2"}


# --- D1: resume_cmd ---

def test_resume_cmd_dead():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=False)
    cmd = resume_cmd(s)
    assert cmd == "cd /tmp/proj && claude --resume sid1"


def test_resume_cmd_alive_non_current():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    cmd = resume_cmd(s)
    assert cmd == "kill 4242 && sleep 1 && cd /tmp/proj && claude --resume sid1"


def test_resume_cmd_fork():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=False)
    cmd = resume_cmd(s, fork=True)
    assert cmd == "cd /tmp/proj && claude --resume sid1 --fork-session"


def test_resume_cmd_fork_while_alive_drops_kill_prefix():
    # Unified semantics (decision A): fork is a copy and leaves the original
    # running, so forking a live non-current session must NOT kill it.
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    cmd = resume_cmd(s, fork=True)
    assert cmd == "cd /tmp/proj && claude --resume sid1 --fork-session"


def test_resume_cmd_current_no_kill():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=True, pid=4242)
    cmd = resume_cmd(s)
    assert cmd == "cd /tmp/proj && claude --resume sid1"


# --- D1: cleanup_stats ---

def test_cleanup_stats_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sessions = [
        _make_session(sid="empty1", prompts=0),
        _make_session(sid="short1", prompts=1),
        _make_session(sid="short2", prompts=2),
        _make_session(sid="full1", prompts=5),
    ]
    # session-env: one matching sid (full1), one orphan dir
    env = tmp_path / "session-env"
    env.mkdir()
    (env / "full1").mkdir()
    (env / "orphan-xyz").mkdir()

    stats = cleanup_stats(sessions)
    assert stats["total"] == 4
    assert stats["empty"] == 1
    assert stats["short"] == 2
    assert stats["orphans"] == 1


def test_cleanup_stats_no_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sessions = [_make_session(sid="full1", prompts=5)]
    stats = cleanup_stats(sessions)
    assert stats == {"total": 1, "empty": 0, "short": 0, "orphans": 0}


# --- D4: _parse_transcript ---

def _write_jsonl(tmp_path, sid, lines):
    # Compact separators so the '"type":"user"' substring pre-check in
    # _parse_transcript matches, mirroring Claude's actual transcript format.
    f = tmp_path / f"{sid}.jsonl"
    f.write_text(
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n"
    )
    return str(f)


def test_parse_transcript_basic_fields(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hello world"}},
        {"type": "user", "message": {"content": "second prompt"}},
    ])
    s = _parse_transcript(path, alive={}, cur=set())
    assert s is not None
    assert s.sid == "sid1"
    assert s.cwd == "/tmp/proj"
    assert s.prompts == 2
    assert s.pid is None
    assert s.alive is False
    assert s.current is False
    assert s.file == path


def test_parse_transcript_none_when_no_cwd(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"type": "user", "message": {"content": "hello"}},
    ])
    assert _parse_transcript(path, alive={}, cur=set()) is None


def test_parse_transcript_label_priority_aititle(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"aiTitle": "The Title"},
        {"lastPrompt": "the last prompt"},
        {"type": "user", "message": {"content": "first prompt"}},
    ])
    s = _parse_transcript(path, alive={}, cur=set())
    assert s.label == "The Title"


def test_parse_transcript_label_priority_first_prompt(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"lastPrompt": "the last prompt"},
        {"type": "user", "message": {"content": "first real prompt"}},
    ])
    s = _parse_transcript(path, alive={}, cur=set())
    assert s.label == "first real prompt"


def test_parse_transcript_label_priority_last_prompt(tmp_path):
    # No aiTitle, and the only user prompt is noise -> falls back to lastPrompt.
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"lastPrompt": "the last prompt"},
        {"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}},
    ])
    s = _parse_transcript(path, alive={}, cur=set())
    assert s.label == "the last prompt"


def test_parse_transcript_label_untitled(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
    ])
    s = _parse_transcript(path, alive={}, cur=set())
    assert s.label == "(untitled)"


def test_parse_transcript_alive_and_current(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    s = _parse_transcript(path, alive={"sid1": 4242}, cur={4242})
    assert s.pid == 4242
    assert s.alive is True
    assert s.current is True


def test_parse_transcript_hidden_tags(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj", "kind": "sdk-ts"},
        {"note": "bridge-session"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    s = _parse_transcript(path, alive={}, cur=set())
    assert s.hidden == {"sdk", "bridge"}

"""Unit tests for the headless resume listing (`csctl resume`).

Command synthesis itself is covered by session_ops tests; here we verify
selection (metadata + transcript-body keyword fallback), paging, and that the
formatter routes each liveness state to the right guidance without re-deriving
kill semantics.
"""

import types

from cc_session_control.actions import resume_list
from cc_session_control.models import Session


def _session(
    tmp_path,
    sid="aaaa1111",
    *,
    alive=False,
    current=False,
    pid=None,
    label="fix the login bug",
    body="",
    mtime=1_700_000_000.0,
):
    f = tmp_path / f"{sid}.jsonl"
    f.write_text(body or '{"cwd": "/w"}\n')
    return Session(
        sid=sid,
        cwd="/w/proj",
        label=label,
        mtime=mtime,
        prompts=3,
        pid=pid,
        alive=alive,
        current=current,
        file=str(f),
    )


def test_keyword_matches_metadata_then_body(tmp_path):
    s = _session(
        tmp_path, label="fix the login bug", body='{"text": "the SECRET-token issue"}\n'
    )
    assert resume_list.keyword_matches(s, "")  # empty matches all
    assert resume_list.keyword_matches(s, "login")  # label hit
    assert resume_list.keyword_matches(s, "proj")  # cwd hit
    assert resume_list.keyword_matches(s, "secret-token")  # body fallback hit
    assert not resume_list.keyword_matches(s, "no-such-word")


def test_keyword_body_missing_file_is_no_match_not_refusal(tmp_path):
    # kimi's append-only session index routinely outlives a manually removed
    # session directory (no official delete command) — a deleted transcript
    # must degrade that one row to "no match", not fail the whole search.
    s = _session(tmp_path, label="fix the login bug")
    s = types.SimpleNamespace(**{**s.__dict__, "file": str(tmp_path / "gone.jsonl")})
    result = resume_list.render([s], keyword="no-such-word")
    assert result.complete
    assert result.issues == ()
    assert "No matching sessions" in result.text


def test_keyword_metadata_hit_skips_missing_body_file(tmp_path):
    # A metadata hit (label/cwd/sid) must short-circuit before ever opening
    # the transcript file, so a missing file can't block a metadata match.
    s = _session(tmp_path, label="fix the login bug")
    s = types.SimpleNamespace(**{**s.__dict__, "file": str(tmp_path / "gone.jsonl")})
    result = resume_list.render([s], keyword="login")
    assert result.complete
    assert result.issues == ()
    assert "fix the login bug" in result.text


def test_keyword_body_permission_error_is_still_a_refusal(tmp_path, monkeypatch):
    # Missing files degrade to no-match, but other OSErrors (e.g. a transcript
    # file that exists but can't be read) must still refuse the search rather
    # than silently mask a real access problem.
    s = _session(tmp_path, body='{"text": "whatever"}\n')
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == s.file:
            raise PermissionError(13, "Permission denied", s.file)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    result = resume_list.render([s], keyword="anything")
    assert not result.complete
    assert result.text == ""
    assert result.issues[0].source == "session transcript body"
    assert result.issues[0].path == s.file


def test_paginate_clamps_and_slices(tmp_path):
    rows = [_session(tmp_path, sid=f"s{i:03d}") for i in range(5)]
    page_rows, page, pages = resume_list.paginate(
        rows, page=2, limit=2, all_pages=False
    )
    assert (page, pages) == (2, 3)
    assert [s.sid for s in page_rows] == ["s002", "s003"]
    # out-of-range page clamps to the last page
    page_rows, page, pages = resume_list.paginate(
        rows, page=99, limit=2, all_pages=False
    )
    assert page == 3 and [s.sid for s in page_rows] == ["s004"]
    # --all ignores paging
    page_rows, page, pages = resume_list.paginate(
        rows, page=99, limit=2, all_pages=True
    )
    assert pages == 1 and len(page_rows) == 5


def test_format_dead_session_gives_cd_resume(tmp_path):
    s = _session(tmp_path, alive=False)
    text = "\n".join(resume_list.format_session(s))
    assert "[dead]" in text
    assert "cd /w/proj && claude --resume aaaa1111" in text
    assert "kill" not in text


def test_format_live_session_gives_takeover_command(tmp_path):
    s = _session(tmp_path, alive=True, pid=4242)
    text = "\n".join(resume_list.format_session(s))
    assert "[live]" in text
    assert "csctl resume --take-over aaaa1111" in text
    assert "4242" not in text
    assert "kill" not in text
    assert "re-checks the live process" in text


def test_format_hosted_session_is_read_only_without_command(tmp_path):
    s = _session(tmp_path, alive=False)
    s = types.SimpleNamespace(**{**s.__dict__, "provider": "codex", "hosted": True})

    text = "\n".join(resume_list.format_session(s))

    assert "[hosted]" in text
    assert "read-only" in text
    assert "codex resume" not in text


def test_format_non_claude_live_session_is_honest_about_no_takeover(tmp_path):
    # ADR-0005: non-Claude live rows print a direct provider resume command,
    # not the Claude-only guarded takeover path — the annotation must not
    # claim re-check/guard/single-timeline semantics that do not apply.
    s = _session(tmp_path, alive=True, pid=4242)
    s = types.SimpleNamespace(**{**s.__dict__, "provider": "codex"})
    text = "\n".join(resume_list.format_session(s))
    assert "[live]" in text
    assert "re-checks the live process" not in text
    assert "guarded" not in text
    assert "single-timeline" not in text
    assert "does NOT stop the running process" in text


def test_format_current_session_never_prints_kill(tmp_path):
    s = _session(tmp_path, alive=True, current=True, pid=4242)
    text = "\n".join(resume_list.format_session(s))
    assert "you are IN this session" in text
    assert "kill" not in text.replace("kill it only", "")  # no runnable kill command


def test_render_reports_paging_hints(tmp_path):
    rows = [_session(tmp_path, sid=f"s{i:03d}") for i in range(3)]
    out = resume_list.render(rows, page=1, limit=2).text
    assert "page 1/2, 3 session(s)" in out
    assert "csctl resume --page 2" in out
    assert "csctl resume --all" in out


def test_render_no_match(tmp_path):
    rows = [_session(tmp_path)]
    assert "No matching sessions" in resume_list.render(rows, keyword="zzz-none").text

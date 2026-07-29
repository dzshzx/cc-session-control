"""One canonical Session factory for every test module.

The three per-module `_make_session` copies had drifted defaults (label,
mtime, prompts, hidden) — the single neutral default set lives here and
tests state anything they rely on explicitly.
"""

from cc_session_control.models import Session


def make_session(**overrides) -> Session:
    defaults = dict(
        sid="abc123",
        cwd="/tmp/proj",
        label="test",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        hidden=set(),
        file="/tmp/abc123.jsonl",
    )
    defaults.update(overrides)
    return Session(**defaults)

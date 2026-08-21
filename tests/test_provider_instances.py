"""Multi-home codex identities (ADR-0008).

Three properties are load-bearing here:

1. Without `providers.json`, everything behaves exactly as before — one
   codex instance following `cfg.codex_home`, no env on any command.
2. With it, the declaration is the COMPLETE inventory and an inherited
   `CODEX_HOME` stops deciding what csctl can see (the drift this ADR fixes).
3. Each identity's sessions, trust records, running processes, and
   synthesized commands stay separated from the others'.
"""

from __future__ import annotations

import json

import pytest
from factories import make_session

from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.proc import ProcCli, ProcCliInventory
from cc_session_control.data.provider_config import (
    ProviderConfigState,
    read_provider_config,
)
from cc_session_control.data.providers.codex import CodexProvider

UUID_A = "019fc1d3-842a-7601-81ac-da922aedf794"
UUID_B = "019ff6b5-4ef7-7fa3-b545-2ee1657a4893"


def _declare(tmp_path, monkeypatch, entries):
    """Write a providers.json and point csctl's XDG config home at it."""
    xdg = tmp_path / "xdg"
    (xdg / "csctl").mkdir(parents=True)
    (xdg / "csctl" / "providers.json").write_text(json.dumps({"codex_homes": entries}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    providers.reset()
    return xdg / "csctl" / "providers.json"


def _codex_home(root, name, sid=None, cwd="/tmp/proj", when="2026/08/13"):
    """A codex state home, optionally holding one rollout."""
    home = root / name
    home.mkdir(parents=True, exist_ok=True)
    if sid is not None:
        day = home / "sessions" / when
        day.mkdir(parents=True)
        meta = {
            "timestamp": "2026-08-13T00:01:45.000Z",
            "type": "session_meta",
            "payload": {"id": sid, "session_id": sid, "cwd": cwd, "source": "cli"},
        }
        (day / f"rollout-2026-08-13T00-01-45-{sid}.jsonl").write_text(
            json.dumps(meta) + "\n"
        )
    return home


# --- the declaration itself --------------------------------------------------


def test_absent_declaration_keeps_one_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    providers.reset()

    codex = [p for p in providers.all_providers() if p.key.startswith("codex")]

    assert [p.key for p in codex] == ["codex"]
    assert codex[0].label == "cx"
    assert codex[0].env == {}  # commands stay byte-identical to pre-ADR-0008
    assert providers.config_issues() == ()


def test_declaration_replaces_the_inherited_home(tmp_path, monkeypatch):
    """THE drift fix: an inherited CODEX_HOME no longer decides the inventory."""
    default = _codex_home(tmp_path, "default")
    second = _codex_home(tmp_path, "eva02")
    # csctl launched from inside the SECOND identity's session:
    monkeypatch.setattr(cfg, "codex_home", second)
    _declare(
        tmp_path,
        monkeypatch,
        [
            {"label": "cx", "home": str(default)},
            {"label": "cx2", "home": str(second)},
        ],
    )

    codex = [p for p in providers.all_providers() if p.key.startswith("codex")]

    assert [(p.key, p.label, p.home) for p in codex] == [
        ("codex", "cx", default),
        ("codex:cx2", "cx2", second),
    ]


def test_broken_declaration_degrades_to_one_instance_with_detail(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    (xdg / "csctl").mkdir(parents=True)
    (xdg / "csctl" / "providers.json").write_text("{not json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    providers.reset()

    codex = [p for p in providers.all_providers() if p.key.startswith("codex")]
    issues = providers.config_issues()

    assert [p.key for p in codex] == ["codex"]
    assert len(issues) == 1
    assert "malformed" in issues[0].detail


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([], "'codex_homes' is empty"),
        ([{"label": "cx", "home": "relative/path"}], "is not an absolute path"),
        ([{"label": "toolong", "home": "/a"}], "exceeds 3 characters"),
        ([{"label": "c x", "home": "/a"}], "must be ASCII alphanumeric"),
        (
            [{"label": "cx", "home": "/a"}, {"label": "cx", "home": "/b"}],
            "duplicate label",
        ),
        (
            [{"label": "cx", "home": "/a"}, {"label": "cx2", "home": "/a"}],
            "duplicate home",
        ),
        ([{"home": "/a"}], "'label' must be a non-empty string"),
        (["/a"], "is not an object"),
        (
            [{"label": "cx", "home": "/a", "env_file": "rel/env"}],
            "is not an absolute path",
        ),
        (
            [{"label": "cx", "home": "/a", "env_file": 5}],
            "'env_file' must be a non-empty string",
        ),
    ],
)
def test_invalid_declarations_are_refused_with_a_reason(
    tmp_path, monkeypatch, entries, expected
):
    path = _declare(tmp_path, monkeypatch, entries)

    result = read_provider_config(path)

    assert result.state is ProviderConfigState.INVALID
    assert expected in result.detail
    assert result.codex_instances == ()


def test_home_need_not_exist_yet(tmp_path, monkeypatch):
    """A declared-but-absent home is inactive, not a config error."""
    path = _declare(
        tmp_path, monkeypatch, [{"label": "cx", "home": str(tmp_path / "gone")}]
    )

    result = read_provider_config(path)

    assert result.state is ProviderConfigState.AVAILABLE
    assert not CodexProvider(home=tmp_path / "gone").available()


# --- separation between identities -------------------------------------------


def test_each_identity_discovers_only_its_own_sessions(tmp_path, monkeypatch):
    default = _codex_home(tmp_path, "default", sid=UUID_A, cwd="/tmp/a")
    second = _codex_home(tmp_path, "eva02", sid=UUID_B, cwd="/tmp/b")

    rows_a = CodexProvider(home=default).discover(ProcCliInventory(), frozenset())
    rows_b = CodexProvider(key="codex:cx2", label="cx2", home=second).discover(
        ProcCliInventory(), frozenset()
    )

    assert [(r.sid, r.provider) for r in rows_a.sessions] == [(UUID_A, "codex")]
    assert [(r.sid, r.provider) for r in rows_b.sessions] == [(UUID_B, "codex:cx2")]


def test_each_identity_reads_its_own_trust_store(tmp_path, monkeypatch):
    default = _codex_home(tmp_path, "default")
    second = _codex_home(tmp_path, "eva02")
    (default / "config.toml").write_text('[projects."/a"]\ntrust_level = "trusted"\n')
    (second / "config.toml").write_text('[projects."/b"]\ntrust_level = "trusted"\n')

    assert CodexProvider(home=default).trusted_dirs().directories == ("/a",)
    assert CodexProvider(home=second).trusted_dirs().directories == ("/b",)


class TestProcessAttribution:
    """`/proc` environ decides which identity owns a running codex."""

    def _record(self, pid, env, cwd="/tmp/proj"):
        return ProcCli(
            pid=pid,
            argv=("codex",),  # a BARE TUI — the unbound-live hint source
            starttime="100",
            cwd=cwd,
            env=env,
        )

    def _hinted(self, provider, records, cwd="/tmp/proj"):
        scan = provider.discover(ProcCliInventory(records=records), frozenset())
        return [r.sid for r in scan.sessions if r.unbound_live_hint]

    def test_bare_tui_hints_only_its_own_identity(self, tmp_path):
        default = _codex_home(tmp_path, "default", sid=UUID_A)
        second = _codex_home(tmp_path, "eva02", sid=UUID_B)
        # One bare codex running under the SECOND identity.
        records = (self._record(900, {"CODEX_HOME": str(second)}),)

        assert self._hinted(CodexProvider(home=default), records) == []
        assert self._hinted(
            CodexProvider(key="codex:cx2", label="cx2", home=second), records
        ) == [UUID_B]

    def test_unset_codex_home_belongs_to_the_default_home(self, tmp_path, monkeypatch):
        default = tmp_path / ".codex"
        _codex_home(tmp_path, ".codex", sid=UUID_A)
        second = _codex_home(tmp_path, "eva02", sid=UUID_B)
        monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
        records = (self._record(900, {}),)  # environ read; CODEX_HOME simply unset

        assert self._hinted(CodexProvider(home=default), records) == [UUID_A]
        assert (
            self._hinted(
                CodexProvider(key="codex:cx2", label="cx2", home=second), records
            )
            == []
        )

    def test_unreadable_environ_warns_on_every_identity(self, tmp_path):
        """`env is None` proves nothing, and a hint is a WARNING: a redundant
        one costs a confirmation, a missing one loses the double-open
        warning — so no evidence must fail toward warning, not silence."""
        default = _codex_home(tmp_path, "default", sid=UUID_A)
        second = _codex_home(tmp_path, "eva02", sid=UUID_B)
        records = (self._record(900, None),)

        assert self._hinted(CodexProvider(home=default), records) == [UUID_A]
        assert self._hinted(
            CodexProvider(key="codex:cx2", label="cx2", home=second), records
        ) == [UUID_B]

    def test_argv_binding_survives_without_environ_evidence(self, tmp_path):
        """Liveness never depended on environ: a sid only resolves against the
        home that records it, so attribution is precision, not safety."""
        second = _codex_home(tmp_path, "eva02", sid=UUID_B)
        record = ProcCli(
            pid=901,
            argv=("codex", "resume", UUID_B),
            starttime="100",
            cwd="/tmp/proj",
            env=None,
        )
        provider = CodexProvider(key="codex:cx2", label="cx2", home=second)

        scan = provider.discover(ProcCliInventory(records=(record,)), frozenset())

        assert [(r.sid, r.alive, r.pid) for r in scan.sessions] == [(UUID_B, True, 901)]


# --- commands state their identity -------------------------------------------


class TestCommandIdentity:
    def test_declared_instance_carries_codex_home(self, tmp_path):
        provider = CodexProvider(key="codex:cx2", label="cx2", home=tmp_path / "eva02")

        assert provider.env == {"CODEX_HOME": str(tmp_path / "eva02")}

    def test_single_instance_carries_nothing(self):
        assert CodexProvider().env == {}

    def test_copied_command_prefixes_the_home(self, tmp_path, monkeypatch):
        second = _codex_home(tmp_path, "eva02")
        _declare(
            tmp_path,
            monkeypatch,
            [
                {"label": "cx", "home": str(_codex_home(tmp_path, "default"))},
                {"label": "cx2", "home": str(second)},
            ],
        )
        row = make_session(
            sid=UUID_B, provider="codex:cx2", cwd="/tmp/proj", alive=False
        )

        command = session_ops.resume_cmd(row)

        assert command == (f"cd /tmp/proj && CODEX_HOME={second} codex resume {UUID_B}")

    def test_quoting_survives_a_home_with_spaces(self, tmp_path, monkeypatch):
        spaced = tmp_path / "two words"
        spaced.mkdir()
        _declare(
            tmp_path,
            monkeypatch,
            [
                {"label": "cx", "home": str(_codex_home(tmp_path, "default"))},
                {"label": "cx2", "home": str(spaced)},
            ],
        )

        prefix = session_ops.env_prefix("codex:cx2")

        assert prefix == f"CODEX_HOME='{spaced}' "

    def test_window_names_never_contain_a_colon(self, tmp_path):
        """`:` is tmux target syntax — a multi-instance key must not leak in."""
        provider = CodexProvider(key="codex:cx2", label="cx2", home=tmp_path)

        assert ":" not in provider.window_tag
        assert ":" not in provider.window_name(UUID_B)
        assert provider.window_name(UUID_B).startswith("cx2-")
        assert CodexProvider().window_tag == "codex"  # unchanged for one instance


# --- per-instance env_file → launch_env (ADR-0012) ---------------------------


class TestInstanceEnvFile:
    """A declared `env_file` reaches the SPAWN environment as a launch-only
    secret, never a copied command — so a codex identity can carry its
    provider API key (`env_key` in config.toml) with no launcher wrapper."""

    def _envfile(self, tmp_path, body="DEEPSEEK_API_KEY=sk-secret\n"):
        f = tmp_path / "deepseek.env"
        f.write_text(body)
        return f

    def test_declaration_parses_env_file_into_the_spec(self, tmp_path, monkeypatch):
        secret = self._envfile(tmp_path)
        path = _declare(
            tmp_path,
            monkeypatch,
            [
                {"label": "cx", "home": str(_codex_home(tmp_path, "default"))},
                {
                    "label": "cx2",
                    "home": str(_codex_home(tmp_path, "eva02")),
                    "env_file": str(secret),
                },
            ],
        )

        result = read_provider_config(path)

        assert result.state is ProviderConfigState.AVAILABLE
        assert [s.env_file for s in result.codex_instances] == [None, secret]

    def test_absent_env_file_is_none(self, tmp_path, monkeypatch):
        path = _declare(tmp_path, monkeypatch, [{"label": "cx", "home": "/a"}])
        (spec,) = read_provider_config(path).codex_instances
        assert spec.env_file is None

    def test_launch_env_carries_the_secret_but_env_does_not(self, tmp_path):
        secret = self._envfile(tmp_path)
        provider = CodexProvider(
            key="codex:cx2", label="cx2", home=tmp_path / "eva02", env_file=secret
        )

        assert provider.env == {"CODEX_HOME": str(tmp_path / "eva02")}
        assert provider.launch_env == {
            "CODEX_HOME": str(tmp_path / "eva02"),
            "DEEPSEEK_API_KEY": "sk-secret",
        }

    def test_copied_command_never_leaks_the_secret(self, tmp_path, monkeypatch):
        secret = self._envfile(tmp_path)
        second = _codex_home(tmp_path, "eva02")
        _declare(
            tmp_path,
            monkeypatch,
            [
                {"label": "cx", "home": str(_codex_home(tmp_path, "default"))},
                {"label": "cx2", "home": str(second), "env_file": str(secret)},
            ],
        )
        row = make_session(
            sid=UUID_B, provider="codex:cx2", cwd="/tmp/proj", alive=False
        )

        command = session_ops.resume_cmd(row)
        prefix = session_ops.env_prefix("codex:cx2")

        assert "sk-secret" not in command
        assert "DEEPSEEK_API_KEY" not in command
        assert prefix == f"CODEX_HOME={second} "

    def test_missing_env_file_degrades_to_env(self, tmp_path):
        provider = CodexProvider(
            key="codex:cx2",
            label="cx2",
            home=tmp_path / "eva02",
            env_file=tmp_path / "gone.env",
        )
        assert provider.launch_env == provider.env

    def test_env_file_read_fresh_so_a_rotated_key_takes_effect(self, tmp_path):
        secret = self._envfile(tmp_path, "DEEPSEEK_API_KEY=old\n")
        provider = CodexProvider(
            key="codex:cx2", label="cx2", home=tmp_path / "eva02", env_file=secret
        )
        assert provider.launch_env["DEEPSEEK_API_KEY"] == "old"
        secret.write_text("DEEPSEEK_API_KEY=new\n")
        assert provider.launch_env["DEEPSEEK_API_KEY"] == "new"


class TestNonCodexLaunchEnv:
    def test_single_home_clis_launch_env_equals_env(self):
        from cc_session_control.data.providers.claude import ClaudeProvider
        from cc_session_control.data.providers.kimi import KimiProvider
        from cc_session_control.data.providers.opencode import OpencodeProvider

        for provider in (ClaudeProvider(), KimiProvider(), OpencodeProvider()):
            assert provider.launch_env == provider.env == {}

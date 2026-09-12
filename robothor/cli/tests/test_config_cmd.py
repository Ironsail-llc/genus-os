"""``genus config`` -- reading and changing settings without editing files.

The commands under test are the operator's only supported way to answer "what
is this set to, and who set it?" and to change it. The contracts that matter:

* a secret is never printed, by any sub-command;
* ``set`` routes by the field's own metadata rather than by what the operator
  typed -- governed flags to the DB store, secrets refused, the rest to
  config.yaml -- and says whether the change is live or waiting on a restart;
* an unknown name is an exit code and a suggestion, not a silent no-op;
* ``validate`` treats Telegram as optional, and reports the three things that
  make a config file lie: unknown keys, deprecated names, and a file that
  disagrees with the running process.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

from robothor.cli.config_cmd import cmd_config


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """A workspace of our own, with none of the box's own configuration."""
    from robothor.settings import reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES, reset_alias_warnings
    from robothor.settings.sources import reset_unknown_key_warnings

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_alias_warnings()
    reset_unknown_key_warnings()
    reset_settings()
    yield
    reset_settings()
    reset_alias_warnings()
    reset_unknown_key_warnings()


def _args(**kwargs) -> argparse.Namespace:
    kwargs.setdefault("json", False)
    return argparse.Namespace(**kwargs)


def _config_path(workspace: Path) -> Path:
    return workspace / ".robothor" / "config.yaml"


def _write_config(workspace: Path, body: str) -> Path:
    path = _config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ── get ──────────────────────────────────────────────────────────────────────


def test_get_reports_the_default_and_says_so(capsys) -> None:
    assert cmd_config(_args(config_command="get", name="ROBOTHOR_MAX_CONCURRENT_AGENTS")) == 0
    out = capsys.readouterr().out
    assert "3" in out
    assert "default" in out


def test_get_reports_a_value_from_config_yaml(tmp_path, capsys) -> None:
    _write_config(tmp_path, "settings:\n  engine:\n    max_concurrent_agents: 7\n")
    assert cmd_config(_args(config_command="get", name="ROBOTHOR_MAX_CONCURRENT_AGENTS")) == 0
    out = capsys.readouterr().out
    assert "7" in out
    assert "config.yaml" in out


def test_get_reports_a_value_from_the_environment(tmp_path, monkeypatch, capsys) -> None:
    _write_config(tmp_path, "settings:\n  engine:\n    max_concurrent_agents: 7\n")
    monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_AGENTS", "9")
    assert cmd_config(_args(config_command="get", name="ROBOTHOR_MAX_CONCURRENT_AGENTS")) == 0
    out = capsys.readouterr().out
    assert "9" in out
    assert "env" in out


def test_get_never_prints_a_secret(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "hunter2-the-real-one")
    assert cmd_config(_args(config_command="get", name="ROBOTHOR_DB_PASSWORD")) == 0
    out = capsys.readouterr().out
    assert "hunter2-the-real-one" not in out
    assert "sha256:" in out
    assert "set" in out


def test_get_an_unset_secret_says_unset(capsys) -> None:
    assert cmd_config(_args(config_command="get", name="ROBOTHOR_DB_PASSWORD")) == 0
    assert "unset" in capsys.readouterr().out


def test_get_accepts_a_deprecated_alias(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert cmd_config(_args(config_command="get", name="TELEGRAM_CHAT_ID")) == 0
    out = capsys.readouterr().out
    assert "12345" in out
    assert "ROBOTHOR_TELEGRAM_CHAT_ID" in out  # named as the replacement


def test_get_an_unknown_name_exits_two_and_suggests(capsys) -> None:
    assert cmd_config(_args(config_command="get", name="ROBOTHOR_MAX_CONCURENT_AGENTS")) == 2
    err = capsys.readouterr().err
    assert "ROBOTHOR_MAX_CONCURRENT_AGENTS" in err


def test_get_json_shape(capsys) -> None:
    assert (
        cmd_config(_args(config_command="get", name="ROBOTHOR_MAX_CONCURRENT_AGENTS", json=True))
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "ROBOTHOR_MAX_CONCURRENT_AGENTS"
    assert payload["value"] == 3
    assert payload["source"] == "default"


# ── explain ──────────────────────────────────────────────────────────────────


def test_explain_prints_the_declaration(capsys) -> None:
    assert cmd_config(_args(config_command="explain", name="ROBOTHOR_TELEGRAM_BOT_TOKEN")) == 0
    out = capsys.readouterr().out
    assert "Bot token for the Telegram channel" in out
    assert "TELEGRAM_BOT_TOKEN" in out  # the deprecated alias
    assert "channels" in out  # the group
    assert "secret" in out
    assert "restart" in out
    assert "default" in out  # the provenance


def test_explain_an_unknown_name_exits_two(capsys) -> None:
    assert cmd_config(_args(config_command="explain", name="ROBOTHOR_NOT_A_SETTING")) == 2


# ── set ──────────────────────────────────────────────────────────────────────


def test_set_a_governed_flag_goes_to_the_flag_store(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        "robothor.flags.store.set_flag",
        lambda name, value, actor, reason: calls.append((name, value, actor, reason)),
    )
    rc = cmd_config(_args(config_command="set", name="ROBOTHOR_RBAC_MODE", value="enforce"))
    assert rc == 0
    assert calls and calls[0][0] == "ROBOTHOR_RBAC_MODE"
    assert calls[0][1] == "enforce"
    assert calls[0][2].startswith("operator:")
    assert "applied" in capsys.readouterr().out


def test_set_a_governed_flag_rejects_a_rung_the_engine_does_not_honour(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "robothor.flags.store.set_flag",
        lambda *a, **k: pytest.fail("must not write an out-of-range value"),
    )
    rc = cmd_config(_args(config_command="set", name="ROBOTHOR_RBAC_MODE", value="mostly"))
    assert rc == 1
    assert "enforce" in capsys.readouterr().err  # the allowed values


def test_set_a_governed_flag_does_not_touch_config_yaml(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("robothor.flags.store.set_flag", lambda *a, **k: None)
    cmd_config(_args(config_command="set", name="ROBOTHOR_RBAC_MODE", value="enforce"))
    assert not _config_path(tmp_path).exists()


def test_set_refuses_a_secret_and_names_the_vault(tmp_path, capsys) -> None:
    rc = cmd_config(
        _args(config_command="set", name="ROBOTHOR_DB_PASSWORD", value="hunter2-the-real-one")
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "genus vault set" in err
    assert "hunter2-the-real-one" not in err
    assert not _config_path(tmp_path).exists()


def test_set_a_hot_field_writes_config_yaml_and_says_applied(tmp_path, capsys) -> None:
    rc = cmd_config(_args(config_command="set", name="ROBOTHOR_MAX_CONCURRENT_AGENTS", value="7"))
    assert rc == 0
    assert "applied" in capsys.readouterr().out
    import yaml

    document = yaml.safe_load(_config_path(tmp_path).read_text())
    assert document["settings"]["engine"]["max_concurrent_agents"] == 7


def test_set_a_restart_required_field_names_the_units(tmp_path, capsys) -> None:
    rc = cmd_config(_args(config_command="set", name="ROBOTHOR_ENGINE_PORT", value="18801"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "restart required" in out
    assert "robothor-engine" in out


def test_set_json_shape(tmp_path, capsys) -> None:
    rc = cmd_config(
        _args(config_command="set", name="ROBOTHOR_ENGINE_PORT", value="18801", json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert "robothor-engine" in payload["pending_restart"]
    assert payload["errors"] == []


def test_set_preserves_unrelated_keys_and_comments(tmp_path) -> None:
    original = (
        "# The instance's own config. Hand-edited, and it shows.\n"
        "instance_id: alpha\n"
        "settings:\n"
        "  # concurrency was raised during the August backlog\n"
        "  engine:\n"
        "    max_concurrent_agents: 7\n"
        "  channels:\n"
        "    ai_name: Robothor\n"
    )
    _write_config(tmp_path, original)
    assert cmd_config(_args(config_command="set", name="ROBOTHOR_TIMEZONE", value="UTC")) == 0

    text = _config_path(tmp_path).read_text()
    assert "# The instance's own config. Hand-edited, and it shows." in text
    assert "# concurrency was raised during the August backlog" in text

    import yaml

    document = yaml.safe_load(text)
    assert document["instance_id"] == "alpha"
    assert document["settings"]["engine"]["max_concurrent_agents"] == 7
    assert document["settings"]["channels"]["ai_name"] == "Robothor"
    assert document["settings"]["engine"]["timezone"] == "UTC"


def test_set_overwrites_an_existing_value_in_place(tmp_path) -> None:
    _write_config(
        tmp_path,
        "settings:\n  engine:\n    max_concurrent_agents: 7  # raised in August\n",
    )
    assert (
        cmd_config(_args(config_command="set", name="ROBOTHOR_MAX_CONCURRENT_AGENTS", value="2"))
        == 0
    )
    text = _config_path(tmp_path).read_text()
    assert "max_concurrent_agents: 2" in text
    assert text.count("max_concurrent_agents") == 1


def test_set_leaves_no_temporary_file_behind(tmp_path) -> None:
    cmd_config(_args(config_command="set", name="ROBOTHOR_MAX_CONCURRENT_AGENTS", value="7"))
    assert sorted(p.name for p in _config_path(tmp_path).parent.iterdir()) == ["config.yaml"]


def test_set_rejects_a_value_the_field_cannot_hold(tmp_path, capsys) -> None:
    rc = cmd_config(
        _args(config_command="set", name="ROBOTHOR_MAX_CONCURRENT_AGENTS", value="plenty")
    )
    assert rc == 1
    assert not _config_path(tmp_path).exists()
    assert "plenty" in capsys.readouterr().err


def test_set_an_unknown_name_exits_two_and_suggests(capsys) -> None:
    rc = cmd_config(_args(config_command="set", name="ROBOTHOR_MAX_CONCURENT_AGENTS", value="7"))
    assert rc == 2
    assert "ROBOTHOR_MAX_CONCURRENT_AGENTS" in capsys.readouterr().err


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_masks_secrets(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "hunter2-the-real-one")
    assert cmd_config(_args(config_command="list", group=None, changed=False)) == 0
    out = capsys.readouterr().out
    assert "hunter2-the-real-one" not in out
    assert "ROBOTHOR_DB_PASSWORD" in out


def test_list_filters_by_group(capsys) -> None:
    assert cmd_config(_args(config_command="list", group="redis", changed=False)) == 0
    out = capsys.readouterr().out
    assert "ROBOTHOR_REDIS_HOST" in out
    assert "ROBOTHOR_MAX_CONCURRENT_AGENTS" not in out


def test_list_changed_shows_only_what_is_configured(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_AGENTS", "9")
    assert cmd_config(_args(config_command="list", group=None, changed=True)) == 0
    out = capsys.readouterr().out
    assert "ROBOTHOR_MAX_CONCURRENT_AGENTS" in out
    assert "ROBOTHOR_REDIS_HOST" not in out


def test_list_rejects_an_unknown_group(capsys) -> None:
    assert cmd_config(_args(config_command="list", group="engines", changed=False)) == 2
    assert "engine" in capsys.readouterr().err


# ── validate is now an alias for `genus doctor` ──────────────────────────────
#
# Everything this section used to assert -- Telegram treated as optional, an
# unknown key named, a deprecated alias pointed at its replacement, a file that
# disagrees with the running process -- moved WITH the code it tests, into
# `robothor/doctor/tests/test_checks_config.py` and `test_checks_channels.py`.
# The alias itself is covered by `robothor/cli/tests/test_doctor_cli.py`.


# ── schema ───────────────────────────────────────────────────────────────────


def test_schema_still_prints_json(capsys) -> None:
    assert cmd_config(_args(config_command="schema")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "GenusSettings"


def test_no_subcommand_prints_usage(capsys) -> None:
    assert cmd_config(_args(config_command=None)) == 0
    assert "get" in capsys.readouterr().out


# ── the shared connectivity checks ───────────────────────────────────────────


def test_required_env_no_longer_demands_telegram(monkeypatch) -> None:
    """A Telegram-free deploy is supported, so it must not fail a check.

    The daemon has always started without a bot token -- agents with
    ``delivery: none`` talk through CRM tasks -- but validate() demanded one,
    so every such instance failed two checks it could never pass. Telegram is
    checked by ``_telegram_checks``: absent is information, misconfigured is an
    error.
    """
    from robothor.config import required_env_checks

    names = {name for name, _ok, _detail in required_env_checks()}
    assert not [name for name in names if "TELEGRAM" in name]
    assert "env:OPENROUTER_API_KEY" in names


def test_explain_a_governed_flag_does_not_ask_for_a_restart(capsys) -> None:
    """It is applied through the flag store, which the engine re-reads."""
    assert cmd_config(_args(config_command="explain", name="ROBOTHOR_RBAC_MODE")) == 0
    out = capsys.readouterr().out
    assert "governed: yes" in out
    assert "restart:  not required" in out


def test_set_updates_a_key_written_under_its_env_name(tmp_path) -> None:
    """Either spelling is legal in config.yaml; writing must not make both."""
    _write_config(
        tmp_path,
        "settings:\n  engine:\n    ROBOTHOR_MAX_CONCURRENT_AGENTS: 7\n",
    )
    assert (
        cmd_config(_args(config_command="set", name="ROBOTHOR_MAX_CONCURRENT_AGENTS", value="2"))
        == 0
    )
    text = _config_path(tmp_path).read_text()
    assert "ROBOTHOR_MAX_CONCURRENT_AGENTS: 2" in text
    assert "max_concurrent_agents" not in text.replace("ROBOTHOR_MAX_CONCURRENT_AGENTS", "")

    import yaml

    document = yaml.safe_load(text)
    assert list(document["settings"]["engine"]) == ["ROBOTHOR_MAX_CONCURRENT_AGENTS"]


def test_set_does_not_mistake_a_hash_in_a_value_for_a_comment(tmp_path) -> None:
    _write_config(tmp_path, 'settings:\n  channels:\n    ai_name: "Ada # the first"\n')
    assert cmd_config(_args(config_command="set", name="ROBOTHOR_AI_NAME", value="Ada")) == 0

    import yaml

    document = yaml.safe_load(_config_path(tmp_path).read_text())
    assert document["settings"]["channels"]["ai_name"] == "Ada"

"""The config category -- inherited whole from ``genus config validate``.

Every assertion here was a ``config validate`` test before this package
existed; they moved with the code. The four things they hold down are the four
ways a configuration lies: it does not load, it holds a key nothing reads, it
uses a name that has been replaced, or it disagrees with the process that is
actually running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003 - used at runtime by the fixtures

import pytest

from robothor.doctor.checks import config as config_checks
from robothor.doctor.tests.conftest import make_ctx


def _run(check_id: str, ctx=None):
    check = next(check for check in config_checks.CHECKS if check.id == check_id)
    return asyncio.run(check.run(ctx or make_ctx()))


def _rows(answer):
    return answer if isinstance(answer, list) else [answer]


def _write_config(workspace: Path, body: str) -> Path:
    path = workspace / ".robothor" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


@pytest.fixture
def workspace(monkeypatch, tmp_path) -> Path:
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    reset_settings()
    return tmp_path


# ── config.settings_load ─────────────────────────────────────────────────────


def test_settings_load_passes_on_a_clean_workspace(workspace) -> None:
    result = _run("config.settings_load")
    assert result.status == "pass"
    assert str(workspace) in result.detail


def test_settings_load_fails_and_names_the_key_when_the_model_refuses(
    workspace, monkeypatch
) -> None:
    monkeypatch.setenv("ROBOTHOR_ENGINE_PORT", "not-a-port")
    from robothor.settings import reset_settings

    reset_settings()
    result = _run("config.settings_load")
    assert result.status == "fail"
    assert "ROBOTHOR_ENGINE_PORT" in result.detail or "port" in result.detail


# ── config.unknown_keys ──────────────────────────────────────────────────────


def test_unknown_keys_passes_when_there_are_none(workspace) -> None:
    _write_config(workspace, "settings:\n  engine:\n    max_concurrent_agents: 7\n")
    assert _rows(_run("config.unknown_keys"))[0].status == "pass"


def test_unknown_keys_names_the_typo(workspace) -> None:
    _write_config(workspace, "settings:\n  engine:\n    max_concurent_agents: 7\n")
    rows = _rows(_run("config.unknown_keys"))
    assert [row.status for row in rows] == ["fail"]
    assert "max_concurent_agents" in rows[0].detail
    assert rows[0].sub_id.endswith("max_concurent_agents")


def test_an_unknown_key_is_recommended_not_required() -> None:
    """In ``observe`` the instance still starts; ``enforce`` makes
    ``config.settings_load`` fail instead, which IS required."""
    check = next(c for c in config_checks.CHECKS if c.id == "config.unknown_keys")
    assert check.severity == "recommended"


# ── config.deprecated_aliases ────────────────────────────────────────────────


def test_a_deprecated_name_in_use_is_reported_with_its_replacement(workspace, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    rows = _rows(_run("config.deprecated_aliases"))
    assert rows[0].status == "fail"
    assert "TELEGRAM_CHAT_ID" in rows[0].detail
    assert "ROBOTHOR_TELEGRAM_CHAT_ID" in rows[0].detail


def test_no_deprecated_names_is_a_pass(workspace) -> None:
    assert _rows(_run("config.deprecated_aliases"))[0].status == "pass"


# ── config.pending_restart ───────────────────────────────────────────────────


def test_a_file_that_disagrees_with_the_process_is_reported(workspace, monkeypatch) -> None:
    _write_config(workspace, "settings:\n  engine:\n    port: 18801\n")
    monkeypatch.setenv("ROBOTHOR_ENGINE_PORT", "18800")
    rows = _rows(_run("config.pending_restart"))
    assert [row.sub_id for row in rows] == ["ROBOTHOR_ENGINE_PORT"]
    assert "restart" in rows[0].detail


def test_a_pending_restart_is_recommended_so_it_cannot_fail_the_command() -> None:
    """The environment winning over the file is documented precedence. Exiting
    non-zero for it would make the command useless as a gate for what IS
    broken."""
    check = next(c for c in config_checks.CHECKS if c.id == "config.pending_restart")
    assert check.severity == "recommended"


@pytest.mark.parametrize("spelling", ["true", "True", "yes", "on", "1"])
def test_a_typed_file_value_and_a_text_environment_value_can_agree(
    workspace, monkeypatch, spelling
) -> None:
    """A YAML ``true`` and an environment ``true`` are the same value.

    Compared as strings they never are -- ``True`` is not ``"true"`` -- and
    every boolean disagreed with itself, reporting a pending restart on a box
    where nothing was wrong.
    """
    _write_config(workspace, "settings:\n  database:\n    rls_enabled: true\n")
    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", spelling)
    rows = _rows(_run("config.pending_restart"))
    assert [row.status for row in rows] == ["pass"]


def test_a_boolean_that_really_disagrees_is_still_reported(workspace, monkeypatch) -> None:
    _write_config(workspace, "settings:\n  database:\n    rls_enabled: true\n")
    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "0")
    rows = _rows(_run("config.pending_restart"))
    assert [row.sub_id for row in rows] == ["ROBOTHOR_RLS_ENABLED"]


def test_a_secret_is_never_printed_by_a_pending_restart_line(workspace, monkeypatch) -> None:
    _write_config(workspace, "settings:\n  database:\n    password: file-side-secret\n")
    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "env-side-secret")
    rows = _rows(_run("config.pending_restart"))
    joined = " ".join(row.detail for row in rows)
    assert "file-side-secret" not in joined
    assert "env-side-secret" not in joined
    assert "<set>" in joined

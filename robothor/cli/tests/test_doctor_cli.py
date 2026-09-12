"""``genus doctor`` from the command line.

The CLI half is thin on purpose -- the runner does the work -- so what is
pinned here is the surface an operator and a CI gate depend on: the flags
parse, ``--json`` is machine-readable, the exit code is the runner's, and
``genus config validate`` still answers on the same exit codes it always did
while saying it has moved.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from robothor.cli import _build_parser
from robothor.cli.doctor_cmd import cmd_doctor
from robothor.doctor.model import Check, Result
from robothor.doctor.runner import DoctorReport


def _check(check_id: str, status: str = "pass", severity: str = "required") -> Check:
    async def run(_ctx) -> Result:
        """A stub."""
        return Result(status=status, detail="stubbed")

    return Check(
        id=check_id,
        title=check_id,
        category=check_id.split(".")[0],
        severity=severity,  # type: ignore[arg-type]
        run=run,
    )


@pytest.fixture
def stub_checks(monkeypatch):
    """Replace the real registry: the CLI test must not touch this box."""
    holder: list[Check] = [_check("a.one")]

    def _all_checks(*_args, **_kwargs):
        return tuple(holder)

    monkeypatch.setattr("robothor.doctor.registry.all_checks", _all_checks)
    monkeypatch.setattr("robothor.doctor.runner._default_checks", _all_checks)
    return holder


# ── the parser ───────────────────────────────────────────────────────────────


def test_doctor_is_a_subcommand_with_every_documented_flag() -> None:
    args = _build_parser().parse_args(
        ["doctor", "--fix", "--json", "--only", "db.connect", "--offline", "--timeout", "9"]
    )
    assert args.command == "doctor"
    assert args.fix is True
    assert args.json is True
    assert args.only == "db.connect"
    assert args.offline is True
    assert args.timeout == 9.0


def test_doctor_defaults_are_a_five_second_budget_and_no_repair() -> None:
    args = _build_parser().parse_args(["doctor"])
    assert args.fix is False
    assert args.dry_run is False
    assert args.timeout == 5.0
    assert args.only is None
    assert args.category is None


def test_category_is_accepted() -> None:
    assert _build_parser().parse_args(["doctor", "--category", "database"]).category == "database"


# ── running it ───────────────────────────────────────────────────────────────


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "json": False,
        "fix": False,
        "dry_run": False,
        "offline": False,
        "timeout": 1.0,
        "only": None,
        "category": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_json_output_parses_and_carries_the_documented_keys(stub_checks, capsys) -> None:
    assert cmd_doctor(_args(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert set(payload["summary"]) == {
        "required_failed",
        "recommended_failed",
        "passed",
        "skipped",
    }
    assert set(payload["checks"][0]) == {
        "id",
        "title",
        "category",
        "severity",
        "status",
        "detail",
        "fixable",
    }


def test_a_required_failure_exits_one(stub_checks, capsys) -> None:
    stub_checks[:] = [_check("a.one", status="fail")]
    assert cmd_doctor(_args(json=True)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_an_unknown_only_id_exits_two(stub_checks, capsys) -> None:
    assert cmd_doctor(_args(only="nope.nope")) == 2
    assert "nope.nope" in capsys.readouterr().out


def test_the_human_table_names_every_check(stub_checks, capsys) -> None:
    stub_checks[:] = [_check("a.one"), _check("b.one", status="fail")]
    assert cmd_doctor(_args()) == 1
    out = capsys.readouterr().out
    assert "a.one" in out
    assert "b.one" in out
    assert "failed" in out


def test_offline_and_timeout_reach_the_context(monkeypatch, stub_checks) -> None:
    seen = {}

    def _fake_run_sync(ctx, **kwargs):
        seen["offline"] = ctx.offline
        seen["timeout"] = ctx.timeout_s
        seen["fix"] = ctx.fix
        seen["dry_run"] = ctx.dry_run
        return DoctorReport()

    monkeypatch.setattr("robothor.cli.doctor_cmd.run_sync", _fake_run_sync)
    cmd_doctor(_args(offline=True, timeout=2.5, fix=True, dry_run=True))
    assert seen == {"offline": True, "timeout": 2.5, "fix": True, "dry_run": True}


# ── the alias ────────────────────────────────────────────────────────────────


def test_config_validate_says_it_moved_and_returns_the_doctor_exit_code(
    monkeypatch, capsys
) -> None:
    from robothor.cli.config_cmd import cmd_config

    monkeypatch.setattr(
        "robothor.cli.doctor_cmd.run_sync",
        lambda ctx, **kwargs: DoctorReport(),
    )
    assert cmd_config(argparse.Namespace(config_command="validate", json=False)) == 0
    err = capsys.readouterr().err
    assert "genus doctor" in err
    assert "deprecated" in err.lower()


def test_config_validate_exits_one_when_a_required_check_failed(monkeypatch, capsys) -> None:
    from robothor.cli.config_cmd import cmd_config
    from robothor.doctor.runner import CheckResult

    failed = CheckResult(
        id="db.connect",
        title="t",
        category="database",
        severity="required",
        status="fail",
        detail="nope",
        fixable=False,
    )
    monkeypatch.setattr(
        "robothor.cli.doctor_cmd.run_sync",
        lambda ctx, **kwargs: DoctorReport(results=[failed]),
    )
    assert cmd_config(argparse.Namespace(config_command="validate", json=False)) == 1


def test_config_validate_runs_offline_so_a_deprecated_alias_gets_no_dearer(
    monkeypatch,
) -> None:
    """The command this replaces made no upstream call. Every runbook and cron
    entry that still types it must not start spending provider budget."""
    from robothor.cli.config_cmd import cmd_config

    seen = {}

    def _fake_run_sync(ctx, **kwargs):
        seen["offline"] = ctx.offline
        return DoctorReport()

    monkeypatch.setattr("robothor.cli.doctor_cmd.run_sync", _fake_run_sync)
    cmd_config(argparse.Namespace(config_command="validate", json=False))
    assert seen["offline"] is True


def test_the_alias_note_says_the_json_shape_changed(monkeypatch, capsys) -> None:
    """A script doing `--json | jq .errors` gets null and reads it as healthy,
    so the one place it will be seen has to say so."""
    from robothor.cli.config_cmd import cmd_config

    monkeypatch.setattr("robothor.cli.doctor_cmd.run_sync", lambda ctx, **kw: DoctorReport())
    cmd_config(argparse.Namespace(config_command="validate", json=True))
    err = capsys.readouterr().err
    assert "genus doctor" in err
    assert "summary" in err and "errors" in err


def test_config_validate_deprecation_note_is_not_on_stdout(monkeypatch, capsys) -> None:
    """``genus config validate --json | jq`` must keep working."""
    from robothor.cli.config_cmd import cmd_config

    monkeypatch.setattr(
        "robothor.cli.doctor_cmd.run_sync",
        lambda ctx, **kwargs: DoctorReport(),
    )
    cmd_config(argparse.Namespace(config_command="validate", json=True))
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "deprecated" in captured.err.lower()


def test_config_usage_still_lists_validate(capsys) -> None:
    from robothor.cli.config_cmd import cmd_config

    cmd_config(argparse.Namespace(config_command=None, json=False))
    assert "validate" in capsys.readouterr().out


class TestItReadsTheInstanceEnvFileFirst:
    """A compose instance keeps its only copy of the database password in
    ``<workspace>/genus.env``, because the platform deliberately stores it
    nowhere else. Without this, `genus doctor` on the host reported
    ``db.connect`` failing against a database that was running fine, and the
    documented workaround was a `set -a; . ./genus.env` line an install gate
    could silently drop.
    """

    def _env_file(self, workspace, body='ROBOTHOR_DB_PASSWORD="from-the-file"\n', mode=0o600):
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "genus.env"
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_the_workspace_env_file_reaches_the_checks(
        self, stub_checks, env_workspace, monkeypatch, capsys
    ) -> None:
        monkeypatch.delenv("ROBOTHOR_DB_PASSWORD", raising=False)
        self._env_file(env_workspace)

        cmd_doctor(argparse.Namespace(json=True, timeout=5.0))

        assert os.environ["ROBOTHOR_DB_PASSWORD"] == "from-the-file"

    def test_a_readable_env_file_is_refused_out_loud(
        self, stub_checks, env_workspace, monkeypatch, capsys
    ) -> None:
        monkeypatch.delenv("ROBOTHOR_DB_PASSWORD", raising=False)
        path = self._env_file(env_workspace, mode=0o644)

        cmd_doctor(argparse.Namespace(json=True, timeout=5.0))

        assert "ROBOTHOR_DB_PASSWORD" not in os.environ
        captured = capsys.readouterr()
        # The refusal is a finding, so it is said -- on stderr, because stdout
        # is a JSON contract.
        assert str(path) in captured.err
        assert "chmod 600" in captured.err
        assert str(path) not in captured.out


class TestTheEnvFileRefreshesEveryCacheItFeeds:
    """`genus doctor` on a compose host reads genus.env for the database
    password and host. Resetting only the settings left `robothor.config` --
    the singleton the database layer actually reads -- holding the environment
    as it was before the file was loaded, so five required checks failed
    against a database answering on its published port.
    """

    def test_loading_the_file_drops_the_config_singleton_and_the_pool(self, tmp_path, monkeypatch):
        from robothor.secrets.env_file import apply_instance_env, env_line, write_private

        dropped: list[str] = []
        monkeypatch.setattr("robothor.settings.reset_settings", lambda: dropped.append("settings"))
        monkeypatch.setattr("robothor.config.reset_config", lambda: dropped.append("config"))
        monkeypatch.setattr("robothor.db.connection.close_pool", lambda: dropped.append("pool"))
        monkeypatch.delenv("ROBOTHOR_DB_HOST", raising=False)

        write_private(tmp_path / "genus.env", env_line("ROBOTHOR_DB_HOST", "127.0.0.1") + "\n")

        result = apply_instance_env(tmp_path)

        assert result.loaded
        assert dropped == ["settings", "config", "pool"]

    def test_an_absent_file_drops_nothing(self, tmp_path, monkeypatch):
        from robothor.secrets.env_file import apply_instance_env

        dropped: list[str] = []
        monkeypatch.setattr("robothor.config.reset_config", lambda: dropped.append("config"))
        monkeypatch.setattr("robothor.db.connection.close_pool", lambda: dropped.append("pool"))

        assert apply_instance_env(tmp_path).loaded is False
        assert dropped == []

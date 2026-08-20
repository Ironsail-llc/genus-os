"""CLI surface contracts — documented subcommands must be reachable.

Regression coverage for the 2026-04-09 export/import/tenant modules that
shipped with ``cmd_*`` implementations but were never wired into the
argparse tree, leaving the advertised commands unreachable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.cli import main


@pytest.mark.parametrize(
    "argv",
    [
        ["export", "--help"],
        ["import", "--help"],
        ["tenant", "--help"],
        ["tenant", "create", "--help"],
        ["tenant", "list", "--help"],
        ["tenant", "status", "--help"],
    ],
)
def test_subcommand_help_exits_zero(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_export_dispatches_with_defaults() -> None:
    with patch("robothor.cli.exporter.cmd_export", return_value=0) as cmd:
        assert main(["export"]) == 0
    args = cmd.call_args.args[0]
    assert args.tenant is None
    assert args.output is None
    assert args.include_memory is False


def test_export_parses_flags() -> None:
    with patch("robothor.cli.exporter.cmd_export", return_value=0) as cmd:
        assert main(["export", "--tenant", "acme", "--output", "/tmp/x", "--include-memory"]) == 0
    args = cmd.call_args.args[0]
    assert args.tenant == "acme"
    assert args.output == "/tmp/x"
    assert args.include_memory is True


def test_import_defaults_to_auto_detection() -> None:
    with patch("robothor.cli.importer.cmd_import", return_value=0) as cmd:
        assert main(["import", "--source", "/tmp/bundle"]) == 0
    args = cmd.call_args.args[0]
    assert args.platform == "auto"
    assert args.source == "/tmp/bundle"


def test_import_parses_platform() -> None:
    with patch("robothor.cli.importer.cmd_import", return_value=0) as cmd:
        assert main(["import", "hermes", "--tenant", "acme"]) == 0
    args = cmd.call_args.args[0]
    assert args.platform == "hermes"
    assert args.tenant == "acme"


def test_import_rejects_unknown_platform(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["import", "not-a-platform"])
    assert exc.value.code == 2


def test_tenant_create_dispatches() -> None:
    with patch("robothor.cli.tenant.cmd_tenant", return_value=0) as cmd:
        assert main(["tenant", "create", "acme", "--name", "Acme Corp"]) == 0
    args = cmd.call_args.args[0]
    assert args.tenant_command == "create"
    assert args.id == "acme"
    assert args.name == "Acme Corp"


def test_tenant_status_requires_tenant_id() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["tenant", "status"])
    assert exc.value.code == 2
    with patch("robothor.cli.tenant.cmd_tenant", return_value=0) as cmd:
        assert main(["tenant", "status", "acme"]) == 0
    assert cmd.call_args.args[0].tenant_id == "acme"

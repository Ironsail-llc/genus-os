"""CLI surface contracts — documented subcommands must be reachable.

Regression coverage for the 2026-04-09 export/import/tenant modules that
shipped with ``cmd_*`` implementations but were never wired into the
argparse tree, leaving the advertised commands unreachable.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from robothor.cli import _build_parser, main


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


@pytest.mark.parametrize(
    ("argv0", "expected"),
    [
        ("/x/genus", "genus"),
        ("/x/robothor", "robothor"),
        ("/usr/local/bin/genusos", "genusos"),
    ],
)
def test_prog_reflects_invoked_name(argv0: str, expected: str, monkeypatch) -> None:
    """``--help`` must name the verb the user actually typed, not a hardcoded one."""
    monkeypatch.setattr(sys, "argv", [argv0])
    assert _build_parser().prog == expected


@pytest.mark.parametrize("argv", [[], [""]])
def test_prog_falls_back_to_genus(argv: list[str], monkeypatch) -> None:
    """An empty or unusable ``sys.argv`` still yields the documented verb."""
    monkeypatch.setattr(sys, "argv", argv)
    assert _build_parser().prog == "genus"


def test_console_scripts_declared() -> None:
    """The wheel must expose ``genus`` alongside the two legacy verbs."""
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    if not pyproject.exists():  # pragma: no cover - installed wheel, no source tree
        pytest.skip("pyproject.toml is not part of an installed distribution")
    with pyproject.open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]
    assert scripts["genus"] == "robothor.cli:main"
    assert scripts["genusos"] == "robothor.cli:main"
    assert scripts["robothor"] == "robothor.cli:main"

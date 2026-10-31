"""``genus tenant move-workload`` (the move itself mocked; see the integration tests)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from argparse import Namespace

from robothor.cli import _build_parser
from robothor.cli.tenant import cmd_tenant
from robothor.tenancy import move_workload as mw

MOVE = "robothor.tenancy.move_workload.move_workload"


def _parse(*argv: str) -> Namespace:
    return _build_parser().parse_args(["tenant", *argv])


def _report(**overrides: Any) -> mw.WorkloadMoveReport:
    fields: dict[str, Any] = {
        "from_tenant": "acme-parent",
        "to_tenant": "acme-child-a",
        "agents": ["triage"],
        "workflows": ["intake"],
        "dry_run": False,
        "counts": {"agent_runs": 3, "chat_sessions": 1, "workflow_runs": 0},
        "followed": {"agent_run_steps": 9},
        "links_cleared": {"agent_runs.person_id": 1},
        "audit_id": 7,
    }
    fields.update(overrides)
    return mw.WorkloadMoveReport(**fields)


BASE = ("move-workload", "--from", "acme-parent", "--to", "acme-child-a")


def test_parser_accepts_repeated_agents_and_workflows() -> None:
    args = _parse(
        *BASE, "--agent", "triage", "--agent", "helper", "--workflow", "intake", "--dry-run",
    )  # fmt: skip
    assert args.tenant_command == "move-workload"
    assert args.from_tenant == "acme-parent"
    assert args.to == "acme-child-a"
    assert args.agent == ["triage", "helper"]
    assert args.workflow == ["intake"]
    assert args.dry_run is True


def test_parser_requires_from_and_to() -> None:
    with pytest.raises(SystemExit):
        _parse("move-workload", "--to", "acme-child-a", "--agent", "triage")
    with pytest.raises(SystemExit):
        _parse("move-workload", "--from", "acme-parent", "--agent", "triage")


def test_passes_the_workload_to_the_move(capsys) -> None:
    with patch(MOVE, return_value=_report()) as mv:
        rc = cmd_tenant(_parse(*BASE, "--agent", "triage", "--workflow", "intake"))
    assert rc == 0
    kw = mv.call_args.kwargs
    assert kw["from_tenant"] == "acme-parent"
    assert kw["to_tenant"] == "acme-child-a"
    assert kw["agents"] == ["triage"]
    assert kw["workflows"] == ["intake"]
    assert kw["dry_run"] is False
    assert kw["actor"].startswith("cli:")
    out = capsys.readouterr().out
    assert "Moved workload from 'acme-parent' to 'acme-child-a'" in out
    assert "agent_runs" in out and "3" in out
    assert "agent_run_steps" in out
    assert "agent_runs.person_id" in out


def test_no_workflow_flag_means_none(capsys) -> None:
    with patch(MOVE, return_value=_report(workflows=[])) as mv:
        assert cmd_tenant(_parse(*BASE, "--agent", "triage")) == 0
    assert mv.call_args.kwargs["workflows"] == []


def test_dry_run_says_would_move(capsys) -> None:
    with patch(MOVE, return_value=_report(dry_run=True, audit_id=None)) as mv:
        assert cmd_tenant(_parse(*BASE, "--agent", "triage", "--dry-run")) == 0
    assert mv.call_args.kwargs["dry_run"] is True
    assert "Would move workload" in capsys.readouterr().out


_NOTHING = {"counts": {"agent_runs": 0, "chat_sessions": 0}, "followed": {}, "links_cleared": {}}


def test_a_move_with_nothing_to_move_says_so(capsys) -> None:
    with patch(MOVE, return_value=_report(audit_id=None, **_NOTHING)):
        assert cmd_tenant(_parse(*BASE, "--agent", "triage")) == 0
    out = capsys.readouterr().out
    assert "Nothing to move" in out
    assert "Moved workload" not in out
    assert "agent_runs" not in out


def test_a_dry_run_with_nothing_to_move_says_so(capsys) -> None:
    with patch(MOVE, return_value=_report(dry_run=True, audit_id=None, **_NOTHING)):
        assert cmd_tenant(_parse(*BASE, "--agent", "triage", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "Nothing to move" in out
    assert "Would move workload" not in out


def test_json_output(capsys) -> None:
    with patch(MOVE, return_value=_report()):
        assert cmd_tenant(_parse(*BASE, "--agent", "triage", "--json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["agent_runs"] == 3
    assert data["audit_id"] == 7


def test_a_refusal_is_reported_and_fails(capsys) -> None:
    with patch(MOVE, side_effect=mw.WorkloadMoveError("target tenant 'x' does not exist")):
        rc = cmd_tenant(_parse(*BASE, "--agent", "triage"))
    assert rc == 1
    assert capsys.readouterr().err == "Error: target tenant 'x' does not exist\n"

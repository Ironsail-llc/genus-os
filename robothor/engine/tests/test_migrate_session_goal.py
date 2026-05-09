"""Tests for the brain/GOAL.md → crm_task migration script."""

from __future__ import annotations

# The script lives outside the package; ensure it's importable in tests.
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "migrate_session_goal.py"
_spec = importlib.util.spec_from_file_location("migrate_session_goal", str(_SCRIPT_PATH))
assert _spec is not None and _spec.loader is not None
migrate_mod = importlib.util.module_from_spec(_spec)
sys.modules["migrate_session_goal"] = migrate_mod
_spec.loader.exec_module(migrate_mod)


LEGACY_FIXTURE = """# Active Goal

id: goal-60366c4318c7
status: active
created_at: 2026-05-09T00:50:17.459125+00:00
updated_at: 2026-05-09T00:50:17.459152+00:00
schema_version: 1

## Objective
Create a goal feature in Genus OS. Continue until the feature matches the expected Codex-style long-goal behavior, is tested, and is confirmed capable of carrying long goals with the same quality.

## Success Criteria
- Genus OS CLI can set and show an active long-running goal.
- Active goals are injected into agent runs so agents continue across turns/runs.
- The goal cannot be marked complete until test and verification evidence are recorded.
- Focused automated tests pass for the goal feature.

## Evidence
- implementation: Added DAL helpers
- test: pytest passed
- none yet

## Completion Note


## Agent Instruction
Treat this as the active short-term objective.
"""


def test_parse_legacy_goal_extracts_objective_and_criteria():
    parsed = migrate_mod.parse_legacy_goal(LEGACY_FIXTURE)
    assert parsed["objective"].startswith("Create a goal feature in Genus OS")
    assert "Genus OS CLI can set" in parsed["success_criteria"][0]
    assert len(parsed["success_criteria"]) == 4


def test_parse_legacy_goal_extracts_evidence_lines_excluding_none_yet():
    parsed = migrate_mod.parse_legacy_goal(LEGACY_FIXTURE)
    assert "implementation: Added DAL helpers" in parsed["evidence_lines"]
    assert "test: pytest passed" in parsed["evidence_lines"]
    assert "none yet" not in parsed["evidence_lines"]


def test_parse_handles_missing_sections_gracefully():
    parsed = migrate_mod.parse_legacy_goal("# Active Goal\n\n## Objective\nFoo\n")
    assert parsed["objective"] == "Foo"
    assert parsed["success_criteria"] == []
    assert parsed["evidence_lines"] == []
    assert parsed["completion_note"] == ""


@patch.object(migrate_mod.dal, "add_session_goal_evidence", return_value=True)
@patch.object(migrate_mod.dal, "create_session_goal", return_value="task-new")
@patch.object(migrate_mod.dal, "get_active_session_goal", return_value=None)
@patch.object(migrate_mod, "regenerate_goal_md_cache", return_value=None)
def test_migrate_creates_task_and_moves_file(_cache, mock_get, mock_create, mock_add_ev, tmp_path):
    legacy = tmp_path / "brain" / "GOAL.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(LEGACY_FIXTURE)

    rc = migrate_mod.migrate(tmp_path, tenant_id="default")
    assert rc == 0

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["tenant_id"] == "default"
    assert kwargs["objective"].startswith("Create a goal feature")

    # Each legacy evidence line copied as a 'note' kind.
    assert mock_add_ev.call_count == 2
    for call in mock_add_ev.call_args_list:
        assert call.kwargs["kind"] == "note"

    # Original file moved aside; no GOAL.md left at original path
    # (regenerate_goal_md_cache is mocked, so no replacement here).
    assert not legacy.exists()
    backups = list(legacy.parent.glob("GOAL.md.legacy.*.bak"))
    assert len(backups) == 1


@patch.object(migrate_mod.dal, "create_session_goal")
@patch.object(migrate_mod.dal, "get_active_session_goal")
def test_migrate_idempotent_when_task_exists(mock_get, mock_create, tmp_path, capsys):
    mock_get.return_value = {"id": "task-existing"}
    legacy = tmp_path / "brain" / "GOAL.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(LEGACY_FIXTURE)

    rc = migrate_mod.migrate(tmp_path, tenant_id="default")
    assert rc == 0
    mock_create.assert_not_called()
    assert legacy.exists()  # untouched
    assert "already exists" in capsys.readouterr().out


@patch.object(migrate_mod.dal, "get_active_session_goal", return_value=None)
def test_migrate_skip_when_no_legacy_file(mock_get, tmp_path, capsys):
    rc = migrate_mod.migrate(tmp_path, tenant_id="default")
    assert rc == 0
    assert "no legacy file" in capsys.readouterr().out


@patch.object(migrate_mod.dal, "create_session_goal")
@patch.object(migrate_mod.dal, "get_active_session_goal", return_value=None)
def test_migrate_dry_run_writes_nothing(mock_get, mock_create, tmp_path, capsys):
    legacy = tmp_path / "brain" / "GOAL.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(LEGACY_FIXTURE)

    rc = migrate_mod.migrate(tmp_path, tenant_id="default", dry_run=True)
    assert rc == 0
    mock_create.assert_not_called()
    assert legacy.exists()
    output = capsys.readouterr().out
    assert "[dry-run]" in output

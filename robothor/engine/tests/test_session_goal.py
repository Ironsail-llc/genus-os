"""Tests for the session_goal module — DAL-backed.

Session goals live in crm_tasks (tag=session_goal). The session_goal module
exposes:
  - SessionGoal / GoalEvidence dataclasses with typed evidence kinds
  - validate_evidence (per-kind verifiable references)
  - missing_completion_requirements (≥1 valid test_run AND ≥1 valid commit)
  - DAL-backed create_active_goal / add_evidence / complete_goal
  - build_goal_context (owner-only scoping)
  - regenerate_goal_md_cache (atomic write of denorm markdown to brain/GOAL.md)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import session_goal as sg

# ─────────────────────────────────────────────────────────────────────────────
# validate_evidence
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateEvidence:
    def test_test_run_pytest_summary_is_valid(self, tmp_path):
        item = sg.GoalEvidence(
            kind="test_run",
            summary="all green",
            reference="pytest:passed:42",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok

    def test_test_run_failed_summary_is_valid(self, tmp_path):
        item = sg.GoalEvidence(
            kind="test_run",
            summary="2 failed",
            reference="pytest:failed:2",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok

    def test_test_run_uuid_is_valid(self, tmp_path):
        item = sg.GoalEvidence(
            kind="test_run",
            summary="step",
            reference="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok

    def test_test_run_garbage_reference_invalid(self, tmp_path):
        item = sg.GoalEvidence(
            kind="test_run",
            summary="claim",
            reference="trust me bro",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason

    @patch("robothor.engine.session_goal.subprocess.run")
    def test_commit_valid_when_git_resolves_sha(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        item = sg.GoalEvidence(
            kind="commit",
            summary="shipped",
            reference="abcdef1234567",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok
        # Confirm we asked git to verify the object exists.
        called = mock_run.call_args[0][0]
        assert called[:3] == ["git", "cat-file", "-e"]

    @patch("robothor.engine.session_goal.subprocess.run")
    def test_commit_invalid_when_git_rejects(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="bad")
        item = sg.GoalEvidence(
            kind="commit",
            summary="claim",
            reference="deadbeefdeadbeef",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason

    def test_commit_invalid_when_too_short(self, tmp_path):
        item = sg.GoalEvidence(
            kind="commit",
            summary="x",
            reference="abc",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok

    def test_commit_invalid_when_not_hex(self, tmp_path):
        item = sg.GoalEvidence(
            kind="commit",
            summary="x",
            reference="zzzzzzz",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok

    def test_ci_run_valid_with_https_url(self, tmp_path):
        item = sg.GoalEvidence(
            kind="ci_run",
            summary="green",
            reference="https://ci.example/run/123",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok

    def test_ci_run_invalid_with_non_url(self, tmp_path):
        item = sg.GoalEvidence(
            kind="ci_run",
            summary="claim",
            reference="probably ran",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok

    def test_note_always_valid(self, tmp_path):
        item = sg.GoalEvidence(
            kind="note",
            summary="freeform",
            reference="",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok

    def test_unknown_kind_invalid(self, tmp_path):
        item = sg.GoalEvidence(
            kind="bogus",
            summary="x",
            reference="",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok

    @patch("robothor.engine.session_goal.dal.run_step_exists")
    def test_tool_output_valid_when_step_exists(self, mock_exists, tmp_path):
        mock_exists.return_value = True
        item = sg.GoalEvidence(
            kind="tool_output",
            summary="ran the tool",
            reference="3fa85f64-5717-4562-b3fc-2c963f66afa6:3",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok
        mock_exists.assert_called_once_with("3fa85f64-5717-4562-b3fc-2c963f66afa6", 3)

    @patch("robothor.engine.session_goal.dal.run_step_exists")
    def test_tool_output_invalid_when_step_missing(self, mock_exists, tmp_path):
        mock_exists.return_value = False
        item = sg.GoalEvidence(
            kind="tool_output",
            summary="claim",
            reference="3fa85f64-5717-4562-b3fc-2c963f66afa6:3",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason

    def test_tool_output_invalid_when_ref_malformed(self, tmp_path):
        item = sg.GoalEvidence(
            kind="tool_output",
            summary="claim",
            reference="not-a-run-ref",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason

    @patch("robothor.engine.session_goal.dal.run_step_exists")
    def test_tool_output_dal_error_is_invalid(self, mock_exists, tmp_path):
        mock_exists.side_effect = RuntimeError("db down")
        item = sg.GoalEvidence(
            kind="tool_output",
            summary="claim",
            reference="3fa85f64-5717-4562-b3fc-2c963f66afa6:3",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason

    @patch("robothor.engine.session_goal.dal.benchmark_result_exists")
    def test_benchmark_run_valid_when_row_exists(self, mock_exists, tmp_path):
        mock_exists.return_value = True
        item = sg.GoalEvidence(
            kind="benchmark_run",
            summary="suite passed",
            reference="42",
        )
        ok, _ = sg.validate_evidence(item, workspace=str(tmp_path))
        assert ok
        mock_exists.assert_called_once_with(42)

    @patch("robothor.engine.session_goal.dal.benchmark_result_exists")
    def test_benchmark_run_invalid_when_row_missing(self, mock_exists, tmp_path):
        mock_exists.return_value = False
        item = sg.GoalEvidence(
            kind="benchmark_run",
            summary="claim",
            reference="999",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason

    def test_benchmark_run_invalid_when_ref_not_numeric(self, tmp_path):
        item = sg.GoalEvidence(
            kind="benchmark_run",
            summary="claim",
            reference="suite-42",
        )
        ok, reason = sg.validate_evidence(item, workspace=str(tmp_path))
        assert not ok
        assert reason


# ─────────────────────────────────────────────────────────────────────────────
# missing_completion_requirements
# ─────────────────────────────────────────────────────────────────────────────


def _goal(*evidence: sg.GoalEvidence, criteria: list[str] | None = None) -> sg.SessionGoal:
    return sg.SessionGoal(
        id="task-1",
        objective="ship it",
        success_criteria=criteria or ["matters"],
        agent_id="",
        status="active",
        evidence=list(evidence),
        completion_note="",
    )


class TestMissingCompletionRequirements:
    def test_no_evidence_blocks(self, tmp_path):
        goal = _goal()
        missing = sg.missing_completion_requirements(goal, workspace=str(tmp_path))
        assert any("evidence" in m for m in missing)

    def test_only_note_blocks(self, tmp_path):
        goal = _goal(sg.GoalEvidence(kind="note", summary="x", reference="", valid=True))
        missing = sg.missing_completion_requirements(goal, workspace=str(tmp_path))
        assert any("test_run" in m for m in missing)
        assert any("commit" in m for m in missing)

    def test_test_run_alone_blocks_on_commit(self, tmp_path):
        goal = _goal(
            sg.GoalEvidence(kind="test_run", summary="ok", reference="pytest:passed:5", valid=True),
        )
        missing = sg.missing_completion_requirements(goal, workspace=str(tmp_path))
        assert any("commit" in m for m in missing)
        assert not any("test_run" in m for m in missing)

    @patch("robothor.engine.session_goal.subprocess.run")
    def test_one_of_each_unblocks_completion(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        goal = _goal(
            sg.GoalEvidence(kind="test_run", summary="ok", reference="pytest:passed:5", valid=True),
            sg.GoalEvidence(kind="commit", summary="ship", reference="abcdef1234567", valid=True),
        )
        missing = sg.missing_completion_requirements(goal, workspace=str(tmp_path))
        assert missing == []

    @patch("robothor.engine.session_goal.subprocess.run")
    def test_invalid_commit_does_not_count(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="")
        goal = _goal(
            sg.GoalEvidence(kind="test_run", summary="ok", reference="pytest:passed:5", valid=True),
            sg.GoalEvidence(
                kind="commit", summary="claim", reference="deadbeefdeadbeef", valid=True
            ),
        )
        missing = sg.missing_completion_requirements(goal, workspace=str(tmp_path))
        assert any("commit" in m for m in missing)


# ─────────────────────────────────────────────────────────────────────────────
# build_goal_context — DAL-backed, owner-only scoping
# ─────────────────────────────────────────────────────────────────────────────


def _row(
    *,
    tags: list[str],
    objective: str = "Ship it",
    status: str = "TODO",
    meta: dict[str, Any] | None = None,
    task_id: str = "t1",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "objective": objective,
        "tags": tags,
        "status": status,
        "session_goal_meta": meta
        or {
            "success_criteria": ["c1"],
            "evidence": [],
            "completion_note": "",
        },
    }


class TestBuildGoalContext:
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_workspace_goal_injects_for_main(self, mock_get):
        # No agent-scoped goal for main; workspace goal exists.
        def side_effect(*, tenant_id, agent_id=""):
            if agent_id:
                return None
            return _row(tags=["session_goal"], objective="Ship session goal")

        mock_get.side_effect = side_effect

        ctx = sg.build_goal_context(tenant_id="default", agent_id="main")

        assert "ACTIVE SHORT-TERM GOAL" in ctx
        assert "Ship session goal" in ctx
        # Owner resolution: agent-scoped looked up first, then workspace fallback.
        calls = mock_get.call_args_list
        assert any(c.kwargs.get("agent_id", "") == "" for c in calls), (
            "expected a workspace-scope lookup"
        )

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_workspace_goal_does_not_inject_for_worker(self, mock_get):
        # Agent-scoped lookup returns nothing; workspace lookup returns a goal.
        # Worker agents must NOT see the workspace goal.
        def side_effect(*, tenant_id, agent_id=""):
            if agent_id:
                return None
            return _row(tags=["session_goal"], objective="not for workers")

        mock_get.side_effect = side_effect

        ctx = sg.build_goal_context(tenant_id="default", agent_id="email-classifier")
        assert ctx == ""

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_agent_scoped_goal_injects_only_for_owner(self, mock_get):
        def side_effect(*, tenant_id, agent_id=""):
            if agent_id == "delphi":
                return _row(tags=["session_goal", "agent:delphi"], objective="Delphi work")
            return None

        mock_get.side_effect = side_effect

        # Owner sees it.
        own_ctx = sg.build_goal_context(tenant_id="default", agent_id="delphi")
        assert "Delphi work" in own_ctx
        # Other agent does not.
        other_ctx = sg.build_goal_context(tenant_id="default", agent_id="email-classifier")
        assert other_ctx == ""

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_no_active_goal_returns_empty(self, mock_get):
        mock_get.return_value = None
        assert sg.build_goal_context(tenant_id="default", agent_id="main") == ""


# ─────────────────────────────────────────────────────────────────────────────
# regenerate_goal_md_cache — atomic write
# ─────────────────────────────────────────────────────────────────────────────


class TestRegenerateGoalMdCache:
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_writes_cache_when_active_goal_exists(self, mock_get, tmp_path):
        mock_get.return_value = _row(
            tags=["session_goal"],
            objective="Ship it",
            meta={
                "success_criteria": ["work", "tests"],
                "evidence": [
                    {
                        "kind": "note",
                        "summary": "started",
                        "reference": "",
                        "recorded_at": "2026-05-09T00:00:00+00:00",
                        "valid": True,
                    }
                ],
                "completion_note": "",
            },
        )
        cache_path = sg.regenerate_goal_md_cache(tenant_id="default", workspace=str(tmp_path))
        assert cache_path is not None
        assert cache_path.exists()
        text = cache_path.read_text()
        assert "Ship it" in text
        assert "- work" in text
        assert "started" in text

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_removes_cache_when_no_active_goal(self, mock_get, tmp_path):
        # Pre-create the file to verify it gets removed.
        path = tmp_path / "brain" / "GOAL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale")

        mock_get.return_value = None

        result = sg.regenerate_goal_md_cache(tenant_id="default", workspace=str(tmp_path))
        assert result is None
        assert not path.exists()

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_atomic_write_uses_tempfile_and_rename(self, mock_get, tmp_path):
        mock_get.return_value = _row(tags=["session_goal"], objective="x")
        # Just verify the result file lands at the expected path and that
        # no temp files leak in the directory.
        sg.regenerate_goal_md_cache(tenant_id="default", workspace=str(tmp_path))
        files = list((tmp_path / "brain").iterdir())
        assert any(f.name == "GOAL.md" for f in files)
        assert not any(f.name.startswith("GOAL.md.tmp") for f in files)


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle: create_active_goal / add_evidence / complete_goal — DAL-backed
# ─────────────────────────────────────────────────────────────────────────────


class TestCreateActiveGoal:
    @patch("robothor.engine.session_goal.dal.create_session_goal")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_creates_when_none_active(self, mock_get, mock_create):
        mock_get.return_value = None
        mock_create.return_value = "task-new"

        goal = sg.create_active_goal(
            tenant_id="default",
            objective="Ship it",
            criteria=["one", "two"],
        )
        assert goal.id == "task-new"
        assert goal.objective == "Ship it"
        assert goal.success_criteria == ["one", "two"]

    @patch("robothor.engine.session_goal.dal.create_session_goal")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_refuses_when_active_exists(self, mock_get, mock_create):
        mock_get.return_value = _row(tags=["session_goal"], objective="existing")
        with pytest.raises(ValueError, match="already exists"):
            sg.create_active_goal(
                tenant_id="default",
                objective="another",
            )
        mock_create.assert_not_called()


class TestAddEvidence:
    @patch("robothor.engine.session_goal.dal.add_session_goal_evidence")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    @patch("robothor.engine.session_goal.subprocess.run")
    def test_adds_valid_commit_evidence(self, mock_run, mock_get, mock_add):
        mock_run.return_value = MagicMock(returncode=0)
        mock_get.return_value = _row(tags=["session_goal"], task_id="t1")
        mock_add.return_value = True

        sg.add_evidence(
            tenant_id="default",
            kind="commit",
            summary="shipped",
            reference="abcdef1234567",
            workspace="/tmp",
        )
        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["task_id"] == "t1"
        assert kwargs["kind"] == "commit"
        assert kwargs["valid"] is True

    @patch("robothor.engine.session_goal.dal.add_session_goal_evidence")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_records_invalid_evidence_with_valid_false(self, mock_get, mock_add):
        mock_get.return_value = _row(tags=["session_goal"], task_id="t1")
        mock_add.return_value = True

        sg.add_evidence(
            tenant_id="default",
            kind="test_run",
            summary="claim",
            reference="trust me",  # not a valid pytest summary or UUID
            workspace="/tmp",
        )
        kwargs = mock_add.call_args.kwargs
        assert kwargs["valid"] is False

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_raises_when_no_active_goal(self, mock_get):
        mock_get.return_value = None
        with pytest.raises(ValueError, match="no active goal"):
            sg.add_evidence(
                tenant_id="default",
                kind="note",
                summary="x",
                reference="",
                workspace="/tmp",
            )


class TestCompleteGoal:
    @patch("robothor.engine.session_goal.dal.complete_session_goal")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    @patch("robothor.engine.session_goal.subprocess.run")
    def test_completes_when_evidence_satisfied(self, mock_run, mock_get, mock_complete):
        mock_run.return_value = MagicMock(returncode=0)
        mock_get.return_value = _row(
            tags=["session_goal"],
            task_id="t1",
            meta={
                "success_criteria": ["c1"],
                "evidence": [
                    {
                        "kind": "test_run",
                        "summary": "ok",
                        "reference": "pytest:passed:5",
                        "recorded_at": "2026-05-09T00:00:00+00:00",
                        "valid": True,
                    },
                    {
                        "kind": "commit",
                        "summary": "ship",
                        "reference": "abcdef1234567",
                        "recorded_at": "2026-05-09T00:00:00+00:00",
                        "valid": True,
                    },
                ],
                "completion_note": "",
            },
        )
        mock_complete.return_value = True

        result = sg.complete_goal(
            tenant_id="default",
            note="shipped",
            workspace="/tmp",
        )
        assert result.status == "complete"
        mock_complete.assert_called_once()

    @patch("robothor.engine.session_goal.dal.complete_session_goal")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_blocks_when_evidence_missing(self, mock_get, mock_complete):
        mock_get.return_value = _row(
            tags=["session_goal"],
            task_id="t1",
            meta={"success_criteria": ["c1"], "evidence": [], "completion_note": ""},
        )
        with pytest.raises(ValueError, match="not ready to complete"):
            sg.complete_goal(tenant_id="default", note="x", workspace="/tmp")
        mock_complete.assert_not_called()

    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    def test_raises_when_no_active_goal(self, mock_get):
        mock_get.return_value = None
        with pytest.raises(ValueError, match="no active goal"):
            sg.complete_goal(tenant_id="default", note="x", workspace="/tmp")

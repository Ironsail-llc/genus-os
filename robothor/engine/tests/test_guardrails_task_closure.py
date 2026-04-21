"""Tests for the requires_human_task_closure post-run guardrail."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from robothor.engine.guardrails import (
    _collect_closed_task_ids,
    _collect_driven_task_ids,
    check_post_run,
)


def _step(**kw):
    defaults = {"tool_name": None, "tool_input": None, "tool_output": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _run(steps, task_id=None, run_id="run-xyz", agent_id="main"):
    return SimpleNamespace(
        id=run_id,
        agent_id=agent_id,
        task_id=task_id,
        steps=steps,
        tenant_id="",
    )


class _Cfg:
    def __init__(self, policies):
        self.guardrails = policies


# ─── Helper collectors ───────────────────────────────────────────────


class TestCollectDrivenTaskIds:
    def test_collects_from_get_task_with_requires_human(self):
        steps = [
            _step(tool_name="get_task", tool_output={"id": "t1", "requires_human": True}),
            _step(tool_name="get_task", tool_output={"id": "t2", "requires_human": False}),
        ]
        assert _collect_driven_task_ids(_run(steps)) == {"t1"}

    def test_handles_camelCase_flag(self):
        steps = [
            _step(tool_name="get_task", tool_output={"id": "t1", "requiresHuman": True}),
        ]
        assert _collect_driven_task_ids(_run(steps)) == {"t1"}

    def test_includes_run_task_id(self):
        steps = []
        assert _collect_driven_task_ids(_run(steps, task_id="t-root")) == {"t-root"}


class TestCollectClosedTaskIds:
    def test_update_task_to_done_counts_as_closed(self):
        steps = [
            _step(tool_name="update_task", tool_input={"id": "t1", "status": "DONE"}),
        ]
        assert _collect_closed_task_ids(_run(steps)) == {"t1"}

    def test_update_task_without_status_change_does_not_count(self):
        steps = [
            _step(tool_name="update_task", tool_input={"id": "t1", "title": "new title"}),
        ]
        assert _collect_closed_task_ids(_run(steps)) == set()

    def test_resolve_task_counts(self):
        steps = [_step(tool_name="resolve_task", tool_input={"id": "t1"})]
        assert _collect_closed_task_ids(_run(steps)) == {"t1"}


# ─── Full check_post_run behavior ────────────────────────────────────


class TestCheckPostRun:
    def test_noop_when_guardrail_not_enabled(self):
        steps = [
            _step(tool_name="get_task", tool_output={"id": "t1", "requires_human": True}),
        ]
        out = check_post_run(_run(steps), _Cfg([]))
        assert out == []

    def test_noop_when_driven_task_was_closed(self):
        steps = [
            _step(tool_name="get_task", tool_output={"id": "t1", "requires_human": True}),
            _step(tool_name="resolve_task", tool_input={"id": "t1"}),
        ]
        # No DAL calls expected since no unclosed tasks
        with patch("robothor.crm.dal.get_task") as gt, patch("robothor.crm.dal.update_task") as ut:
            out = check_post_run(_run(steps), _Cfg(["requires_human_task_closure"]))
        assert out == []
        gt.assert_not_called()
        ut.assert_not_called()

    def test_advances_unclosed_requires_human_task(self):
        steps = [
            _step(tool_name="get_task", tool_output={"id": "t99", "requires_human": True}),
            # no update_task / resolve_task for t99
        ]
        with (
            patch(
                "robothor.crm.dal.get_task",
                return_value={"id": "t99", "status": "TODO", "body": "original body"},
            ) as gt,
            patch("robothor.crm.dal.update_task", return_value=True) as ut,
        ):
            out = check_post_run(_run(steps, run_id="r-5"), _Cfg(["requires_human_task_closure"]))

        assert out == ["t99"]
        gt.assert_called_once()
        ut.assert_called_once()
        kwargs = ut.call_args.kwargs
        assert kwargs["status"] == "IN_PROGRESS"
        assert "auto-advanced by run r-5" in kwargs["body"]

    def test_skips_when_task_already_past_todo(self):
        steps = [
            _step(tool_name="get_task", tool_output={"id": "t1", "requires_human": True}),
        ]
        with (
            patch(
                "robothor.crm.dal.get_task",
                return_value={"id": "t1", "status": "DONE", "body": "x"},
            ),
            patch("robothor.crm.dal.update_task", return_value=True) as ut,
        ):
            out = check_post_run(_run(steps), _Cfg(["requires_human_task_closure"]))
        assert out == []
        ut.assert_not_called()

    def test_uses_run_task_id_as_driving_task(self):
        # No get_task step, but run.task_id is set — still treat as driven.
        steps = []
        with (
            patch(
                "robothor.crm.dal.get_task",
                return_value={"id": "t-root", "status": "TODO", "body": "b"},
            ),
            patch("robothor.crm.dal.update_task", return_value=True) as ut,
        ):
            out = check_post_run(
                _run(steps, task_id="t-root"), _Cfg(["requires_human_task_closure"])
            )
        assert out == ["t-root"]
        ut.assert_called_once()

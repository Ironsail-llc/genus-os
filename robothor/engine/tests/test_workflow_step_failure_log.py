"""A workflow step that fails logs its error text.

The step's row in ``workflow_run_steps`` always carried the error, but nothing
reached the log, so a failing workflow was invisible to anyone reading logs.
The line is written whether or not the row could be stored.
"""

from __future__ import annotations

import logging

from robothor.engine.models import (
    WorkflowRun,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowStepType,
)
from robothor.engine.workflow import WorkflowEngine


def _persist(monkeypatch, result: WorkflowStepResult) -> None:
    def _no_db(*_a, **_k):
        raise RuntimeError("no database in this test")

    monkeypatch.setattr("robothor.db.connection.get_connection", _no_db)
    engine = WorkflowEngine.__new__(WorkflowEngine)
    run = WorkflowRun(id="wr-1", workflow_id="example-flow")
    engine._persist_step(run, result)


def _step_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "event=workflow_step_failed" in r.getMessage()]


def test_a_failed_step_logs_its_error_text(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    _persist(
        monkeypatch,
        WorkflowStepResult(
            step_id="fetch",
            step_type=WorkflowStepType.TOOL,
            status=WorkflowStepStatus.FAILED,
            tool_name="example_tool",
            error_message="the remote service answered 503",
            duration_ms=42,
        ),
    )
    (line,) = _step_lines(caplog)
    assert line.levelno == logging.WARNING
    text = line.getMessage()
    for part in (
        "workflow=example-flow",
        "run_id=wr-1",
        "step=fetch",
        "tool=example_tool",
        "duration_ms=42",
        "answered 503",
    ):
        assert part in text


def test_the_error_text_is_redacted_and_bounded(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    _persist(
        monkeypatch,
        WorkflowStepResult(
            step_id="fetch",
            status=WorkflowStepStatus.FAILED,
            error_message="password=hunter2hunter2hunter2 " + "z" * 100_000,
        ),
    )
    (line,) = _step_lines(caplog)
    assert "hunter2hunter2hunter2" not in line.getMessage()
    assert len(line.getMessage()) < 600


def test_a_completed_step_logs_no_failure(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    _persist(
        monkeypatch,
        WorkflowStepResult(step_id="fetch", status=WorkflowStepStatus.COMPLETED),
    )
    assert not _step_lines(caplog)

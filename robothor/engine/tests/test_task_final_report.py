"""Final task receipts require explicit intent, scoped authority and stored proof."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robothor.engine.goal_report_delivery import (
    finish_goal_report,
    record_report_turn,
    report_scope,
)
from robothor.engine.runtime import ExecutionContext, effects, task_report
from robothor.engine.tests.test_goal_report_delivery import setup_turn
from robothor.goals.report_channel import publish_report


def prepare(args=None):
    session, req, ctx = setup_turn()
    session.run.task_text = 'Create one task titled "One" with description "Synthetic".'
    args = args if args is not None else {"title": "One", "body": "Synthetic", "finalReport": True}
    req.assistant_msg = SimpleNamespace(
        tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(args)))]
    )
    return session, req, ctx, args


def record():
    return {
        "state": "confirmed",
        "tool_name": "create_task",
        "resolution": {
            "result": {
                "id": "task-id",
                "title": "One",
                "body": "Synthetic",
                "status": "TODO",
                "verification": "verified",
                "verification_scope": "stored_task_snapshot",
            }
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing_flag",
        "string_flag",
        "additional_fields",
        "batch",
        "delegated",
        "readonly",
        "background",
        "benchmark",
        "todo",
        "resumed",
        "approved",
        "prior_tool",
    ],
)
async def test_no_report_or_audit_lookup_outside_admitted_single_task(monkeypatch, change):
    session, req, ctx, args = prepare()
    names = ["create_task"]
    if change == "missing_flag":
        args.pop("finalReport")
    elif change == "string_flag":
        args["finalReport"] = "true"
    elif change == "additional_fields":
        args["dueAt"] = "2026-10-01"
    elif change == "batch":
        names.append("list_tasks")
    elif change == "delegated":
        session.run.parent_run_id = "parent"
    elif change == "readonly":
        req.readonly_mode = True
    elif change == "background":
        session.run.trigger_type = "cron"
    elif change == "benchmark":
        session.run.is_benchmark = True
    elif change == "todo":
        session.todo_list = SimpleNamespace(items=[SimpleNamespace(status="pending")])
    elif change == "prior_tool":
        session.run.steps.append(SimpleNamespace(step_type="tool_call"))
    elif change == "resumed":
        session.run.resume_from_run_id = "prior"
    elif change == "approved":
        session.run.trigger_detail = "plan-exec:chat"
    req.assistant_msg.tool_calls[0].function.arguments = json.dumps(args)
    read = Mock(side_effect=AssertionError("No receipt read outside admitted report"))
    monkeypatch.setattr(effects, "read", read)
    with report_scope(req, names) as state:
        await task_report.publish(
            ExecutionContext("tenant", "operator", "request"), "effect", args, ctx
        )
    record_report_turn(state, session, [])
    assert not finish_goal_report(session)
    read.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        "unconfirmed",
        "wrong_tool",
        "unverified",
        "wrong_scope",
        "title",
        "body",
        "status",
        "missing",
        "unavailable",
    ],
)
async def test_unproven_receipt_cannot_finalize(monkeypatch, bad):
    session, req, ctx, args = prepare()
    saved = record()
    if bad == "unconfirmed":
        saved["state"] = "uncertain"
    elif bad == "wrong_tool":
        saved["tool_name"] = "create_note"
    elif bad == "unverified":
        saved["resolution"]["result"]["verification"] = "reported"
    elif bad == "wrong_scope":
        saved["resolution"]["result"]["verification_scope"] = "unknown"
    elif bad in {"title", "body", "status"}:
        saved["resolution"]["result"][bad] = "Different"
    elif bad == "missing":
        saved = None
    read = Mock(
        return_value=saved, side_effect=OSError("offline") if bad == "unavailable" else None
    )
    monkeypatch.setattr(effects, "read", read)
    with report_scope(req, ["create_task"]) as state:
        await task_report.publish(
            ExecutionContext("tenant", "operator", "request"), "effect", args, ctx
        )
    record_report_turn(state, session, [])
    assert not finish_goal_report(session)


@pytest.mark.parametrize("control", [None, "steer", "interrupt", "error"])
@pytest.mark.parametrize("replayed", [False, True])
async def test_verified_receipt_preserves_late_controls_and_origin(monkeypatch, control, replayed):
    session, req, ctx, args = prepare()
    monkeypatch.setattr(effects, "read", Mock(return_value=record()))
    with report_scope(req, ["create_task"]) as state:
        with pytest.raises(ValueError, match="unavailable"):
            publish_report(ctx, "A goal tool cannot use a task's capability")
        await task_report.publish(
            ExecutionContext("tenant", "operator", "request"),
            "effect",
            args,
            ctx,
            replayed=replayed,
        )
        if control == "steer":
            session.steer("Also do something else")
        if control == "interrupt":
            session.interrupt("Stop")
    record_report_turn(
        state, session, [("create_task", "error", None)] if control == "error" else []
    )
    assert finish_goal_report(session) is (control is None)
    if control is None:
        assert session.run.steps[-1].tool_name == "create_task"
        assert session.run.steps[-1].tool_output["origin"] == "trusted_task_report"
        assert "Status: TODO" in session.messages[-1]["content"]
        assert session.messages[-1]["content"].startswith(
            "Previously recorded task" if replayed else "Created task"
        )
        from robothor.engine.output_validation import output_validation_scope, validated_completion

        with output_validation_scope(lambda run, text: "Additional required outcome missing"):
            run = validated_completion(session, session.get_final_text())
        assert str(run.status) == "failed"
        assert "Additional required outcome missing" in run.error_message
    assert not finish_goal_report(session)


def test_report_option_does_not_change_action_identity():
    args = {"title": "One", "body": "Synthetic"}
    assert effects.fingerprint("create_task", args) == effects.fingerprint(
        "create_task", {**args, "finalReport": True}
    )
    assert effects.fingerprint("create_task", args) != effects.fingerprint(
        "create_task", {**args, "body": "Other"}
    )

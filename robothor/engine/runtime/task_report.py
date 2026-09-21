"""An explicitly requested final task receipt, published only from durable evidence."""

import asyncio
import json
import logging

from robothor.engine.runtime import effects
from robothor.goals.report_channel import publish_report, require_report_context

_FIELDS = {"title", "body", "status", "finalReport"}


def requested(req, names):
    run = req.session.run
    todos = getattr(req.session, "todo_list", None)
    if (
        names != ["create_task"]
        or any(str(step.step_type) == "tool_call" for step in run.steps)
        or getattr(run, "resume_from_run_id", None)
        or str(run.trigger_detail or "").startswith("plan")
        or todos is not None
        and any(item.status != "completed" for item in todos.items)
    ):
        return False
    try:
        args = json.loads(req.assistant_msg.tool_calls[0].function.arguments)
        from robothor.engine.runtime.task_intent import permits

        return (
            isinstance(args, dict)
            and args.get("finalReport") is True
            and set(args) <= _FIELDS
            and permits(req.session, args)
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


async def publish(context, record_id, args, ctx, *, replayed=False):
    if args.get("finalReport") is not True or not set(args) <= _FIELDS:
        return
    try:
        require_report_context(ctx, tool_name="create_task")
    except ValueError:
        return
    try:
        record = await asyncio.to_thread(effects.read, context, record_id)
        if not record or record["state"] != "confirmed" or record["tool_name"] != "create_task":
            return
        result = record["resolution"]["result"]
        if (
            result.get("verification") != "verified"
            or result.get("verification_scope") != "stored_task_snapshot"
            or result.get("title") != args.get("title")
            or (result.get("body") or "") != (args.get("body") or "")
            or result.get("status") != args.get("status", "TODO")
            or not result.get("id")
        ):
            return
        from robothor.secrets.redaction import redact

        prefix = (
            "Previously recorded task"
            if replayed
            else "Task already exists"
            if result.get("deduplicated")
            else "Created task"
        )
        message = (
            f"{prefix}: {json.dumps(result['title'], ensure_ascii=False)}.\n"
            f"Status: {result['status']}.\nTask ID: {result['id']}."
        )
        if result.get("body"):
            message += "\nDescription: " + result["body"]
        publish_report(ctx, redact(message), tool_name="create_task")
    except Exception as exc:
        # Failure to render/read a receipt cannot retry an already dispatched write.
        logging.getLogger(__name__).warning("Task final report unavailable: %s", type(exc).__name__)

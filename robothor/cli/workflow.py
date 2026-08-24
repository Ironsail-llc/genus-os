"""Workflow commands (list, run, pending, approve, reject)."""

from __future__ import annotations

import argparse  # noqa: TC003
from typing import Any


def cmd_engine_workflow(args: argparse.Namespace) -> int:
    """Manage workflows."""
    wf_sub = getattr(args, "workflow_command", None)

    if wf_sub == "list":
        return _cmd_workflow_list()
    if wf_sub == "run":
        return _cmd_workflow_run(args)
    if wf_sub == "pending":
        return _cmd_workflow_pending()
    if wf_sub in ("approve", "reject"):
        return _cmd_workflow_decide(args, wf_sub)
    print("Usage: robothor engine workflow {list|run|pending|approve|reject}")
    return 0


def _cmd_workflow_pending() -> int:
    """Show what is waiting on the operator, and for how much longer."""
    from datetime import UTC, datetime

    from robothor.engine.approvals import list_pending_approvals
    from robothor.engine.config import EngineConfig

    config = EngineConfig.from_env()
    pending = list_pending_approvals(tenant_id=config.tenant_id)
    if not pending:
        print("Nothing awaiting approval.")
        return 0

    now = datetime.now(UTC)
    for req in pending:
        hours_left = (req.expires_at - now).total_seconds() / 3600
        print(f"{req.workflow_id} / {req.step_id}")
        print(f"  {req.prompt}")
        print(f"  run {req.run_id}   expires in {hours_left:.1f}h")
        print(f"  robothor engine workflow approve {req.run_id} --step {req.step_id}")
        print()

    print(f"{len(pending)} awaiting approval")
    return 0


def _cmd_workflow_decide(args: argparse.Namespace, verb: str) -> int:
    """Approve or reject a waiting step, then resume the run immediately.

    Resuming here rather than leaving it to the watchdog is what makes the
    command feel like an action instead of a filing: the operator sees the
    workflow finish, not a promise that something will pick it up later.
    """
    import asyncio

    from robothor.engine.approvals import (
        ApprovalDecision,
        decide_approval,
        list_pending_approvals,
    )
    from robothor.engine.config import EngineConfig
    from robothor.engine.workflow import WorkflowEngine

    config = EngineConfig.from_env()
    run_id = args.run_id
    step_id = args.step

    if not step_id:
        waiting = [
            r for r in list_pending_approvals(tenant_id=config.tenant_id) if r.run_id == run_id
        ]
        if not waiting:
            print(f"Error: run {run_id} has nothing awaiting approval")
            return 1
        if len(waiting) > 1:
            print(f"Error: {len(waiting)} steps are waiting on run {run_id} — pass --step:")
            for r in waiting:
                print(f"  --step {r.step_id}   {r.prompt}")
            return 1
        step_id = waiting[0].step_id

    decision = ApprovalDecision.APPROVED if verb == "approve" else ApprovalDecision.REJECTED
    settled = decide_approval(
        run_id,
        step_id,
        decision,
        decided_by=_operator_identity(),
        note=args.note,
        tenant_id=config.tenant_id,
    )
    if not settled:
        # Not an error: someone (or the timeout) got there first. Saying so
        # plainly beats a stack trace or a silent success.
        from robothor.engine.approvals import get_approval

        existing = get_approval(run_id, step_id, tenant_id=config.tenant_id)
        state = existing.status if existing else "unknown"
        print(f"Already settled ({state}) — nothing changed.")
        return 1

    print(f"{verb.capitalize()}d {run_id} / {step_id}. Resuming...")

    from robothor.engine.runner import AgentRunner

    engine = WorkflowEngine(config, AgentRunner(config))
    engine.load_workflows(config.workflow_dir)
    run = asyncio.run(engine.resume_run(run_id))
    if run is None:
        print("Run was not resumable here — the engine daemon will pick it up.")
        return 0

    print(f"Status: {run.status.value}")
    if run.error_message:
        print(f"  {run.error_message}")
    return 0


def _operator_identity() -> str:
    """Who to record as the decider.

    A CLI decision is the operator at the keyboard; the CRM row for them is
    the durable identity, and the OS username is the fallback that works on a
    box with no owner config.
    """
    import getpass

    try:
        from robothor.owner_config import load_owner_config

        cfg = load_owner_config()
        name = getattr(cfg, "name", "") or ""
        if name:
            return f"operator:{name}"
    except Exception:
        pass
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli"


def _cmd_workflow_list() -> int:
    """List loaded workflow definitions."""
    from robothor.engine.config import EngineConfig
    from robothor.engine.workflow import WorkflowEngine

    config = EngineConfig.from_env()

    # We don't need a full runner just to list workflows
    engine = WorkflowEngine(config, None)  # type: ignore[arg-type]
    engine.load_workflows(config.workflow_dir)

    workflows = engine.list_workflows()
    if not workflows:
        print(f"No workflows found in {config.workflow_dir}")
        return 1

    print(f"{'Workflow ID':<25} {'Name':<30} {'Steps':<8} {'Triggers'}")
    print("-" * 90)
    for wf in workflows:
        trigger_strs = []
        for t in wf.triggers:
            if t.type == "hook":
                trigger_strs.append(f"hook:{t.stream}.{t.event_type}")
            elif t.type == "cron":
                trigger_strs.append(f"cron:{t.cron}")
        print(f"{wf.id:<25} {wf.name:<30} {len(wf.steps):<8} {', '.join(trigger_strs)}")

    print(f"\n{len(workflows)} workflows configured")
    return 0


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    """Run a workflow by ID."""
    import asyncio

    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.engine.workflow import WorkflowEngine

    config = EngineConfig.from_env()
    runner = AgentRunner(config)
    engine = WorkflowEngine(config, runner)
    engine.load_workflows(config.workflow_dir)

    workflow_id = args.workflow_id
    wf = engine.get_workflow(workflow_id)
    if not wf:
        print(f"Error: Workflow '{workflow_id}' not found in {config.workflow_dir}")
        return 1

    print(f"Running workflow: {wf.name} ({wf.id})")
    print(f"Steps: {len(wf.steps)}")
    print()

    async def _run() -> Any:
        return await engine.execute(
            workflow_id=workflow_id,
            trigger_type="manual",
            trigger_detail="cli",
        )

    run = asyncio.run(_run())

    print(f"Status: {run.status.value}")
    print(f"Duration: {run.duration_ms}ms")
    print(f"Steps executed: {len(run.step_results)}")
    print()

    for result in run.step_results:
        icon = {
            "completed": "+",
            "failed": "!",
            "skipped": "~",
        }.get(result.status.value, "?")
        line = f"  [{icon}] {result.step_id} ({result.step_type.value}): {result.status.value}"
        if result.duration_ms:
            line += f" ({result.duration_ms}ms)"
        if result.condition_branch:
            line += f" -> {result.condition_branch}"
        if result.error_message:
            line += f" ERROR: {result.error_message}"
        print(line)

    if run.error_message:
        print(f"\nError: {run.error_message}")

    return 0 if run.status.value == "completed" else 1

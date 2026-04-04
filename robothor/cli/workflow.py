"""Workflow commands (list, run)."""

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
    print("Usage: robothor engine workflow {list|run}")
    return 0


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

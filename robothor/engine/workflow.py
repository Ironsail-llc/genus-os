"""
Declarative Workflow Engine — multi-step agent pipelines with conditional routing.

Workflows are defined in YAML files (docs/workflows/*.yaml) alongside agent
manifests. The engine loads them at startup and executes them when triggered
by hooks, cron, or manual invocation.

Step types:
  - agent:     Run an existing agent via runner.execute()
  - tool:      Call a tool directly (skip LLM)
  - condition: Branch based on previous step output
  - transform: Reshape data between steps
  - noop:      Explicit pipeline end marker

Usage:
    engine = WorkflowEngine(config, runner)
    engine.load_workflows(Path("docs/workflows"))
    run = await engine.execute("email-pipeline", "hook", "email:email.new")
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from robothor.engine.models import (
    ConditionBranch,
    RunStatus,
    TriggerType,
    WorkflowDef,
    WorkflowRun,
    WorkflowStepDef,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowStepType,
    WorkflowTriggerDef,
)

logger = logging.getLogger(__name__)

# Template pattern: {{ expr }}
_TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _render_template(template: str, context: dict[str, Any]) -> str:
    """Render {{ expr }} templates against context dict.

    Safe because workflow YAMLs are checked into git (same trust as agent manifests).
    """

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        try:
            result = eval(expr, {"__builtins__": {}}, context)
            return str(result) if result is not None else ""
        except Exception as e:
            logger.warning("Template eval failed for '%s': %s", expr, e)
            return str(match.group(0))

    return str(_TEMPLATE_RE.sub(_replace, template))


def _eval_condition(expression: str, value: Any) -> bool:
    """Evaluate a condition expression with 'value' as the input variable."""
    try:
        return bool(eval(expression, {"__builtins__": {}}, {"value": value}))
    except Exception as e:
        logger.warning("Condition eval failed for '%s': %s", expression, e)
        return False


def parse_workflow(data: dict) -> WorkflowDef:
    """Parse a workflow definition from a YAML dict."""
    triggers = []
    for t in data.get("triggers", []):
        triggers.append(
            WorkflowTriggerDef(
                type=t.get("type", ""),
                stream=t.get("stream", ""),
                event_type=t.get("event_type", ""),
                cron=t.get("cron", ""),
                timezone=t.get("timezone", "America/Grenada"),
            )
        )

    steps = []
    for s in data.get("steps", []):
        step_type = WorkflowStepType(s.get("type", "noop"))

        branches = []
        for b in s.get("branches", []):
            branches.append(
                ConditionBranch(
                    when=b.get("when"),
                    otherwise=b.get("otherwise", False),
                    goto=b.get("goto", ""),
                )
            )

        steps.append(
            WorkflowStepDef(
                id=s["id"],
                type=step_type,
                agent_id=s.get("agent_id", ""),
                message=s.get("message", ""),
                tool_name=s.get("tool_name", ""),
                tool_args=s.get("tool_args", {}),
                input_expr=s.get("input", ""),
                branches=branches,
                transform_expr=s.get("expression", ""),
                on_failure=s.get("on_failure", "abort"),
                retry_count=s.get("retry_count", 0),
                next=s.get("next", ""),
            )
        )

    delivery = data.get("delivery", {})

    return WorkflowDef(
        id=data["id"],
        name=data.get("name", data["id"]),
        description=data.get("description", ""),
        version=data.get("version", ""),
        triggers=triggers,
        steps=steps,
        timeout_seconds=data.get("timeout_seconds", 900),
        delivery_mode=delivery.get("mode", "none"),
        delivery_channel=delivery.get("channel", ""),
        delivery_to=delivery.get("to", ""),
    )


class WorkflowEngine:
    """Executes declarative multi-step workflows."""

    def __init__(self, config, runner) -> None:
        from robothor.engine.config import EngineConfig
        from robothor.engine.runner import AgentRunner

        self.config: EngineConfig = config
        self.runner: AgentRunner = runner
        self._workflows: dict[str, WorkflowDef] = {}

    def load_workflows(self, workflow_dir: Path) -> int:
        """Load all workflow YAML files from a directory."""
        if not workflow_dir.is_dir():
            logger.warning("Workflow directory not found: %s", workflow_dir)
            return 0

        loaded = 0
        for f in sorted(workflow_dir.glob("*.yaml")):
            try:
                with open(f) as fh:
                    data = yaml.safe_load(fh)
                if data and isinstance(data, dict) and "id" in data:
                    wf = parse_workflow(data)
                    self._workflows[wf.id] = wf
                    loaded += 1
                    logger.info("Loaded workflow: %s (%d steps)", wf.id, len(wf.steps))
            except Exception as e:
                logger.error("Failed to load workflow %s: %s", f, e)

        logger.info("Loaded %d workflows from %s", loaded, workflow_dir)
        return loaded

    def get_workflow(self, workflow_id: str) -> WorkflowDef | None:
        """Get a workflow definition by ID."""
        return self._workflows.get(workflow_id)

    def list_workflows(self) -> list[WorkflowDef]:
        """List all loaded workflow definitions."""
        return list(self._workflows.values())

    def get_workflows_for_event(self, stream: str, event_type: str) -> list[WorkflowDef]:
        """Find workflows triggered by a specific event."""
        matches = []
        for wf in self._workflows.values():
            for trigger in wf.triggers:
                if (
                    trigger.type == "hook"
                    and trigger.stream == stream
                    and trigger.event_type == event_type
                ):
                    matches.append(wf)
                    break
        return matches

    def get_workflows_for_cron(self) -> list[tuple[WorkflowDef, WorkflowTriggerDef]]:
        """Find workflows with cron triggers, returning (workflow, trigger) pairs."""
        results = []
        for wf in self._workflows.values():
            for trigger in wf.triggers:
                if trigger.type == "cron" and trigger.cron:
                    results.append((wf, trigger))
        return results

    async def execute(
        self,
        workflow_id: str,
        trigger_type: str = "manual",
        trigger_detail: str = "",
        initial_context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> WorkflowRun:
        """Execute a workflow by ID."""
        wf = self._workflows.get(workflow_id)
        if not wf:
            run = WorkflowRun(
                workflow_id=workflow_id,
                status=RunStatus.FAILED,
                error_message=f"Workflow not found: {workflow_id}",
            )
            return run

        # Initialize run
        run = WorkflowRun(
            workflow_id=workflow_id,
            tenant_id=self.config.tenant_id,
            trigger_type=trigger_type,
            trigger_detail=trigger_detail,
            correlation_id=correlation_id,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
            context={"steps": {}, **(initial_context or {})},
        )

        # Record run in DB
        self._persist_run_start(run, wf)

        logger.info(
            "Workflow started: %s (trigger=%s, run=%s)",
            workflow_id,
            trigger_type,
            run.id,
        )

        try:
            async with asyncio.timeout(wf.timeout_seconds):
                await self._execute_steps(run, wf)
        except TimeoutError:
            run.status = RunStatus.TIMEOUT
            run.error_message = f"Timed out after {wf.timeout_seconds}s"
            logger.warning("Workflow %s timed out", workflow_id)
        except Exception as e:
            run.status = RunStatus.FAILED
            run.error_message = str(e)
            logger.error("Workflow %s failed: %s", workflow_id, e, exc_info=True)

        # Finalize
        run.completed_at = datetime.now(UTC)
        if run.started_at:
            run.duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)

        # Set final status if not already failed/timed out
        if run.status == RunStatus.RUNNING:
            failed = sum(1 for r in run.step_results if r.status == WorkflowStepStatus.FAILED)
            run.status = RunStatus.FAILED if failed > 0 else RunStatus.COMPLETED

        self._persist_run_end(run)

        logger.info(
            "Workflow complete: %s status=%s duration=%dms steps=%d",
            workflow_id,
            run.status.value,
            run.duration_ms,
            len(run.step_results),
        )

        return run

    async def _execute_steps(self, run: WorkflowRun, wf: WorkflowDef) -> None:
        """Execute workflow steps sequentially with flow control."""
        # Build step index for lookups
        step_index = {s.id: i for i, s in enumerate(wf.steps)}
        current_idx = 0

        while current_idx < len(wf.steps):
            step = wf.steps[current_idx]

            result = await self._execute_step(step, run, wf)
            run.step_results.append(result)

            # Store result in context for template rendering
            run.context["steps"][step.id] = {
                "status": result.status.value,
                "output_text": result.output_text or "",
                "tool_output": result.tool_output,
                "condition_branch": result.condition_branch,
                "agent_run_id": result.agent_run_id,
            }

            # Persist step result
            self._persist_step(run, result)

            # Handle failure
            if result.status == WorkflowStepStatus.FAILED:
                if step.on_failure == "abort":
                    run.error_message = f"Step '{step.id}' failed: {result.error_message}"
                    run.status = RunStatus.FAILED
                    return
                elif step.on_failure == "skip":
                    result.status = WorkflowStepStatus.SKIPPED
                    # Continue to next step

            # Determine next step
            if result.condition_branch and result.condition_branch in step_index:
                # Condition branch — jump to target
                current_idx = step_index[result.condition_branch]
            elif step.next and step.next in step_index:
                # Explicit next step
                current_idx = step_index[step.next]
            else:
                # Sequential
                current_idx += 1

    async def _execute_step(
        self, step: WorkflowStepDef, run: WorkflowRun, wf: WorkflowDef
    ) -> WorkflowStepResult:
        """Execute a single workflow step."""
        result = WorkflowStepResult(
            step_id=step.id,
            step_type=step.type,
            status=WorkflowStepStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        start = time.monotonic()

        try:
            if step.type == WorkflowStepType.AGENT:
                await self._run_agent_step(step, run, result)
            elif step.type == WorkflowStepType.TOOL:
                await self._run_tool_step(step, run, result)
            elif step.type == WorkflowStepType.CONDITION:
                self._run_condition_step(step, run, result)
            elif step.type == WorkflowStepType.TRANSFORM:
                self._run_transform_step(step, run, result)
            elif step.type == WorkflowStepType.NOOP:
                result.status = WorkflowStepStatus.COMPLETED
        except Exception as e:
            result.status = WorkflowStepStatus.FAILED
            result.error_message = str(e)
            logger.error("Step %s failed: %s", step.id, e, exc_info=True)

        result.duration_ms = int((time.monotonic() - start) * 1000)
        result.completed_at = datetime.now(UTC)

        logger.info(
            "Step %s: %s status=%s duration=%dms",
            step.id,
            step.type.value,
            result.status.value,
            result.duration_ms,
        )

        return result

    async def _run_agent_step(
        self, step: WorkflowStepDef, run: WorkflowRun, result: WorkflowStepResult
    ) -> None:
        """Execute an agent step via runner.execute()."""
        from robothor.engine.config import load_agent_config
        from robothor.engine.delivery import deliver
        from robothor.engine.warmup import build_warmth_preamble

        agent_config = load_agent_config(step.agent_id, self.config.manifest_dir)
        if not agent_config:
            result.status = WorkflowStepStatus.FAILED
            result.error_message = f"Agent config not found: {step.agent_id}"
            return

        # Render message template
        message = _render_template(step.message, run.context)

        # Add warmth preamble
        try:
            preamble = build_warmth_preamble(
                agent_config, self.config.workspace, self.config.tenant_id
            )
            if preamble:
                message = f"{preamble}\n\n{message}"
        except Exception as e:
            logger.debug("Warmup failed for workflow step %s: %s", step.id, e)

        agent_run = await self.runner.execute(
            agent_id=step.agent_id,
            message=message,
            trigger_type=TriggerType.WORKFLOW,
            trigger_detail=f"workflow:{run.workflow_id}:{step.id}",
            correlation_id=run.correlation_id or run.id,
            agent_config=agent_config,
        )

        # Deliver agent output
        await deliver(agent_config, agent_run)

        result.agent_run_id = agent_run.id
        result.output_text = agent_run.output_text

        if agent_run.status.value in ("completed",):
            result.status = WorkflowStepStatus.COMPLETED
        else:
            result.status = WorkflowStepStatus.FAILED
            result.error_message = agent_run.error_message or agent_run.status.value

    async def _run_tool_step(
        self, step: WorkflowStepDef, run: WorkflowRun, result: WorkflowStepResult
    ) -> None:
        """Execute a tool step directly via registry."""
        # Render template args
        rendered_args = {}
        for k, v in step.tool_args.items():
            if isinstance(v, str):
                rendered_args[k] = _render_template(v, run.context)
            else:
                rendered_args[k] = v

        tool_result = await self.runner.registry.execute(
            step.tool_name,
            rendered_args,
            agent_id=f"workflow:{run.workflow_id}",
            tenant_id=run.tenant_id,
            workspace=str(self.config.workspace),
        )

        result.tool_output = tool_result
        result.output_text = str(tool_result)

        if isinstance(tool_result, dict) and tool_result.get("error"):
            result.status = WorkflowStepStatus.FAILED
            result.error_message = str(tool_result["error"])
        else:
            result.status = WorkflowStepStatus.COMPLETED

    def _run_condition_step(
        self, step: WorkflowStepDef, run: WorkflowRun, result: WorkflowStepResult
    ) -> None:
        """Evaluate condition branches."""
        # Render input expression
        input_val = _render_template(step.input_expr, run.context)

        for branch in step.branches:
            if branch.otherwise:
                result.condition_branch = branch.goto
                result.status = WorkflowStepStatus.COMPLETED
                return
            if branch.when and _eval_condition(branch.when, input_val):
                result.condition_branch = branch.goto
                result.status = WorkflowStepStatus.COMPLETED
                return

        # No branch matched — continue sequential
        result.status = WorkflowStepStatus.COMPLETED

    def _run_transform_step(
        self, step: WorkflowStepDef, run: WorkflowRun, result: WorkflowStepResult
    ) -> None:
        """Evaluate transform expression and store result."""
        rendered = _render_template(step.transform_expr, run.context)
        result.output_text = rendered
        result.status = WorkflowStepStatus.COMPLETED

    # ── Persistence ────────────────────────────────────────────────────

    def _persist_run_start(self, run: WorkflowRun, wf: WorkflowDef) -> None:
        """Record workflow run start in DB."""
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO workflow_runs
                       (id, tenant_id, workflow_id, trigger_type, trigger_detail,
                        correlation_id, status, steps_total, started_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run.id,
                        run.tenant_id,
                        run.workflow_id,
                        run.trigger_type,
                        run.trigger_detail,
                        run.correlation_id,
                        run.status.value,
                        len(wf.steps),
                        run.started_at,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to persist workflow run start: %s", e)

    def _persist_run_end(self, run: WorkflowRun) -> None:
        """Update workflow run with final status."""
        try:
            import json

            from robothor.db.connection import get_connection

            completed = sum(1 for r in run.step_results if r.status == WorkflowStepStatus.COMPLETED)
            failed = sum(1 for r in run.step_results if r.status == WorkflowStepStatus.FAILED)
            skipped = sum(1 for r in run.step_results if r.status == WorkflowStepStatus.SKIPPED)

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """UPDATE workflow_runs
                       SET status = %s, completed_at = %s, duration_ms = %s,
                           steps_completed = %s, steps_failed = %s, steps_skipped = %s,
                           error_message = %s,
                           context = %s
                       WHERE id = %s""",
                    (
                        run.status.value,
                        run.completed_at,
                        run.duration_ms,
                        completed,
                        failed,
                        skipped,
                        run.error_message,
                        json.dumps(run.context, default=str),
                        run.id,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to persist workflow run end: %s", e)

    def _persist_step(self, run: WorkflowRun, result: WorkflowStepResult) -> None:
        """Record a workflow step result in DB."""
        try:
            import json

            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO workflow_run_steps
                       (run_id, step_id, step_type, status,
                        agent_id, agent_run_id, tool_name,
                        tool_input, tool_output,
                        condition_branch, output_text,
                        error_message, duration_ms,
                        started_at, completed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run.id,
                        result.step_id,
                        result.step_type.value,
                        result.status.value,
                        None,
                        result.agent_run_id,
                        None,
                        None,
                        json.dumps(result.tool_output, default=str) if result.tool_output else None,
                        result.condition_branch,
                        result.output_text[:2000] if result.output_text else None,
                        result.error_message,
                        result.duration_ms,
                        result.started_at,
                        result.completed_at,
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to persist workflow step: %s", e)

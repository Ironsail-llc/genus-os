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

import ast as _ast
import asyncio
import logging
import operator as _op
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

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

if TYPE_CHECKING:
    from pathlib import Path

    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

from robothor.engine.sanitize import sanitize_log as _sanitize  # noqa: E402

logger = logging.getLogger(__name__)


# Safe expression evaluator for workflow templates/conditions. Replaces raw
# eval(): even with __builtins__={} the old approach allowed attribute chains
# like value.__class__.__bases__[0].__subclasses__() to escape. This whitelists
# AST node types, blocks dunder attribute access, and permits only a fixed set
# of helper functions.
_SAFE_BINOPS: dict[type, Any] = {
    _ast.Add: _op.add,
    _ast.Sub: _op.sub,
    _ast.Mult: _op.mul,
    _ast.Div: _op.truediv,
    _ast.FloorDiv: _op.floordiv,
    _ast.Mod: _op.mod,
    _ast.Pow: _op.pow,
}
_SAFE_CMP: dict[type, Any] = {
    _ast.Eq: _op.eq,
    _ast.NotEq: _op.ne,
    _ast.Lt: _op.lt,
    _ast.LtE: _op.le,
    _ast.Gt: _op.gt,
    _ast.GtE: _op.ge,
    _ast.In: lambda a, b: a in b,
    _ast.NotIn: lambda a, b: a not in b,
    _ast.Is: _op.is_,
    _ast.IsNot: _op.is_not,
}
_SAFE_FUNCS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sum": sum,
}


def _safe_eval(expr: str, context: dict[str, Any]) -> Any:
    """Evaluate a restricted Python expression against ``context`` (no eval())."""
    return _eval_node(_ast.parse(expr, mode="eval").body, context)


def _eval_node(node: Any, ctx: dict[str, Any]) -> Any:  # noqa: PLR0911
    if isinstance(node, _ast.Constant):
        return node.value
    if isinstance(node, _ast.Name):
        if node.id in ctx:
            return ctx[node.id]
        if node.id in _SAFE_FUNCS:
            return _SAFE_FUNCS[node.id]
        raise ValueError(f"unknown name: {node.id}")
    if isinstance(node, _ast.Attribute):
        if node.attr.startswith("_"):
            raise ValueError("private attribute access blocked")
        obj = _eval_node(node.value, ctx)
        return obj.get(node.attr) if isinstance(obj, dict) else getattr(obj, node.attr)
    if isinstance(node, _ast.Subscript):
        return _eval_node(node.value, ctx)[_eval_node(node.slice, ctx)]
    if isinstance(node, _ast.BinOp) and type(node.op) in _SAFE_BINOPS:
        return _SAFE_BINOPS[type(node.op)](_eval_node(node.left, ctx), _eval_node(node.right, ctx))
    if isinstance(node, _ast.UnaryOp):
        val = _eval_node(node.operand, ctx)
        if isinstance(node.op, _ast.Not):
            return not val
        if isinstance(node.op, _ast.USub):
            return -val
        if isinstance(node.op, _ast.UAdd):
            return +val
        raise ValueError("unary op not allowed")
    if isinstance(node, _ast.BoolOp):
        result: Any = isinstance(node.op, _ast.And)
        for v in node.values:
            result = _eval_node(v, ctx)
            if isinstance(node.op, _ast.And) and not result:
                return result
            if isinstance(node.op, _ast.Or) and result:
                return result
        return result
    if isinstance(node, _ast.Compare):
        left = _eval_node(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators, strict=False):
            right = _eval_node(comp, ctx)
            fn = _SAFE_CMP.get(type(op))
            if fn is None:
                raise ValueError("comparison not allowed")
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, _ast.Call):
        fn = _eval_node(node.func, ctx)
        if fn not in _SAFE_FUNCS.values():
            raise ValueError("only whitelisted functions may be called")
        return fn(*[_eval_node(a, ctx) for a in node.args])
    if isinstance(node, _ast.List):
        return [_eval_node(e, ctx) for e in node.elts]
    if isinstance(node, _ast.Tuple):
        return tuple(_eval_node(e, ctx) for e in node.elts)
    raise ValueError(f"unsupported expression: {type(node).__name__}")


# Template pattern: {{ expr }}
_TEMPLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")

# Backoff schedule (seconds) between retries of a failed step when the step
# declares retry_count > 0. First retry waits 60s, every later retry 300s.
# All retries run inside the workflow's asyncio.timeout, so the workflow
# deadline still bounds the total budget.
_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300)


def _retry_delay(attempt: int) -> int:
    """Backoff before retry ``attempt`` (0-based): 60s, then 300s thereafter."""
    if not _RETRY_BACKOFF_SECONDS:
        return 0
    return _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]


def _render_template(template: str, context: dict[str, Any]) -> str:
    """Render {{ expr }} templates against context dict.

    Safe because workflow YAMLs are checked into git (same trust as agent manifests).
    """

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        try:
            result = _safe_eval(expr, context)
            return str(result) if result is not None else ""
        except Exception as e:
            logger.warning("Template eval failed for '%s': %s", expr, e)
            return str(match.group(0))

    return str(_TEMPLATE_RE.sub(_replace, template))


def _eval_condition(expression: str, value: Any) -> bool:
    """Evaluate a condition expression with 'value' as the input variable."""
    try:
        return bool(_safe_eval(expression, {"value": value}))
    except Exception as e:
        logger.warning("Condition eval failed for '%s': %s", expression, e)
        return False


def parse_workflow(data: dict[str, Any]) -> WorkflowDef:
    """Parse a workflow definition from a YAML dict."""
    triggers = [
        WorkflowTriggerDef(
            type=t.get("type", ""),
            stream=t.get("stream", ""),
            event_type=t.get("event_type", ""),
            cron=t.get("cron", ""),
            timezone=t.get("timezone", "America/New_York"),
        )
        for t in data.get("triggers", [])
    ]

    def _parse_step(s: dict[str, Any], *, inside_parallel: bool = False) -> WorkflowStepDef:
        step_type = WorkflowStepType(s.get("type", "noop"))

        if inside_parallel and step_type == WorkflowStepType.PARALLEL:
            raise ValueError(f"step {s.get('id')!r}: nested parallel steps are not supported")
        if inside_parallel and step_type == WorkflowStepType.CONDITION:
            # A condition's goto has no meaning inside a concurrent branch
            # set — flow control stays at the top level.
            raise ValueError(f"step {s.get('id')!r}: condition steps cannot run inside parallel")

        branches = [
            ConditionBranch(
                when=b.get("when"),
                otherwise=b.get("otherwise", False),
                goto=b.get("goto", ""),
            )
            for b in s.get("branches", [])
        ]
        parallel_steps = [
            _parse_step(sub, inside_parallel=True) for sub in s.get("parallel_steps", [])
        ]

        return WorkflowStepDef(
            id=s["id"],
            type=step_type,
            agent_id=s.get("agent_id", ""),
            message=s.get("message", ""),
            tool_name=s.get("tool_name", ""),
            tool_args=s.get("tool_args", {}),
            input_expr=s.get("input", ""),
            branches=branches,
            transform_expr=s.get("expression", ""),
            parallel_steps=parallel_steps,
            max_concurrent=s.get("max_concurrent", 0),
            on_failure=s.get("on_failure", "abort"),
            retry_count=s.get("retry_count", 0),
            next=s.get("next", ""),
        )

    steps = [_parse_step(s) for s in data.get("steps", [])]

    # Branch results land in run.context["steps"] beside top-level results, so
    # every step id — at either level — must be unique.
    seen_ids: set[str] = set()
    for step in steps:
        for sid in (step.id, *(b.id for b in step.parallel_steps)):
            if sid in seen_ids:
                raise ValueError(f"duplicate step id {sid!r} in workflow {data.get('id')!r}")
            seen_ids.add(sid)

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

    def __init__(self, config: EngineConfig, runner: AgentRunner) -> None:

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
                with f.open() as fh:
                    data = yaml.safe_load(fh)
                if data and isinstance(data, dict) and "id" in data:
                    wf = parse_workflow(data)
                    self._workflows[wf.id] = wf
                    loaded += 1
                    logger.info("Loaded workflow: %s (%d steps)", wf.id, len(wf.steps))
                    self._warn_dangling_agent_refs(wf)
            except Exception as e:
                logger.error("Failed to load workflow %s: %s", f, e)

        logger.info("Loaded %d workflows from %s", loaded, workflow_dir)
        return loaded

    def _warn_dangling_agent_refs(self, wf: WorkflowDef) -> None:
        """Warn when an agent step references an agent_id with no config.

        A dangling reference (agent retired, manifest renamed) otherwise only
        surfaces as a run failure the next time the workflow fires — the
        monthly-goal-review workflow failed silently every month for four
        months this way. Same channel as manifest checks ('Config validation
        [...]') so it lands in the startup log and validation surfaces.
        Warn-only: the workflow still loads.
        """
        from robothor.engine.config import load_agent_config

        for step in wf.steps:
            if step.type != WorkflowStepType.AGENT or not step.agent_id:
                continue
            try:
                found = load_agent_config(step.agent_id, self.config.manifest_dir) is not None
            except Exception as e:
                logger.debug("Agent-ref check failed for %s: %s", _sanitize(step.agent_id), e)
                continue
            if not found:
                logger.warning(
                    "Config validation [workflow:%s]: step '%s' references agent "
                    "'%s' with no registered agent config",
                    _sanitize(wf.id),
                    _sanitize(step.id),
                    _sanitize(step.agent_id),
                )

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
        return [
            (wf, trigger)
            for wf in self._workflows.values()
            for trigger in wf.triggers
            if trigger.type == "cron" and trigger.cron
        ]

    async def execute(
        self,
        workflow_id: str,
        trigger_type: str = "manual",
        trigger_detail: str = "",
        initial_context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        user_id: str = "",
        user_role: str = "",
    ) -> WorkflowRun:
        """Execute a workflow by ID under an explicit caller identity.

        Cron and hook executions are trusted Engine workloads and receive a
        narrow, auditable service identity.  Interactive/manual callers must
        propagate their verified user identity; a missing identity fails
        closed outside the explicit loopback development mode.
        """
        from robothor.engine.dedup import release, try_acquire

        wf = self._workflows.get(workflow_id)
        if not wf:
            run = WorkflowRun(
                workflow_id=workflow_id,
                user_id=user_id,
                user_role=user_role,
                status=RunStatus.FAILED,
                error_message=f"Workflow not found: {workflow_id}",
            )
            return run

        effective_user_id = user_id
        effective_user_role = user_role
        if trigger_type in {"cron", "hook"}:
            effective_user_id = effective_user_id or f"service:workflow:{workflow_id}"
            effective_user_role = effective_user_role or "service"
        elif not effective_user_id or not effective_user_role:
            import os

            from robothor.auth.runtime import auth_required

            bind_host = os.environ.get("ROBOTHOR_ENGINE_HOST", "127.0.0.1")
            if not auth_required(bind_host=bind_host):
                effective_user_id = effective_user_id or "loopback-development-operator"
                effective_user_role = effective_user_role or "owner"
            else:
                logger.warning(
                    "Rejected workflow without verified identity: workflow=%s trigger=%s",
                    _sanitize(workflow_id),
                    _sanitize(trigger_type),
                )
                return WorkflowRun(
                    workflow_id=workflow_id,
                    tenant_id=self.config.tenant_id,
                    trigger_type=trigger_type,
                    trigger_detail=trigger_detail,
                    user_id=effective_user_id,
                    user_role=effective_user_role,
                    status=RunStatus.FAILED,
                    error_message="Authentication identity required for workflow execution",
                )

        # Prevent concurrent runs of the same workflow
        dedup_key = f"workflow:{workflow_id}"
        if not await try_acquire(dedup_key):
            logger.info(
                "Workflow %s already running, skipping duplicate",
                _sanitize(workflow_id),
            )
            run = WorkflowRun(
                workflow_id=workflow_id,
                tenant_id=self.config.tenant_id,
                user_id=effective_user_id,
                user_role=effective_user_role,
                trigger_type=trigger_type,
                trigger_detail=trigger_detail,
                status=RunStatus.SKIPPED,
                error_message="Skipped: workflow already running",
            )
            return run

        # Initialize run
        run = WorkflowRun(
            workflow_id=workflow_id,
            tenant_id=self.config.tenant_id,
            user_id=effective_user_id,
            user_role=effective_user_role,
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
            _sanitize(workflow_id),
            _sanitize(trigger_type),
            run.id,
        )

        try:
            try:
                async with asyncio.timeout(wf.timeout_seconds):
                    await self._execute_steps(run, wf)
            except TimeoutError:
                run.status = RunStatus.TIMEOUT
                run.error_message = f"Timed out after {wf.timeout_seconds}s"
                logger.warning("Workflow %s timed out", _sanitize(workflow_id))
            except asyncio.CancelledError:
                # Engine shutdown/restart cancelled this task mid-run. Without
                # this finalizer the DB row stays 'running' forever (the 2026-08
                # diagnosis found 29 immortal orphans, oldest 171 days old).
                # Persist a terminal status, then re-raise so cancellation
                # propagates normally.
                run.status = RunStatus.CANCELLED
                run.error_message = "Cancelled: engine shutdown mid-run"
                run.completed_at = datetime.now(UTC)
                if run.started_at:
                    run.duration_ms = int(
                        (run.completed_at - run.started_at).total_seconds() * 1000
                    )
                self._persist_run_end(run)
                logger.warning(
                    "Workflow %s cancelled mid-run (engine shutdown), run=%s",
                    _sanitize(workflow_id),
                    run.id,
                )
                raise
            except Exception as e:
                run.status = RunStatus.FAILED
                run.error_message = str(e)
                logger.error(
                    "Workflow %s failed: %s",
                    _sanitize(workflow_id),
                    _sanitize(e),
                    exc_info=True,
                )

            # Finalize
            run.completed_at = datetime.now(UTC)
            if run.started_at:
                run.duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)

            # Set final status if not already failed/timed out
            if run.status == RunStatus.RUNNING:
                failed = sum(1 for r in run.step_results if r.status == WorkflowStepStatus.FAILED)
                run.status = RunStatus.FAILED if failed > 0 else RunStatus.COMPLETED

            # A FAILED run that consumed the whole workflow budget was really
            # killed by the deadline: asyncio.timeout cancels the step, but the
            # runner swallows the CancelledError and reports a step *failure*
            # ('Run cancelled externally'), so the TimeoutError branch above is
            # unreachable for agent steps. Reclassify honestly.
            if run.status == RunStatus.FAILED and run.duration_ms >= wf.timeout_seconds * 1000:
                last = run.step_results[-1] if run.step_results else None
                last_step = last.step_id if last else "unknown"
                last_error = (last.error_message if last else None) or run.error_message or ""
                run.status = RunStatus.TIMEOUT
                run.error_message = (
                    f"Timed out after {wf.timeout_seconds}s "
                    f"(step '{last_step}' cancelled: {last_error})"
                )
                logger.warning(
                    "Workflow %s reclassified failed→timeout (budget %ds exhausted)",
                    _sanitize(workflow_id),
                    wf.timeout_seconds,
                )

            self._persist_run_end(run)

            logger.info(
                "Workflow complete: %s status=%s duration=%dms steps=%d",
                _sanitize(workflow_id),
                run.status.value,
                run.duration_ms,
                len(run.step_results),
            )

            # Cron pipelines run unattended — a terminal failure must page the
            # operator and leave a durable notification, not just a log line.
            if run.trigger_type == "cron" and run.status in (
                RunStatus.FAILED,
                RunStatus.TIMEOUT,
            ):
                self._notify_run_failure(run)

            return run
        finally:
            await release(dedup_key)

    def _notify_run_failure(self, run: WorkflowRun) -> None:
        """Page the operator about a failed/timed-out cron workflow run.

        Two independent, best-effort channels (2026-08-20: a total
        email-pipeline failure produced zero pages and zero notifications):

        1. ``robothor.engine.alerts.alert('critical', ...)`` spawned via the
           task registry — the immediate Telegram page.
        2. A ``crm_agent_notifications`` row so the failure lands in briefings
           deterministically even if Telegram is down.
        """
        error = run.error_message or f"status={run.status.value}"

        try:
            from robothor.engine.alerts import alert
            from robothor.engine.task_registry import get_task_registry

            get_task_registry().spawn(
                alert("critical", f"Workflow failed: {run.workflow_id}", error),
                name=f"workflow-failure-alert:{run.workflow_id}",
            )
        except Exception as e:
            logger.error(
                "Failed to spawn workflow failure alert for %s: %s",
                _sanitize(run.workflow_id),
                e,
            )

        try:
            from robothor.crm import dal

            # 'workflow_failure' is allowed by the crm_agent_notifications
            # CHECK constraint since migration 099; the drift test in
            # test_schema_drift.py keeps the write sites and the constraint
            # in lockstep.
            notif_id = dal.send_notification(
                from_agent="engine",
                to_agent="main",
                notification_type="workflow_failure",
                subject=f"Workflow failed: {run.workflow_id}",
                body=(
                    f"Workflow '{run.workflow_id}' finished {run.status.value} "
                    f"(trigger={run.trigger_type}, run={run.id}, "
                    f"duration={run.duration_ms}ms).\n\n{error}"
                ),
                metadata={
                    "kind": "workflow_failure",
                    "workflow_id": run.workflow_id,
                    "run_id": run.id,
                    "status": run.status.value,
                },
                tenant_id=run.tenant_id,
            )
            if not notif_id:
                logger.error(
                    "Workflow failure notification for %s was dropped — the "
                    "briefing fallback is not delivering",
                    _sanitize(run.workflow_id),
                )
        except Exception as e:
            logger.error(
                "Failed to write workflow failure notification for %s: %s",
                _sanitize(run.workflow_id),
                e,
            )

    async def _execute_steps(self, run: WorkflowRun, wf: WorkflowDef) -> None:
        """Execute workflow steps sequentially with flow control."""
        # Build step index for lookups
        step_index = {s.id: i for i, s in enumerate(wf.steps)}
        current_idx = 0

        while current_idx < len(wf.steps):
            step = wf.steps[current_idx]

            result = await self._execute_step(step, run, wf)

            # Retry failed steps per step.retry_count (backoff 60s, then
            # 300s). The surrounding asyncio.timeout still bounds the total
            # budget, so retries can never outlive the workflow deadline.
            attempt = 0
            while result.status == WorkflowStepStatus.FAILED and attempt < step.retry_count:
                delay = _retry_delay(attempt)
                logger.warning(
                    "Step %s failed (attempt %d/%d) — retrying in %ds: %s",
                    step.id,
                    attempt + 1,
                    step.retry_count + 1,
                    delay,
                    result.error_message,
                )
                await asyncio.sleep(delay)
                attempt += 1
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
                if step.on_failure == "skip":
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

    async def _execute_parallel(
        self, step: WorkflowStepDef, run: WorkflowRun, wf: WorkflowDef
    ) -> WorkflowStepResult:
        """Fan out the step's branches concurrently and join.

        Every branch is a full step whose result lands in
        ``run.context["steps"][branch.id]`` exactly like a top-level step's, so
        later steps template on branch outputs with no new syntax. Branch
        retries honor each branch's own ``retry_count`` (same backoff as the
        top-level loop); the surrounding workflow ``asyncio.timeout`` still
        bounds the total budget.

        The join is all-or-nothing by status: the parallel step COMPLETES only
        when every branch completed (or was skipped by its own on_failure);
        one failed branch fails the join, and the parallel step's own
        ``on_failure`` then decides abort-vs-skip at the top level — the same
        contract every other step follows. A failed sibling never cancels the
        other branches mid-flight: their results are still recorded, because a
        half-done fan-out with missing context entries is harder to reason
        about than a completed one with one failure in it.
        """
        result = WorkflowStepResult(
            step_id=step.id,
            step_type=step.type,
            status=WorkflowStepStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        semaphore = asyncio.Semaphore(step.max_concurrent) if step.max_concurrent > 0 else None

        async def run_branch(branch: WorkflowStepDef) -> WorkflowStepResult:
            async def once() -> WorkflowStepResult:
                if semaphore is None:
                    return await self._execute_single_step(branch, run, wf)
                async with semaphore:
                    return await self._execute_single_step(branch, run, wf)

            branch_result = await once()
            attempt = 0
            while (
                branch_result.status == WorkflowStepStatus.FAILED and attempt < branch.retry_count
            ):
                delay = _retry_delay(attempt)
                logger.warning(
                    "Parallel branch %s failed (attempt %d/%d) — retrying in %ds: %s",
                    branch.id,
                    attempt + 1,
                    branch.retry_count + 1,
                    delay,
                    branch_result.error_message,
                )
                await asyncio.sleep(delay)
                attempt += 1
                branch_result = await once()
            if branch_result.status == WorkflowStepStatus.FAILED and branch.on_failure == "skip":
                branch_result.status = WorkflowStepStatus.SKIPPED
            return branch_result

        branch_results = await asyncio.gather(*(run_branch(b) for b in step.parallel_steps))

        failed = []
        for branch, branch_result in zip(step.parallel_steps, branch_results, strict=True):
            run.step_results.append(branch_result)
            run.context["steps"][branch.id] = {
                "status": branch_result.status.value,
                "output_text": branch_result.output_text or "",
                "tool_output": branch_result.tool_output,
                "condition_branch": branch_result.condition_branch,
                "agent_run_id": branch_result.agent_run_id,
            }
            self._persist_step(run, branch_result)
            if branch_result.status == WorkflowStepStatus.FAILED:
                failed.append(branch.id)

        result.completed_at = datetime.now(UTC)
        if failed:
            result.status = WorkflowStepStatus.FAILED
            result.error_message = (
                f"{len(failed)} of {len(step.parallel_steps)} parallel branch(es) "
                f"failed: {', '.join(failed)}"
            )
            if step.on_failure == "skip":
                # The top-level loop will recast THIS result to SKIPPED and
                # continue — the same object-mutation contract every step
                # follows. The absorbed branch failures must be recast too:
                # the run finalizer counts ANY FAILED row in step_results, so
                # an honest-but-absorbed branch failure would fail the whole
                # run the operator explicitly chose to continue. The
                # error_message stays on the branch row — skip absorbs the
                # failure, it does not erase the evidence.
                for branch_result in branch_results:
                    if branch_result.status == WorkflowStepStatus.FAILED:
                        branch_result.status = WorkflowStepStatus.SKIPPED
                        run.context["steps"][branch_result.step_id]["status"] = (
                            WorkflowStepStatus.SKIPPED.value
                        )
        else:
            result.status = WorkflowStepStatus.COMPLETED
            result.output_text = f"{len(step.parallel_steps)} branch(es) completed"
        return result

    async def _execute_step(
        self, step: WorkflowStepDef, run: WorkflowRun, wf: WorkflowDef
    ) -> WorkflowStepResult:
        """Execute a single workflow step, including parallel fan-out."""
        if step.type == WorkflowStepType.PARALLEL:
            return await self._execute_parallel(step, run, wf)
        return await self._execute_single_step(step, run, wf)

    async def _execute_single_step(
        self, step: WorkflowStepDef, run: WorkflowRun, wf: WorkflowDef
    ) -> WorkflowStepResult:
        """Execute one non-parallel step."""
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
        from robothor.engine.dedup import release, try_acquire
        from robothor.engine.delivery import deliver

        agent_config = load_agent_config(step.agent_id, self.config.manifest_dir)
        if not agent_config:
            result.status = WorkflowStepStatus.FAILED
            result.error_message = f"Agent config not found: {step.agent_id}"
            return

        # Prevent overlap with cron-triggered or other workflow-triggered runs
        if not await try_acquire(step.agent_id):
            logger.info(
                "Agent %s already running, skipping workflow step %s",
                step.agent_id,
                step.id,
            )
            result.status = WorkflowStepStatus.SKIPPED
            result.error_message = f"Agent {step.agent_id} already running"
            return

        try:
            # Render message template — warmup handled centrally by runner.execute()
            message = _render_template(step.message, run.context)

            agent_run = await self.runner.execute(
                agent_id=step.agent_id,
                message=message,
                trigger_type=TriggerType.WORKFLOW,
                trigger_detail=f"workflow:{run.workflow_id}:{step.id}",
                correlation_id=run.correlation_id or run.id,
                agent_config=agent_config,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                user_role=run.user_role,
            )

            # Deliver agent output (delivery.py now handles its own DB persistence)
            await deliver(agent_config, agent_run)

            result.agent_run_id = agent_run.id
            result.output_text = agent_run.output_text

            if agent_run.status.value == "completed":
                result.status = WorkflowStepStatus.COMPLETED
            else:
                result.status = WorkflowStepStatus.FAILED
                result.error_message = agent_run.error_message or agent_run.status.value
        finally:
            await release(step.agent_id)

    def _workflow_guardrails(self) -> Any:
        """Baseline guardrails for workflow tool steps.

        Workflow YAML steps call tools directly via ``registry.execute`` — they
        have no agent manifest, so they used to bypass guardrails entirely (a
        workflow step could run ``exec``/``git_push``/cold email unguarded;
        audit 2026-05-29). Apply the fleet defaults plus protected-branch
        safety so this path is no longer a hole.
        """
        cached = getattr(self, "_wf_guardrail_engine", None)
        if cached is not None:
            return cached
        from robothor.engine.guardrails import DEFAULT_GUARDRAILS, GuardrailEngine

        policies = [*DEFAULT_GUARDRAILS, "no_main_branch_push"]
        engine = GuardrailEngine(
            enabled_policies=policies,
            workspace=str(self.config.workspace) + "/",
        )
        self._wf_guardrail_engine = engine
        return engine

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

        # ── [GUARDRAILS] Pre-execution check (workflow steps are otherwise unguarded) ──
        guardrails = self._workflow_guardrails()
        gr = guardrails.check_pre_execution(
            step.tool_name, rendered_args, agent_id=f"workflow:{run.workflow_id}"
        )
        if not gr.allowed:
            result.status = WorkflowStepStatus.FAILED
            result.error_message = f"Blocked by guardrail ({gr.guardrail_name}): {gr.reason}"
            result.output_text = result.error_message
            return

        tool_result = await self.runner.registry.execute(
            step.tool_name,
            rendered_args,
            agent_id=f"workflow:{run.workflow_id}",
            tenant_id=run.tenant_id,
            workspace=str(self.config.workspace),
            user_id=run.user_id,
            user_role=run.user_role,
        )

        # ── [GUARDRAILS] Post-execution check (secrets in output, etc.) ──
        post = guardrails.check_post_execution(step.tool_name, tool_result)
        if post.action == "warned":
            logger.warning(
                "Workflow %s step %s guardrail warning (%s): %s",
                run.workflow_id,
                step.tool_name,
                post.guardrail_name,
                post.reason,
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

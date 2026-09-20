"""One durable coordination turn at a time, with immediate fair continuation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
from contextlib import suppress

from robothor.goals import store
from robothor.goals.runtime import Binding, binding

logger = logging.getLogger(__name__)

# Minimum wall-clock gap between two coordination runs. `tick` used to drain
# while any goal stayed ready — claim, execute, `sleep(0)`, claim again — so
# `serve`'s one-second sleep was unreachable for as long as one goal kept
# re-queueing itself, and a probe drove 201 runs out of a single tick. Thirty
# seconds caps pursuit at two runs a minute: fast enough that a goal still
# makes visible progress, slow enough that an operator watching the Goals view
# can pause a misbehaving one before it has spent much, and it is what stops a
# parent from spinning turns while its execution child waits its turn.
MIN_RUN_INTERVAL_SECONDS = 30

# How long `serve` waits between ticks while pursuit is switched off for this
# tenant. At one second the disabled feature still opened ~86,400 connections
# a day to read a flag that cannot have changed more than a handful of times.
DISABLED_POLL_SECONDS = 60

PURSUIT_INSTRUCTIONS = """You are pursuing an explicitly authorized operator goal.
Use get_pursuit_goal and update_pursuit_goal for this goal. Read the current version
before updates. Keep working until the criteria are verified; a final response does
not complete the goal. Record progress and a concrete next action before ending a run.
Never weaken the criteria. Cite observed tool results or artifacts for evidence.
Only mark a criterion satisfied after checking it. Ordinary prose is not proof.
For a long-term goal, create short-term execution children for actionable work;
reuse existing children and tasks instead of duplicating them. Link delegated CRM
tasks using link_task. Only one coordination run can execute at a time: queued
execution children cannot start until this parent run ends. After creating or
finding a ready child, record a durable wait and end this turn immediately. Do
not poll or sleep for a queued child inside this run. Wait with a concrete date,
task, or event condition when nothing is actionable. Waiting needs a reason and a fallback review time.
If a child waits, its parent must register the future dependency and resume or
replace the child only when actionable. Do not complete a parent merely because
a task completed. Ongoing targets use assess, never complete.
Only create goals for explicit user requests or execution children of this goal.
Respect existing tool permissions and approval rules. Do not pause merely because
work is difficult. Record the same concrete blocker across attempts when stuck.
When recovery_required is true, inspect the previous run's tool results and external
state BEFORE repeating any action. Record reconciled with the observed outcome.
"""


class GoalController:
    def __init__(self, runner: AgentRunner, config: EngineConfig) -> None:
        self.runner = runner
        self.config = config
        self._lock = asyncio.Lock()
        self._last_capture = 0.0
        self._last_run = 0.0
        self._enabled = True

    async def serve(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Goal pursuit reconciliation failed")
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(1 if self._enabled else DISABLED_POLL_SECONDS)

    def _paced(self) -> bool:
        """True while the minimum inter-run delay has not elapsed."""
        return time.monotonic() - self._last_run < MIN_RUN_INTERVAL_SECONDS

    async def tick(self) -> None:
        if self._lock.locked() or self._paced():
            return
        async with self._lock:
            self._enabled = await asyncio.to_thread(store.enabled, self.config.tenant_id)
            if not self._enabled:
                return
            # Drain ready goals, yielding and collecting events between turns,
            # until the pacing gap is due. One tick is at most one run.
            while True:
                if time.monotonic() - self._last_capture >= 60:
                    from robothor.goals.events import capture

                    try:
                        await asyncio.to_thread(capture, self.config.tenant_id)
                    except Exception:
                        logger.warning(
                            "Goal event capture unavailable; timed reviews remain active",
                            exc_info=True,
                        )
                    self._last_capture = time.monotonic()
                claimed = await asyncio.to_thread(store.claim, self.config.tenant_id)
                if not claimed:
                    return
                goal, attempt = claimed
                try:
                    await self.execute(goal, attempt)
                finally:
                    self._last_run = time.monotonic()
                if self._paced():
                    return
                await asyncio.sleep(0)

    async def execute(self, goal: dict[str, Any], attempt: str) -> None:
        from robothor.engine.config import load_agent_config_or_broken
        from robothor.engine.models import TriggerType

        tenant = self.config.tenant_id
        remaining = goal["token_budget"] - goal["tokens_used"] if goal["token_budget"] else None
        if goal["parent_goal_id"]:
            parent = await asyncio.to_thread(store.control, tenant, goal["parent_goal_id"])
            if parent["token_budget"]:
                parent_remaining = max(0, parent["token_budget"] - parent["tokens_used"])
                remaining = (
                    min(remaining, parent_remaining) if remaining is not None else parent_remaining
                )
        if remaining is not None and remaining <= 0:
            await asyncio.to_thread(
                store.finish, tenant, goal["id"], attempt, budget_exhausted=True
            )
            return
        current = Binding(tenant, goal["id"], attempt, remaining)
        token = binding.set(current)
        run = None
        error = ""
        work = None
        try:
            config = load_agent_config_or_broken("main", self.config.manifest_dir, "goal pursuit")
            if config is None:
                raise ValueError("main agent configuration unavailable")
            detail = await asyncio.to_thread(store.get, tenant, goal["id"])
            prompt = PURSUIT_INSTRUCTIONS + "\nCurrent goal:\n" + json.dumps(detail, default=str)
            work = asyncio.create_task(
                self.runner.execute(
                    agent_id="main",
                    message=prompt,
                    trigger_type=TriggerType.CRON,
                    trigger_detail=f"goal:{goal['id']}",
                    correlation_id=attempt,
                    agent_config=config,
                    tenant_id=tenant,
                )
            )
            seen_steer = goal.get("steer_version", 0)
            while not work.done():
                done, _ = await asyncio.wait({work}, timeout=5)
                if done:
                    break
                control = await asyncio.to_thread(store.control, tenant, goal["id"])
                if control.get("steer_version", 0) > seen_steer:
                    from robothor.engine.session_registry import lookup

                    session = lookup(current.run_id)
                    if session:
                        session.steer(
                            "Operator updated this goal: " + json.dumps(control, default=str)
                        )
                        seen_steer = control["steer_version"]
                if not await asyncio.to_thread(
                    store.heartbeat,
                    tenant,
                    goal["id"],
                    attempt,
                    tokens=sum(r.input_tokens + r.output_tokens for r in current.runs.values()),
                    cost=sum(r.total_cost_usd for r in current.runs.values()),
                ):
                    work.cancel()
                    break
            with suppress(asyncio.CancelledError):
                run = await work
            if run is not None and str(run.status) in {"failed", "timeout", "skipped"}:
                error = run.error_message or str(run.status)
        except asyncio.CancelledError:
            if work:
                work.cancel()
                with suppress(asyncio.CancelledError):
                    await work
            error = "execution interrupted; reconcile external actions before retrying"
            raise
        except Exception as exc:
            error = str(exc)
            logger.exception("Goal execution failed for %s", goal["id"])
        finally:
            # Revoke the live runner before releasing its durable lease, including
            # when a control/heartbeat lookup fails while execution is in flight.
            if work is not None and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            live_run = run or current.runs.get(current.run_id)
            all_runs = list(current.runs.values()) or ([run] if run else [])
            binding.reset(token)
            await asyncio.to_thread(
                store.finish,
                tenant,
                goal["id"],
                attempt,
                run_id=run.id if run else current.run_id or None,
                tokens=sum(r.input_tokens + r.output_tokens for r in all_runs),
                cost=sum(r.total_cost_usd for r in all_runs),
                budget_exhausted=any(r.budget_exhausted for r in all_runs),
                checkpoint=live_run.output_text or "" if live_run else "",
                interrupted=run is None,
                error=error,
            )


async def stop_controller(task: asyncio.Task[None] | None) -> None:
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

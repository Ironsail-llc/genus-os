"""
Cron Scheduler — APScheduler wrapper for scheduled agent runs.

Loads all YAML manifests on startup, creates CronTrigger jobs.
max_instances=1 prevents concurrent runs of the same agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from robothor.engine.config import load_all_manifests, manifest_to_agent_config
from robothor.engine.dedup import release, try_acquire
from robothor.engine.delivery import deliver
from robothor.engine.models import AgentConfig, AgentRun, TriggerType
from robothor.engine.task_registry import get_task_registry
from robothor.engine.tracking import delete_stale_schedules, update_schedule_state, upsert_schedule

# Circuit breaker: skip agent after this many consecutive errors
CIRCUIT_BREAKER_THRESHOLD = 5

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)


class CronScheduler:
    """APScheduler-based cron scheduler for agent runs."""

    def __init__(
        self,
        config: EngineConfig,
        runner: AgentRunner,
        workflow_engine: Any = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.workflow_engine = workflow_engine
        self.scheduler = AsyncIOScheduler(timezone=config.default_timezone)

    async def start(self) -> None:
        """Load manifests and start the scheduler."""
        manifests = load_all_manifests(self.config.manifest_dir)
        loaded = 0
        active_schedule_ids: set[str] = set()

        for manifest in manifests:
            agent_config = manifest_to_agent_config(manifest)

            # Register heartbeat cron job if present
            if agent_config.heartbeat and agent_config.heartbeat.cron_expr:
                try:
                    hb_trigger = CronTrigger.from_crontab(
                        agent_config.heartbeat.cron_expr,
                        timezone=agent_config.heartbeat.timezone,
                    )
                    hb_job_id = f"{agent_config.id}:heartbeat"
                    self.scheduler.add_job(
                        self._run_heartbeat,
                        trigger=hb_trigger,
                        args=[agent_config.id],
                        id=hb_job_id,
                        name=f"heartbeat:{agent_config.name}",
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=60,
                    )

                    # Upsert schedule state for heartbeat
                    try:
                        upsert_schedule(
                            agent_id=hb_job_id,
                            tenant_id=self.config.tenant_id,
                            enabled=True,
                            cron_expr=agent_config.heartbeat.cron_expr,
                            timezone=agent_config.heartbeat.timezone,
                            timeout_seconds=agent_config.heartbeat.timeout_seconds,
                            model_primary=agent_config.model_primary,
                            model_fallbacks=agent_config.model_fallbacks,
                            delivery_mode=agent_config.heartbeat.delivery_mode.value,
                            delivery_channel=agent_config.heartbeat.delivery_channel,
                            delivery_to=agent_config.heartbeat.delivery_to,
                            session_target=agent_config.heartbeat.session_target,
                        )
                        active_schedule_ids.add(hb_job_id)
                    except Exception as e:
                        logger.warning(
                            "Failed to upsert heartbeat schedule for %s: %s",
                            agent_config.id,
                            e,
                        )

                    loaded += 1
                    logger.info(
                        "Registered heartbeat for %s: %s",
                        agent_config.id,
                        agent_config.heartbeat.cron_expr,
                    )
                except Exception as e:
                    logger.error(
                        "Invalid heartbeat cron for %s: %s — %s",
                        agent_config.id,
                        agent_config.heartbeat.cron_expr,
                        e,
                    )

            if not agent_config.cron_expr:
                continue

            # Parse cron expression
            try:
                trigger = CronTrigger.from_crontab(
                    agent_config.cron_expr,
                    timezone=agent_config.timezone,
                )
            except Exception as e:
                logger.error(
                    "Invalid cron expression for %s: %s — %s",
                    agent_config.id,
                    agent_config.cron_expr,
                    e,
                )
                continue

            # Add job — use APScheduler's misfire_grace_time for catch-up logic
            if agent_config.catch_up == "skip_if_stale":
                grace_time = agent_config.stale_after_minutes * 60
            else:
                grace_time = None  # always run missed fires
            self.scheduler.add_job(
                self._run_agent,
                trigger=trigger,
                args=[agent_config.id],
                id=agent_config.id,
                name=f"agent:{agent_config.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=grace_time,
            )

            # Upsert schedule state in database
            try:
                upsert_schedule(
                    agent_id=agent_config.id,
                    tenant_id=self.config.tenant_id,
                    enabled=True,
                    cron_expr=agent_config.cron_expr,
                    timezone=agent_config.timezone,
                    timeout_seconds=agent_config.timeout_seconds,
                    model_primary=agent_config.model_primary,
                    model_fallbacks=agent_config.model_fallbacks,
                    delivery_mode=agent_config.delivery_mode.value,
                    delivery_channel=agent_config.delivery_channel,
                    delivery_to=agent_config.delivery_to,
                    session_target=agent_config.session_target,
                )
                active_schedule_ids.add(agent_config.id)
            except Exception as e:
                logger.warning("Failed to upsert schedule for %s: %s", agent_config.id, e)

            loaded += 1

        logger.info("Loaded %d scheduled agents from %d manifests", loaded, len(manifests))

        # Clean up stale schedule rows for removed agents
        if active_schedule_ids:
            try:
                deleted = delete_stale_schedules(
                    active_schedule_ids, tenant_id=self.config.tenant_id
                )
                if deleted:
                    logger.info("Pruned %d stale schedule(s): %s", len(deleted), deleted)
            except Exception as e:
                logger.warning("Failed to prune stale schedules: %s", e)

        # Register workflow cron jobs
        wf_loaded = 0
        if self.workflow_engine:
            for wf, wf_trigger in self.workflow_engine.get_workflows_for_cron():
                try:
                    wf_cron_trigger = CronTrigger.from_crontab(
                        wf_trigger.cron,
                        timezone=wf_trigger.timezone,
                    )
                    self.scheduler.add_job(
                        self._run_workflow,
                        trigger=wf_cron_trigger,
                        args=[wf.id],
                        id=f"workflow:{wf.id}",
                        name=f"workflow:{wf.name}",
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=60,
                    )
                    wf_loaded += 1
                except Exception as e:
                    logger.error(
                        "Invalid workflow cron for %s: %s — %s",
                        wf.id,
                        wf_trigger.cron,
                        e,
                    )
            logger.info("Loaded %d workflow cron jobs", wf_loaded)

        self.scheduler.start()
        logger.info("Cron scheduler started")

        # Keep running
        while True:
            await asyncio.sleep(60)

    # ─── Shared execution path ────────────────────────────────────────

    async def _run_scheduled(
        self,
        agent_id: str,
        dedup_key: str,
        agent_config: AgentConfig,
        trigger_detail: str,
        *,
        downstream_agents: list[str] | None = None,
    ) -> None:
        """Shared entry point for cron and heartbeat runs.

        Handles: dedup → circuit breaker → safety timeout → execute/deliver → track.
        """
        if not await try_acquire(dedup_key):
            logger.info("Cron skipped: %s already running", dedup_key)
            return

        try:
            logger.info("Cron trigger: running %s (key=%s)", agent_id, dedup_key)

            # Circuit breaker: skip after too many consecutive errors
            if self._circuit_breaker_tripped(dedup_key, agent_config):
                return

            safety_timeout = agent_config.timeout_seconds + 120

            try:
                async with asyncio.timeout(safety_timeout):
                    await self._execute_and_deliver(
                        agent_id,
                        dedup_key,
                        agent_config,
                        trigger_detail,
                        downstream_agents=downstream_agents,
                    )
            except TimeoutError:
                logger.error(
                    "Scheduler safety timeout (%ds) hit for %s — "
                    "runner.execute() hung beyond timeout (%ds)",
                    safety_timeout,
                    dedup_key,
                    agent_config.timeout_seconds,
                )
                self._record_timeout(dedup_key)

        finally:
            await release(dedup_key)

    def _circuit_breaker_tripped(self, dedup_key: str, agent_config: AgentConfig) -> bool:
        """Check circuit breaker. Returns True if tripped (should skip)."""
        try:
            from robothor.engine.tracking import get_schedule

            schedule = get_schedule(dedup_key)
            if schedule:
                errors = schedule.get("consecutive_errors", 0) or 0
                if errors >= CIRCUIT_BREAKER_THRESHOLD:
                    logger.warning(
                        "Circuit breaker: %s has %d consecutive errors, skipping",
                        dedup_key,
                        errors,
                    )
                    # Create a CRM task so heartbeat surfaces it naturally
                    try:
                        from robothor.crm.dal import create_task as dal_create_task

                        dal_create_task(
                            title=f"{agent_config.name} paused — {errors} consecutive failures",
                            body=(
                                f"Agent has been automatically paused after {errors} "
                                f"consecutive errors.\n"
                                f"Check agent_runs for {agent_config.id}.\n"
                                f"To resume: reset consecutive_errors in agent_schedules."
                            ),
                            status="TODO",
                            assigned_to_agent="main",
                            created_by_agent="engine",
                            priority="high",
                            tags=[agent_config.id, "paused", "needs-attention"],
                            requires_human=True,
                            tenant_id=self.config.tenant_id,
                        )
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        return False

    def _record_timeout(self, dedup_key: str) -> None:
        """Record a timeout in the schedule state for circuit breaker tracking."""
        try:
            from robothor.engine.tracking import get_schedule

            prev_schedule = None
            with contextlib.suppress(Exception):
                prev_schedule = get_schedule(dedup_key)
            consecutive_errors = (
                (prev_schedule.get("consecutive_errors", 0) + 1) if prev_schedule else 1
            )
            update_schedule_state(
                agent_id=dedup_key,
                last_run_at=datetime.now(UTC),
                last_status="timeout",
                consecutive_errors=consecutive_errors,
            )
        except Exception:
            pass

    async def _execute_and_deliver(
        self,
        agent_id: str,
        dedup_key: str,
        agent_config: AgentConfig,
        trigger_detail: str,
        *,
        downstream_agents: list[str] | None = None,
    ) -> AgentRun:
        """Run agent, deliver output, update schedule state."""
        from robothor.engine.tracking import get_schedule

        payload = self._build_payload(agent_config)

        # Load prior session for persistent agents (like Telegram does)
        conversation_history = None
        if agent_config.session_target == "persistent":
            try:
                from robothor.engine.chat_store import load_session

                session_key = f"cron:{dedup_key}"
                session_data = await asyncio.to_thread(load_session, session_key, limit=20)
                if session_data and session_data.get("history"):
                    conversation_history = session_data["history"]
                    logger.info(
                        "Loaded %d prior messages for persistent session %s",
                        len(conversation_history),
                        session_key,
                    )
            except Exception as e:
                logger.warning("Failed to load persistent session for %s: %s", dedup_key, e)

        run = await self.runner.execute(
            agent_id=agent_id,
            message=payload,
            trigger_type=TriggerType.CRON,
            trigger_detail=trigger_detail,
            agent_config=agent_config,
            conversation_history=conversation_history,
        )

        # Save session for persistent agents
        if agent_config.session_target == "persistent" and run.output_text:
            try:
                from robothor.engine.chat_store import save_exchange

                session_key = f"cron:{dedup_key}"
                await asyncio.to_thread(
                    save_exchange,
                    session_key,
                    payload,
                    run.output_text,
                    channel="cron",
                )
                logger.debug("Saved persistent session for %s", session_key)
            except Exception as e:
                logger.warning("Failed to save persistent session for %s: %s", dedup_key, e)

        # Deliver output
        await deliver(agent_config, run)

        # Persist delivery status back to DB
        if run.delivery_status or run.delivered_at:
            try:
                from robothor.engine.tracking import update_run

                update_run(
                    run.id,
                    delivery_status=run.delivery_status,
                    delivered_at=run.delivered_at,
                    delivery_channel=run.delivery_channel,
                )
            except Exception as e:
                logger.warning("Failed to persist delivery status for %s: %s", agent_id, e)

        # Update schedule state
        try:
            consecutive_errors = 0
            if run.status.value in ("failed", "timeout"):
                prev_schedule = None
                with contextlib.suppress(Exception):
                    prev_schedule = get_schedule(dedup_key)
                consecutive_errors = (
                    (prev_schedule.get("consecutive_errors", 0) + 1) if prev_schedule else 1
                )

            update_schedule_state(
                agent_id=dedup_key,
                last_run_at=run.started_at,
                last_run_id=run.id,
                last_status=run.status.value,
                last_duration_ms=run.duration_ms,
                consecutive_errors=consecutive_errors,
            )
        except Exception as e:
            logger.warning("Failed to update schedule state for %s: %s", dedup_key, e)

        logger.info(
            "Cron complete: %s status=%s duration=%dms tokens=%d/%d",
            agent_id,
            run.status.value,
            run.duration_ms or 0,
            run.input_tokens,
            run.output_tokens,
        )

        # Downstream agent triggers (fire-and-forget on success)
        if run.status.value == "completed" and downstream_agents:
            for downstream_id in downstream_agents:
                logger.info("Triggering downstream agent: %s", downstream_id)
                get_task_registry().spawn(
                    self._run_agent(downstream_id),
                    name=f"sched-downstream:{downstream_id}",
                )

        return run

    # ─── Thin wrappers ────────────────────────────────────────────────

    async def _run_agent(self, agent_id: str) -> None:
        """Execute an agent as a scheduled cron job."""
        from robothor.engine.config import load_agent_config

        agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if not agent_config:
            logger.error("Agent config not found for cron job: %s", agent_id)
            return

        await self._run_scheduled(
            agent_id,
            agent_id,
            agent_config,
            agent_config.cron_expr,
            downstream_agents=agent_config.downstream_agents,
        )

    async def _run_heartbeat(self, agent_id: str) -> None:
        """Execute a heartbeat run for an agent."""
        from robothor.engine.config import load_agent_config

        agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if not agent_config or not agent_config.heartbeat:
            logger.error("Agent config or heartbeat not found for: %s", agent_id)
            return

        override_config = _build_heartbeat_config(agent_config)

        await self._run_scheduled(
            agent_id,
            f"{agent_id}:heartbeat",
            override_config,
            f"heartbeat:{agent_config.heartbeat.cron_expr}",
        )

    async def _run_workflow(self, workflow_id: str) -> None:
        """Execute a workflow as a scheduled cron job."""
        if not self.workflow_engine:
            return
        try:
            logger.info("Cron trigger: running workflow %s", workflow_id)
            run = await self.workflow_engine.execute(
                workflow_id=workflow_id,
                trigger_type="cron",
                trigger_detail=f"cron:{workflow_id}",
            )
            logger.info(
                "Workflow cron complete: %s status=%s duration=%dms",
                workflow_id,
                run.status.value,
                run.duration_ms,
            )
        except Exception as e:
            logger.error("Workflow cron failed for %s: %s", workflow_id, e)

    def _build_payload(self, config: AgentConfig) -> str:
        """Build the cron payload message from agent config."""
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"Current time: {now}\n\n"
            f"You are {config.name} ({config.id}). "
            f"Execute your scheduled tasks as described in your instructions."
        )

    def reconcile_schedules(self) -> list[str]:
        """Reconcile DB + in-memory jobs against current manifests.

        Removes orphaned schedule rows and APScheduler jobs for agents
        whose manifests no longer exist. Skips workflow:* jobs.
        Returns list of pruned agent IDs.
        """
        manifests = load_all_manifests(self.config.manifest_dir)
        active_ids: set[str] = set()

        for manifest in manifests:
            agent_config = manifest_to_agent_config(manifest)
            if agent_config.cron_expr:
                active_ids.add(agent_config.id)
            if agent_config.heartbeat and agent_config.heartbeat.cron_expr:
                active_ids.add(f"{agent_config.id}:heartbeat")

        # Prune stale DB rows
        pruned: list[str] = []
        if active_ids:
            try:
                pruned = delete_stale_schedules(active_ids, tenant_id=self.config.tenant_id)
            except Exception as e:
                logger.warning("Reconcile: failed to prune stale DB rows: %s", e)

        # Remove orphaned APScheduler in-memory jobs
        for job in self.scheduler.get_jobs():
            if job.id.startswith("workflow:"):
                continue
            if job.id not in active_ids:
                logger.info("Reconcile: removing orphaned job %s", job.id)
                job.remove()
                if job.id not in pruned:
                    pruned.append(job.id)

        return pruned

    async def stop(self) -> None:
        """Shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Cron scheduler stopped")


def _build_heartbeat_config(agent_config: AgentConfig) -> AgentConfig:
    """Build override AgentConfig for heartbeat runs.

    Inherits model + tools from parent agent, overrides instruction file,
    delivery, warmup, and budget from heartbeat config.
    Falls back to parent warmup config if heartbeat doesn't specify its own.
    """
    hb = agent_config.heartbeat
    assert hb is not None

    # Inherit parent warmup if heartbeat doesn't specify its own
    warmup_memory_blocks = hb.warmup_memory_blocks or agent_config.warmup_memory_blocks
    warmup_context_files = hb.warmup_context_files or agent_config.warmup_context_files
    warmup_peer_agents = hb.warmup_peer_agents or agent_config.warmup_peer_agents

    # Cost cap: heartbeat override wins; fall back to parent agent's cap.
    # When the heartbeat sets its own budget we force hard-budget semantics so
    # the override actually bites; otherwise inherit whatever the parent agent
    # configured.
    max_cost_usd = hb.cost_budget_usd or agent_config.max_cost_usd
    hard_budget = hb.cost_budget_usd > 0 or agent_config.hard_budget

    return AgentConfig(
        id=agent_config.id,
        name=agent_config.name,
        description=agent_config.description,
        model_primary=agent_config.model_primary,
        model_fallbacks=agent_config.model_fallbacks,
        temperature=agent_config.temperature,
        cron_expr=hb.cron_expr,
        timezone=hb.timezone,
        timeout_seconds=hb.timeout_seconds,
        max_iterations=hb.max_iterations,
        safety_cap=hb.safety_cap,
        session_target=hb.session_target,
        delivery_mode=hb.delivery_mode,
        delivery_channel=hb.delivery_channel,
        delivery_to=hb.delivery_to,
        tools_allowed=agent_config.tools_allowed,
        tools_denied=agent_config.tools_denied,
        instruction_file=hb.instruction_file,
        bootstrap_files=hb.bootstrap_files,
        reports_to=agent_config.reports_to,
        department=agent_config.department,
        task_protocol=agent_config.task_protocol,
        review_workflow=agent_config.review_workflow,
        notification_inbox=agent_config.notification_inbox,
        shared_working_state=agent_config.shared_working_state,
        warmup_memory_blocks=warmup_memory_blocks,
        warmup_context_files=warmup_context_files,
        warmup_peer_agents=warmup_peer_agents,
        stall_timeout_seconds=hb.stall_timeout_seconds,
        error_feedback=agent_config.error_feedback,
        max_cost_usd=max_cost_usd,
        hard_budget=hard_budget,
        # Sub-agent config inherited from parent
        can_spawn_agents=agent_config.can_spawn_agents,
        max_nesting_depth=agent_config.max_nesting_depth,
        sub_agent_max_iterations=agent_config.sub_agent_max_iterations,
        sub_agent_timeout_seconds=agent_config.sub_agent_timeout_seconds,
    )

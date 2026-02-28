"""
Cron Scheduler — APScheduler wrapper for scheduled agent runs.

Loads all YAML manifests on startup, creates CronTrigger jobs.
max_instances=1 prevents concurrent runs of the same agent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from robothor.engine.config import load_all_manifests, manifest_to_agent_config
from robothor.engine.dedup import release, try_acquire
from robothor.engine.delivery import deliver
from robothor.engine.models import TriggerType
from robothor.engine.tracking import update_schedule_state, upsert_schedule

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
        workflow_engine=None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.workflow_engine = workflow_engine
        self.scheduler = AsyncIOScheduler(timezone=config.default_timezone)

    async def start(self) -> None:
        """Load manifests and start the scheduler."""
        manifests = load_all_manifests(self.config.manifest_dir)
        loaded = 0

        for manifest in manifests:
            agent_config = manifest_to_agent_config(manifest)
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

            # Add job
            self.scheduler.add_job(
                self._run_agent,
                trigger=trigger,
                args=[agent_config.id],
                id=agent_config.id,
                name=f"agent:{agent_config.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
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
            except Exception as e:
                logger.warning("Failed to upsert schedule for %s: %s", agent_config.id, e)

            loaded += 1

        logger.info("Loaded %d scheduled agents from %d manifests", loaded, len(manifests))

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

    async def _run_agent(self, agent_id: str) -> None:
        """Execute an agent as a scheduled cron job."""
        from robothor.engine.config import load_agent_config

        # Cross-trigger dedup
        if not try_acquire(agent_id):
            logger.info("Cron skipped: %s already running", agent_id)
            return

        try:
            logger.info("Cron trigger: running %s", agent_id)

            agent_config = load_agent_config(agent_id, self.config.manifest_dir)
            if not agent_config:
                logger.error("Agent config not found for cron job: %s", agent_id)
                return

            # Circuit breaker: skip after too many consecutive errors
            try:
                from robothor.engine.tracking import get_schedule

                schedule = get_schedule(agent_id)
                if schedule:
                    errors = schedule.get("consecutive_errors", 0) or 0
                    if errors >= CIRCUIT_BREAKER_THRESHOLD:
                        logger.warning(
                            "Circuit breaker: %s has %d consecutive errors, skipping",
                            agent_id,
                            errors,
                        )
                        # Send Telegram alert (best-effort)
                        try:
                            from robothor.engine.delivery import get_telegram_sender

                            sender = get_telegram_sender()
                            if sender and agent_config.delivery_to:
                                await sender(
                                    agent_config.delivery_to,
                                    f"*Circuit Breaker*\n\n{agent_config.name} "
                                    f"has {errors} consecutive errors. "
                                    f"Skipping scheduled run. Check logs.",
                                )
                        except Exception:
                            pass
                        return
            except Exception:
                pass  # Don't block execution if circuit breaker check fails

            # Build the cron payload message
            payload = self._build_payload(agent_config)

            run = await self.runner.execute(
                agent_id=agent_id,
                message=payload,
                trigger_type=TriggerType.CRON,
                trigger_detail=agent_config.cron_expr,
                agent_config=agent_config,
            )

            # Deliver output
            await deliver(agent_config, run)

            # Update schedule state
            try:
                consecutive_errors = 0
                if run.status.value in ("failed", "timeout"):
                    prev_schedule = None
                    try:
                        prev_schedule = get_schedule(agent_id)
                    except Exception:
                        pass
                    consecutive_errors = (
                        (prev_schedule.get("consecutive_errors", 0) + 1) if prev_schedule else 1
                    )

                update_schedule_state(
                    agent_id=agent_id,
                    last_run_at=run.started_at,
                    last_run_id=run.id,
                    last_status=run.status.value,
                    last_duration_ms=run.duration_ms,
                    consecutive_errors=consecutive_errors,
                )
            except Exception as e:
                logger.warning("Failed to update schedule state for %s: %s", agent_id, e)

            logger.info(
                "Cron complete: %s status=%s duration=%dms tokens=%d/%d",
                agent_id,
                run.status.value,
                run.duration_ms or 0,
                run.input_tokens,
                run.output_tokens,
            )

            # Downstream agent triggers (fire-and-forget on success)
            if run.status.value == "completed" and agent_config.downstream_agents:
                for downstream_id in agent_config.downstream_agents:
                    logger.info("Triggering downstream agent: %s", downstream_id)
                    asyncio.create_task(self._run_agent(downstream_id))

        finally:
            release(agent_id)

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
        """Build the cron payload message from agent config.

        Prepends warmth preamble (session history, memory blocks, context
        files, peer status) so agents start warm instead of cold.
        """
        from robothor.engine.warmup import build_warmth_preamble

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        base = (
            f"Current time: {now}\n\n"
            f"You are {config.name} ({config.id}). "
            f"Execute your scheduled tasks as described in your instructions."
        )

        # Build warmth preamble (never crashes)
        try:
            preamble = build_warmth_preamble(config, self.config.workspace, self.config.tenant_id)
        except Exception as e:
            logger.debug("Warmup preamble failed for %s: %s", config.id, e)
            preamble = ""

        if preamble:
            return f"{preamble}\n\n{base}"
        return base

    async def stop(self) -> None:
        """Shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Cron scheduler stopped")

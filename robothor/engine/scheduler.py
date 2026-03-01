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
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from robothor.engine.config import load_all_manifests, manifest_to_agent_config
from robothor.engine.dedup import release, try_acquire
from robothor.engine.delivery import deliver
from robothor.engine.models import AgentConfig, TriggerType
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
                        prev_schedule = get_schedule(agent_id)
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

    async def _run_heartbeat(self, agent_id: str) -> None:
        """Execute a heartbeat run for an agent.

        Uses the heartbeat config overrides (instruction file, delivery,
        warmup, budget) while inheriting model + tools from the parent agent.
        Dedup key is {agent_id}:heartbeat so it doesn't block interactive runs.
        """
        from robothor.engine.config import load_agent_config

        dedup_key = f"{agent_id}:heartbeat"

        if not try_acquire(dedup_key):
            logger.info("Heartbeat skipped: %s already running", dedup_key)
            return

        try:
            logger.info("Heartbeat trigger: running %s", agent_id)

            agent_config = load_agent_config(agent_id, self.config.manifest_dir)
            if not agent_config or not agent_config.heartbeat:
                logger.error("Agent config or heartbeat not found for: %s", agent_id)
                return

            hb = agent_config.heartbeat

            # Circuit breaker (uses heartbeat-specific schedule key)
            try:
                from robothor.engine.tracking import get_schedule

                schedule = get_schedule(dedup_key)
                if schedule:
                    errors = schedule.get("consecutive_errors", 0) or 0
                    if errors >= CIRCUIT_BREAKER_THRESHOLD:
                        logger.warning(
                            "Circuit breaker: %s heartbeat has %d consecutive errors, skipping",
                            agent_id,
                            errors,
                        )
                        try:
                            from robothor.engine.delivery import get_telegram_sender

                            sender = get_telegram_sender()
                            if sender and hb.delivery_to:
                                await sender(
                                    hb.delivery_to,
                                    f"*Circuit Breaker*\n\n{agent_config.name} heartbeat "
                                    f"has {errors} consecutive errors. "
                                    f"Skipping scheduled run. Check logs.",
                                )
                        except Exception:
                            pass
                        return
            except Exception:
                pass

            # Build override config from heartbeat settings,
            # inheriting model + tools from parent
            override_config = AgentConfig(
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
                warmup_memory_blocks=hb.warmup_memory_blocks,
                warmup_context_files=hb.warmup_context_files,
                warmup_peer_agents=hb.warmup_peer_agents,
                # token_budget auto-derived at runtime from model registry
                error_feedback=agent_config.error_feedback,
            )

            # Build the payload
            payload = self._build_payload(override_config)

            run = await self.runner.execute(
                agent_id=agent_id,
                message=payload,
                trigger_type=TriggerType.CRON,
                trigger_detail=f"heartbeat:{hb.cron_expr}",
                agent_config=override_config,
            )

            # Deliver with heartbeat's announce mode
            await deliver(override_config, run)

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
                    logger.warning(
                        "Failed to persist heartbeat delivery status for %s: %s",
                        agent_id,
                        e,
                    )

            # Update schedule state under heartbeat key
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
                logger.warning(
                    "Failed to update heartbeat schedule state for %s: %s",
                    agent_id,
                    e,
                )

            logger.info(
                "Heartbeat complete: %s status=%s duration=%dms tokens=%d/%d",
                agent_id,
                run.status.value,
                run.duration_ms or 0,
                run.input_tokens,
                run.output_tokens,
            )

        finally:
            release(dedup_key)

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

        Warmup preamble is now handled centrally by runner.execute(),
        so this method just returns the base instruction.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"Current time: {now}\n\n"
            f"You are {config.name} ({config.id}). "
            f"Execute your scheduled tasks as described in your instructions."
        )

    async def stop(self) -> None:
        """Shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Cron scheduler stopped")

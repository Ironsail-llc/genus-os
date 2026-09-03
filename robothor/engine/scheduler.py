"""
Cron Scheduler — APScheduler wrapper for scheduled agent runs.

Loads all YAML manifests on startup, creates CronTrigger jobs.
max_instances=1 prevents concurrent runs of the same agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter  # type: ignore[import-untyped,unused-ignore]

from robothor.engine.admission import admit, complete, register
from robothor.engine.config import (
    ManifestScan,
    load_manifest_dir,
    manifest_to_agent_config,
)
from robothor.engine.dedup import release, try_acquire
from robothor.engine.delivery import _beat_incomplete, _looks_like_mid_thought, deliver
from robothor.engine.manifest_guard import alert_manifest_scan
from robothor.engine.models import AgentConfig, AgentRun, RunStatus, TriggerType
from robothor.engine.task_registry import get_task_registry
from robothor.engine.tracking import (
    delete_stale_schedules,
    get_schedule,
    update_schedule_state,
    upsert_schedule,
)
from robothor.sanitize import sanitize_log

# Circuit breaker: skip agent after this many consecutive errors
CIRCUIT_BREAKER_THRESHOLD = 5

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)

#: Job-id namespaces that are engine-owned infrastructure, not agent schedules
#: derived from a manifest. reconcile must never touch these.
#:
#: This used to be a bare ``startswith("workflow:")`` test, so the interval jobs
#: registered in ``start()`` — ``memory:write-job-sweeper``,
#: ``memory:vault-projection`` — were culled on the first watchdog reconcile
#: after EVERY restart (confirmed live 2026-08-23 13:31:58, five minutes after
#: one). The sweeper ran for at most five minutes per engine lifetime.
#:
#: Prefix-based rather than an id allowlist on purpose: the registration sites
#: are already namespaced, so a future ``memory:*`` job is protected by
#: construction. An allowlist is the thing that rots.
_SYSTEM_JOB_PREFIXES: tuple[str, ...] = ("workflow:", "memory:", "plugin:")


#: Prefix for jobs a plugin contributed. Namespaced into
#: ``_SYSTEM_JOB_PREFIXES`` above so ``reconcile_schedules`` never culls
#: them: reconcile rebuilds the live set from what the manifests declare,
#: and a manifest cannot know about a plugin's job. That is precisely how
#: ``memory:write-job-sweeper`` came to run for at most five minutes per
#: engine lifetime.
PLUGIN_JOB_PREFIX = "plugin:"


def plugin_job_specs() -> dict[str, dict[str, Any]]:
    """Schedulable jobs contributed by installed plugins.

    Each entry needs a 5-field cron expression and a callable. Anything
    malformed is dropped with a warning rather than raised — a third-party
    package must not be able to stop the scheduler from starting, which is
    the rule every other plugin seam here follows.
    """
    try:
        from robothor.plugins import load_plugins

        loaded = load_plugins(reserved_names=set())
    except Exception as exc:  # noqa: BLE001 - plugins must not break scheduling
        logger.warning("Plugin jobs unavailable: %s", exc)
        return {}

    specs: dict[str, dict[str, Any]] = {}
    for name, spec in (loaded.jobs or {}).items():
        if not isinstance(spec, dict):
            logger.warning("Plugin job %r skipped: expected a mapping", name)
            continue
        cron = spec.get("cron")
        func = spec.get("func")
        if not isinstance(cron, str) or not cron.strip():
            logger.warning("Plugin job %r skipped: no cron expression", name)
            continue
        if not callable(func):
            logger.warning("Plugin job %r skipped: 'func' is not callable", name)
            continue
        try:
            CronTrigger.from_crontab(cron)
        except Exception as exc:  # noqa: BLE001 - a bad cron is the plugin's bug
            logger.warning("Plugin job %r skipped: bad cron %r (%s)", name, cron, exc)
            continue
        specs[f"{PLUGIN_JOB_PREFIX}{name}"] = dict(spec)
    return specs


def _is_agent_job(job_id: str) -> bool:
    """True when a scheduler job id came from an agent manifest."""
    return not job_id.startswith(_SYSTEM_JOB_PREFIXES)


def _now_iso() -> str:
    """UTC ISO timestamp for log correlation."""
    return datetime.now(UTC).isoformat()


def _next_fire_time(trigger: CronTrigger) -> str:
    """Next fire time for a cron trigger, as a log-friendly string.

    Never raises: a startup log line is not worth crashing the scheduler for.
    """
    try:
        next_fire = trigger.get_next_fire_time(None, datetime.now(UTC))
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("Could not compute next fire time: %s", e)
        return "unknown"
    return next_fire.isoformat() if next_fire else "never"


def _is_heartbeat_trigger(trigger_detail: str | None) -> bool:
    """True when this run was triggered by a heartbeat cron."""
    return bool(trigger_detail and trigger_detail.startswith("heartbeat:"))


def _catchup_now(timezone: str) -> datetime:
    """Current time in an agent's cron timezone. Isolated as a patch point
    for tests — everything else about catch-up math is deterministic given
    `now`."""
    return datetime.now(ZoneInfo(timezone))


async def _maybe_emit_heartbeat_status_ping(
    agent_config: AgentConfig, run: AgentRun, dedup_key: str
) -> None:
    """Send a one-line health ping to Telegram when the heartbeat would
    otherwise be silent. Only fires when the delivery did NOT produce visible
    output for the operator.

    Visible = delivery_status == "delivered". Anything else (no_output,
    suppressed_trivial, silent, failed:*, timeout with no output) means the
    operator saw nothing — so we ship a fallback status so they know the
    engine is alive.
    """
    from robothor.engine.delivery import get_platform_sender

    delivered = (run.delivery_status or "").startswith("delivered")
    if delivered:
        return

    sender = get_platform_sender("telegram")
    if sender is None:
        logger.debug("No telegram sender registered — skipping status ping for %s", dedup_key)
        return
    chat_id = agent_config.delivery_to
    if not chat_id or "${" in chat_id:
        return

    status = getattr(run.status, "value", str(run.status))
    now_hm = datetime.now(UTC).strftime("%H:%M UTC")
    delivery_status = run.delivery_status or "no_delivery"
    short_err = ""
    if run.error_message:
        short_err = f" — {run.error_message.splitlines()[0][:120]}"
    ping = (
        f"⏱ {now_hm} heartbeat ping ({dedup_key}): "
        f"run={status}, delivery={delivery_status}{short_err}"
    )
    try:
        await sender(chat_id, ping)
        logger.info("Emitted heartbeat status ping for %s: %s", dedup_key, delivery_status)
    except Exception as e:
        logger.warning("Failed to emit heartbeat status ping for %s: %s", dedup_key, e)


def _filter_poisoned_history(history: list[dict[str, Any]], dedup_key: str) -> list[dict[str, Any]]:
    """Drop assistant turns that look like mid-thought fragments.

    The save-gate prevents new poison, but a session may already contain
    bad turns from before the gate landed. Without this filter, the model
    loads the prior fragment as context and continues the stale chain-of-
    thought instead of starting a fresh scan-and-report.

    Only assistant turns are filtered — user turns are kept verbatim so we
    don't desynchronize exchange pairings.
    """
    if not history:
        return history
    kept: list[dict[str, Any]] = []
    dropped = 0
    for msg in history:
        role = msg.get("role") if isinstance(msg, dict) else None
        content = (msg.get("content") or "") if isinstance(msg, dict) else ""
        if role == "assistant" and isinstance(content, str):
            text = content.strip()
            if text and _looks_like_mid_thought(text):
                dropped += 1
                continue
            if text and len(text) < 200 and "\n" not in text:
                dropped += 1
                continue
        kept.append(msg)
    if dropped:
        logger.info(
            "Filtered %d poisoned assistant turn(s) from persistent session cron:%s",
            dropped,
            dedup_key,
        )
    return kept


def _persistent_save_skip_reason(run: AgentRun) -> str | None:
    """Return a reason-string if this run should NOT be saved to the persistent
    session, or None if it's safe to persist.

    Degenerate outputs (mid-thought fragments, budget-capped runs, timeouts,
    run-level errors) poison the next heartbeat by making the model continue
    the stale chain-of-thought. Gate at persistence, not just at delivery.
    """
    if run.status != RunStatus.COMPLETED:
        return f"status={getattr(run.status, 'value', run.status)}"
    if getattr(run, "budget_exhausted", False):
        return "budget_exhausted"
    if _beat_incomplete(run):
        return "beat_incomplete"
    text = (run.output_text or "").strip()
    if text and _looks_like_mid_thought(text):
        return "mid_thought"
    # Short output (< 200 chars) with no newlines looks like a fragment, not a
    # structured beat report. Real reports have headers/bullets and newlines.
    if text and len(text) < 200 and "\n" not in text:
        return "short_no_structure"
    return None


#: Gap between catch-up spawns after downtime. Catching up must not become the
#: next outage: the loop launched every missed agent at once, at a device that
#: serves a handful of concurrent requests. This paces them; the catch_up /
#: stale_after_minutes policy still decides WHETHER each one runs.
CATCH_UP_STAGGER_SECONDS = 5.0


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
        # At boot a broken manifest is strictly worse than at reconcile time:
        # the agent gets no job at all, rather than keeping a stale one. Same
        # dedup key as the watchdog path, so a restart during a known-broken
        # window does not double-page.
        scan = load_manifest_dir(self.config.manifest_dir)
        await alert_manifest_scan(scan, context="scheduler start")
        manifests = list(scan.manifests)
        loaded = 0
        active_schedule_ids: set[str] = set()
        cron_agent_configs: list[AgentConfig] = []

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

            # Register worker cron job if present (drain cycle — symmetric to heartbeat)
            if agent_config.worker and agent_config.worker.cron_expr:
                try:
                    w_trigger = CronTrigger.from_crontab(
                        agent_config.worker.cron_expr,
                        timezone=agent_config.worker.timezone,
                    )
                    w_job_id = f"{agent_config.id}:worker"
                    self.scheduler.add_job(
                        self._run_worker,
                        trigger=w_trigger,
                        args=[agent_config.id],
                        id=w_job_id,
                        name=f"worker:{agent_config.name}",
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=120,
                    )

                    try:
                        upsert_schedule(
                            agent_id=w_job_id,
                            tenant_id=self.config.tenant_id,
                            enabled=True,
                            cron_expr=agent_config.worker.cron_expr,
                            timezone=agent_config.worker.timezone,
                            timeout_seconds=agent_config.worker.timeout_seconds,
                            model_primary=agent_config.model_primary,
                            model_fallbacks=agent_config.model_fallbacks,
                            delivery_mode=agent_config.worker.delivery_mode.value,
                            delivery_channel=agent_config.worker.delivery_channel,
                            delivery_to=agent_config.worker.delivery_to,
                            session_target=agent_config.worker.session_target,
                        )
                        active_schedule_ids.add(w_job_id)
                    except Exception as e:
                        logger.warning(
                            "Failed to upsert worker schedule for %s: %s",
                            agent_config.id,
                            e,
                        )

                    loaded += 1
                    logger.info(
                        "Registered worker for %s: %s",
                        agent_config.id,
                        agent_config.worker.cron_expr,
                    )
                except Exception as e:
                    logger.error(
                        "Invalid worker cron for %s: %s — %s",
                        agent_config.id,
                        agent_config.worker.cron_expr,
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
            cron_agent_configs.append(agent_config)

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
        # Deferred memory writes: reclaim jobs abandoned by a dead process.
        # task_registry's drain budget is 10s (daemon.py) against a ~60s
        # extraction, so a restart strands work in `running` while the agent has
        # already been told the write succeeded. Registered unconditionally —
        # the flag gates whether jobs are *created*, and a sweeper that only
        # exists when the flag is on cannot recover jobs left by a run that had
        # it on.
        try:
            from robothor.memory.write_jobs import sweep_stale_jobs

            self.scheduler.add_job(
                sweep_stale_jobs,
                trigger="interval",
                minutes=5,
                id="memory:write-job-sweeper",
                name="memory:write-job-sweeper",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
        except Exception as e:  # never let this stop the scheduler booting
            logger.warning("could not register memory write-job sweeper: %s", e)

        # Read-only markdown projection for the operator's vault. Flag-gated
        # and off by default: it is on trial with fixed 7-day kill criteria
        # (robothor.memory.projection), so it must not start writing files into
        # someone's vault just because the engine restarted.
        try:
            from robothor.memory.projection import project, projection_enabled

            if projection_enabled():
                self.scheduler.add_job(
                    lambda: project(),
                    trigger="cron",
                    hour=4,
                    minute=15,
                    id="memory:vault-projection",
                    name="memory:vault-projection",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                )
        except Exception as e:
            logger.warning("could not register memory vault projection: %s", e)

        await self._register_workflow_cron_jobs()

        # Jobs contributed by installed plugins. Namespaced under `plugin:`
        # so reconcile leaves them alone; re-run on SIGHUP so a package
        # installed while the engine is up starts on schedule.
        try:
            self.register_plugin_jobs()
        except Exception as e:  # never let a plugin stop the scheduler booting
            logger.warning("could not register plugin jobs: %s", e)

        # Catch up cron occurrences missed while the daemon was down. The
        # in-memory jobstore has no record of these — misfire_grace_time only
        # covers misses within a single process's uptime.
        self._catch_up_missed_runs(cron_agent_configs)

        self.scheduler.start()
        logger.info("Cron scheduler started")

        # Keep running; poll user-authored cron jobs each minute.
        while True:
            await asyncio.sleep(60)
            await self._tick_user_cronjobs()

    # ─── Startup catch-up ──────────────────────────────────────────────

    def _catch_up_missed_runs(self, agent_configs: list[AgentConfig]) -> None:
        """Fire, at most once each, any cron occurrence missed while the
        daemon was down. Best-effort per agent — one bad schedule row or
        cron expression must never stop the others from being checked."""
        spawned = 0
        for agent_config in agent_configs:
            try:
                if self._catch_up_one(
                    agent_config, delay_seconds=spawned * CATCH_UP_STAGGER_SECONDS
                ):
                    spawned += 1
            except Exception as e:
                logger.warning("Cron catch-up check failed for %s: %s", agent_config.id, e)

    def _catch_up_one(self, agent_config: AgentConfig, delay_seconds: float = 0.0) -> bool:
        """Spawn exactly one catch-up run for `agent_config` if the most
        recent scheduled occurrence happened after its last recorded run.

        Returns True when a run was spawned, so the caller advances the
        stagger only for agents that actually launched -- otherwise one
        skipped agent would push every later one out for no reason."""
        schedule = get_schedule(agent_config.id)
        if not schedule:
            return False  # no schedule row — nothing to catch up against

        last_run_at = schedule.get("last_run_at")
        if last_run_at is None:
            return False  # no baseline — avoid a stampede on first-ever start

        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=UTC)

        now = _catchup_now(agent_config.timezone)

        try:
            prev_occurrence = croniter(agent_config.cron_expr, now).get_prev(datetime)
        except Exception as e:
            logger.warning("Cron catch-up: invalid cron expression for %s: %s", agent_config.id, e)
            return False

        if prev_occurrence <= last_run_at:
            return False  # last run already covers the most recent occurrence

        if agent_config.catch_up == "skip_if_stale":
            stale_after = timedelta(minutes=agent_config.stale_after_minutes)
            if now - prev_occurrence > stale_after:
                return False  # missed occurrence is older than the policy allows

        logger.info(
            "Cron catch-up: %s missed %s, running now",
            agent_config.id,
            prev_occurrence.isoformat(),
        )
        get_task_registry().spawn(
            self._run_agent_after(agent_config.id, delay_seconds),
            name=f"cron-catchup:{agent_config.id}",
        )
        return True

    async def _run_agent_after(self, agent_id: str, delay_seconds: float) -> None:
        """Run `agent_id` after `delay_seconds`, so a batch of catch-ups arrives
        paced rather than all at once."""
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self._run_agent(agent_id)

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

        Handles: leadership → dedup → circuit breaker → safety timeout → execute/deliver → track.
        """
        # HA: only the leader replica fires scheduled jobs (no-op when HA off).
        from robothor.engine.leader import is_leader

        if not is_leader():
            logger.debug("Skipping scheduled %s — not the leader replica", agent_id)
            return
        if not await try_acquire(dedup_key):
            # Bumped to warning to make hour-boundary contention visible.
            # The noon-storm investigation (2026-05) needs to know when a
            # new fire arrives while the previous run is still active.
            logger.warning(
                "Cron skipped: %s already running (new fire at %s)",
                dedup_key,
                _now_iso(),
            )
            return

        try:
            logger.info("Cron trigger: running %s (key=%s)", agent_id, dedup_key)

            # Circuit breaker: skip after too many consecutive errors
            if self._circuit_breaker_tripped(dedup_key, agent_config):
                return

            if not admit(agent_id, agent_config, self.config):
                return
            # No scheduler-level wall-clock cap. When the agent has an
            # explicit timeout_seconds > 0 the runner's asyncio.timeout
            # handles it; otherwise the run goes until completion. A
            # truly hung run (dead HTTP socket with no progress) would be
            # caught by the progress-based stall watchdog if the operator
            # opts in.
            try:
                register(dedup_key, agent_id)  # released in the finally below
                if agent_config.timeout_seconds > 0:
                    safety_timeout = agent_config.timeout_seconds + 120
                    async with asyncio.timeout(safety_timeout):
                        await self._execute_and_deliver(
                            agent_id,
                            dedup_key,
                            agent_config,
                            trigger_detail,
                            downstream_agents=downstream_agents,
                        )
                else:
                    await self._execute_and_deliver(
                        agent_id,
                        dedup_key,
                        agent_config,
                        trigger_detail,
                        downstream_agents=downstream_agents,
                    )
            except TimeoutError:
                logger.error(
                    "Scheduler safety timeout hit for %s (agent timeout=%ds)",
                    dedup_key,
                    agent_config.timeout_seconds,
                )
                self._record_timeout(dedup_key)

        finally:
            complete(dedup_key)
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
                    except Exception as e:
                        logger.warning(
                            "Circuit breaker: failed to create paused-agent task for %s: %s",
                            dedup_key,
                            e,
                        )
                    return True
        except Exception as e:
            # A failed breaker read must be visible: otherwise a failing agent
            # keeps firing every tick precisely when the DB is unhealthy
            # (audit 2026-05-29).
            logger.warning("Circuit breaker check failed for %s: %s", dedup_key, e)
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
        except Exception as e:
            # If timeout recording stops, consecutive-error tracking silently
            # stalls and the breaker never trips (audit 2026-05-29).
            logger.warning("Failed to record timeout for circuit breaker (%s): %s", dedup_key, e)

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
                hist_limit = (
                    agent_config.persistent_history_limit
                    if agent_config.persistent_history_limit > 0
                    else 20
                )
                session_data = await asyncio.to_thread(load_session, session_key, limit=hist_limit)
                if session_data and session_data.get("history"):
                    raw_history = session_data["history"]
                    # Drop any mid-thought assistant turns before they reach the
                    # runner — defence against legacy poison persisted before
                    # the save-gate existed.
                    conversation_history = _filter_poisoned_history(raw_history, dedup_key)
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

        # Save session for persistent agents — but only if the output is clean.
        # Mid-thought fragments and budget-capped runs would poison the next beat
        # by making the model continue a stale chain-of-thought. See
        # delivery._beat_incomplete / _looks_like_mid_thought for the heuristics.
        if agent_config.session_target == "persistent" and run.output_text:
            skip_reason = _persistent_save_skip_reason(run)
            if skip_reason:
                logger.info("Skipped persistent-save for cron:%s: %s", dedup_key, skip_reason)
            else:
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

        # Heartbeat status ping — operator is never blind. If the beat didn't
        # surface anything visible to Telegram (timeout, no output, trivial
        # suppression, delivery failure), emit a one-line health signal.
        if _is_heartbeat_trigger(trigger_detail):
            await _maybe_emit_heartbeat_status_ping(agent_config, run, dedup_key)

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

    async def _resurface_followups_phase0(self, tenant_id: str, mode: str) -> None:
        """Phase-0 hook for scout and drain: resurface tasks whose
        follow_up_at has passed. Runs once at the start of each cycle;
        cleared rows become visible to the thread pool / drain queue
        naturally on their next query.
        """
        try:
            from robothor.crm.dal import resurface_due_followups

            resurfaced = await asyncio.to_thread(resurface_due_followups, tenant_id)
            if resurfaced:
                logger.info(
                    "Resurfaced %d task(s) from follow-up before %s cycle: %s",
                    len(resurfaced),
                    mode,
                    resurfaced[:10],
                )
        except Exception as e:
            # Never fail the beat because of the resurface hook.
            logger.warning("resurface_due_followups failed in %s phase-0: %s", mode, e)

    async def _run_heartbeat(self, agent_id: str) -> None:
        """Execute a heartbeat run for an agent."""
        from robothor.engine.config import load_agent_config

        agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if not agent_config or not agent_config.heartbeat:
            logger.error("Agent config or heartbeat not found for: %s", agent_id)
            return

        # Phase-0: wake any tasks whose follow_up_at has passed so the
        # thread pool + list_tasks queries see them on this beat.
        await self._resurface_followups_phase0(self.config.tenant_id, "heartbeat")

        override_config = _build_heartbeat_config(agent_config)

        await self._run_scheduled(
            agent_id,
            f"{agent_id}:heartbeat",
            override_config,
            f"heartbeat:{agent_config.heartbeat.cron_expr}",
        )

    async def _run_worker(self, agent_id: str) -> None:
        """Execute a drain/worker run for an agent.

        Symmetric to _run_heartbeat but with the worker's override config.
        Uses `{agent_id}:worker` dedup key so it never collides with the
        heartbeat or an interactive session.
        """
        from robothor.engine.config import load_agent_config

        agent_config = load_agent_config(agent_id, self.config.manifest_dir)
        if not agent_config or not agent_config.worker:
            logger.debug("Agent config or worker not found for: %s", agent_id)
            return

        # Phase-0: wake any snoozing tasks whose follow_up_at has passed.
        await self._resurface_followups_phase0(self.config.tenant_id, "worker")

        override_config = _build_worker_config(agent_config)

        await self._run_scheduled(
            agent_id,
            f"{agent_id}:worker",
            override_config,
            f"worker:{agent_config.worker.cron_expr}",
        )

    async def trigger_channel_event(
        self,
        tenant_id: str,
        chat_id: str,
        agents: list[str],
        run_ids: list[str],
    ) -> None:
        """Wake main for a channel surface review (Phase 3 of the channel bus).

        Called by ``WakeDebouncer._fire_after_delay`` after the 15s debounce
        window closes on a burst of fleet deliveries. Uses a distinct dedup
        key so it can't collide with main's heartbeat or a user-interactive
        turn that happens to be in flight.
        """
        from robothor.engine.config import load_agent_config

        agent_config = load_agent_config("main", self.config.manifest_dir)
        if agent_config is None:
            logger.warning("trigger_channel_event: main config not found")
            return
        if agent_config.channel_bus is None or not agent_config.channel_bus.wake_on_surface:
            logger.debug("trigger_channel_event: wake_on_surface disabled, skipping")
            return

        dedup_key = "main:channel_wake"
        if not await try_acquire(dedup_key):
            logger.info("channel_wake skipped: %s already running", dedup_key)
            return

        try:
            preamble = _build_channel_wake_preamble(agents, run_ids)

            # Load main's canonical session so the wake run sees every fleet
            # surface that was dual-written (plus its own prior turns).
            conversation_history = None
            try:
                from robothor.engine.chat import get_main_session_key
                from robothor.engine.chat_store import load_session

                session_key = get_main_session_key()
                hist_limit = (
                    agent_config.channel_bus.wake_preamble_history_lines * 4
                    if agent_config.channel_bus
                    else 40
                )
                session_data = await asyncio.to_thread(
                    load_session, session_key, limit=hist_limit, tenant_id=tenant_id
                )
                if session_data and session_data.get("history"):
                    conversation_history = session_data["history"]
            except Exception as e:
                logger.debug("channel_wake: failed to load main session: %s", e)

            trigger_detail = f"channel_event:{chat_id}:batch={len(run_ids)}"
            try:
                run = await self.runner.execute(
                    agent_id="main",
                    message=preamble,
                    trigger_type=TriggerType.CHANNEL_EVENT,
                    trigger_detail=trigger_detail,
                    agent_config=agent_config,
                    conversation_history=conversation_history,
                    tenant_id=tenant_id,
                )
                logger.info(
                    "channel_wake complete: status=%s agents=%s",
                    run.status.value if run else "?",
                    ",".join(agents),
                )
                if run is not None:
                    from robothor.engine.delivery import deliver

                    await deliver(agent_config, run)
            except Exception as e:
                logger.warning("channel_wake execute failed: %s", e)
        finally:
            await release(dedup_key)

    async def _register_workflow_cron_jobs(self) -> int:
        """Register one APScheduler job per cron-triggered workflow.

        Every workflow that declares a cron trigger must come out of this with
        a job. A workflow that does not has *no* trigger at all — it never runs
        again, and the only prior evidence was the gap between two aggregate
        log lines ("Loaded 5 workflows" vs "Loaded 4 workflow cron jobs"),
        which named nothing and reached nobody.

        So: one INFO line per registered job (id, cron, next fire time), a
        parity check that names every workflow whose cron did not register,
        and a warning-level alert so the miss reaches the operator instead of
        dying in the log. A failure on one workflow never skips the rest.

        Returns:
            The number of workflow cron jobs successfully registered.
        """
        if not self.workflow_engine:
            return 0

        try:
            cron_workflows = list(self.workflow_engine.get_workflows_for_cron())
        except Exception as e:
            logger.error("Could not enumerate cron-triggered workflows: %s", e)
            await self._alert_workflow_cron_failure(
                "Workflow cron registration skipped entirely",
                f"No workflow cron job was registered — enumeration failed: {e}",
                {"error": str(e)},
            )
            return 0

        expected: dict[str, str] = {}
        registered: set[str] = set()
        failures: dict[str, str] = {}

        for wf, wf_trigger in cron_workflows:
            expected[wf.id] = wf_trigger.cron
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
            except Exception as e:
                failures[wf.id] = f"{type(e).__name__}: {e}"
                logger.error(
                    "Workflow cron registration FAILED for %s (cron=%s tz=%s): %s: %s",
                    sanitize_log(wf.id),
                    sanitize_log(wf_trigger.cron),
                    sanitize_log(wf_trigger.timezone),
                    type(e).__name__,
                    e,
                )
                continue

            registered.add(wf.id)
            logger.info(
                "Registered workflow cron: workflow=%s cron=%s tz=%s next_fire=%s",
                sanitize_log(wf.id),
                sanitize_log(wf_trigger.cron),
                sanitize_log(wf_trigger.timezone),
                _next_fire_time(wf_cron_trigger),
            )

        logger.info(
            "Loaded %d workflow cron jobs from %d cron-triggered workflow(s)",
            len(registered),
            len(expected),
        )

        # Parity: declared-with-a-cron vs actually-registered. `missing` lost
        # its only trigger and will never fire again. `degraded` registered one
        # trigger but lost another (the job id is per-workflow, so a second
        # cron trigger collides) — a lost run window either way.
        missing = sorted(set(expected) - registered)
        degraded = sorted(set(failures) - set(missing))
        if not missing and not degraded:
            return len(registered)

        detail = "; ".join(
            f"{sanitize_log(wf_id)} (cron={sanitize_log(expected[wf_id])}): "
            f"{failures.get(wf_id, 'no registration attempt recorded')}"
            for wf_id in missing + degraded
        )
        logger.error(
            "Workflow cron parity check FAILED (%d of %d cron-triggered "
            "workflow(s) registered): %d with NO trigger %s, %d missing a "
            "trigger %s — %s",
            len(registered),
            len(expected),
            len(missing),
            [sanitize_log(w) for w in missing],
            len(degraded),
            [sanitize_log(w) for w in degraded],
            detail,
        )
        safe_missing = [sanitize_log(wf_id) for wf_id in missing]
        safe_degraded = [sanitize_log(wf_id) for wf_id in degraded]
        await self._alert_workflow_cron_failure(
            f"Workflow cron registration incomplete: "
            f"{len(missing) + len(degraded)} workflow(s) affected",
            (
                f"{len(registered)} of {len(expected)} cron-triggered workflow(s) "
                f"registered.\n"
                f"No trigger at all (will never fire): "
                f"{', '.join(safe_missing) or 'none'}\n"
                f"Lost at least one trigger: {', '.join(safe_degraded) or 'none'}\n"
                f"{detail}"
            ),
            {
                "missing_workflows": safe_missing,
                "degraded_workflows": safe_degraded,
                "registered": len(registered),
                "expected": len(expected),
            },
        )

        return len(registered)

    async def _alert_workflow_cron_failure(
        self, title: str, body: str, metadata: dict[str, Any]
    ) -> None:
        """Raise a warning-level alert so the operator sees it (never fatal).

        A dropped workflow cron used to live and die in the log. Routing it
        through ``alerts.alert`` puts it in the operator's digest instead.
        """
        try:
            from robothor.engine.alerts import alert

            await alert("warning", title, body, metadata=metadata)
        except Exception as e:  # an alert failure must not stop the scheduler booting
            logger.error("Failed to alert workflow cron failure (%s): %s", title, e)

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
                user_id=f"service:workflow:{workflow_id}",
                user_role="service",
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

    async def _tick_user_cronjobs(self) -> None:
        """Fire any due user-authored cron jobs and advance their schedules.

        Poll-based (not APScheduler-registered) so registering a job never
        churns the live job registry. Best-effort: a DB hiccup is logged, not
        fatal to the scheduler loop.
        """
        import json

        try:
            from robothor.engine.user_cron import (
                compute_next_run,
                list_due_cronjobs,
                mark_cronjob_fired,
            )

            now = datetime.now(UTC)
            due = await asyncio.to_thread(list_due_cronjobs, self.config.tenant_id, now)
            for job in due:
                job_id = job["job_id"]
                # Per-job isolation: a single malformed job (bad cron payload, DB
                # error advancing its schedule) must not abort the whole tick and
                # starve every other due job.
                try:
                    payload = job.get("schedule_payload") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    next_run = compute_next_run(payload, now)
                    fire_count = (job.get("fire_count") or 0) + 1
                    max_fires = job.get("max_fires")
                    disable = next_run is None or (
                        max_fires is not None and fire_count >= max_fires
                    )
                    # Mark-before-fire: advance the schedule BEFORE launching the
                    # run. If the mark write fails we skip launching this cycle,
                    # so the job can never fire every tick with an unadvanced
                    # next_run_at (duplicate execution).
                    await asyncio.to_thread(
                        mark_cronjob_fired, job_id, next_run_at=next_run, disable=disable
                    )
                    # Gate the fire behind the per-agent dedup lock, like every
                    # other execution path — user_cron was the only one bypassing
                    # it, letting two ticks/replicas double-run the same agent.
                    asyncio.create_task(
                        self._run_user_cronjob(job["agent_id"], job["prompt"], job_id)
                    )
                except Exception as e:
                    logger.warning("user_cron job %s failed to fire: %s", job_id, e)
        except Exception as e:
            logger.debug("user_cron tick error: %s", e)

    async def _run_user_cronjob(self, agent_id: str, prompt: str, job_id: str) -> None:
        """Execute a user-cron fire under the per-agent dedup lock."""
        if not await try_acquire(agent_id):
            logger.warning("user_cron %s skipped: agent %s already running", job_id, agent_id)
            return
        try:
            await self.runner.execute(
                agent_id=agent_id,
                message=prompt,
                trigger_type=TriggerType.CRON,
                trigger_detail=f"user_cron:{job_id}",
            )
        finally:
            await release(agent_id)

    def _reconcile_from_scan(self, scan: ManifestScan) -> list[str]:
        """Prune schedules that no longer have a manifest — from a CLEAN scan only.

        The interlock at the top is the whole point. On 2026-08-23 a YAML typo
        made main.yaml unparseable; the loader dropped it, this function could
        not tell "broken" from "deleted", and it DELETED main's heartbeat and
        worker schedules five minutes later. The operator got silence for
        3h48m.

        Refusing to prune, rather than pruning and then paging, because:

        * The delete is one-way. ``agent_schedules`` has no tombstone and no
          last_seen_at, so once ``delete_stale_schedules`` returns the evidence
          is gone. "I just did something irreversible with incomplete
          information" is not a useful page.
        * The costs are wildly asymmetric. Pruning a live agent means total
          silence. NOT pruning a deleted one means a stale row and an orphan
          job that logs "Agent config not found" and fails — noisy, harmless,
          and self-correcting on the next clean scan.
        * A transient read failure would otherwise empty everything.
          ``delete_stale_schedules`` happens to guard the DB half against an
          empty active_ids; the in-memory loop below never did.

        Accepted cost: deleting an agent while a DIFFERENT manifest is broken
        is not reconciled until the break is fixed. That is correct, and the
        page says so.
        """
        if not scan.clean:
            if not scan.dir_readable:
                logger.error(
                    "Reconcile: manifest directory unreadable — refusing to prune "
                    "any schedule. Nothing was deleted."
                )
            else:
                logger.error(
                    "Reconcile: %d manifest(s) failed to parse (%s) — refusing to "
                    "prune any schedule. Nothing was deleted.",
                    len(scan.failures),
                    ", ".join(f.filename for f in scan.failures),
                )
            return []

        active_ids: set[str] = set()
        for manifest in scan.manifests:
            agent_config = manifest_to_agent_config(manifest)
            if agent_config.cron_expr:
                active_ids.add(agent_config.id)
            if agent_config.heartbeat and agent_config.heartbeat.cron_expr:
                active_ids.add(f"{agent_config.id}:heartbeat")
            if agent_config.worker and agent_config.worker.cron_expr:
                active_ids.add(f"{agent_config.id}:worker")

        # Prune stale DB rows
        pruned: list[str] = []
        if active_ids:
            try:
                pruned = delete_stale_schedules(active_ids, tenant_id=self.config.tenant_id)
            except Exception as e:
                logger.warning("Reconcile: failed to prune stale DB rows: %s", e)

        # Remove orphaned APScheduler in-memory jobs
        for job in self.scheduler.get_jobs():
            if not _is_agent_job(job.id):
                continue
            if job.id not in active_ids:
                logger.info("Reconcile: removing orphaned job %s", job.id)
                job.remove()
                if job.id not in pruned:
                    pruned.append(job.id)

        return pruned

    def register_plugin_jobs(self) -> int:
        """Schedule every job installed plugins contribute; return the count.

        Called at startup and again after a plugin reload, so it is
        idempotent in both directions: an already-scheduled job is replaced
        rather than duplicated, and a job whose plugin has been uninstalled
        is removed. ``replace_existing`` covers the first; the withdrawal
        sweep covers the second, which ``replace_existing`` alone cannot.
        """
        specs = plugin_job_specs()
        wanted = set(specs)

        # Clear every plugin job first, then add the current set. Not just
        # the withdrawn ones, and not `replace_existing` alone: before the
        # scheduler is started APScheduler holds additions as *pending*, and
        # pending jobs are not de-duplicated by `replace_existing`, so the
        # startup call followed by a reload would register each job twice.
        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith(PLUGIN_JOB_PREFIX):
                if job.id not in wanted:
                    logger.info("Plugin job %s withdrawn — its plugin is gone", job.id)
                with contextlib.suppress(Exception):
                    job.remove()

        registered = 0
        for job_id, spec in specs.items():
            try:
                self.scheduler.add_job(
                    spec["func"],
                    trigger=CronTrigger.from_crontab(spec["cron"]),
                    id=job_id,
                    name=job_id,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=spec.get("misfire_grace_time", 300),
                    replace_existing=True,
                )
                registered += 1
            except Exception as exc:  # noqa: BLE001 - one bad job, not all of them
                logger.warning("Plugin job %s could not be scheduled: %s", job_id, exc)
        if registered:
            logger.info("Registered %d plugin job(s)", registered)
        return registered

    def reconcile_schedules(self) -> list[str]:
        """Reconcile DB + in-memory jobs against current manifests.

        Synchronous and unchanged in signature so existing callers keep working.
        It cannot page — see :meth:`reconcile` for the alerting wrapper the
        watchdog uses. Either way, a dirty scan prunes nothing.
        """
        return self._reconcile_from_scan(load_manifest_dir(self.config.manifest_dir))

    async def reconcile(self) -> list[str]:
        """Reconcile, and page the operator when a manifest cannot be read.

        The watchdog's entry point. Reading manifests and the DB prune are both
        blocking, so they run in the executor (rule 10: no ``asyncio.run`` in
        engine internals).
        """
        loop = asyncio.get_running_loop()
        scan = await loop.run_in_executor(None, load_manifest_dir, self.config.manifest_dir)
        # Unconditionally: the guard owns BOTH transitions. A clean scan is how
        # it clears its dedup key and sends the recovery notice. Gating this on
        # `not scan.clean` left the guard armed forever after a fix, so the next
        # breakage of the same file would be swallowed by a stale floor.
        await alert_manifest_scan(scan, context="watchdog reconcile")
        return await loop.run_in_executor(None, self._reconcile_from_scan, scan)

    async def stop(self) -> None:
        """Shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Cron scheduler stopped")


def _build_channel_wake_preamble(agents: list[str], run_ids: list[str]) -> str:
    """Compose the wake prompt handed to main during a CHANNEL_EVENT run.

    Brief and directive: main should audit what just landed in the channel
    (visible as recent assistant turns in its own session history) and
    decide whether to respond, consolidate, or stay silent. Short preamble
    keeps the run cheap — main already sees the full content upstream.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if agents:
        agents_line = ", ".join(f"@{a}" for a in agents)
    else:
        agents_line = "(none listed — check session history)"
    batch_note = (
        f"{len(run_ids)} run{'s' if len(run_ids) != 1 else ''} in this batch"
        if run_ids
        else "debounce-only wake (no new run ids)"
    )
    return (
        f"Channel surface review — {now}\n\n"
        f"Since your last turn, {agents_line} posted to the channel ({batch_note}).\n"
        f"Their messages are already in your session history above, labeled with "
        f"[@agent-id] prefixes. Review them and decide:\n"
        f"- respond in the channel if operator visibility is needed,\n"
        f"- condense/consolidate if the fleet is repeating itself,\n"
        f"- stay silent if the messages speak for themselves.\n\n"
        f"Do not duplicate content the fleet already delivered. If nothing "
        f"warrants a reply, output a single line such as 'noted' or stay "
        f"silent — trivial outputs are suppressed by delivery."
    )


def _build_worker_config(agent_config: AgentConfig) -> AgentConfig:
    """Build override AgentConfig for drain/worker runs.

    Mirrors _build_heartbeat_config but for the drain cycle: full tool
    inheritance by default (worker executes work; it needs spawn_agent,
    gws_*, exec, etc.). Set `worker.tools_allowed` in the manifest to
    restrict further if needed.
    """
    w = agent_config.worker
    assert w is not None

    warmup_memory_blocks = w.warmup_memory_blocks or agent_config.warmup_memory_blocks
    warmup_context_files = w.warmup_context_files or agent_config.warmup_context_files
    warmup_peer_agents = w.warmup_peer_agents or agent_config.warmup_peer_agents

    max_cost_usd = w.cost_budget_usd or agent_config.max_cost_usd
    hard_budget = w.cost_budget_usd > 0 or agent_config.hard_budget

    tools_allowed = w.tools_allowed or agent_config.tools_allowed

    return AgentConfig(
        # SECURITY POSTURE — carried over verbatim. The worker override exists to
        # change budget and warmup for a drain cycle; it must never quietly relax
        # what the agent is allowed to do. These were previously omitted, so the
        # dataclass defaults applied and every worker run executed with ZERO
        # guardrails and sandbox="local" (2026-07-13).
        guardrails=agent_config.guardrails,
        guardrails_opt_out=agent_config.guardrails_opt_out,
        sandbox=agent_config.sandbox,
        exec_allowlist=agent_config.exec_allowlist,
        write_path_allowlist=agent_config.write_path_allowlist,
        human_approval_tools=agent_config.human_approval_tools,
        human_approval_timeout=agent_config.human_approval_timeout,
        id=agent_config.id,
        name=agent_config.name,
        description=agent_config.description,
        model_primary=agent_config.model_primary,
        model_fallbacks=agent_config.model_fallbacks,
        temperature=agent_config.temperature,
        cron_expr=w.cron_expr,
        timezone=w.timezone,
        timeout_seconds=w.timeout_seconds,
        max_iterations=w.max_iterations,
        safety_cap=w.safety_cap,
        session_target=w.session_target,
        delivery_mode=w.delivery_mode,
        delivery_channel=w.delivery_channel,
        delivery_to=w.delivery_to,
        tools_allowed=tools_allowed,
        tools_denied=agent_config.tools_denied,
        instruction_file=w.instruction_file,
        bootstrap_files=w.bootstrap_files,
        reports_to=agent_config.reports_to,
        department=agent_config.department,
        task_protocol=agent_config.task_protocol,
        review_workflow=agent_config.review_workflow,
        notification_inbox=agent_config.notification_inbox,
        shared_working_state=agent_config.shared_working_state,
        warmup_memory_blocks=warmup_memory_blocks,
        warmup_context_files=warmup_context_files,
        warmup_peer_agents=warmup_peer_agents,
        stall_timeout_seconds=w.stall_timeout_seconds,
        early_stall_timeout_seconds=w.early_stall_timeout_seconds,
        persistent_history_limit=w.persistent_history_limit,
        error_feedback=agent_config.error_feedback,
        max_cost_usd=max_cost_usd,
        hard_budget=hard_budget,
        can_spawn_agents=agent_config.can_spawn_agents,
        max_nesting_depth=agent_config.max_nesting_depth,
        sub_agent_max_iterations=agent_config.sub_agent_max_iterations,
        sub_agent_timeout_seconds=agent_config.sub_agent_timeout_seconds,
        # Drain runs do NOT override task authorship — filed tasks stay
        # attributed to 'main' (the agent identity).
        task_author_override="",
    )


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

    # Model override: use heartbeat's model if set, else inherit from parent.
    beat_model_primary = hb.model_primary or agent_config.model_primary
    beat_model_fallbacks = hb.model_fallbacks or agent_config.model_fallbacks

    return AgentConfig(
        id=agent_config.id,
        name=agent_config.name,
        description=agent_config.description,
        model_primary=beat_model_primary,
        model_fallbacks=beat_model_fallbacks,
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
        tools_allowed=(hb.tools_allowed or agent_config.tools_allowed),
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
        early_stall_timeout_seconds=hb.early_stall_timeout_seconds,
        persistent_history_limit=hb.persistent_history_limit,
        error_feedback=agent_config.error_feedback,
        max_cost_usd=max_cost_usd,
        hard_budget=hard_budget,
        # Sub-agent config inherited from parent
        can_spawn_agents=agent_config.can_spawn_agents,
        max_nesting_depth=agent_config.max_nesting_depth,
        sub_agent_max_iterations=agent_config.sub_agent_max_iterations,
        sub_agent_timeout_seconds=agent_config.sub_agent_timeout_seconds,
        # Scout filings attributed to `hb.task_authorship_agent` (if set)
        # for CRM timeline clarity — agent_id on the run stays 'main'.
        task_author_override=hb.task_authorship_agent,
    )

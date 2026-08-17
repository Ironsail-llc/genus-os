"""
Main daemon entry point — starts all engine subsystems.

Runs as: python -m robothor.engine.daemon

Subsystems:
- Telegram bot (long-polling)
- Cron scheduler (APScheduler)
- Event hooks (Redis Stream consumers)
- Health endpoint (FastAPI on port 18800)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time
from datetime import UTC
from typing import Any

from robothor.engine.config import EngineConfig
from robothor.engine.health import serve_health, validate_engine_auth_configuration
from robothor.engine.hooks import EventHooks
from robothor.engine.runner import AgentRunner
from robothor.engine.scheduler import CronScheduler
from robothor.engine.telegram import TelegramBot
from robothor.engine.workflow import WorkflowEngine

logger = logging.getLogger(__name__)


def _sd_notify(state: str) -> None:
    """Send a notification to systemd via $NOTIFY_SOCKET (sd_notify protocol).

    No-ops silently if NOTIFY_SOCKET is not set or the socket is unreachable.
    Uses stdlib only — no external dependencies.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        # Abstract socket (starts with @) or filesystem path
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(state.encode(), addr)
        finally:
            sock.close()
    except Exception:
        # Best-effort — never crash the daemon for a notification failure
        pass


# Set on daemon startup so the reaper can distinguish runs killed by a daemon
# restart from runs where the runner process itself crashed. ISO8601 string.
_DAEMON_START_TS: str | None = None


def _set_daemon_start_ts() -> None:
    """Record the daemon's start time in-process and in env for reaper use."""
    global _DAEMON_START_TS
    from datetime import datetime

    ts = datetime.now(UTC).isoformat()
    _DAEMON_START_TS = ts
    os.environ["ROBOTHOR_DAEMON_START_TS"] = ts


def classify_reap_reason(
    run_id: str,
    started_at_iso: str,
    daemon_start_ts: str | None,
) -> tuple[str, str]:
    """Classify why a run was reaped. Returns (category, error_message).

    Categories:
        no_steps         — no agent_run_steps rows (likely crash during setup)
        post_llm_crash   — LLM was called; runner died after
        post_tool_crash  — last step was a tool_call / tool_result
        post_error_crash — last step was an 'error' step
        daemon_restart   — run started before current daemon boot
    """
    # daemon_restart wins if we know the daemon booted after this run started
    if daemon_start_ts and started_at_iso and started_at_iso < daemon_start_ts:
        return (
            "daemon_restart",
            f"Reaped by watchdog: likely cancelled by daemon restart at {daemon_start_ts}",
        )

    try:
        from robothor.engine.tracking import list_steps

        steps = list_steps(run_id)
    except Exception as e:
        logger.debug("classify_reap_reason: list_steps failed for %s: %s", run_id, e)
        steps = []

    if not steps:
        return (
            "no_steps",
            "Reaped by watchdog: no steps recorded (likely runner crash during setup)",
        )

    last = steps[-1]
    last_type = str(last.get("step_type") or "").lower()
    last_tool = last.get("tool_name")
    last_err = (last.get("error_message") or "").strip()

    if last_type == "error":
        msg = (
            f"Reaped by watchdog: runner crashed after error step (last_error={last_err[:160]})"
            if last_err
            else "Reaped by watchdog: runner crashed after error step"
        )
        return ("post_error_crash", msg)

    if last_type in ("tool_call", "tool_result"):
        return (
            "post_tool_crash",
            f"Reaped by watchdog: runner crashed after tool {last_tool or 'unknown'} "
            f"(last step_type={last_type})",
        )

    if last_type in ("llm_call", "llm_response"):
        return (
            "post_llm_crash",
            f"Reaped by watchdog: runner crashed after {last_type} (total steps={len(steps)})",
        )

    # Generic fallback — keep the category specific to what we saw
    return (
        "post_llm_crash"
        if any(s.get("step_type") in ("llm_call", "llm_response") for s in steps)
        else "no_steps",
        f"Reaped by watchdog: runner crashed after {last_type or 'unknown'} "
        f"(total steps={len(steps)})",
    )


def _cleanup_stale_runs() -> int:
    """Mark stale 'running' agent_runs as 'timeout' with per-run classification.

    Called on startup and periodically by the watchdog. Instead of applying a
    single hardcoded error_message to every reaped row, this now inspects the
    run's step history to produce a truthful diagnosis (see classify_reap_reason).

    Returns the number of runs cleaned up.
    """
    try:
        from robothor.db.connection import get_connection

        daemon_start_ts = _DAEMON_START_TS or os.environ.get("ROBOTHOR_DAEMON_START_TS")

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, agent_id, started_at "
                "FROM agent_runs "
                "WHERE status='running' AND started_at < NOW() - INTERVAL '30 minutes'"
            )
            stale = cur.fetchall()
            if not stale:
                return 0

            for run_id, agent_id, started_at in stale:
                started_iso = started_at.isoformat() if started_at is not None else ""
                category, message = classify_reap_reason(str(run_id), started_iso, daemon_start_ts)
                cur.execute(
                    "UPDATE agent_runs SET status='timeout', "
                    "completed_at=NOW(), "
                    "duration_ms=EXTRACT(EPOCH FROM (NOW()-started_at))*1000, "
                    "error_message=%s, "
                    "reap_category=%s "
                    "WHERE id=%s AND status='running'",
                    (message, category, run_id),
                )
                logger.warning(
                    "Cleaned up stale run %s (agent=%s, category=%s)",
                    run_id,
                    agent_id,
                    category,
                )

            conn.commit()

            # Release dedup locks for cleaned-up agents
            from robothor.engine.dedup import release_sync

            for row in stale:
                release_sync(row[1])

            return len(stale)
    except Exception as e:
        logger.warning("Stale run cleanup failed: %s", e)
        return 0


async def _start_federation(config: EngineConfig, runner: Any = None) -> Any:
    """Start federation NATS transport if connections exist.

    Returns the NATSManager (connected) or None. Backward-compatible no-op
    when no federation is configured. When ``runner`` is provided, also registers
    an inbound responder per active connection so peer federation_query/trigger
    calls are actually answered (not just sendable).
    """
    try:
        from robothor.federation.config import FederationConfig
        from robothor.federation.connections import load_connections
        from robothor.federation.nats import NATSManager

        # Resolve federation config: engine env vars → federation.yaml fallback
        fed_config = FederationConfig.from_env()
        instance_id = config.instance_id or fed_config.instance_id
        nats_url = config.nats_url or (fed_config.nats_url if fed_config.nats_enabled else "")

        if not instance_id:
            return None

        connections = load_connections()
        if not connections:
            logger.debug("Federation: no connections, skipping NATS")
            return None

        if not nats_url:
            logger.info("Federation: %d connections but no NATS URL configured", len(connections))
            return None

        nats_mgr = NATSManager(nats_url)
        connected = await nats_mgr.connect()
        if connected:
            logger.info(
                "Federation: NATS connected, %d connections loaded",
                len(connections),
            )
            # Ensure streams + register an inbound responder for active
            # connections so peer federation_query/trigger calls are answered.
            for conn in connections:
                if conn.state.value == "active":
                    await nats_mgr.ensure_stream(conn.id)
                    if runner is not None:
                        from robothor.engine.federation_responder import make_command_handler

                        await nats_mgr.serve_requests(conn.id, make_command_handler(conn, runner))
        else:
            logger.warning("Federation: NATS connection failed, federation disabled")
            return None

        return nats_mgr
    except Exception as e:
        logger.warning("Federation startup failed (non-fatal): %s", e)
        return None


async def _maybe_run_alert_selftest() -> None:
    """Optional live probe of the alert delivery path (env-gated).

    ROBOTHOR_ALERT_SELFTEST=1 fires one info alert so the alert() ->
    send_fn(chat_id, text) path can be verified end-to-end on a running
    box — a code-free way to confirm the fixed sender arity actually
    reaches the operator, without waiting for a real incident to trip it.
    Best-effort: never raises into the caller.
    """
    if os.environ.get("ROBOTHOR_ALERT_SELFTEST") != "1":
        return
    try:
        from robothor.engine.alerts import alert

        await alert(
            "info",
            "Alert delivery self-test",
            "Engine startup self-test — the alert() delivery path is live.",
        )
    except Exception as e:
        logger.debug("Alert delivery self-test failed: %s", e)


def _log_task_results(done: set[asyncio.Task[Any]]) -> None:
    """Log the outcome of each finished top-level subsystem task.

    Mirrors task_registry.py's ``_on_done``: a task that ended cancelled
    must be skipped BEFORE calling ``.exception()`` — on a cancelled task
    that call raises ``CancelledError`` (a BaseException), which sails past
    ``except Exception`` in ``run()``'s top-level handler and kills the
    daemon outright. This is the containment layer for an orphaned stall
    watchdog cancelling the wrong task (Aug 5/9 crashes); the sleep-inside-
    try fixes on the curiosity/curator loops are the primary fix, this is
    the backstop for any other task that ends up cancelled.
    """
    for task in done:
        if task.cancelled():
            logger.error("Task %s was cancelled externally", task.get_name())
            continue
        if task.exception():
            logger.error("Task %s failed: %s", task.get_name(), task.exception())
        else:
            logger.info("Task %s completed", task.get_name())


async def main() -> None:
    """Start all engine subsystems."""
    # Reject unsafe production authentication before touching the database,
    # loading agents, or starting any background subsystem. The Engine verifies
    # Bridge-issued tokens but is not an SSO exchange authority, so it must not
    # require the Bridge's IdP credentials.
    validate_engine_auth_configuration()

    # Configure structured logging via structlog
    # Wraps stdlib logging so existing logging.getLogger() calls get structured output
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer()
        if os.environ.get("ROBOTHOR_LOG_FORMAT") != "json"
        else structlog.processors.JSONRenderer(),
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    logger.info("Starting Genus OS Agent Engine...")

    # Record daemon boot time before reaping so runs started before this boot
    # can be classified as 'daemon_restart' rather than 'post_llm_crash'.
    _set_daemon_start_ts()

    # Clean up stale runs from previous crash/restart
    cleaned = await asyncio.to_thread(_cleanup_stale_runs)
    if cleaned:
        logger.info("Startup: cleaned %d stale agent runs", cleaned)

    # Link the operator's CRM row to tenant_users.person_id (idempotent).
    # Driven by ~/.robothor/owner.yaml; no-op if not configured.
    try:
        from robothor.crm.dal import bootstrap_owner_person_links

        link_result = await asyncio.to_thread(bootstrap_owner_person_links)
        if link_result.get("linked"):
            logger.info(
                "Operator identity: linked tenant=%s → person_id=%s%s",
                link_result.get("tenant_id"),
                link_result.get("person_id"),
                " (created new person)" if link_result.get("created_person") else "",
            )
    except Exception:
        logger.exception("bootstrap_owner_person_links failed (non-fatal)")

    # Load config
    config = EngineConfig.from_env()
    logger.info("Tenant: %s", config.tenant_id)
    logger.info("Workspace: %s", config.workspace)
    logger.info("Health port: %d", config.port)
    logger.info("Telegram bot: %s", "configured" if config.bot_token else "disabled")

    # Create subsystems
    runner = AgentRunner(config)

    # Initialize fleet pool for admission control
    from robothor.engine.pool import init_fleet_pool

    init_fleet_pool(
        max_concurrent=config.max_concurrent_agents,
        hourly_cost_cap_usd=config.hourly_cost_cap_usd,
    )
    logger.info(
        "Fleet pool: max_concurrent=%d, hourly_cost_cap=$%.2f",
        config.max_concurrent_agents,
        config.hourly_cost_cap_usd,
    )

    # Initialize inter-agent messaging + teams so the send_agent_message /
    # receive_agent_messages / create_team / team_scratchpad_* tools work
    # (their handlers no-op with "not initialized" until these are called).
    from robothor.engine.messaging import init_messenger
    from robothor.engine.teams import init_team_manager

    init_messenger()
    init_team_manager()
    logger.info("Inter-agent messaging + teams initialized")

    # HA: scheduler leadership elector (no-op / always-leader when HA off).
    from robothor.engine.leader import LeaderElector, ha_leader_enabled, set_elector

    _elector = LeaderElector()
    set_elector(_elector)
    if ha_leader_enabled():
        logger.info("HA leader election enabled")

    # Initialize lifecycle hook registry
    from robothor.engine.hook_registry import (
        init_hook_registry,
        load_global_hooks,
        load_hooks_from_manifest,
    )

    hook_registry = init_hook_registry()

    # Register channel-bus surface handler so POST_DELIVERY dual-writes fleet
    # outputs into main's session for supervisor visibility. Must be registered
    # before load_global_hooks so the YAML entry can bind to it by name.
    from robothor.engine.channel_bus import on_post_delivery as _channel_bus_surface

    hook_registry.register_python_handler("channel_bus.surface", _channel_bus_surface)

    # Channel-bus main busy-state hooks: AGENT_START/AGENT_END on the main
    # agent flip a flag in the WakeDebouncer so wakes deferred during a
    # concurrent interactive turn can re-fire cleanly when main goes idle.
    async def _channel_bus_on_main_start(ctx: Any) -> Any:
        from robothor.engine.channel_bus import get_debouncer
        from robothor.engine.hook_registry import HookResult

        if ctx.agent_id == "main":
            deb = get_debouncer()
            if deb:
                deb.mark_main_started()
        return HookResult()

    async def _channel_bus_on_main_end(ctx: Any) -> Any:
        from robothor.engine.channel_bus import get_debouncer
        from robothor.engine.hook_registry import HookResult

        if ctx.agent_id == "main":
            deb = get_debouncer()
            if deb:
                await deb.mark_main_finished()
        return HookResult()

    hook_registry.register_python_handler("channel_bus.main_started", _channel_bus_on_main_start)
    hook_registry.register_python_handler("channel_bus.main_finished", _channel_bus_on_main_end)

    global_hooks = load_global_hooks(config.workspace / "docs" / "hooks")
    if global_hooks:
        hook_registry.register_many(global_hooks)
        logger.info("Loaded %d global lifecycle hooks", len(global_hooks))

    # Load per-agent lifecycle hooks from manifests
    from robothor.engine.config import load_all_manifests

    agent_hook_count = 0
    for manifest in load_all_manifests(config.manifest_dir):
        agent_id = manifest.get("id", "")
        agent_hooks = load_hooks_from_manifest(manifest, agent_id)
        if agent_hooks:
            hook_registry.register_many(agent_hooks)
            agent_hook_count += len(agent_hooks)
    if agent_hook_count:
        logger.info("Loaded %d agent lifecycle hooks", agent_hook_count)

    # Register buddy lifecycle hooks
    from robothor.engine.buddy_hooks import register_buddy_hooks

    register_buddy_hooks(hook_registry)

    # Register runner for sub-agent spawning
    from robothor.engine.tools import set_runner

    set_runner(runner, config)

    workflow_engine = WorkflowEngine(config, runner)
    wf_count = workflow_engine.load_workflows(config.workflow_dir)
    logger.info("Loaded %d workflows", wf_count)

    bot = TelegramBot(config, runner) if config.bot_token else None
    if bot is None:
        logger.warning(
            "ROBOTHOR_TELEGRAM_BOT_TOKEN is empty — Telegram delivery disabled. "
            "Engine API, scheduler, and hooks will still run."
        )
    else:
        # Wire human-approval escalations to the operator's Telegram. Without
        # this, get_permission_manager() returns None and every human_approval
        # escalation auto-approves (see runner fail-closed branch).
        from robothor.engine.permission_escalation import init_permission_manager

        init_permission_manager(bot, config.default_chat_id)
        logger.info("Permission escalation manager wired to Telegram")
    scheduler = CronScheduler(config, runner, workflow_engine=workflow_engine)
    hooks = EventHooks(config, runner, workflow_engine=workflow_engine)

    # Channel-bus debouncer: submits fleet surfaces, fires CHANNEL_EVENT wake
    # on main after the debounce window. Uses main's YAML config for timing.
    try:
        from robothor.engine.channel_bus import WakeDebouncer, set_debouncer
        from robothor.engine.config import load_agent_config as _load_main_cfg

        _main_cfg = _load_main_cfg("main", config.manifest_dir)
        cb_cfg = _main_cfg.channel_bus if _main_cfg else None
        if cb_cfg is not None:
            _debouncer = WakeDebouncer(
                trigger_fn=scheduler.trigger_channel_event,
                debounce_seconds=cb_cfg.wake_debounce_seconds,
                cooldown_seconds=cb_cfg.main_wake_cooldown_seconds,
                rate_limit_per_hour=cb_cfg.per_agent_rate_limit_per_hour,
                enabled=cb_cfg.wake_on_surface,
            )
            set_debouncer(_debouncer)
            logger.info(
                "Channel-bus wake: enabled=%s debounce=%ds cooldown=%ds limit=%d/hr",
                cb_cfg.wake_on_surface,
                cb_cfg.wake_debounce_seconds,
                cb_cfg.main_wake_cooldown_seconds,
                cb_cfg.per_agent_rate_limit_per_hour,
            )
    except Exception as e:
        logger.warning("Channel-bus debouncer init failed (non-fatal): %s", e)

    # Federation — start NATS if connections exist (no-op otherwise)
    nats_mgr = await _start_federation(config, runner=runner)

    # Start all subsystems concurrently
    tasks = [
        asyncio.create_task(scheduler.start(), name="scheduler"),
        asyncio.create_task(hooks.start(), name="hooks"),
        asyncio.create_task(
            serve_health(config, runner=runner, workflow_engine=workflow_engine),
            name="health",
        ),
        asyncio.create_task(_watchdog(config, scheduler), name="watchdog"),
        asyncio.create_task(_autodream_loop(), name="autodream"),
        asyncio.create_task(_curiosity_density_loop(scheduler), name="curiosity-density"),
        asyncio.create_task(_curator_loop(scheduler), name="curator"),
        asyncio.create_task(_extension_watcher_loop(), name="extensions"),
        asyncio.create_task(_elector.run(), name="leader"),
    ]
    if bot is not None:
        tasks.insert(0, asyncio.create_task(bot.start_polling(), name="telegram"))

    # Slack channel (Socket Mode) — env-gated; start() self-gates on the tokens.
    slack_bot = None
    if os.environ.get("ROBOTHOR_SLACK_BOT_TOKEN") and os.environ.get("ROBOTHOR_SLACK_APP_TOKEN"):
        from robothor.engine.slack import SlackBot

        slack_bot = SlackBot(runner, config)
        tasks.append(asyncio.create_task(slack_bot.start(), name="slack"))
        logger.info("Slack channel enabled (Socket Mode)")

    logger.info("All subsystems started")
    _sd_notify("READY=1")

    # Startup announcement (best-effort)
    try:
        from robothor.engine.config import load_all_manifests, manifest_to_agent_config

        manifests = load_all_manifests(config.manifest_dir)
        scheduled = sum(1 for m in manifests if manifest_to_agent_config(m).cron_expr)
        from robothor.engine.delivery import get_telegram_sender

        sender = get_telegram_sender()
        if sender and config.default_chat_id:
            await sender(
                config.default_chat_id,
                f"*Engine Online*\n\n"
                f"{scheduled} scheduled agents loaded.\n"
                f"Port {config.port} | Tenant {config.tenant_id}",
            )
    except Exception as e:
        logger.debug("Startup announcement failed: %s", e)

    # Alert delivery self-test (env-gated, best-effort) — see docstring.
    await _maybe_run_alert_selftest()

    # Wait for any task to complete (aiogram handles SIGTERM and stops polling,
    # which completes the telegram task — that's our shutdown trigger)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Log what finished
    _log_task_results(done)

    logger.info("Shutting down subsystems...")

    # Shutdown announcement (best-effort)
    try:
        from robothor.engine.dedup import running_agents
        from robothor.engine.delivery import get_telegram_sender

        active = running_agents()
        sender = get_telegram_sender()
        if sender and config.default_chat_id:
            active_str = ", ".join(active) if active else "none"
            await sender(
                config.default_chat_id,
                f"*Engine Shutting Down*\n\nActive agents: {active_str}",
            )
    except Exception as e:
        logger.debug("Shutdown announcement failed: %s", e)

    # Disconnect federation NATS (if connected)
    if nats_mgr is not None:
        try:
            await nats_mgr.disconnect()
            logger.info("Federation: NATS disconnected")
        except Exception as e:
            logger.debug("Federation NATS disconnect failed: %s", e)

    # Drain tracked background tasks before stopping subsystems
    from robothor.engine.task_registry import get_task_registry

    await get_task_registry().drain()

    await scheduler.stop()
    await hooks.stop()
    if bot is not None:
        await bot.stop()
    if slack_bot is not None:
        try:
            await slack_bot.stop()
        except Exception as e:
            logger.debug("Slack bot stop failed: %s", e)

    # Release the leadership lease explicitly before cancelling its task —
    # otherwise the lease sits until its TTL expires and the whole fleet is
    # leaderless (no cron/heartbeat) for up to that window.
    try:
        await _elector.stop()
    except Exception as e:
        logger.debug("Leader elector stop failed: %s", e)

    # Cancel remaining tasks
    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)
    logger.info("Engine stopped")


def _record_watchdog_event(event_type: str, detail: str) -> None:
    """Append a watchdog event to the watchdog_log memory block."""
    try:
        from datetime import UTC, datetime

        from robothor.memory.blocks import read_block, write_block

        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"[{ts}] {event_type}: {detail}"
        existing = read_block("watchdog_log")
        content = existing.get("content", "") if "error" not in existing else ""
        lines = [ln for ln in content.strip().splitlines() if ln] if content else []
        lines.append(entry)
        lines = lines[-50:]
        write_block("watchdog_log", "\n".join(lines))
    except Exception:
        logger.debug("Watchdog event recording failed", exc_info=True)


async def _watchdog(config: EngineConfig, scheduler: CronScheduler) -> None:
    """Subsystem watchdog — pings PostgreSQL and Redis every 30s, notifies systemd, cleans stale sessions daily."""
    global _autodream_stale_alerted  # noqa: PLW0603

    pg_failures = 0
    redis_failures = 0
    tick_count = 0

    while True:
        await asyncio.sleep(30)
        tick_count += 1
        _sd_notify("WATCHDOG=1")

        # Ping PostgreSQL (with timeout to prevent event loop blocking)
        try:
            loop = asyncio.get_running_loop()

            def _pg_ping() -> None:
                from robothor.db.connection import get_connection

                with get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")

            await asyncio.wait_for(loop.run_in_executor(None, _pg_ping), timeout=10.0)
            pg_failures = 0
        except Exception as e:
            pg_failures += 1
            logger.warning("Watchdog: PostgreSQL ping failed (%d): %s", pg_failures, e)
            if pg_failures in (1, 3, 10) or (pg_failures > 10 and pg_failures % 100 == 0):
                _record_watchdog_event("pg_failure", f"consecutive={pg_failures}: {e}")

        # Ping Redis (with timeout to prevent event loop blocking)
        try:
            loop = asyncio.get_running_loop()

            def _redis_ping() -> None:
                import redis as _redis

                from robothor.config import get_config

                cfg = get_config()
                r = _redis.Redis(
                    host=cfg.redis.host,
                    port=cfg.redis.port,
                    db=cfg.redis.db,
                    password=cfg.redis.password or None,
                )
                r.ping()
                r.close()

            await asyncio.wait_for(loop.run_in_executor(None, _redis_ping), timeout=10.0)
            redis_failures = 0
        except Exception as e:
            redis_failures += 1
            logger.warning("Watchdog: Redis ping failed (%d): %s", redis_failures, e)
            if redis_failures in (1, 3, 10) or (redis_failures > 10 and redis_failures % 100 == 0):
                _record_watchdog_event("redis_failure", f"consecutive={redis_failures}: {e}")

        # Schedule reconciliation (every 10 ticks = 5 minutes)
        if tick_count % 10 == 0:
            try:
                loop = asyncio.get_running_loop()
                pruned = await loop.run_in_executor(None, scheduler.reconcile_schedules)
                if pruned:
                    logger.info("Watchdog: reconciled schedules, pruned: %s", pruned)
            except Exception as e:
                logger.warning("Watchdog: schedule reconciliation failed: %s", e)

        # Zombie run reaper (every 40 ticks = 20 minutes)
        if tick_count % 40 == 0:
            try:
                loop = asyncio.get_running_loop()
                reaped = await loop.run_in_executor(None, _cleanup_stale_runs)
                if reaped:
                    logger.warning("Watchdog: reaped %d zombie agent runs", reaped)
            except Exception as e:
                logger.warning("Watchdog: zombie reaper failed: %s", e)

        # Failure-mode detectors — read-only, alerts only, never kills runs.
        # Disabled via ROBOTHOR_DETECTORS_ENABLED=0.
        # Runaway burn: every 4 ticks = 2 minutes
        if tick_count % 4 == 0:
            try:
                from robothor.engine.detectors import runaway_burn_detector

                fired = await runaway_burn_detector()
                if fired:
                    logger.info("Detectors: %d runaway-burn alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: runaway_burn check failed: %s", e)

        # Repeat errors + tool degradation: every 10 ticks = 5 minutes
        if tick_count % 10 == 0:
            try:
                from robothor.engine.detectors import (
                    repeat_error_detector,
                    tool_degradation_detector,
                )

                fired = await repeat_error_detector(tenant_id=config.tenant_id)
                if fired:
                    logger.info("Detectors: %d repeat-error alerts fired", fired)
                fired = await tool_degradation_detector()
                if fired:
                    logger.info("Detectors: %d tool-degradation alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: pattern checks failed: %s", e)

        # Zombie runner: every 20 ticks = 10 minutes
        if tick_count % 20 == 0:
            try:
                from robothor.engine.detectors import zombie_runner_detector

                fired = await zombie_runner_detector()
                if fired:
                    logger.info("Detectors: %d zombie-runner alerts fired", fired)
            except Exception as e:
                logger.debug("Detectors: zombie_runner check failed: %s", e)

        # Daily chat session TTL cleanup (every 2880 ticks = 24h)
        if tick_count % 2880 == 0:
            try:
                from robothor.engine.chat_store import cleanup_stale_sessions

                loop = asyncio.get_running_loop()
                deleted = await loop.run_in_executor(None, cleanup_stale_sessions)
                if deleted:
                    logger.info("Watchdog: cleaned up %d stale chat sessions", deleted)
            except Exception as e:
                logger.warning("Watchdog: chat session cleanup failed: %s", e)

        # Data retention cleanup (every 2880 ticks = 24h)
        if tick_count % 2880 == 0:
            try:
                from robothor.engine.retention import run_retention_cleanup

                loop = asyncio.get_running_loop()
                results = await asyncio.wait_for(
                    loop.run_in_executor(None, run_retention_cleanup),
                    timeout=300.0,
                )
                total = sum(v for v in results.values() if v > 0)
                if total > 0:
                    logger.info("Watchdog: retention cleanup deleted %d rows: %s", total, results)
                    _record_watchdog_event("retention_cleanup", f"deleted {total} rows")
            except TimeoutError:
                logger.warning("Watchdog: retention cleanup timed out after 300s")
                _record_watchdog_event("retention_timeout", "cleanup exceeded 300s")
            except Exception as e:
                logger.warning("Watchdog: retention cleanup failed: %s", e)

        # autoDream staleness check (every 20 ticks = 10 min)
        if tick_count % 20 == 0 and tick_count > 20:
            try:
                from robothor.engine.autodream import COOLDOWN_SECONDS

                last_run, source = _resolve_last_run()
                staleness = (time.time() - last_run) if last_run is not None else None
                decision = _autodream_staleness_decision(
                    staleness,
                    _autodream_stale_alerted,
                    _AUTODREAM_MAX_DEFER_SECONDS,
                    COOLDOWN_SECONDS,
                )
                if decision["reset"]:
                    _autodream_stale_alerted = False
                if decision["warn"] and staleness is not None:
                    logger.warning(
                        "Watchdog: autoDream stale (last run %.0f min ago, source=%s)",
                        staleness / 60,
                        source,
                    )
                if decision["alert"] and staleness is not None:
                    from robothor.engine.delivery import get_telegram_sender

                    sender = get_telegram_sender()
                    if sender and config.default_chat_id:
                        hours = int(staleness / 3600)
                        await sender(
                            config.default_chat_id,
                            f"*Watchdog Alert*\n\nautoDream has not run for {hours}h "
                            f"(source: {source}). Memory consolidation may be stalled.",
                        )
                        # Latch only after a delivered page so a transient
                        # sender outage doesn't suppress the alert.
                        _autodream_stale_alerted = True
            except Exception as e:
                logger.debug("Watchdog: autoDream staleness check failed: %s", e)

        # Alert after 3 consecutive PG failures
        if pg_failures == 3:
            try:
                from robothor.engine.delivery import get_telegram_sender

                sender = get_telegram_sender()
                if sender and config.default_chat_id:
                    await sender(
                        config.default_chat_id,
                        "*Watchdog Alert*\n\nPostgreSQL unreachable for 3 consecutive checks.",
                    )
            except Exception:
                pass


_CURIOSITY_COOLDOWN_SECONDS = 6 * 3600  # 6h minimum between reactive spawns
_CURIOSITY_CHECK_INTERVAL = 30 * 60  # check density every 30 min
_curiosity_last_spawn_ts: float = 0.0


async def _curiosity_density_loop(scheduler: Any) -> None:
    """Reactive curiosity engine spawner.

    Every 30 minutes, polls gap_analysis metrics. If knowledge density is low
    (orphans / thin clusters / uncertainty signals cross thresholds) AND the
    reactive cooldown has elapsed, spawn the curiosity-engine agent off-cycle
    so it can fill gaps without waiting for the Sunday cron.

    The cron-scheduled Sunday run remains as a floor.
    """
    global _curiosity_last_spawn_ts  # noqa: PLW0603

    from robothor.engine.dedup import running_agents
    from robothor.memory.gap_analysis import get_memory_density_metrics

    # Initial wait so we don't spawn immediately after boot
    await asyncio.sleep(300)

    while True:
        try:
            # Sleep inside the try — a cancel landing here (e.g. from an
            # orphaned stall watchdog monitoring the wrong task) must be
            # caught by except asyncio.CancelledError below, not propagate
            # out of the coroutine and mark this task itself as cancelled.
            await asyncio.sleep(_CURIOSITY_CHECK_INTERVAL)
            now = time.time()
            if now - _curiosity_last_spawn_ts < _CURIOSITY_COOLDOWN_SECONDS:
                continue
            if "curiosity-engine" in running_agents():
                continue

            metrics = await get_memory_density_metrics()
            if not metrics.get("should_spawn"):
                continue

            logger.info(
                "curiosity-density: reactive spawn triggered "
                "(orphans=%d thin=%d uncertainty=%d low_conf=%d)",
                metrics["orphan_count"],
                metrics["thin_cluster_count"],
                metrics["uncertainty_count"],
                metrics["low_confidence_count"],
            )
            _curiosity_last_spawn_ts = now
            await scheduler._run_agent("curiosity-engine")  # noqa: SLF001
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("curiosity-density loop error: %s", e)


_CURATOR_CHECK_INTERVAL = 6 * 3600  # re-evaluate cadence every 6h


async def _curator_loop(scheduler: Any) -> None:
    """Skill-library maintenance (Phase 3 / Rip 5).

    Tier 1 ALWAYS (no flag, no LLM): apply_skill_lifecycle() persists time-derived
    stale/archived transitions — reversible, content-preserving anti-bloat.
    Tier 2 OPT-IN (curator_enabled(), default OFF): destructive LLM consolidation.
    Cadence keyed to should_run_curator (default 7d); only acts when engine idle.
    """
    from datetime import UTC, datetime

    from robothor.engine.curator import (
        load_curator_last_pass,
        should_run_curator,
        spawn_curator,
        store_curator_last_pass,
    )
    from robothor.engine.dedup import running_agents
    from robothor.engine.feature_flags import curator_enabled
    from robothor.engine.skills import apply_skill_lifecycle

    await asyncio.sleep(600)  # stagger past boot

    while True:
        try:
            # Sleep inside the try — a cancel landing here (e.g. from an
            # orphaned stall watchdog monitoring the wrong task) must be
            # caught by except asyncio.CancelledError below, not propagate
            # out of the coroutine and mark this task itself as cancelled.
            await asyncio.sleep(_CURATOR_CHECK_INTERVAL)
            if not should_run_curator(load_curator_last_pass()):
                continue
            if running_agents():
                logger.debug("curator: engine busy, deferring pass")
                continue

            loop = asyncio.get_running_loop()
            transitions = await loop.run_in_executor(None, apply_skill_lifecycle)
            if any(transitions.values()):
                logger.info("curator: lifecycle transitions %s", transitions)

            if curator_enabled():
                if running_agents():
                    logger.info("curator: busy at LLM gate, deferring consolidation")
                else:
                    await spawn_curator(scheduler)
            else:
                logger.debug("curator: RIP_5 off — lifecycle only, no LLM pass")

            await loop.run_in_executor(None, store_curator_last_pass, datetime.now(UTC))
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("curator loop error: %s", e)


async def _extension_watcher_loop() -> None:
    """Hot-reload adapter YAML changes without an engine restart (ExtensionWatcher)."""
    try:
        from robothor.engine.extensions import ExtensionWatcher

        await ExtensionWatcher().watch()
    except Exception as e:
        logger.debug("Extension watcher exited: %s", e)


# Maximum seconds to defer autoDream when agents are continuously busy.
# After this ceiling the loop fires anyway (post_stall mode) to prevent
# memory consolidation from being starved on active deployments.
_AUTODREAM_MAX_DEFER_SECONDS = int(os.environ.get("AUTODREAM_MAX_DEFER_SECONDS", str(4 * 3600)))

# Extra back-off when a forced post_stall run is skipped because a concurrent
# run already holds the lock — avoids a 60s busy-loop while that run finishes.
_AUTODREAM_FORCE_BACKOFF_SECONDS = int(os.environ.get("AUTODREAM_FORCE_BACKOFF_SECONDS", "600"))

# In-process state for the autoDream loop/watchdog. Both run in the same event
# loop (no threads), and neither awaits between reading and writing these, so
# plain module globals are safe.
_autodream_defer_started_at: float | None = None  # start of the current continuous-busy streak
_autodream_stale_alerted: bool = False  # latch so the staleness page fires once per episode


def _resolve_last_run() -> tuple[float | None, str]:
    """Resolve the last-run timestamp and the tier that served it.

    Single source of truth shared by `_watchdog` and `_autodream_loop`: reads
    the validated timestamp from autodream, and when none is usable anchors to
    daemon boot time so a fresh / Redis-down daemon still has a clock. The
    source faithfully reflects the tier ("redis" | "file" | "daemon boot" |
    "unknown"), so the watchdog alert can no longer mislabel a filesystem hit.
    """
    from robothor.engine.autodream import _get_last_run_ts_with_source

    ts, source = _get_last_run_ts_with_source()
    if ts is not None:
        return ts, source
    if _DAEMON_START_TS:
        try:
            from datetime import datetime as _dt

            return _dt.fromisoformat(_DAEMON_START_TS).timestamp(), "daemon boot"
        except Exception:
            return None, "unknown"
    return None, "unknown"


def _autodream_staleness_decision(
    staleness: float | None,
    already_alerted: bool,
    max_defer: float,
    cooldown: float,
) -> dict[str, bool]:
    """Decide whether to page/warn about autoDream staleness (pure, testable).

    The Telegram page fires only past ``max_defer + cooldown`` — i.e. after the
    loop's own self-heal force-run window has elapsed — so the operator is
    never paged before the system itself acts. A separate log-WARNING keeps the
    earlier 3×cooldown signal for dashboards. ``reset`` clears the page latch
    once staleness returns to healthy. Returns {"alert", "warn", "reset"}.
    """
    alert_threshold = max_defer + cooldown
    warn_threshold = cooldown * 3
    if staleness is None or staleness <= warn_threshold:
        return {"alert": False, "warn": False, "reset": True}
    if staleness > alert_threshold:
        return {"alert": not already_alerted, "warn": False, "reset": False}
    return {"alert": False, "warn": True, "reset": False}


def _autodream_defer_decision(
    agents_busy: bool,
    now: float,
    defer_started_at: float | None,
    max_defer: float,
) -> dict[str, Any]:
    """Decide whether to force a deferred autoDream run (pure, testable).

    Measures *continuous* busy time: the streak begins when agents first go
    busy and resets the moment they idle, so a long idle gap no longer
    masquerades as deferral. Returns {"force", "defer_started_at",
    "deferred_for"}.
    """
    if not agents_busy:
        return {"force": False, "defer_started_at": None, "deferred_for": 0.0}
    started = defer_started_at if defer_started_at is not None else now
    deferred_for = now - started
    return {
        "force": deferred_for >= max_defer,
        "defer_started_at": started,
        "deferred_for": deferred_for,
    }


async def _autodream_loop() -> None:
    """Background loop — triggers autoDream memory consolidation when engine is idle.

    Implements exponential backoff on consecutive errors (60s → 120s → ... → 3600s max).
    Resets to normal 60s interval on success.

    Max-defer ceiling: if running_agents() blocks the loop for longer than
    _AUTODREAM_MAX_DEFER_SECONDS (default 4h), autoDream fires anyway in
    post_stall mode so memory consolidation is never permanently starved.
    """
    global _autodream_defer_started_at  # noqa: PLW0603

    from robothor.engine.autodream import is_cooled_down, run_autodream
    from robothor.engine.dedup import running_agents

    consecutive_errors = 0

    while True:
        sleep_seconds = 60 if consecutive_errors == 0 else min(60 * 2**consecutive_errors, 3600)
        await asyncio.sleep(sleep_seconds)
        try:
            agents_busy = bool(running_agents())
            cooled = is_cooled_down()

            if agents_busy or not cooled:
                # Max-defer ceiling: force a run only after agents have been
                # *continuously* busy for _AUTODREAM_MAX_DEFER_SECONDS, tracked
                # in-process (not via the possibly-stale last-run timestamp).
                decision = _autodream_defer_decision(
                    agents_busy,
                    time.time(),
                    _autodream_defer_started_at,
                    _AUTODREAM_MAX_DEFER_SECONDS,
                )
                _autodream_defer_started_at = decision["defer_started_at"]
                if decision["force"]:
                    logger.warning(
                        "autoDream continuously deferred for %.1fh due to busy agents — "
                        "forcing DEEP run",
                        decision["deferred_for"] / 3600,
                    )
                    # Deep, not post_stall: only deep mode runs full lifecycle
                    # maintenance (importance scoring, decay, GC). Under sustained
                    # load a post_stall force would reset the staleness clock while
                    # deep maintenance silently never runs — the exact starvation
                    # this MAX_DEFER ceiling exists to prevent.
                    result = await run_autodream(mode="deep")
                    if result.get("skipped"):
                        # A concurrent run holds the lock. Back off instead of
                        # re-firing every 60s; keep the defer streak so we retry,
                        # and let that run's own _set_last_run_ts() end the streak.
                        logger.debug(
                            "autoDream force-run skipped (%s); backing off %ds",
                            result.get("reason"),
                            _AUTODREAM_FORCE_BACKOFF_SECONDS,
                        )
                        await asyncio.sleep(_AUTODREAM_FORCE_BACKOFF_SECONDS)
                        continue
                    _autodream_defer_started_at = None
                    consecutive_errors = 0
                continue

            await run_autodream(mode="idle")
            _autodream_defer_started_at = None
            consecutive_errors = 0
        except asyncio.CancelledError:
            return
        except Exception as e:
            consecutive_errors += 1
            logger.warning(
                "autoDream loop error (%d consecutive, next retry in %ds): %s",
                consecutive_errors,
                min(60 * 2**consecutive_errors, 3600),
                e,
            )


def run() -> None:
    """Entry point for python -m robothor.engine.daemon"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("Engine crashed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    run()

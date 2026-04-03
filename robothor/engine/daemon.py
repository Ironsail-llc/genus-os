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
from typing import Any

from robothor.engine.config import EngineConfig
from robothor.engine.health import serve_health
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


def _cleanup_stale_runs() -> int:
    """Mark stale 'running' agent_runs as 'timeout'.

    Called on startup and periodically by the watchdog.
    Returns the number of runs cleaned up.
    """
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE agent_runs SET status='timeout', "
                "completed_at=NOW(), "
                "duration_ms=EXTRACT(EPOCH FROM (NOW()-started_at))*1000, "
                "error_message='Reaped by watchdog: stuck in initialization (no LLM call reached)' "
                "WHERE status='running' AND started_at < NOW() - INTERVAL '30 minutes' "
                "RETURNING id, agent_id"
            )
            rows = cur.fetchall()
            conn.commit()
            if rows:
                for row in rows:
                    logger.warning("Cleaned up stale run %s (agent: %s)", row[0], row[1])
                # Release dedup locks for cleaned-up agents
                from robothor.engine.dedup import release

                for row in rows:
                    release(row[1])
            return len(rows)
    except Exception as e:
        logger.warning("Stale run cleanup failed: %s", e)
        return 0


async def _start_federation(config: EngineConfig) -> Any:
    """Start federation NATS transport if connections exist.

    Returns the NATSManager (connected) or None. Backward-compatible no-op
    when no federation is configured.
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
            # Ensure streams for active connections
            for conn in connections:
                if conn.state.value == "active":
                    await nats_mgr.ensure_stream(conn.id)
        else:
            logger.warning("Federation: NATS connection failed, federation disabled")
            return None

        return nats_mgr
    except Exception as e:
        logger.warning("Federation startup failed (non-fatal): %s", e)
        return None


async def main() -> None:
    """Start all engine subsystems."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Starting Genus OS Agent Engine...")

    # Clean up stale runs from previous crash/restart
    cleaned = await asyncio.get_event_loop().run_in_executor(None, _cleanup_stale_runs)
    if cleaned:
        logger.info("Startup: cleaned %d stale agent runs", cleaned)

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

    # Initialize lifecycle hook registry
    from robothor.engine.hook_registry import (
        init_hook_registry,
        load_global_hooks,
        load_hooks_from_manifest,
    )

    hook_registry = init_hook_registry()
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

    bot = TelegramBot(config, runner)
    scheduler = CronScheduler(config, runner, workflow_engine=workflow_engine)
    hooks = EventHooks(config, runner, workflow_engine=workflow_engine)

    # Federation — start NATS if connections exist (no-op otherwise)
    nats_mgr = await _start_federation(config)

    # Start all subsystems concurrently
    tasks = [
        asyncio.create_task(bot.start_polling(), name="telegram"),
        asyncio.create_task(scheduler.start(), name="scheduler"),
        asyncio.create_task(hooks.start(), name="hooks"),
        asyncio.create_task(
            serve_health(config, runner=runner, workflow_engine=workflow_engine),
            name="health",
        ),
        asyncio.create_task(_watchdog(config, scheduler), name="watchdog"),
        asyncio.create_task(_autodream_loop(), name="autodream"),
    ]

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

    # Wait for any task to complete (aiogram handles SIGTERM and stops polling,
    # which completes the telegram task — that's our shutdown trigger)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Log what finished
    for task in done:
        if task.exception():
            logger.error("Task %s failed: %s", task.get_name(), task.exception())
        else:
            logger.info("Task %s completed", task.get_name())

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

    await scheduler.stop()
    await hooks.stop()
    await bot.stop()

    # Cancel remaining tasks
    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)
    logger.info("Engine stopped")


async def _watchdog(config: EngineConfig, scheduler: CronScheduler) -> None:
    """Subsystem watchdog — pings PostgreSQL and Redis every 30s, notifies systemd, cleans stale sessions daily."""
    pg_failures = 0
    redis_failures = 0
    tick_count = 0

    while True:
        await asyncio.sleep(30)
        tick_count += 1
        _sd_notify("WATCHDOG=1")

        # Ping PostgreSQL
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1")
            pg_failures = 0
        except Exception as e:
            pg_failures += 1
            logger.warning("Watchdog: PostgreSQL ping failed (%d): %s", pg_failures, e)

        # Ping Redis
        try:
            import redis

            from robothor.config import get_config

            cfg = get_config()
            r = redis.Redis(
                host=cfg.redis.host,
                port=cfg.redis.port,
                db=cfg.redis.db,
                password=cfg.redis.password or None,
            )
            r.ping()
            r.close()
            redis_failures = 0
        except Exception as e:
            redis_failures += 1
            logger.warning("Watchdog: Redis ping failed (%d): %s", redis_failures, e)

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

        # autoDream staleness check (every 20 ticks = 10 min)
        if tick_count % 20 == 0 and tick_count > 20:
            try:
                from robothor.engine.autodream import COOLDOWN_SECONDS, _get_last_run_ts

                last_run = _get_last_run_ts()
                if last_run is not None:
                    staleness = time.time() - last_run
                    if staleness > COOLDOWN_SECONDS * 6:
                        from robothor.engine.delivery import get_telegram_sender

                        sender = get_telegram_sender()
                        if sender and config.default_chat_id:
                            hours = int(staleness / 3600)
                            await sender(
                                config.default_chat_id,
                                f"*Watchdog Alert*\n\nautoDream has not run for {hours}h. "
                                "Memory consolidation may be stalled.",
                            )
                    elif staleness > COOLDOWN_SECONDS * 3:
                        logger.warning(
                            "Watchdog: autoDream stale (last run %.0f min ago)",
                            staleness / 60,
                        )
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


async def _autodream_loop() -> None:
    """Background loop — triggers autoDream memory consolidation when engine is idle.

    Implements exponential backoff on consecutive errors (60s → 120s → ... → 3600s max).
    Resets to normal 60s interval on success.
    """
    from robothor.engine.autodream import is_cooled_down, run_autodream
    from robothor.engine.dedup import running_agents

    consecutive_errors = 0

    while True:
        sleep_seconds = 60 if consecutive_errors == 0 else min(60 * 2**consecutive_errors, 3600)
        await asyncio.sleep(sleep_seconds)
        try:
            if running_agents() or not is_cooled_down():
                continue
            await run_autodream(mode="idle")
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

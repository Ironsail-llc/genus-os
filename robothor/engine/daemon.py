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
import sys

from robothor.engine.config import EngineConfig
from robothor.engine.health import serve_health
from robothor.engine.hooks import EventHooks
from robothor.engine.runner import AgentRunner
from robothor.engine.scheduler import CronScheduler
from robothor.engine.telegram import TelegramBot
from robothor.engine.workflow import WorkflowEngine

logger = logging.getLogger(__name__)


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
                "duration_ms=EXTRACT(EPOCH FROM (NOW()-started_at))*1000 "
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


async def main() -> None:
    """Start all engine subsystems."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Starting Robothor Agent Engine...")

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

    # Register runner for sub-agent spawning
    from robothor.engine.tools import set_runner

    set_runner(runner)

    workflow_engine = WorkflowEngine(config, runner)
    wf_count = workflow_engine.load_workflows(config.workflow_dir)
    logger.info("Loaded %d workflows", wf_count)

    bot = TelegramBot(config, runner)
    scheduler = CronScheduler(config, runner, workflow_engine=workflow_engine)
    hooks = EventHooks(config, runner, workflow_engine=workflow_engine)

    # Start all subsystems concurrently
    tasks = [
        asyncio.create_task(bot.start_polling(), name="telegram"),
        asyncio.create_task(scheduler.start(), name="scheduler"),
        asyncio.create_task(hooks.start(), name="hooks"),
        asyncio.create_task(
            serve_health(config, runner=runner, workflow_engine=workflow_engine),
            name="health",
        ),
        asyncio.create_task(_watchdog(config), name="watchdog"),
    ]

    logger.info("All subsystems started")

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

    await scheduler.stop()
    await hooks.stop()
    await bot.stop()

    # Cancel remaining tasks
    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)
    logger.info("Engine stopped")


async def _watchdog(config: EngineConfig) -> None:
    """Subsystem watchdog — pings PostgreSQL and Redis every 60s, cleans stale chat sessions daily."""
    pg_failures = 0
    redis_failures = 0
    tick_count = 0

    while True:
        await asyncio.sleep(60)
        tick_count += 1

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

        # Zombie run reaper (every 20 ticks = 20 minutes)
        if tick_count % 20 == 0:
            try:
                loop = asyncio.get_running_loop()
                reaped = await loop.run_in_executor(None, _cleanup_stale_runs)
                if reaped:
                    logger.warning("Watchdog: reaped %d zombie agent runs", reaped)
            except Exception as e:
                logger.warning("Watchdog: zombie reaper failed: %s", e)

        # Daily chat session TTL cleanup (every 1440 ticks = 24h)
        if tick_count % 1440 == 0:
            try:
                from robothor.engine.chat_store import cleanup_stale_sessions

                loop = asyncio.get_running_loop()
                deleted = await loop.run_in_executor(None, cleanup_stale_sessions)
                if deleted:
                    logger.info("Watchdog: cleaned up %d stale chat sessions", deleted)
            except Exception as e:
                logger.warning("Watchdog: chat session cleanup failed: %s", e)

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

"""Startup timing, concurrent startup steps, and pre-READY warm-up.

``daemon.main`` runs its startup through a :class:`StartupClock`: each step is
timed, steps that do not depend on one another run together, and the total
is logged once as ``STARTUP <total>ms <step>=<ms> ...`` and served on
``/health`` as ``startup_ms``.

A failing step fails startup exactly as it did when the steps ran in
sequence: the original exception propagates out of ``main`` and the steps
still running beside it are cancelled, so READY=1 is never sent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Milliseconds from the start of ``main`` to READY=1; None until then.
_total_ms: int | None = None


def startup_ms() -> int | None:
    """How long this process took to reach READY=1, or None if it has not."""
    return _total_ms


class StartupClock:
    """Times named startup steps and runs independent ones concurrently."""

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self.steps: dict[str, int] = {}

    async def step(self, name: str, awaitable: Awaitable[Any]) -> Any:
        """Await one step and record how long it took."""
        started = time.perf_counter()
        try:
            return await awaitable
        finally:
            self.steps[name] = round((time.perf_counter() - started) * 1000)

    async def together(self, **steps: Awaitable[Any]) -> list[Any]:
        """Run independent steps concurrently; results in argument order.

        The first exception is re-raised as itself (not wrapped in a group),
        after the sibling steps are cancelled and awaited.
        """
        tasks = [
            asyncio.ensure_future(self.step(name, awaitable)) for name, awaitable in steps.items()
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def finish(self) -> int:
        """Log the STARTUP line and publish the total for ``/health``."""
        global _total_ms
        total = round((time.perf_counter() - self._started) * 1000)
        _total_ms = total
        parts = " ".join(f"{name}={ms}ms" for name, ms in self.steps.items())
        logger.info("STARTUP %dms %s", total, parts)
        return total


def warm_db_pool() -> None:
    """Create the connection pool now, not on the first query. Never raises.

    A database that is down at boot is reported by the steps that need it,
    exactly as before; the pool is then created lazily on the next query.
    """
    try:
        from robothor.db.connection import get_pool

        get_pool()
    except Exception as e:  # noqa: BLE001 - a warm-up must not change startup's outcome
        logger.warning("DB pool not pre-created at startup: %s", type(e).__name__)


def warm_system_prompts(agent_ids: Iterable[str], manifest_dir: Path, workspace: Path) -> int:
    """Fill the system-prompt cache for every installed agent. Never raises.

    Built from the same ``load_agent_config`` a run uses, so the cached body
    is the one the first run would have built. An agent whose prompt cannot
    be built is left cold; its first run reports the reason, as it always did.
    """
    from robothor.engine.config import build_system_prompt, load_agent_config

    warmed = 0
    for agent_id in agent_ids:
        try:
            agent_config = load_agent_config(agent_id, manifest_dir)
            if agent_config is None:
                continue
            build_system_prompt(agent_config, workspace)
            warmed += 1
        except Exception as e:  # noqa: BLE001 - the first run owns the error
            logger.debug("System prompt not pre-warmed for %s: %s", agent_id, type(e).__name__)
    return warmed

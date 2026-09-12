"""Redis: the event bus, the session store and the scheduler's locks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]


async def _connect(ctx: DoctorContext) -> Result:
    """Redis answers a PING.

    The engine's event bus, the channel wake-ups and the scheduler's locks all
    live here. Without it agents still run one at a time but nothing reacts to
    anything: an inbound message raises no wake, and two schedulers would both
    believe they hold the lock. The detail names host and port -- never the
    password, which is why the URL is not printed.
    """
    settings = ctx.settings.redis

    def _ping() -> None:
        import redis as redis_lib

        client = redis_lib.Redis(
            host=settings.host,
            port=settings.port,
            db=settings.db,
            password=settings.password or None,
            socket_timeout=ctx.timeout_s,
            socket_connect_timeout=ctx.timeout_s,
        )
        try:
            client.ping()
        finally:
            client.close()

    try:
        await ctx.run_blocking(_ping)
    except ImportError:
        return fail("the redis package is not installed in this environment")
    except Exception as exc:  # noqa: BLE001 - unreachable is a result
        return fail(f"{settings.host}:{settings.port} did not answer: {type(exc).__name__}")
    return ok(f"{settings.host}:{settings.port} db {settings.db}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="redis.connect",
        title="Redis is reachable",
        category="redis",
        severity="required",
        run=_connect,
    ),
)

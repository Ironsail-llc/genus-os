"""Verify each federation link is really attached, and record it.

`link_health` reads `last_seen_at`. Nothing wrote it, so this exists to — and
the rule it follows is the one the original defect broke: only write the column
when the transport genuinely holds the connection. A heartbeat that touches
unconditionally records that the heartbeat ran, which is the same lie in a
different column.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.federation.models import Connection, ConnectionState

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

#: How often the daemon runs a beat. `health.STALE_AFTER_SECONDS` is several
#: multiples of this, so one missed beat is not an alert.
HEARTBEAT_INTERVAL_SECONDS = 180


@dataclass
class BeatResult:
    alive: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def beat(
    connections: Sequence[Connection],
    transport: Any,
    *,
    touch: Callable[..., Any],
    record_errors: bool = False,
) -> BeatResult:
    """One pass over every link that is supposed to be carrying traffic.

    PENDING and SUSPENDED links are skipped entirely: one is mid-pairing and
    the other is off on purpose, and reporting either as dropped would page the
    operator through normal operation until they muted the channel.
    """
    result = BeatResult()
    for connection in connections:
        if connection.state not in (ConnectionState.ACTIVE, ConnectionState.LIMITED):
            continue

        attached = transport is not None and bool(transport.is_attached(connection.id))
        if not attached:
            result.dropped.append(connection.id)
            logger.warning(
                "Federation: connection %s (%s) is %s but the transport does not hold it",
                connection.id,
                connection.peer_name or "unpaired",
                connection.state.value,
            )
            if record_errors:
                try:
                    touch(
                        connection.id,
                        error="transport not attached — this link is not carrying traffic",
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Federation: could not record drop for %s: %s", connection.id, e)
            continue

        try:
            touch(connection.id, error="")
            result.alive.append(connection.id)
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the rest
            logger.warning("Federation: heartbeat write failed for %s: %s", connection.id, e)
    return result


async def heartbeat_loop() -> None:
    """Daemon task: beat forever, and page the first time a link drops.

    Paged once per transition, not once per beat — the credential pool logged
    the same outage 452 times and paged zero.
    """
    import asyncio

    from robothor.federation.connections import load_connections, touch_last_seen
    from robothor.federation.transport import get_transport

    previously_dropped: set[str] = set()
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            connections = await asyncio.to_thread(load_connections)
            if not connections:
                continue
            result = await asyncio.to_thread(
                beat,
                connections,
                get_transport(),
                touch=touch_last_seen,
                record_errors=True,
            )
            newly_dropped = set(result.dropped) - previously_dropped
            if newly_dropped:
                names = {c.id: (c.peer_name or c.id[:12]) for c in connections}
                await _alert_dropped([names.get(cid, cid) for cid in sorted(newly_dropped)])
            previously_dropped = set(result.dropped)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a heartbeat must not kill the daemon
            logger.exception("Federation heartbeat failed")


async def _alert_dropped(peers: list[str]) -> None:
    try:
        from robothor.engine.alerts import alert

        await alert(
            "critical",
            "Federation link dropped",
            f"{len(peers)} federation link(s) are marked active but the transport "
            f"no longer holds them: {', '.join(peers)}.\n\n"
            f"Check: robothor federation status",
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not alert on dropped federation links")

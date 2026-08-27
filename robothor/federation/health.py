"""Is this link carrying traffic? — a question `state` cannot answer.

`federation status` printed `conn.state.value` and stopped there. The column
records that a link was once established; it says nothing about whether the
transport exists now. So for five months the operator's only diagnostic
confirmed the operator's wrong belief, and a total outage looked like a
healthy fleet.

Two facts, kept apart:

    state         was this link established, and is it meant to be running?
    last_seen_at  when did the transport last actually report?

The verdict that matters is NEVER_ATTACHED — marked active, transport silent.
That is what this box has been in since 2026-03-09.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from robothor.federation.models import Connection, ConnectionState

#: How long a link may go unheard before it is considered stale. The daemon
#: touches `last_seen_at` on attach and on every heartbeat, so this is several
#: missed heartbeats, not one.
STALE_AFTER_SECONDS = 15 * 60

#: Skew tolerance. `last_seen_at` is written by THIS instance, so a timestamp
#: from the future means a clock jump — and must not read as permanently fresh.
MAX_CLOCK_SKEW_SECONDS = 120


class LinkVerdict(StrEnum):
    ATTACHED = "attached"
    STALE = "stale"
    NEVER_ATTACHED = "never attached"
    PAIRING = "pairing"
    SUSPENDED = "suspended"
    LIMITED = "limited"


@dataclass
class LinkHealth:
    verdict: LinkVerdict
    detail: str
    healthy: bool
    #: Worth paging about. A link the operator suspended is not.
    alarming: bool = False


def _age_seconds(timestamp: str) -> float | None:
    """Seconds since ``timestamp``, or None if it cannot be read.

    Unreadable fails TOWARD reporting a problem. The alternative — a corrupt
    value reading as healthy — is how the original defect worked.
    """
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds()


def _humanise(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def link_health(connection: Connection) -> LinkHealth:
    """What this one link is actually doing, as opposed to what it says."""
    state = connection.state

    if state == ConnectionState.PENDING:
        return LinkHealth(
            LinkVerdict.PAIRING,
            "waiting for the peer's handshake — not yet carrying traffic",
            healthy=False,
        )
    if state == ConnectionState.SUSPENDED:
        return LinkHealth(
            LinkVerdict.SUSPENDED, "suspended by the operator", healthy=False, alarming=False
        )

    age = _age_seconds(connection.last_seen_at)
    if age is None:
        return LinkHealth(
            LinkVerdict.NEVER_ATTACHED,
            f"marked {state.value} but the transport has never reported — "
            f"this instance is NOT federated on this link",
            healthy=False,
            alarming=True,
        )
    if age < -MAX_CLOCK_SKEW_SECONDS:
        return LinkHealth(
            LinkVerdict.STALE,
            f"last seen in the future ({_humanise(-age)} ahead) — clock skew, "
            f"treat the link as unverified",
            healthy=False,
            alarming=True,
        )
    if age > STALE_AFTER_SECONDS:
        return LinkHealth(
            LinkVerdict.STALE,
            f"last seen {_humanise(age)}",
            healthy=False,
            alarming=True,
        )

    verdict = LinkVerdict.LIMITED if state == ConnectionState.LIMITED else LinkVerdict.ATTACHED
    return LinkHealth(verdict, f"last seen {_humanise(age)}", healthy=True)


@dataclass
class FleetHealth:
    total: int
    attached: int
    alarming: int
    healthy: bool
    links: list[tuple[Connection, LinkHealth]]


def fleet_health(connections: list[Connection]) -> FleetHealth:
    """The whole picture, for the daemon's alert and for /ready.

    An instance with no connections is HEALTHY, not broken. Paging every
    single-box install would train the operator to mute the channel — the
    signal is the gap between "rows exist" and "transport attached".
    """
    links = [(c, link_health(c)) for c in connections]
    alarming = sum(1 for _, h in links if h.alarming)
    return FleetHealth(
        total=len(connections),
        attached=sum(1 for _, h in links if h.healthy),
        alarming=alarming,
        healthy=alarming == 0,
        links=links,
    )

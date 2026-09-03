"""`federation status` must not report a database string as if it were a wire.

Since 2026-03-09 this instance has had connections it believed were fine.
`status` printed `conn.state.value` — the column — and the column said
`active`, so the operator's only diagnostic agreed with the operator's wrong
belief for five months. The state column answers "was this link ever
established?". It cannot answer "is it carrying traffic right now?", and
printing it as though it could is what made a total outage invisible.

`link_health` separates the two. The interesting verdict is NEVER_ATTACHED:
marked active, transport silent — the exact state that was invisible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.federation.health import (
    STALE_AFTER_SECONDS,
    LinkVerdict,
    link_health,
)
from robothor.federation.models import Connection, ConnectionState


def _conn(state, last_seen="", activated="2026-08-27T00:00:00+00:00"):
    return Connection(
        id="c1", peer_name="child-1", state=state, last_seen_at=last_seen, activated_at=activated
    )


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# ── The verdict that was missing ─────────────────────────────────────


def test_active_with_no_transport_report_is_never_attached():
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen=""))

    assert health.verdict is LinkVerdict.NEVER_ATTACHED
    assert not health.healthy
    assert "never" in health.detail.lower()


def test_active_and_recently_seen_is_attached():
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen=_ago(5)))

    assert health.verdict is LinkVerdict.ATTACHED
    assert health.healthy


def test_active_but_long_silent_is_stale():
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen=_ago(STALE_AFTER_SECONDS + 60)))

    assert health.verdict is LinkVerdict.STALE
    assert not health.healthy
    assert "last seen" in health.detail.lower()


def test_stale_says_how_long_it_has_been_silent():
    """'stale' without a duration sends the operator back to the database."""
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen=_ago(3 * 3600)))

    assert "3h" in health.detail or "3 h" in health.detail, health.detail


# ── States that are not outages ──────────────────────────────────────


def test_pending_is_reported_as_pairing_not_as_broken():
    """A connection waiting for its handshake is mid-setup. Paging on it would
    train the operator to ignore the channel."""
    health = link_health(_conn(ConnectionState.PENDING))

    assert health.verdict is LinkVerdict.PAIRING
    assert not health.healthy
    assert not health.alarming


def test_suspended_is_deliberate_and_never_alarming():
    health = link_health(_conn(ConnectionState.SUSPENDED, last_seen=_ago(999999)))

    assert health.verdict is LinkVerdict.SUSPENDED
    assert not health.alarming, "the operator turned this off on purpose"


def test_never_attached_is_alarming():
    assert link_health(_conn(ConnectionState.ACTIVE)).alarming


def test_stale_is_alarming():
    assert link_health(
        _conn(ConnectionState.ACTIVE, last_seen=_ago(STALE_AFTER_SECONDS * 2))
    ).alarming


# ── Robustness ───────────────────────────────────────────────────────


def test_an_unparseable_timestamp_is_treated_as_never_seen():
    """Fail toward reporting a problem. The alternative is a corrupt value
    reading as healthy, which is how this defect worked in the first place."""
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen="not a timestamp"))

    assert health.verdict is LinkVerdict.NEVER_ATTACHED


def test_a_naive_timestamp_does_not_crash_the_status_command():
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()  # noqa: DTZ005
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen=naive))

    assert health.verdict in (LinkVerdict.ATTACHED, LinkVerdict.STALE)


def test_a_future_timestamp_is_not_treated_as_fresh_forever():
    """Clock skew between two instances must not make a dead link look alive."""
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    health = link_health(_conn(ConnectionState.ACTIVE, last_seen=future))

    assert health.verdict is not LinkVerdict.ATTACHED


# ── The fleet summary the daemon and /ready use ──────────────────────


def test_a_fleet_with_one_dead_link_is_not_healthy():
    from robothor.federation.health import fleet_health

    summary = fleet_health(
        [
            _conn(ConnectionState.ACTIVE, last_seen=_ago(5)),
            _conn(ConnectionState.ACTIVE, last_seen=""),
        ]
    )

    assert not summary.healthy
    assert summary.attached == 1
    assert summary.alarming == 1


def test_an_instance_with_no_connections_is_healthy_not_broken():
    """An instance that was never federated is not a fault. Paging every
    single-box install would make the alert worthless."""
    from robothor.federation.health import fleet_health

    summary = fleet_health([])

    assert summary.healthy
    assert summary.total == 0


def test_a_fleet_of_suspended_links_is_healthy():
    from robothor.federation.health import fleet_health

    assert fleet_health([_conn(ConnectionState.SUSPENDED)]).healthy


@pytest.mark.parametrize("state", list(ConnectionState))
def test_every_state_produces_a_verdict(state):
    """A state this code does not know must not crash the operator's only
    diagnostic."""
    assert link_health(_conn(state)).verdict is not None

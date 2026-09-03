"""The heartbeat is what makes `last_seen_at` mean anything.

`link_health` reports NEVER_ATTACHED when `last_seen_at` is empty. That is
correct and it is the whole point — but it also means that if nothing ever
writes the column, a perfectly healthy fleet reports as dead and the operator
learns to ignore the new signal exactly as they learned to ignore the old one.

So the heartbeat has to write it, only when the transport is genuinely alive,
and it has to notice a link that drops.
"""

from __future__ import annotations

from robothor.federation.heartbeat import beat
from robothor.federation.models import Connection, ConnectionState


class FakeTransport:
    def __init__(self, attached):
        self._attached = set(attached)

    def is_attached(self, connection_id):
        return connection_id in self._attached


def _conn(cid, state=ConnectionState.ACTIVE):
    return Connection(id=cid, peer_name=f"peer-{cid}", state=state)


def test_an_attached_link_is_touched():
    touched = []
    result = beat(
        [_conn("a")], FakeTransport(["a"]), touch=lambda cid, error="": touched.append((cid, error))
    )

    assert touched == [("a", "")]
    assert result.alive == ["a"]


def test_a_link_the_transport_does_not_hold_is_not_touched():
    """Touching it anyway would make `last_seen_at` a record of the heartbeat
    running, not of the link working — which is the original defect with extra
    steps."""
    touched = []
    result = beat([_conn("a")], FakeTransport([]), touch=lambda cid, error="": touched.append(cid))

    assert touched == []
    assert result.dropped == ["a"]


def test_the_drop_is_recorded_on_the_connection():
    """So `federation status` can say WHY, not just that something is wrong."""
    errors = {}
    beat(
        [_conn("a")],
        FakeTransport([]),
        touch=lambda cid, error="": errors.__setitem__(cid, error),
        record_errors=True,
    )

    assert "a" in errors
    assert "not attached" in errors["a"].lower()


def test_pending_links_are_left_alone():
    """A link waiting for its handshake is not a dropped link, and marking it
    as one would page the operator through every normal pairing."""
    touched = []
    result = beat(
        [_conn("a", ConnectionState.PENDING)],
        FakeTransport([]),
        touch=lambda cid, error="": touched.append(cid),
    )

    assert touched == []
    assert result.dropped == []


def test_suspended_links_are_left_alone():
    result = beat(
        [_conn("a", ConnectionState.SUSPENDED)], FakeTransport([]), touch=lambda *a, **k: None
    )
    assert result.dropped == []


def test_no_transport_at_all_reports_every_active_link_as_dropped():
    """Federation off with rows present is the state this box was in for five
    months. It must read as an outage, not as silence."""
    result = beat([_conn("a"), _conn("b")], None, touch=lambda *a, **k: None)

    assert sorted(result.dropped) == ["a", "b"]


def test_a_failing_touch_does_not_stop_the_other_links():
    def _boom(cid, error=""):
        if cid == "a":
            raise RuntimeError("db gone")

    result = beat([_conn("a"), _conn("b")], FakeTransport(["a", "b"]), touch=_boom)

    assert result.alive == ["b"]

"""Durable agent-to-agent messaging — Postgres inbox, Redis demoted to wake.

The old fabric was Redis lists with a ONE HOUR TTL and no acknowledgement: a
message to a busy agent silently evaporated if it was not polled within the
hour, and nothing recorded it ever existed. The competitive sweep named the
ephemeral fabric the one thing eroding an otherwise-leading multi-agent
story, and production coordination routed around it (CRM tasks by
convention) — the tell that agents could not trust it.

The public API is unchanged (send / receive / broadcast / inbox_count /
AgentMessage); the store is Postgres under the same tenant-RLS shape as every
other engine table, and Redis keeps exactly one job: the real-time wake
publish, whose loss costs a poll delay, never a message.

These tests run against the real database (integration lane), because a
durability claim proven against a mock is the inert-control pattern with
extra steps.
"""

from __future__ import annotations

import uuid

import pytest

from robothor.engine.messaging import AgentMessenger, purge_old_messages

pytestmark = pytest.mark.integration

TENANT = "robothor-primary"


@pytest.fixture
def messenger():
    m = AgentMessenger(redis_client=False)  # False = no wake publishes in tests
    yield m


@pytest.fixture
def agents():
    """Unique agent ids per test so parallel/repeated runs never collide."""
    suffix = uuid.uuid4().hex[:8]
    return f"probe-a-{suffix}", f"probe-b-{suffix}"


def _cleanup(*agent_ids):
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM agent_messages WHERE to_agent = ANY(%s) OR from_agent = ANY(%s)",
            (list(agent_ids), list(agent_ids)),
        )
        conn.commit()


def test_send_then_receive_round_trip(messenger, agents):
    a, b = agents
    try:
        assert messenger.send(a, b, "hello", tenant_id=TENANT, metadata={"k": "v"})
        msgs = messenger.receive(b, tenant_id=TENANT)
        assert len(msgs) == 1
        assert msgs[0].from_agent == a
        assert msgs[0].content == "hello"
        assert msgs[0].metadata == {"k": "v"}
    finally:
        _cleanup(a, b)


def test_messages_survive_a_new_messenger_instance(messenger, agents):
    """THE durability test: the old fabric lost this on any restart."""
    a, b = agents
    try:
        messenger.send(a, b, "survives", tenant_id=TENANT)
        fresh = AgentMessenger(redis_client=False)
        msgs = fresh.receive(b, tenant_id=TENANT)
        assert [m.content for m in msgs] == ["survives"]
    finally:
        _cleanup(a, b)


def test_delivery_is_exactly_once_per_message(messenger, agents):
    a, b = agents
    try:
        messenger.send(a, b, "one", tenant_id=TENANT)
        first = messenger.receive(b, tenant_id=TENANT)
        second = messenger.receive(b, tenant_id=TENANT)
        assert len(first) == 1
        assert second == [], "a delivered message was handed out twice"
    finally:
        _cleanup(a, b)


def test_fifo_order(messenger, agents):
    a, b = agents
    try:
        for i in range(3):
            messenger.send(a, b, f"m{i}", tenant_id=TENANT)
        msgs = messenger.receive(b, limit=10, tenant_id=TENANT)
        assert [m.content for m in msgs] == ["m0", "m1", "m2"]
    finally:
        _cleanup(a, b)


def test_inbox_count_counts_only_undelivered(messenger, agents):
    a, b = agents
    try:
        messenger.send(a, b, "x", tenant_id=TENANT)
        messenger.send(a, b, "y", tenant_id=TENANT)
        assert messenger.inbox_count(b, tenant_id=TENANT) == 2
        messenger.receive(b, limit=1, tenant_id=TENANT)
        assert messenger.inbox_count(b, tenant_id=TENANT) == 1
    finally:
        _cleanup(a, b)


def test_broadcast_skips_the_sender(messenger, agents):
    a, b = agents
    c = f"probe-c-{uuid.uuid4().hex[:8]}"
    try:
        sent = messenger.broadcast(a, "team-x", [a, b, c], "all hands", tenant_id=TENANT)
        assert sent == 2
        assert messenger.inbox_count(b, tenant_id=TENANT) == 1
        assert messenger.inbox_count(c, tenant_id=TENANT) == 1
        assert messenger.inbox_count(a, tenant_id=TENANT) == 0
    finally:
        _cleanup(a, b, c)


def test_purge_respects_the_two_retention_clocks(messenger, agents):
    """Delivered rows purge at 7d; undelivered hold for 30d — an inbox nobody
    drains for a month is a dead recipient, not a mailbox."""
    a, b = agents
    from robothor.db.connection import get_connection

    try:
        messenger.send(a, b, "old-delivered", tenant_id=TENANT)
        messenger.receive(b, tenant_id=TENANT)
        messenger.send(a, b, "old-undelivered", tenant_id=TENANT)
        messenger.send(a, b, "fresh", tenant_id=TENANT)
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE agent_messages SET created_at = now() - interval '10 days', "
                "delivered_at = CASE WHEN delivered_at IS NOT NULL "
                "THEN now() - interval '9 days' ELSE NULL END "
                "WHERE to_agent = %s AND content LIKE 'old-%%'",
                (b,),
            )
            conn.commit()

        purged = purge_old_messages(delivered_days=7, undelivered_days=30)
        assert purged >= 1

        remaining = {m.content for m in messenger.receive(b, limit=10, tenant_id=TENANT)}
        assert "fresh" in remaining
        assert "old-delivered" not in remaining  # purged (delivered, >7d)
        assert "old-undelivered" in remaining  # held (<30d)
    finally:
        _cleanup(a, b)


class TestWakePublishIsBestEffort:
    def test_send_succeeds_when_redis_is_down(self, agents):
        """The wake ping is a latency optimization; its loss must never lose
        the message — the exact inversion of the old design."""
        a, b = agents

        class DeadRedis:
            def publish(self, *args, **kwargs):
                raise ConnectionError("redis down")

        m = AgentMessenger(redis_client=DeadRedis())
        try:
            assert m.send(a, b, "still lands", tenant_id=TENANT)
            got = m.receive(b, tenant_id=TENANT)
            assert [x.content for x in got] == ["still lands"]
        finally:
            _cleanup(a, b)

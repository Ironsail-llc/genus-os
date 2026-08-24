"""Tests for agent messaging."""

from __future__ import annotations

from unittest.mock import MagicMock

from robothor.engine.messaging import AgentMessage, AgentMessenger, init_messenger


class TestAgentMessage:
    def test_to_json_and_back(self):
        msg = AgentMessage(from_agent="a", to_agent="b", content="hello")
        restored = AgentMessage.from_json(msg.to_json())
        assert restored.from_agent == "a"
        assert restored.to_agent == "b"
        assert restored.content == "hello"
        assert restored.timestamp > 0

    def test_default_timestamp(self):
        msg = AgentMessage(from_agent="a", to_agent="b", content="x")
        assert msg.timestamp > 0


class TestAgentMessenger:
    """Unit surface of the DURABLE messenger (Postgres store, Redis wake).

    The Redis-list mechanics these tests used to mock (lpush/rpop/llen/TTL)
    no longer exist — the 1h-TTL inbox lost undelivered messages by design
    and was replaced in migration 105. The real durability, exactly-once and
    retention proofs run against the actual database in
    test_durable_messaging.py (integration lane); these cover the unit-lane
    contract with the store mocked.
    """

    def _mock_conn(self, rows=None, fail=False):
        conn = MagicMock()
        cur = MagicMock()
        if fail:
            cur.execute.side_effect = RuntimeError("db down")
        cur.fetchall.return_value = rows or []
        cur.fetchone.return_value = [0]
        conn.cursor.return_value = cur
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=conn)
        cm.__exit__ = MagicMock(return_value=False)
        return cm, conn, cur

    def test_send_stores_then_wakes(self, monkeypatch):
        cm, conn, cur = self._mock_conn()
        monkeypatch.setattr("robothor.db.connection.get_connection", lambda: cm)
        r = MagicMock()
        m = AgentMessenger(redis_client=r)

        assert m.send("a", "b", "hello", tenant_id="t1") is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO agent_messages" in sql
        assert params[0] == "t1" and params[1] == "a" and params[2] == "b"
        conn.commit.assert_called_once()
        r.publish.assert_called_once()

    def test_send_fails_closed_when_the_store_fails(self, monkeypatch):
        """The WRITE is the success criterion — a wake ping over a lost
        message is the old fabric's failure mode."""
        cm, _, _ = self._mock_conn(fail=True)
        monkeypatch.setattr("robothor.db.connection.get_connection", lambda: cm)
        r = MagicMock()
        m = AgentMessenger(redis_client=r)

        assert m.send("a", "b", "x") is False
        r.publish.assert_not_called()

    def test_send_succeeds_when_only_the_wake_fails(self, monkeypatch):
        cm, _, _ = self._mock_conn()
        monkeypatch.setattr("robothor.db.connection.get_connection", lambda: cm)
        r = MagicMock()
        r.publish.side_effect = ConnectionError("redis down")
        m = AgentMessenger(redis_client=r)

        assert m.send("a", "b", "x") is True

    def test_receive_claims_atomically(self, monkeypatch):
        cm, conn, cur = self._mock_conn(rows=[("a", "b", "hi", 1000.0, "", {"k": "v"})])
        monkeypatch.setattr("robothor.db.connection.get_connection", lambda: cm)
        m = AgentMessenger(redis_client=False)

        msgs = m.receive("b", tenant_id="t1")
        sql = cur.execute.call_args[0][0]
        assert "delivered_at = now()" in sql
        assert "FOR UPDATE SKIP LOCKED" in sql, (
            "without the locked claim, concurrent receivers double-deliver"
        )
        assert [x.content for x in msgs] == ["hi"]
        assert msgs[0].metadata == {"k": "v"}

    def test_send_with_metadata(self, monkeypatch):
        cm, _, cur = self._mock_conn()
        monkeypatch.setattr("robothor.db.connection.get_connection", lambda: cm)
        m = AgentMessenger(redis_client=False)
        assert m.send("a", "b", "msg", metadata={"key": "val"}) is True
        assert '"key": "val"' in cur.execute.call_args[0][1][5]


class TestMessengerSingleton:
    def test_init_and_get(self):
        from robothor.engine.messaging import get_messenger

        m = init_messenger(redis_client=MagicMock())
        assert get_messenger() is m

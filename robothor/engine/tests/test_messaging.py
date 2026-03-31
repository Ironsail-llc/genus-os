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
    def _mock_redis(self):
        r = MagicMock()
        r.lpush = MagicMock(return_value=1)
        r.expire = MagicMock()
        r.publish = MagicMock()
        r.rpop = MagicMock(return_value=None)
        r.llen = MagicMock(return_value=0)
        return r

    def test_send_success(self):
        r = self._mock_redis()
        m = AgentMessenger(redis_client=r)
        ok = m.send("agent-a", "agent-b", "hello")
        assert ok is True
        r.lpush.assert_called_once()
        r.publish.assert_called_once()

    def test_send_no_redis(self):
        m = AgentMessenger(redis_client=None)
        # Patch _get_redis to return None
        m._get_redis = lambda: None
        ok = m.send("a", "b", "x")
        assert ok is False

    def test_receive_empty(self):
        r = self._mock_redis()
        m = AgentMessenger(redis_client=r)
        msgs = m.receive("agent-a")
        assert msgs == []

    def test_receive_messages(self):
        msg1 = AgentMessage(from_agent="x", to_agent="a", content="hi")
        msg2 = AgentMessage(from_agent="y", to_agent="a", content="hey")

        r = self._mock_redis()
        # rpop returns messages then None
        r.rpop = MagicMock(
            side_effect=[
                msg1.to_json().encode(),
                msg2.to_json().encode(),
                None,
            ]
        )
        m = AgentMessenger(redis_client=r)
        msgs = m.receive("a", limit=10)
        assert len(msgs) == 2
        assert msgs[0].content == "hi"
        assert msgs[1].content == "hey"

    def test_broadcast(self):
        r = self._mock_redis()
        m = AgentMessenger(redis_client=r)
        count = m.broadcast("agent-a", "team1", ["agent-a", "agent-b", "agent-c"], "update")
        assert count == 2  # skips self

    def test_inbox_count(self):
        r = self._mock_redis()
        r.llen = MagicMock(return_value=5)
        m = AgentMessenger(redis_client=r)
        assert m.inbox_count("agent-a") == 5

    def test_send_with_metadata(self):
        r = self._mock_redis()
        m = AgentMessenger(redis_client=r)
        ok = m.send("a", "b", "msg", metadata={"key": "val"})
        assert ok is True


class TestMessengerSingleton:
    def test_init_and_get(self):
        from robothor.engine.messaging import get_messenger

        m = init_messenger(redis_client=MagicMock())
        assert get_messenger() is m

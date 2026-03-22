"""Tests for persistent chat session store."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.chat_store import (
    cleanup_stale_sessions,
    clear_session,
    load_all_sessions,
    load_session,
    save_exchange,
    save_message,
    update_model_override,
    upsert_session,
)


@pytest.fixture
def chat_db():
    """Mock the database connection for chat_store tests."""
    with patch("robothor.engine.chat_store.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.rowcount = 1
        cur.fetchone.return_value = {"id": 42}
        cur.fetchall.return_value = []
        mock_conn.return_value = conn
        yield {"connection": mock_conn, "conn": conn, "cursor": cur}


class TestUpsertSession:
    def test_upsert_creates_session(self, chat_db):
        result = upsert_session("telegram:123", channel="telegram")
        assert result == 42
        chat_db["cursor"].execute.assert_called_once()
        sql = chat_db["cursor"].execute.call_args[0][0]
        assert "INSERT INTO chat_sessions" in sql
        assert "ON CONFLICT" in sql

    def test_upsert_with_model_override(self, chat_db):
        result = upsert_session("telegram:123", model_override="anthropic/claude-sonnet-4.6")
        assert result == 42
        params = chat_db["cursor"].execute.call_args[0][1]
        assert "anthropic/claude-sonnet-4.6" in params


class TestSaveExchange:
    def test_saves_two_messages(self, chat_db):
        save_exchange("telegram:123", "Hello", "Hi there!")
        # Upsert + 2 message inserts = 3 execute calls
        assert chat_db["cursor"].execute.call_count == 3
        chat_db["conn"].commit.assert_called_once()

    def test_session_upsert_increments_count(self, chat_db):
        save_exchange("telegram:123", "Hello", "Hi!")
        upsert_sql = chat_db["cursor"].execute.call_args_list[0][0][0]
        assert "message_count" in upsert_sql
        assert "message_count + 2" in upsert_sql

    def test_messages_are_jsonb(self, chat_db):
        save_exchange("telegram:123", "Hello", "Hi!")
        # Second call = user message insert
        user_params = chat_db["cursor"].execute.call_args_list[1][0][1]
        user_msg = json.loads(user_params[1])
        assert user_msg == {"role": "user", "content": "Hello"}
        # Third call = assistant message insert
        asst_params = chat_db["cursor"].execute.call_args_list[2][0][1]
        asst_msg = json.loads(asst_params[1])
        assert asst_msg == {"role": "assistant", "content": "Hi!"}

    def test_model_override_coalesce(self, chat_db):
        """When model_override is None, COALESCE preserves existing value."""
        save_exchange("telegram:123", "Hello", "Hi!", model_override=None)
        upsert_sql = chat_db["cursor"].execute.call_args_list[0][0][0]
        assert "COALESCE" in upsert_sql
        upsert_params = chat_db["cursor"].execute.call_args_list[0][0][1]
        # model_override param should be None (both insert and COALESCE positions)
        assert upsert_params[3] is None
        assert upsert_params[4] is None

    def test_custom_channel_and_tenant(self, chat_db):
        save_exchange("web:abc", "Hi", "Hey!", channel="webchat", tenant_id="custom-tenant")
        upsert_params = chat_db["cursor"].execute.call_args_list[0][0][1]
        assert upsert_params[0] == "custom-tenant"
        assert upsert_params[1] == "web:abc"
        assert upsert_params[2] == "webchat"


class TestSaveMessage:
    def test_saves_single_message(self, chat_db):
        save_message("web:abc", "system", "Context injected")
        # Upsert + 1 message insert = 2 execute calls
        assert chat_db["cursor"].execute.call_count == 2
        chat_db["conn"].commit.assert_called_once()

    def test_message_role_and_content(self, chat_db):
        save_message("web:abc", "system", "You are a helpful assistant")
        msg_params = chat_db["cursor"].execute.call_args_list[1][0][1]
        msg = json.loads(msg_params[1])
        assert msg == {"role": "system", "content": "You are a helpful assistant"}


class TestLoadSession:
    def test_returns_empty_when_no_session(self, chat_db):
        chat_db["cursor"].fetchone.return_value = None
        result = load_session("telegram:999")
        assert result == {}

    def test_loads_messages_and_model_override(self, chat_db):
        # First fetchone = session row
        chat_db["cursor"].fetchone.return_value = {
            "id": 42,
            "model_override": "anthropic/claude-sonnet-4.6",
        }
        # fetchall = messages (in DESC order from DB, reversed in code)
        chat_db["cursor"].fetchall.return_value = [
            {"message": {"role": "assistant", "content": "Hi!"}},
            {"message": {"role": "user", "content": "Hello"}},
        ]

        result = load_session("telegram:123")
        assert result["model_override"] == "anthropic/claude-sonnet-4.6"
        # Messages should be reversed to chronological order
        assert result["history"][0]["role"] == "user"
        assert result["history"][1]["role"] == "assistant"

    def test_respects_limit(self, chat_db):
        chat_db["cursor"].fetchone.return_value = {"id": 42, "model_override": None}
        chat_db["cursor"].fetchall.return_value = []

        load_session("telegram:123", limit=10)
        # Check the LIMIT parameter in the messages query
        msg_query_params = chat_db["cursor"].execute.call_args_list[1][0][1]
        assert msg_query_params[1] == 10


class TestLoadAllSessions:
    def test_empty_db_returns_empty_dict(self, chat_db):
        chat_db["cursor"].fetchall.return_value = []
        result = load_all_sessions()
        assert result == {}

    def test_loads_multiple_sessions(self, chat_db):
        # First fetchall = session list
        sessions = [
            {"id": 1, "session_key": "telegram:111", "model_override": None},
            {"id": 2, "session_key": "web:abc", "model_override": "gemini/gemini-2.5-pro"},
        ]
        # Subsequent fetchall calls = messages for each session
        messages_1 = [{"message": {"role": "user", "content": "Hi"}}]
        messages_2 = [{"message": {"role": "user", "content": "Hey"}}]

        chat_db["cursor"].fetchall.side_effect = [sessions, messages_1, messages_2]

        result = load_all_sessions()
        assert "telegram:111" in result
        assert "web:abc" in result
        assert result["telegram:111"]["model_override"] is None
        assert result["web:abc"]["model_override"] == "gemini/gemini-2.5-pro"
        assert len(result["telegram:111"]["history"]) == 1
        assert len(result["web:abc"]["history"]) == 1

    def test_ttl_passed_to_query(self, chat_db):
        chat_db["cursor"].fetchall.return_value = []
        load_all_sessions(ttl_days=3)
        query_params = chat_db["cursor"].execute.call_args[0][1]
        assert 3 in query_params


class TestClearSession:
    def test_deletes_session(self, chat_db):
        result = clear_session("telegram:123")
        assert result is True
        sql = chat_db["cursor"].execute.call_args[0][0]
        assert "DELETE FROM chat_sessions" in sql
        chat_db["conn"].commit.assert_called_once()

    def test_returns_false_when_no_session(self, chat_db):
        chat_db["cursor"].rowcount = 0
        result = clear_session("telegram:999")
        assert result is False


class TestUpdateModelOverride:
    def test_updates_model(self, chat_db):
        update_model_override("telegram:123", "anthropic/claude-sonnet-4.6")
        sql = chat_db["cursor"].execute.call_args[0][0]
        assert "UPDATE chat_sessions" in sql
        assert "model_override" in sql
        params = chat_db["cursor"].execute.call_args[0][1]
        assert params[0] == "anthropic/claude-sonnet-4.6"
        chat_db["conn"].commit.assert_called_once()

    def test_clears_model_with_none(self, chat_db):
        update_model_override("telegram:123", None)
        params = chat_db["cursor"].execute.call_args[0][1]
        assert params[0] is None


class TestCleanupStaleSessions:
    def test_deletes_old_sessions(self, chat_db):
        chat_db["cursor"].rowcount = 5
        result = cleanup_stale_sessions(ttl_days=7)
        assert result == 5
        sql = chat_db["cursor"].execute.call_args[0][0]
        assert "DELETE FROM chat_sessions" in sql
        assert "last_active_at" in sql
        chat_db["conn"].commit.assert_called_once()

    def test_custom_ttl(self, chat_db):
        chat_db["cursor"].rowcount = 0
        result = cleanup_stale_sessions(ttl_days=14)
        assert result == 0
        params = chat_db["cursor"].execute.call_args[0][1]
        assert 14 in params

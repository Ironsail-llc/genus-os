"""Chat history pages: real timestamps and paging past the in-memory tail.

The in-memory session history is trimmed to ``MAX_HISTORY`` rows and carries
no time, so the dashboard stamped every loaded message "now" and could never
scroll back further than about twenty turns. ``chat_messages`` keeps every
row with its ``created_at``. Behind ``ROBOTHOR_CHAT_HISTORY_PAGES`` the history
endpoint reads its page from there; off, it answers exactly as before.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine import chat_store
from robothor.engine.chat import _sessions, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _rows(n: int, start_id: int = 1) -> list[dict]:
    """``n`` stored rows, newest first, the way the store query returns them."""
    rows = []
    for i in range(n):
        row_id = start_id + i
        rows.append(
            {
                "id": row_id,
                "message": {
                    "role": "user" if row_id % 2 else "assistant",
                    "content": f"row {row_id}",
                },
                "created_at": T0 + timedelta(minutes=row_id),
            }
        )
    return list(reversed(rows))


@pytest.fixture
def chat_db():
    with patch("robothor.engine.chat_store.get_connection") as mock_conn:
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []
        mock_conn.return_value = conn
        yield cur


class TestLoadHistoryPage:
    def test_a_page_is_chronological_with_each_rows_id_and_time(self, chat_db):
        chat_db.fetchall.return_value = _rows(3, start_id=10)

        page = chat_store.load_history_page("s:key", limit=5, tenant_id="t1")

        assert page is not None
        assert [m["content"] for m in page["messages"]] == ["row 10", "row 11", "row 12"]
        assert [m["id"] for m in page["messages"]] == [10, 11, 12]
        assert page["messages"][0]["timestamp"] == (T0 + timedelta(minutes=10)).isoformat()
        assert page["messages"][0]["role"] == "assistant"
        assert page["hasMore"] is False

    def test_one_row_beyond_the_limit_means_there_is_more(self, chat_db):
        # limit + 1 rows come back: the extra one is the proof, not part of the page
        chat_db.fetchall.return_value = _rows(51, start_id=70)

        page = chat_store.load_history_page("s:key", limit=50, tenant_id="t1")

        assert page is not None
        assert len(page["messages"]) == 50
        assert page["messages"][0]["id"] == 71
        assert page["messages"][-1]["id"] == 120
        assert page["hasMore"] is True

    def test_the_query_is_tenant_scoped_and_carries_limit_offset_and_cursor(self, chat_db):
        chat_db.fetchall.return_value = _rows(1)

        chat_store.load_history_page("s:key", limit=50, offset=7, before=71, tenant_id="t1")

        sql, params = chat_db.execute.call_args[0]
        assert "chat_messages" in sql and "chat_sessions" in sql
        assert "tenant_id" in sql
        assert "t1" in params and "s:key" in params
        assert 71 in params
        assert 51 in params  # limit + 1
        assert 7 in params

    def test_a_stored_page_cannot_serve_a_credential(self, chat_db):
        """Rows written before the store redacted on the way in still hold
        tokens; the in-memory path re-redacts them, and so must a page."""
        token = "ghp_FAKE0000_stored_before_redaction_0000"
        chat_db.fetchall.return_value = [
            {
                "id": 5,
                "message": {"role": "user", "content": f"here it is: {token}"},
                "created_at": T0,
            }
        ]

        page = chat_store.load_history_page("s:key", limit=50, tenant_id="t1")

        assert page is not None
        assert token not in json.dumps(page, default=str)
        assert "here it is" in page["messages"][0]["content"]
        assert page["messages"][0]["id"] == 5

    def test_nothing_stored_is_none(self, chat_db):
        chat_db.fetchall.return_value = []
        assert chat_store.load_history_page("s:key", limit=50, tenant_id="t1") is None


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    return runner


@pytest.fixture
async def client(engine_config, mock_runner):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    _sessions.clear()


async def _one_turn(client, mock_runner, key: str) -> None:
    mock_runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.COMPLETED, output_text="in memory", trigger_type=TriggerType.WEBCHAT
        )
    )
    with patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()):
        await client.post("/chat/send", json={"session_key": key, "message": "hello"})


class TestHistoryEndpoint:
    @pytest.mark.asyncio
    async def test_flag_off_answers_exactly_as_before(self, client, mock_runner, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_CHAT_HISTORY_PAGES", raising=False)
        await _one_turn(client, mock_runner, "off:key")
        store = MagicMock()
        with patch.object(chat_store, "load_history_page", store, create=True):
            res = await client.get("/chat/history?session_key=off:key&offset=10&before=5")

        body = res.json()
        assert set(body) == {"sessionKey", "messages", "recoveryScope"}
        assert [m["content"] for m in body["messages"]] == ["hello", "in memory"]
        store.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_on_reads_the_page_from_the_store_off_the_loop(
        self, client, mock_runner, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_CHAT_HISTORY_PAGES", "1")
        loop_thread = threading.current_thread()
        seen: dict = {}

        def fake_page(session_key, **kwargs):
            seen["thread"] = threading.current_thread()
            seen["key"] = session_key
            seen.update(kwargs)
            return {
                "messages": [
                    {"role": "user", "content": "stored", "id": 9, "timestamp": T0.isoformat()}
                ],
                "hasMore": True,
            }

        with patch.object(chat_store, "load_history_page", fake_page, create=True):
            res = await client.get("/chat/history?session_key=on:key&limit=20&offset=3&before=40")

        body = res.json()
        assert body["messages"] == [
            {"role": "user", "content": "stored", "id": 9, "timestamp": T0.isoformat()}
        ]
        assert body["hasMore"] is True
        assert body["sessionKey"] == "on:key"
        assert seen["key"] == "on:key"
        assert (seen["limit"], seen["offset"], seen["before"]) == (20, 3, 40)
        assert "tenant_id" in seen
        assert seen["thread"] is not loop_thread

    @pytest.mark.asyncio
    async def test_flag_on_first_page_falls_back_to_memory_when_the_store_has_nothing(
        self, client, mock_runner, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_CHAT_HISTORY_PAGES", "1")
        await _one_turn(client, mock_runner, "fallback:key")
        with patch.object(chat_store, "load_history_page", lambda *a, **k: None, create=True):
            res = await client.get("/chat/history?session_key=fallback:key")

        body = res.json()
        assert [m["content"] for m in body["messages"]] == ["hello", "in memory"]
        assert body["hasMore"] is False

    @pytest.mark.asyncio
    async def test_flag_on_first_page_falls_back_to_memory_when_the_store_fails(
        self, client, mock_runner, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_CHAT_HISTORY_PAGES", "1")
        await _one_turn(client, mock_runner, "broken:key")

        def broken(*a, **k):
            raise RuntimeError("database is down")

        with patch.object(chat_store, "load_history_page", broken, create=True):
            res = await client.get("/chat/history?session_key=broken:key")

        assert res.status_code == 200
        assert [m["content"] for m in res.json()["messages"]] == ["hello", "in memory"]
        assert res.json()["hasMore"] is False

    @pytest.mark.asyncio
    async def test_an_older_page_never_repeats_the_in_memory_tail(
        self, client, mock_runner, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_CHAT_HISTORY_PAGES", "1")
        await _one_turn(client, mock_runner, "older:key")
        with patch.object(chat_store, "load_history_page", lambda *a, **k: None, create=True):
            res = await client.get("/chat/history?session_key=older:key&before=3")

        assert res.json()["messages"] == []
        assert res.json()["hasMore"] is False


class TestTheUserTurnKeepsItsOwnTime:
    """Both rows of an exchange are written when the reply finishes, so a
    reloaded chat showed the prompt at the reply's time. The user row now
    carries the time the message arrived."""

    def test_save_exchange_stores_the_user_rows_own_time(self, chat_db):
        chat_db.fetchone.return_value = {"id": 1}
        asked = T0 - timedelta(seconds=40)

        chat_store.save_exchange("s:key", "question", "answer", tenant_id="t1", user_at=asked)

        inserts = [
            c for c in chat_db.execute.call_args_list if "INSERT INTO chat_messages" in c[0][0]
        ]
        assert len(inserts) == 2
        user_sql, user_params = inserts[0][0]
        assistant_sql, assistant_params = inserts[1][0]
        assert asked in user_params
        assert asked not in assistant_params
        assert "created_at" in user_sql

    def test_without_a_time_both_rows_take_the_database_clock(self, chat_db):
        chat_db.fetchone.return_value = {"id": 1}
        chat_store.save_exchange("s:key", "question", "answer", tenant_id="t1")
        inserts = [
            c for c in chat_db.execute.call_args_list if "INSERT INTO chat_messages" in c[0][0]
        ]
        assert all(
            None in params or "created_at" not in sql for sql, params in (c[0] for c in inserts)
        )

    @pytest.mark.asyncio
    async def test_webchat_send_records_when_the_message_arrived(self, client, mock_runner):
        before = datetime.now(UTC)

        async def slow_reply(**kwargs):
            return AgentRun(
                status=RunStatus.COMPLETED, output_text="later", trigger_type=TriggerType.WEBCHAT
            )

        mock_runner.execute = AsyncMock(side_effect=slow_reply)
        saved = AsyncMock()
        with patch("robothor.engine.chat.save_exchange_async", new=saved):
            await client.post("/chat/send", json={"session_key": "time:key", "message": "hi"})

        assert saved.await_count == 1
        user_at = saved.await_args.kwargs.get("user_at")
        assert isinstance(user_at, datetime)
        assert before <= user_at <= datetime.now(UTC)

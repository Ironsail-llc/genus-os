"""Tests for `robothor.memory.intents` — prospective/intent memory.

DB and embeddings are mocked. Covers the confirmation model (stated → active,
inferred → proposed, HMAC-gated promotion), status transitions, goal linkage,
warmup rendering, and handler gating.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory import intents

_FAKE_EMBEDDING = [0.0] * 1024


def _mock_conn(*, fetchone: object = None, fetchall: list | None = None, rowcount: int = 1):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = None
    return cm, cur


class TestUpsertIntent:
    @patch("robothor.memory.intents.get_connection")
    @patch.object(
        intents.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_stated_defaults_active(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchone=(1,))
        mock_conn.return_value = cm
        await intents.upsert_intent("Grow revenue", "double ARR", source="stated", tenant_id="t1")
        params = cur.execute.call_args.args[1]
        # param order puts status at index 5 (after tenant, title, description, horizon, due_at)
        assert params[5] == "active"

    @patch("robothor.memory.intents.get_connection")
    @patch.object(
        intents.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_inferred_defaults_proposed(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchone=(2,))
        mock_conn.return_value = cm
        await intents.upsert_intent("Reduce ops toil", source="inferred", tenant_id="t1")
        params = cur.execute.call_args.args[1]
        assert params[5] == "proposed"

    @patch.object(
        intents.llm_client, "get_embedding_async", new=AsyncMock(return_value=_FAKE_EMBEDDING)
    )
    @pytest.mark.asyncio
    async def test_rejects_bad_horizon(self) -> None:
        with pytest.raises(ValueError):
            await intents.upsert_intent("x", horizon="someday", tenant_id="t1")


class TestConfirmIntent:
    def test_invalid_token_rejected(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_INTENT_HMAC_SECRET": "s3cret"}):
            out = intents.confirm_intent(5, "deadbeef", tenant_id="t1")
        assert out["error"] == "invalid_token"

    @patch("robothor.memory.intents.get_connection")
    def test_valid_token_promotes(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=1)
        mock_conn.return_value = cm
        with patch.dict(os.environ, {"ROBOTHOR_INTENT_HMAC_SECRET": "s3cret"}):
            token = intents.expected_token(5)
            out = intents.confirm_intent(5, token, tenant_id="t1")
        assert out == {"ok": True, "id": 5, "status": "active"}

    @patch("robothor.memory.intents.get_connection")
    def test_valid_token_but_not_proposed(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=0)
        mock_conn.return_value = cm
        with patch.dict(os.environ, {"ROBOTHOR_INTENT_HMAC_SECRET": "s3cret"}):
            token = intents.expected_token(7)
            out = intents.confirm_intent(7, token, tenant_id="t1")
        assert out["error"] == "not_proposed_or_not_found"

    def test_token_roundtrip(self) -> None:
        with patch.dict(os.environ, {"ROBOTHOR_INTENT_HMAC_SECRET": "s3cret"}):
            tok = intents.expected_token(42)
            assert intents.verify_token(42, tok) is True
            assert intents.verify_token(43, tok) is False

    def test_verify_without_secret_is_false(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert intents.verify_token(1, "anything") is False


class TestTransitions:
    @patch("robothor.memory.intents.get_connection")
    def test_mark_advanced(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=1)
        mock_conn.return_value = cm
        assert intents.mark_advanced(5, tenant_id="t1") is True
        assert "last_advanced_at" in cur.execute.call_args.args[0]

    @patch("robothor.memory.intents.get_connection")
    def test_set_status_validates(self, mock_conn: MagicMock) -> None:
        with pytest.raises(ValueError):
            intents.set_status(5, "bogus", tenant_id="t1")

    @patch("robothor.memory.intents.get_connection")
    def test_link_goal_dedups_and_advances(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=1)
        mock_conn.return_value = cm
        assert intents.link_goal(5, 99, tenant_id="t1") is True
        sql = cur.execute.call_args.args[0]
        assert "linked_goal_ids" in sql and "last_advanced_at" in sql

    @patch("robothor.memory.intents.get_connection")
    def test_attribute_goal_completion_counts(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(rowcount=2)
        mock_conn.return_value = cm
        assert intents.attribute_goal_completion(99, tenant_id="t1") == 2


class TestWarmupRendering:
    @patch("robothor.memory.intents.list_active_intents")
    def test_none_when_empty(self, mock_list: MagicMock) -> None:
        mock_list.return_value = []
        assert intents.build_active_intents_context("t1") is None

    @patch("robothor.memory.intents.list_active_intents")
    def test_renders_titles_capped(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {
                "id": 1,
                "title": "Grow revenue",
                "horizon": "ongoing",
                "priority": 1,
                "last_advanced_at": None,
                "due_at": None,
            },
        ]
        text = intents.build_active_intents_context("t1")
        assert "Grow revenue" in text
        assert len(text) <= 600


class TestInference:
    @patch("robothor.memory.intents.upsert_intent", new_callable=AsyncMock)
    @patch("robothor.memory.intents.get_connection")
    @pytest.mark.asyncio
    async def test_infers_and_stores_proposed(
        self, mock_conn: MagicMock, mock_upsert: AsyncMock
    ) -> None:
        cm, cur = _mock_conn(fetchall=[{"fact_text": "Alice keeps asking about ARR growth."}])
        mock_conn.return_value = cm
        mock_upsert.return_value = 11

        payload = (
            '[{"title":"Grow ARR","description":"increase recurring revenue","confidence":0.7}]'
        )
        with patch.object(intents.llm_client, "generate", new=AsyncMock(return_value=payload)):
            created = await intents.infer_intents_from_facts(tenant_id="t1")

        assert created == [11]
        # inferred intents must be stored as source=inferred, status=proposed
        _, kwargs = mock_upsert.call_args
        assert kwargs["source"] == "inferred"
        assert kwargs["status"] == "proposed"

    @patch("robothor.memory.intents.get_connection")
    @pytest.mark.asyncio
    async def test_no_facts_returns_empty(self, mock_conn: MagicMock) -> None:
        cm, cur = _mock_conn(fetchall=[])
        mock_conn.return_value = cm
        assert await intents.infer_intents_from_facts(tenant_id="t1") == []


class TestHandlerGating:
    @pytest.mark.asyncio
    async def test_add_disabled_when_rip14_off(self) -> None:
        from robothor.engine.tools.handlers import intents as h

        with patch.object(h, "is_rip_enabled", return_value=False):
            out = await h._intent_add({"title": "x"}, MagicMock())
        assert "disabled" in out["error"]

    @pytest.mark.asyncio
    async def test_add_runs_when_rip14_on(self) -> None:
        from robothor.engine.tools.handlers import intents as h

        ctx = MagicMock()
        ctx.tenant_id = "t1"
        with (
            patch.object(h, "is_rip_enabled", return_value=True),
            patch("robothor.memory.intents.upsert_intent", new=AsyncMock(return_value=3)),
        ):
            out = await h._intent_add({"title": "Grow revenue"}, ctx)
        assert out["id"] == 3

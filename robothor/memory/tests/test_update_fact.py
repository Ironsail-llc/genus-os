"""Tests for `facts.update_fact()` — the Rip 7 drift-aware writer."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory.drift import compute_fact_hash


def _mock_conn_yielding(rows: list) -> MagicMock:
    """Build a mock get_connection() context manager.

    `rows` are returned in order from successive cur.fetchone() calls.
    """
    cur = MagicMock()
    cur.fetchone.side_effect = rows
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = None
    return cm, cur


@pytest.fixture(autouse=True)
def _embedding() -> Any:
    """No test here reaches a real embedding provider."""
    with patch(
        "robothor.memory.facts.llm_client.get_embedding_async",
        new=AsyncMock(return_value=[0.1, 0.2]),
    ) as embed:
        yield embed


class TestUpdateFactRip7Off:
    """When Rip 7 is off (default), drift detection is bypassed entirely."""

    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_proceeds_without_drift_check(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        # Stored row with a stale hash that would normally trip drift,
        # but Rip 7 off means we never look.
        cm, cur = _mock_conn_yielding([("old text", "t1", "preference", None, "stale_hash")])
        mock_get_conn.return_value = cm

        with patch.dict(os.environ, {}, clear=True):  # rip 7 off
            result = await update_fact(42, fact_text="new text", tenant_id="t1")

        assert result == {"ok": True, "fact_id": 42}
        # SELECT + UPDATE; no audit INSERT.
        assert cur.execute.call_count == 2
        assert "UPDATE memory_facts" in cur.execute.call_args_list[1].args[0]


class TestUpdateFactNotFound:
    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_returns_not_found(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        cm, cur = _mock_conn_yielding([None])
        mock_get_conn.return_value = cm

        result = await update_fact(999, fact_text="x", tenant_id="t")
        assert result == {"error": "not_found", "fact_id": 999}


class TestUpdateFactObserveMode:
    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_drift_logged_but_update_proceeds(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        # Stored content gives a known good hash; pretend the persisted
        # column holds something wrong — drift.
        good_hash = compute_fact_hash("stored text", tenant_id="t", category="preference")
        wrong_stored = "0" * 64
        # Order: SELECT, then audit INSERT (RETURNING id=7), then UPDATE
        cm, cur = _mock_conn_yielding(
            [
                ("stored text", "t", "preference", None, wrong_stored),  # SELECT
                (7,),  # audit RETURNING snapshot_id
                None,  # UPDATE has no RETURNING
            ]
        )
        mock_get_conn.return_value = cm
        _ = good_hash  # noqa — keep variable, lets the test self-document

        with patch.dict(os.environ, {"ROBOTHOR_RIP_7_ENABLED": "1"}, clear=True):
            result = await update_fact(42, fact_text="new text", tenant_id="t")

        assert result == {"ok": True, "fact_id": 42}
        # SELECT + audit INSERT + UPDATE = 3 executes
        assert cur.execute.call_count == 3
        assert "INSERT INTO memory_facts_audit" in cur.execute.call_args_list[1].args[0]
        assert "UPDATE memory_facts" in cur.execute.call_args_list[2].args[0]


class TestUpdateFactEnforceMode:
    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_drift_refuses_update(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        wrong_stored = "0" * 64
        cm, cur = _mock_conn_yielding(
            [
                ("stored text", "t", "preference", None, wrong_stored),  # SELECT
                (99,),  # audit RETURNING snapshot_id
            ]
        )
        mock_get_conn.return_value = cm

        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "enforce"},
            clear=True,
        ):
            result = await update_fact(42, fact_text="new text", tenant_id="t")

        assert result == {
            "error": "drift_refused",
            "fact_id": 42,
            "audit_snapshot_id": 99,
        }
        # SELECT + audit INSERT only — never UPDATE
        assert cur.execute.call_count == 2
        for call in cur.execute.call_args_list:
            assert "UPDATE memory_facts" not in call.args[0]


class TestUpdateFactCleanWrite:
    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_matching_hash_proceeds_without_audit(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        good_hash = compute_fact_hash("stored text", tenant_id="t", category="preference")
        cm, cur = _mock_conn_yielding(
            [
                ("stored text", "t", "preference", None, good_hash),  # SELECT
                None,  # UPDATE
            ]
        )
        mock_get_conn.return_value = cm

        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "enforce"},
            clear=True,
        ):
            result = await update_fact(42, fact_text="new text", tenant_id="t")

        assert result == {"ok": True, "fact_id": 42}
        assert cur.execute.call_count == 2  # SELECT + UPDATE, no audit
        assert "INSERT INTO memory_facts_audit" not in cur.execute.call_args_list[0].args[0]
        assert "UPDATE memory_facts" in cur.execute.call_args_list[1].args[0]


class TestUpdateFactNullStoredHashIsFirstTouch:
    """A NULL content_hash means the row was inserted before the
    backfill ran (or the backfill is still pending). Treat it as a
    fresh row and proceed without drift complaint."""

    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_null_stored_hash_proceeds(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        cm, cur = _mock_conn_yielding(
            [
                ("stored text", "t", "preference", None, None),  # SELECT, stored_hash=None
                None,  # UPDATE
            ]
        )
        mock_get_conn.return_value = cm

        with patch.dict(
            os.environ,
            {"ROBOTHOR_RIP_7_ENABLED": "1", "ROBOTHOR_RIP_7_MODE": "enforce"},
            clear=True,
        ):
            result = await update_fact(42, fact_text="new text", tenant_id="t")

        assert result == {"ok": True, "fact_id": 42}
        assert cur.execute.call_count == 2  # no audit, just SELECT + UPDATE


def _update_call(cur: MagicMock) -> tuple[str, tuple]:
    calls = [c for c in cur.execute.call_args_list if "UPDATE memory_facts" in c.args[0]]
    assert len(calls) == 1
    return calls[0].args[0], tuple(calls[0].args[1])


def _json_params(params: tuple) -> list[dict]:
    out = []
    for p in params:
        if isinstance(p, str):
            try:
                value = json.loads(p)
            except ValueError:
                continue
            if isinstance(value, dict):
                out.append(value)
    return out


class TestUpdateFactEmbedding:
    """A text change re-embeds the fact the way store_fact embeds a new one."""

    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_a_text_change_writes_a_fresh_embedding(
        self, mock_get_conn: MagicMock, _embedding: AsyncMock
    ) -> None:
        from robothor.memory.facts import update_fact

        cm, cur = _mock_conn_yielding([("old text", "t", "preference", None, None), None])
        mock_get_conn.return_value = cm
        with patch.dict(os.environ, {}, clear=True):
            result = await update_fact(42, fact_text="new text", tenant_id="t")

        assert result == {"ok": True, "fact_id": 42}
        _embedding.assert_awaited_once_with("new text")
        sql, params = _update_call(cur)
        assert "embedding" in sql
        assert [0.1, 0.2] in params
        # A fresh vector clears an earlier stale flag.
        assert ["embedding_stale"] in params
        assert not any(m.get("embedding_stale") for m in _json_params(params))

    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_the_same_text_leaves_the_embedding_alone(
        self, mock_get_conn: MagicMock, _embedding: AsyncMock
    ) -> None:
        from robothor.memory.facts import update_fact

        cm, cur = _mock_conn_yielding([("same text", "t", "preference", None, None), None])
        mock_get_conn.return_value = cm
        with patch.dict(os.environ, {}, clear=True):
            result = await update_fact(
                42, fact_text="same text", tenant_id="t", metadata={"note": "checked"}
            )

        assert result == {"ok": True, "fact_id": 42}
        _embedding.assert_not_awaited()
        _sql, params = _update_call(cur)
        assert [0.1, 0.2] not in params
        assert ["embedding_stale"] not in params

    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_an_embedding_failure_keeps_the_vector_and_marks_it_stale(
        self, mock_get_conn: MagicMock, _embedding: AsyncMock
    ) -> None:
        from robothor.memory.facts import update_fact

        _embedding.side_effect = RuntimeError("provider down")
        cm, cur = _mock_conn_yielding([("old text", "t", "preference", None, None), None])
        mock_get_conn.return_value = cm
        with patch.dict(os.environ, {}, clear=True):
            result = await update_fact(42, fact_text="new text", tenant_id="t")

        assert result == {"ok": True, "fact_id": 42}
        sql, params = _update_call(cur)
        assert "COALESCE(%s::vector, embedding)" in sql  # NULL keeps the old vector
        assert [0.1, 0.2] not in params
        assert any(m.get("embedding_stale") is True for m in _json_params(params))


class TestUpdateFactMetadataMerge:
    @patch("robothor.memory.facts.get_connection")
    @pytest.mark.asyncio
    async def test_metadata_is_merged_so_operator_confirmed_survives(
        self, mock_get_conn: MagicMock
    ) -> None:
        from robothor.memory.facts import update_fact

        cm, cur = _mock_conn_yielding([("same text", "t", "preference", None, None), None])
        mock_get_conn.return_value = cm
        with patch.dict(os.environ, {}, clear=True):
            await update_fact(42, fact_text="same text", tenant_id="t", metadata={"note": "x"})

        sql, params = _update_call(cur)
        # Merged onto the stored object, never a whole replacement that would
        # drop keys the caller did not send (operator_confirmed among them).
        assert "COALESCE(metadata, '{}'::jsonb)" in sql
        assert "||" in sql
        assert "COALESCE(%s::jsonb, metadata)" not in sql
        merged = _json_params(params)
        assert {"note": "x"} in merged
        assert not any("operator_confirmed" in m for m in merged)

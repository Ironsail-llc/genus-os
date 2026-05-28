"""Tests for `facts.update_fact()` — the Rip 7 drift-aware writer."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

from robothor.memory.drift import compute_fact_hash


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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


class TestUpdateFactRip7Off:
    """When Rip 7 is off (default), drift detection is bypassed entirely."""

    @patch("robothor.memory.facts.get_connection")
    def test_proceeds_without_drift_check(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        # Stored row with a stale hash that would normally trip drift,
        # but Rip 7 off means we never look.
        cm, cur = _mock_conn_yielding([("old text", "t1", "preference", None, "stale_hash")])
        mock_get_conn.return_value = cm

        with patch.dict(os.environ, {}, clear=True):  # rip 7 off
            result = _run(update_fact(42, fact_text="new text", tenant_id="t1"))

        assert result == {"ok": True, "fact_id": 42}
        # SELECT + UPDATE; no audit INSERT.
        assert cur.execute.call_count == 2
        assert "UPDATE memory_facts" in cur.execute.call_args_list[1].args[0]


class TestUpdateFactNotFound:
    @patch("robothor.memory.facts.get_connection")
    def test_returns_not_found(self, mock_get_conn: MagicMock) -> None:
        from robothor.memory.facts import update_fact

        cm, cur = _mock_conn_yielding([None])
        mock_get_conn.return_value = cm

        result = _run(update_fact(999, fact_text="x", tenant_id="t"))
        assert result == {"error": "not_found", "fact_id": 999}


class TestUpdateFactObserveMode:
    @patch("robothor.memory.facts.get_connection")
    def test_drift_logged_but_update_proceeds(self, mock_get_conn: MagicMock) -> None:
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
            result = _run(update_fact(42, fact_text="new text", tenant_id="t"))

        assert result == {"ok": True, "fact_id": 42}
        # SELECT + audit INSERT + UPDATE = 3 executes
        assert cur.execute.call_count == 3
        assert "INSERT INTO memory_facts_audit" in cur.execute.call_args_list[1].args[0]
        assert "UPDATE memory_facts" in cur.execute.call_args_list[2].args[0]


class TestUpdateFactEnforceMode:
    @patch("robothor.memory.facts.get_connection")
    def test_drift_refuses_update(self, mock_get_conn: MagicMock) -> None:
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
            result = _run(update_fact(42, fact_text="new text", tenant_id="t"))

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
    def test_matching_hash_proceeds_without_audit(self, mock_get_conn: MagicMock) -> None:
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
            result = _run(update_fact(42, fact_text="new text", tenant_id="t"))

        assert result == {"ok": True, "fact_id": 42}
        assert cur.execute.call_count == 2  # SELECT + UPDATE, no audit
        assert "INSERT INTO memory_facts_audit" not in cur.execute.call_args_list[0].args[0]
        assert "UPDATE memory_facts" in cur.execute.call_args_list[1].args[0]


class TestUpdateFactNullStoredHashIsFirstTouch:
    """A NULL content_hash means the row was inserted before the
    backfill ran (or the backfill is still pending). Treat it as a
    fresh row and proceed without drift complaint."""

    @patch("robothor.memory.facts.get_connection")
    def test_null_stored_hash_proceeds(self, mock_get_conn: MagicMock) -> None:
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
            result = _run(update_fact(42, fact_text="new text", tenant_id="t"))

        assert result == {"ok": True, "fact_id": 42}
        assert cur.execute.call_count == 2  # no audit, just SELECT + UPDATE

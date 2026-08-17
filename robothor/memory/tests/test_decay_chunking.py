"""Tests for PR-4: chunked, budgeted decay pass run off the event loop.

lifecycle.py Step 2 used to SELECT all ~29k active facts and UPDATE each one
inside a single synchronous transaction on the asyncio event loop —
200-330s on the production corpus, long enough to starve the loop and trip
systemd's watchdog (WatchdogSec=300, pinged every 30s from daemon.py). These
tests demand:

  1. chunked UPDATEs across multiple get_connection() acquisitions/commits,
     not one giant transaction;
  2. a wall-clock budget that stops the pass cleanly mid-pass and reports
     what's left, without raising;
  3. NULL last_accessed rows skipped + counted instead of raising (the
     decay formula itself, compute_decay_score, is NOT touched — see
     scripts/memory_decay_dryrun.py for why); and
  4. the sync DB work dispatched via asyncio.to_thread, never on the loop.

DB-mocking follows the convention in test_consolidation_guard.py: build a
(ctx, cur) MagicMock pair, patch lifecycle.get_connection to return it, and
assert on cur.execute.call_args_list / the mock's call_count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import chain, repeat
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import robothor.memory.lifecycle as lc


def _mock_conn() -> tuple[Any, Any]:
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


def _fact(fact_id: int, last_accessed: Any = "unset") -> dict[str, Any]:
    return {
        "id": fact_id,
        "last_accessed": datetime.now(UTC) if last_accessed == "unset" else last_accessed,
        "access_count": 1,
        "reinforcement_count": 0,
        "importance_score": 0.5,
        "outcome_failures": 0,
    }


class TestChunkedCommits:
    """The pass must NOT be one giant transaction over all active facts."""

    def test_updates_span_multiple_get_connection_acquisitions(self) -> None:
        chunk_size = lc._DECAY_CHUNK_SIZE
        n_facts = chunk_size * 2 + 5  # forces 3 UPDATE chunks
        facts = [_fact(i) for i in range(n_facts)]

        ctx, cur = _mock_conn()
        cur.fetchall.return_value = facts

        with patch.object(lc, "get_connection", return_value=ctx) as mock_get_conn:
            result = lc._run_decay_pass_sync(budget_s=600.0)

        # 1 acquisition to SELECT + 3 acquisitions to UPDATE (500, 500, 5) —
        # never a single acquisition covering the whole pass.
        assert mock_get_conn.call_count == 4
        assert result["updated"] == n_facts

    def test_update_statements_issued_per_row_in_chunk_sized_batches(self) -> None:
        chunk_size = lc._DECAY_CHUNK_SIZE
        facts = [_fact(i) for i in range(chunk_size + 1)]
        ctx, cur = _mock_conn()
        cur.fetchall.return_value = facts

        with patch.object(lc, "get_connection", return_value=ctx):
            lc._run_decay_pass_sync(budget_s=600.0)

        update_calls = [
            c for c in cur.execute.call_args_list if "UPDATE memory_facts" in c.args[0]
        ]
        assert len(update_calls) == chunk_size + 1


class TestBudget:
    """A wall-clock budget must stop the pass cleanly, not run unbounded."""

    def test_budget_exceeded_mid_pass_stops_without_raising_and_reports_remaining(
        self, caplog: Any
    ) -> None:
        chunk_size = lc._DECAY_CHUNK_SIZE
        n_facts = chunk_size * 3
        facts = [_fact(i) for i in range(n_facts)]
        ctx, cur = _mock_conn()
        cur.fetchall.return_value = facts

        # t_start=0, first budget check=0 (chunk 1 proceeds), every check
        # after that reports far past budget (chunks 2+ must not run).
        clock = chain([0, 0], repeat(1000))

        with (
            patch.object(lc, "get_connection", return_value=ctx),
            patch.object(lc.time, "monotonic", side_effect=lambda: next(clock)),
            caplog.at_level("WARNING"),
        ):
            result = lc._run_decay_pass_sync(budget_s=1.0)

        assert result["budget_exhausted"] is True
        assert result["updated"] == chunk_size  # only the first chunk ran
        assert result["remaining"] == n_facts - chunk_size
        assert any("remaining" in r.message for r in caplog.records)

    def test_budget_not_exceeded_processes_everything(self) -> None:
        facts = [_fact(i) for i in range(10)]
        ctx, cur = _mock_conn()
        cur.fetchall.return_value = facts

        with patch.object(lc, "get_connection", return_value=ctx):
            result = lc._run_decay_pass_sync(budget_s=600.0)

        assert result["budget_exhausted"] is False
        assert result["updated"] == 10
        assert result["remaining"] == 0


class TestNullTolerance:
    """NULL last_accessed must be skipped + counted, never raise."""

    def test_null_last_accessed_skipped_and_counted_pass_continues(self) -> None:
        facts = [_fact(1), _fact(2, last_accessed=None), _fact(3)]
        ctx, cur = _mock_conn()
        cur.fetchall.return_value = facts

        with patch.object(lc, "get_connection", return_value=ctx):
            result = lc._run_decay_pass_sync(budget_s=600.0)

        assert result["updated"] == 2
        assert result["skipped_null"] == 1

    def test_null_row_does_not_raise_attribute_error(self) -> None:
        facts = [_fact(1, last_accessed=None)]
        ctx, cur = _mock_conn()
        cur.fetchall.return_value = facts

        with patch.object(lc, "get_connection", return_value=ctx):
            # Must not raise AttributeError from compute_decay_score(None.tzinfo).
            result = lc._run_decay_pass_sync(budget_s=600.0)

        assert result["updated"] == 0
        assert result["skipped_null"] == 1


class TestOffLoop:
    """The decay pass must run via asyncio.to_thread, never inline on the loop."""

    async def test_decay_step_dispatches_sync_pass_via_to_thread(self, monkeypatch: Any) -> None:
        fake_result = {
            "updated": 7,
            "skipped_null": 0,
            "budget_exhausted": False,
            "remaining": 0,
        }
        to_thread_mock = AsyncMock(return_value=fake_result)
        monkeypatch.setattr(lc.asyncio, "to_thread", to_thread_mock)

        updated = await lc._run_decay_step()

        to_thread_mock.assert_called_once()
        assert to_thread_mock.call_args.args[0] is lc._run_decay_pass_sync
        assert updated == 7

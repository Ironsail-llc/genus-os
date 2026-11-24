"""Recall counts as use: the search tool and warm-up record what they returned.

The write is best-effort and off the request path. A read-only caller (the eval
harness, anything that must have no side effects) passes ``record_access=False``.
``search_facts`` itself never writes; recording lives at these two call sites.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tests.test_offloop_sync_calls import SlowProbe, warm_executor
from robothor.engine.tools.dispatch import ToolContext

TENANT = "test-tenant"

_ROWS = [
    {"id": 11, "fact_text": "Alice prefers email.", "category": "preference", "source": "fact"},
    {"id": 12, "fact_text": "Bob leads billing.", "category": "person"},
    {"id": 13, "fact_text": "Alice met Bob.", "category": "event", "source": "entity_expansion"},
    {"id": 99, "insight_text": "Billing slips on Fridays.", "source": "insight"},
]


def _ctx() -> ToolContext:
    return ToolContext(agent_id="main", tenant_id=TENANT)


async def _search(**kwargs: Any) -> dict[str, Any]:
    from robothor.engine.task_registry import get_task_registry
    from robothor.engine.tools.handlers import memory as handler

    with (
        patch.object(handler, "_recall_fallback", AsyncMock(return_value=(list(_ROWS), "lookup"))),
        patch("robothor.engine.feature_flags.is_rip_enabled", return_value=False),
    ):
        out = await handler._search_memory({"query": "billing"}, _ctx(), **kwargs)
    await get_task_registry().drain(timeout=5.0)
    return out


class TestSearchMemoryRecordsAccess:
    @pytest.mark.asyncio
    async def test_returned_facts_are_recorded_for_the_callers_tenant(self) -> None:
        record = MagicMock()
        with patch("robothor.memory.outcomes.record_fact_access", record):
            out = await _search()

        assert len(out["results"]) == 4
        record.assert_called_once()
        args, kwargs = record.call_args
        assert args == (TENANT,)
        # Facts only: an insight's id is not a memory_facts id.
        assert sorted(kwargs["fact_ids"]) == [11, 12, 13]

    @pytest.mark.asyncio
    async def test_a_read_only_caller_records_nothing(self) -> None:
        record = MagicMock()
        with patch("robothor.memory.outcomes.record_fact_access", record):
            out = await _search(record_access=False)

        assert len(out["results"]) == 4
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failing_write_does_not_fail_the_search(self) -> None:
        record = MagicMock(side_effect=RuntimeError("database is down"))
        with patch("robothor.memory.outcomes.record_fact_access", record):
            out = await _search()

        record.assert_called_once()
        assert [r["id"] for r in out["results"]] == [11, 12, 13, 99]

    @pytest.mark.asyncio
    async def test_failing_to_schedule_the_write_does_not_fail_the_search(self) -> None:
        registry = MagicMock()
        registry.spawn.side_effect = RuntimeError("no loop slot")
        with (
            patch("robothor.memory.outcomes.record_fact_access", MagicMock()),
            patch("robothor.engine.task_registry.get_task_registry", return_value=registry),
        ):
            out = await _search()

        assert len(out["results"]) == 4

    @pytest.mark.asyncio
    async def test_the_write_is_off_the_request_path_and_off_the_loop(self) -> None:
        """The search returns without waiting for the write, and the write does
        not hold the event loop while it runs."""
        from robothor.engine.task_registry import get_task_registry
        from robothor.engine.tools.handlers import memory as handler

        await warm_executor()
        probe = SlowProbe(asyncio.get_running_loop())
        with (
            patch("robothor.memory.outcomes.record_fact_access", probe),
            patch.object(
                handler, "_recall_fallback", AsyncMock(return_value=(list(_ROWS), "lookup"))
            ),
            patch("robothor.engine.feature_flags.is_rip_enabled", return_value=False),
        ):
            started = time.monotonic()
            await handler._search_memory({"query": "billing"}, _ctx())
            returned_after = time.monotonic() - started
            await get_task_registry().drain(timeout=5.0)

        assert returned_after < 0.1, f"search waited {returned_after * 1000:.0f} ms for the write"
        await probe.settle()
        probe.assert_loop_free()


class TestWarmupRecordsAccess:
    def _rows(self) -> list[dict[str, Any]]:
        return [
            {
                "candidate_ordinal": 1,
                "fact_text": "Alice works on the billing team.",
                "category": "person",
                "importance_score": 0.8,
                "person_id": None,
            },
            {
                "candidate_ordinal": 1,
                "fact_text": "Alice asked for weekly summaries.",
                "category": "preference",
                "importance_score": 0.7,
                "person_id": None,
            },
        ]

    def _build(self, **kwargs: Any) -> tuple[str, MagicMock]:
        from robothor.engine import warmup

        defer = MagicMock()
        with (
            patch("robothor.db.get_connection") as conn,
            patch.object(warmup, "_defer_write", defer),
        ):
            cur = conn.return_value.__enter__.return_value.cursor.return_value
            cur.fetchall.return_value = self._rows()
            text = warmup._build_entity_context(
                "Tell Alice about the invoice", tenant_id=TENANT, **kwargs
            )
        return text, defer

    def test_injected_facts_are_recorded_by_content_hash(self) -> None:
        from robothor.memory.drift import compute_fact_hash
        from robothor.memory.outcomes import record_fact_access

        text, defer = self._build()

        assert "Alice works on the billing team." in text
        defer.assert_called_once()
        fn, *args = defer.call_args.args
        assert fn is record_fact_access
        assert args[0] == TENANT
        expected = {
            compute_fact_hash(r["fact_text"], tenant_id=TENANT, category=r["category"])
            for r in self._rows()
        }
        assert set(args[1]) == expected

    def test_a_read_only_warmup_records_nothing(self) -> None:
        text, defer = self._build(record_access=False)

        assert "Alice works on the billing team." in text
        defer.assert_not_called()

    def test_a_failure_to_schedule_does_not_empty_the_recall(self) -> None:
        from robothor.engine import warmup

        with (
            patch("robothor.db.get_connection") as conn,
            patch.object(warmup, "_defer_write", MagicMock(side_effect=RuntimeError("pool gone"))),
        ):
            cur = conn.return_value.__enter__.return_value.cursor.return_value
            cur.fetchall.return_value = self._rows()
            text = warmup._build_entity_context("Tell Alice about the invoice", tenant_id=TENANT)

        assert "Alice asked for weekly summaries." in text


class TestRecordingRespectsTheBenchmarkSandbox:
    def test_a_benchmark_run_writes_nothing(self) -> None:
        """A benchmark child reads under a real tenant; its reads must not
        count as use, the same rule log_fact_access follows."""
        from robothor.memory import outcomes

        conn = MagicMock()
        with (
            patch("robothor.engine.run_context.benchmark_write_refused", return_value=True),
            patch.object(outcomes, "get_connection", conn),
        ):
            outcomes.record_fact_access(TENANT, fact_ids=[1, 2])

        conn.assert_not_called()

    def test_nothing_to_record_opens_no_connection(self) -> None:
        from robothor.memory import outcomes

        conn = MagicMock()
        with patch.object(outcomes, "get_connection", conn):
            outcomes.record_fact_access(TENANT)

        conn.assert_not_called()

"""Recall counts as use: the search tool and warm-up record what they returned.

The write is best-effort and off the request path. A read-only caller (the eval
harness, anything that must have no side effects) passes ``record_access=False``.
``search_facts`` itself never writes; recording lives at these two call sites.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

TENANT = "test-tenant"

#: Generous ceilings that only a broken (deadlocked) handler reaches; nothing
#: asserts on elapsed time.
_GATE_TIMEOUT_S = 10.0

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
    async def test_segment_and_graph_only_rows_are_never_recorded_or_logged(self) -> None:
        """A segment's id and a graph-only row are not memory_facts rows this
        caller was shown as facts: neither access recording nor the per-run
        access log (which outcome utility credits) may count them."""
        from robothor.engine.task_registry import get_task_registry
        from robothor.engine.tools.handlers import memory as handler

        rows = [
            {"id": 11, "fact_text": "Alice prefers email.", "kind": "fact", "source": "fact"},
            {
                "id": 12,
                "fact_text": "[user] the locker code",
                "kind": "segment",
                "source": "segment",
            },
            {"id": 13, "fact_text": "Bob leads billing.", "kind": "fact", "source": "graph"},
        ]
        record, log = MagicMock(), MagicMock()
        ctx = ToolContext(agent_id="main", tenant_id=TENANT, run_id="run-1")
        with (
            patch.object(handler, "_recall_fallback", AsyncMock(return_value=(rows, "lookup"))),
            patch("robothor.engine.feature_flags.is_rip_enabled", return_value=False),
            patch("robothor.memory.outcomes.record_fact_access", record),
            patch("robothor.memory.outcomes.log_fact_access", log),
        ):
            await handler._search_memory({"query": "billing"}, ctx)
        await get_task_registry().drain(timeout=5.0)

        assert record.call_args.kwargs["fact_ids"] == [11]
        assert log.call_args.args[1] == [11]

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
        registry.drain = AsyncMock()
        with (
            patch("robothor.memory.outcomes.record_fact_access", MagicMock()),
            patch("robothor.engine.task_registry.get_task_registry", return_value=registry),
        ):
            out = await _search()

        assert len(out["results"]) == 4

    @pytest.mark.asyncio
    async def test_the_write_is_off_the_request_path_and_off_the_loop(self) -> None:
        """Proved by ordering, not timing, so a loaded machine cannot fail it.

        The fake write blocks on an event the test sets only after the handler
        has returned. A handler that awaited the write could not return, and
        times out with a clear message. The write records the thread it runs
        on, which must not be the loop's, and while it is still blocked the loop
        must still run another task to completion.
        """
        from robothor.engine.task_registry import get_task_registry
        from robothor.engine.tools.handlers import memory as handler

        loop_thread = threading.get_ident()
        entered = threading.Event()
        release = threading.Event()
        seen: dict[str, Any] = {}

        def blocked_write(*_args: Any, **_kwargs: Any) -> None:
            seen["thread"] = threading.get_ident()
            entered.set()
            if not release.wait(timeout=_GATE_TIMEOUT_S):
                # Only reached when the write runs on the loop thread: the test
                # cannot get to release.set() while the loop is held here.
                seen["never_released"] = True
                return
            seen["finished"] = True

        try:
            with (
                patch("robothor.memory.outcomes.record_fact_access", blocked_write),
                patch.object(
                    handler, "_recall_fallback", AsyncMock(return_value=(list(_ROWS), "lookup"))
                ),
                patch("robothor.engine.feature_flags.is_rip_enabled", return_value=False),
            ):
                try:
                    out = await asyncio.wait_for(
                        handler._search_memory({"query": "billing"}, _ctx()),
                        timeout=_GATE_TIMEOUT_S,
                    )
                except TimeoutError:
                    pytest.fail("search_memory did not return while the access write was blocked")

                assert "finished" not in seen, "search_memory returned only after the write ran"
                assert len(out["results"]) == 4

                # The write starts in the background; wait for it without
                # blocking the loop.
                assert await asyncio.to_thread(entered.wait, _GATE_TIMEOUT_S), (
                    "the access write was never started"
                )
                assert seen["thread"] != loop_thread, "the access write runs on the event loop"

                # The loop still services other work while the write is blocked.
                ran = asyncio.Event()
                asyncio.get_running_loop().call_soon(ran.set)
                await asyncio.wait_for(ran.wait(), timeout=_GATE_TIMEOUT_S)
                assert "finished" not in seen
        finally:
            release.set()

        await get_task_registry().drain(timeout=_GATE_TIMEOUT_S)
        assert seen.get("finished") is True
        assert "never_released" not in seen


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
        assert args == [TENANT]
        expected = {
            compute_fact_hash(r["fact_text"], tenant_id=TENANT, category=r["category"])
            for r in self._rows()
        }
        assert set(defer.call_args.kwargs["content_hashes"]) == expected

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

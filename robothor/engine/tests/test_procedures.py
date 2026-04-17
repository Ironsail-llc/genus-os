"""Tests for procedural memory (robothor.memory.procedures)."""

from __future__ import annotations

import pytest

from robothor.memory.procedures import _confidence_from_counts


class TestConfidenceFromCounts:
    def test_no_data_returns_neutral(self):
        assert _confidence_from_counts(0, 0) == 0.5

    def test_all_successes_trends_high(self):
        # 10 successes, 0 failures — should be well above 0.5
        c = _confidence_from_counts(10, 0)
        assert c > 0.8

    def test_all_failures_trends_low(self):
        c = _confidence_from_counts(0, 10)
        assert c < 0.2

    def test_even_split_stays_middle(self):
        c = _confidence_from_counts(5, 5)
        assert 0.4 < c < 0.6

    def test_thin_evidence_shrinks_toward_neutral(self):
        # 1 success, 0 failures: raw is 1.0 but thin evidence shrinks it.
        c = _confidence_from_counts(1, 0)
        assert 0.5 < c < 0.7  # not 1.0

    def test_more_evidence_trusts_ratio(self):
        # 20 successes, 0 failures — confidence should approach 1.0.
        c = _confidence_from_counts(20, 0)
        assert c > 0.9


@pytest.mark.integration
class TestRecordFindRoundtrip:
    """End-to-end CRUD test against the real DB (requires migration 041)."""

    @pytest.mark.asyncio
    async def test_record_find_report_cycle(self, monkeypatch):
        from robothor.memory import procedures

        async def _fake_embed(text):
            # Deterministic 1024-dim vector so different names don't collide.
            base = abs(hash(text)) % 1000 / 1000.0
            return [base] * 1024

        monkeypatch.setattr(
            "robothor.memory.procedures.llm_client.get_embedding_async", _fake_embed
        )

        pid = await procedures.record_procedure(
            name="__test_proc_roundtrip__",
            steps=["Step one", "Step two", "Step three"],
            description="Test procedure, safe to delete",
            applicable_tags=["__test__", "roundtrip"],
            created_by_agent="pytest",
        )
        assert isinstance(pid, int) and pid > 0

        try:
            results = await procedures.find_applicable_procedures(
                task_description="Run the test procedure",
                tags=["__test__"],
                limit=5,
            )
            ids = [r["id"] for r in results]
            assert pid in ids

            outcome = await procedures.report_procedure_outcome(pid, success=True)
            assert outcome["success_count"] == 1
            assert outcome["failure_count"] == 0
            assert outcome["confidence"] > 0.5

            outcome2 = await procedures.report_procedure_outcome(pid, success=False)
            assert outcome2["failure_count"] == 1
            assert outcome2["confidence"] < outcome["confidence"]

            proc = await procedures.get_procedure(pid)
            assert proc is not None
            assert proc["name"] == "__test_proc_roundtrip__"
            assert proc["success_count"] == 1
            assert proc["failure_count"] == 1
        finally:
            # Cleanup
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM memory_procedures WHERE name = %s",
                    ("__test_proc_roundtrip__",),
                )

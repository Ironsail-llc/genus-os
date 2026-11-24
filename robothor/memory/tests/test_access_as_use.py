"""Recall counts as use, and a fact in use is not pruned by age alone.

``memory_facts.access_count`` and ``last_accessed`` are two of the decay
formula's five inputs, and the prune sweep retires a fact only when
``access_count = 0``. Nothing wrote either column, so every fact looked unused
and the sweep retired facts agents were reading every day on age alone.

These run on a throwaway database migrated through the full chain: the decay
pass and the prune sweep are instance-wide, so on the shared test database they
would rescore and lock every other suite's rows.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

pytestmark = pytest.mark.integration

_OLD = "NOW() - INTERVAL '400 days'"


@pytest.fixture
def facts_db(scratch_db):
    """Point every ``get_connection`` at a fully migrated scratch database."""
    from robothor.db.migrate import _discover
    from tests import conftest_integration as ci

    last = list(_discover())[-1].migration_id
    _, dsn = scratch_db(through=last)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant, "Test Tenant"),
        )
    conn.commit()

    ci._current_holder["conn"] = conn
    ci._current_holder["wrapper"] = conn
    try:
        yield conn, tenant
    finally:
        ci._current_holder["conn"] = None
        ci._current_holder["wrapper"] = None
        conn.close()


def _seed(conn: Any, tenant: str, text: str, **cols: Any) -> int:
    """Insert one old, low-importance, never-recalled fact (and any overrides)."""
    from robothor.memory.drift import compute_fact_hash

    base: dict[str, Any] = {
        "fact_text": text,
        "category": "event",
        "tenant_id": tenant,
        "importance_score": 0.2,
        "content_hash": compute_fact_hash(text, tenant_id=tenant, category="event"),
    }
    base.update(cols)
    names = ", ".join(base)
    marks = ", ".join(["%s"] * len(base))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO memory_facts ({names}, created_at, last_accessed) "
            f"VALUES ({marks}, {_OLD}, {_OLD}) RETURNING id",
            list(base.values()),
        )
        fact_id = int(cur.fetchone()[0])
    conn.commit()
    return fact_id


def _row(conn: Any, fact_id: int) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT is_active, access_count, last_accessed, metadata, importance_score "
            "FROM memory_facts WHERE id = %s",
            (fact_id,),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row)


class TestRecordingAccess:
    def test_recording_by_id_raises_count_and_refreshes_last_accessed(self, facts_db) -> None:
        from robothor.memory.outcomes import record_fact_access

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice prefers the quarterly report as a PDF.")
        before = _row(conn, fact_id)

        record_fact_access(tenant, fact_ids=[fact_id])
        record_fact_access(tenant, fact_ids=[fact_id])

        after = _row(conn, fact_id)
        assert after["access_count"] == before["access_count"] + 2
        assert after["last_accessed"] > before["last_accessed"]

    def test_recording_by_content_hash_covers_rows_returned_without_an_id(self, facts_db) -> None:
        """Warm-up's entity recall returns text, not ids; the active
        (tenant_id, content_hash) pair is unique, so the hash finds the row."""
        from robothor.memory.drift import compute_fact_hash
        from robothor.memory.outcomes import record_fact_access

        conn, tenant = facts_db
        text = "Bob owns the release checklist."
        fact_id = _seed(conn, tenant, text)

        record_fact_access(
            tenant,
            content_hashes=[compute_fact_hash(text, tenant_id=tenant, category="event")],
        )

        assert _row(conn, fact_id)["access_count"] == 1

    def test_recording_never_touches_another_tenants_fact(self, facts_db) -> None:
        from robothor.memory.outcomes import record_fact_access

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice is on call this week.")

        record_fact_access("some-other-tenant", fact_ids=[fact_id])

        assert _row(conn, fact_id)["access_count"] == 0

    def test_a_database_failure_is_swallowed(self, facts_db, monkeypatch) -> None:
        """Best-effort: the caller is a retrieval, so the write never raises."""
        from robothor.memory import outcomes

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise psycopg2.OperationalError("connection refused")

        monkeypatch.setattr(outcomes, "get_connection", _boom)
        outcomes.record_fact_access(facts_db[1], fact_ids=[1])


class TestPruneReadsRealUse:
    @pytest.mark.asyncio
    async def test_recently_recalled_old_fact_survives_and_unrecalled_one_is_retired(
        self, facts_db
    ) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync, prune_low_quality_facts
        from robothor.memory.outcomes import record_fact_access

        conn, tenant = facts_db
        recalled = _seed(conn, tenant, "Alice approved the vendor renewal in March.")
        never = _seed(conn, tenant, "Bob mentioned the office plants once.")

        record_fact_access(tenant, fact_ids=[recalled])
        _run_decay_pass_sync(tenant_id=tenant)
        await prune_low_quality_facts(tenant_id=tenant)

        assert _row(conn, recalled)["is_active"] is True
        assert _row(conn, never)["is_active"] is False

    @pytest.mark.asyncio
    async def test_a_fact_search_memory_returned_survives_prune(self, facts_db) -> None:
        """End to end: the agent-facing tool returns the fact, the write lands
        off the request path, and the next maintenance pass keeps it."""
        from unittest.mock import AsyncMock, patch

        from robothor.engine.task_registry import get_task_registry
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers import memory as handler
        from robothor.memory.lifecycle import _run_decay_pass_sync, prune_low_quality_facts

        conn, tenant = facts_db
        text = "Alice signs every purchase order over the threshold."
        recalled = _seed(conn, tenant, text)
        never = _seed(conn, tenant, "Bob once said the coffee machine was loud.")
        row = {"id": recalled, "fact_text": text, "category": "event", "source": "fact"}

        with (
            patch.object(handler, "_recall_fallback", AsyncMock(return_value=([row], "lookup"))),
            patch("robothor.engine.feature_flags.is_rip_enabled", return_value=False),
        ):
            out = await handler._search_memory(
                {"query": "purchase orders"}, ToolContext(agent_id="main", tenant_id=tenant)
            )
        await get_task_registry().drain(timeout=5.0)

        assert [r["id"] for r in out["results"]] == [recalled]
        _run_decay_pass_sync(tenant_id=tenant)
        await prune_low_quality_facts(tenant_id=tenant)
        assert _row(conn, recalled)["is_active"] is True
        assert _row(conn, recalled)["access_count"] == 1
        assert _row(conn, never)["is_active"] is False

    @pytest.mark.asyncio
    async def test_access_history_backfilled_into_the_shadow_columns_counts_as_use(
        self, facts_db
    ) -> None:
        """Migration 094's shadow columns hold the access history recovered from
        fact_access_log. Decay and prune read it alongside the live columns."""
        from robothor.memory.lifecycle import _run_decay_pass_sync, prune_low_quality_facts

        conn, tenant = facts_db
        backfilled = _seed(
            conn,
            tenant,
            "Alice asked for weekly status on the migration project.",
            access_count_shadow=3,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memory_facts SET last_accessed_shadow = NOW() - INTERVAL '2 days' "
                "WHERE id = %s",
                (backfilled,),
            )
        conn.commit()

        _run_decay_pass_sync(tenant_id=tenant)
        with conn.cursor() as cur:
            cur.execute("SELECT decay_score FROM memory_facts WHERE id = %s", (backfilled,))
            decay = float(cur.fetchone()[0])
        conn.commit()
        await prune_low_quality_facts(tenant_id=tenant)

        # 2 days since use, not 400: recency alone is ~0.63.
        assert decay > 0.5
        assert _row(conn, backfilled)["is_active"] is True


class TestImportanceSelection:
    """The judge used to select ``importance_score = 0.5``. One reinforcement
    nudges that to 0.55, so a re-observed fact was never judged at all."""

    def test_a_reinforced_unjudged_fact_is_still_selected_for_scoring(self, facts_db) -> None:
        from robothor.memory.conflicts import _reinforce_fact
        from robothor.memory.lifecycle import _select_unscored_facts

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice moved the standup to Tuesdays.", importance_score=0.5)
        _reinforce_fact(fact_id, tenant_id=tenant)

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            ids = [r["id"] for r in _select_unscored_facts(cur, tenant, limit=10_000)]
        conn.commit()
        assert fact_id in ids

    def test_a_judged_fact_is_not_selected_again(self, facts_db) -> None:
        from robothor.memory.lifecycle import _record_importance, _select_unscored_facts

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Bob signs off on budget changes.", importance_score=0.5)

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _record_importance(cur, fact_id, 0.7)
            ids = [r["id"] for r in _select_unscored_facts(cur, tenant, limit=10_000)]
        conn.commit()

        assert fact_id not in ids
        row = _row(conn, fact_id)
        assert row["importance_score"] == pytest.approx(0.7)
        assert "importance_scored_at" in (row["metadata"] or {})

    def test_a_neutral_verdict_stays_selectable_as_before(self, facts_db) -> None:
        """judge_importance returns 0.5 on a timeout, indistinguishable from a
        verdict, so 0.5 is not marked: it is retried, exactly as today."""
        from robothor.memory.lifecycle import _record_importance, _select_unscored_facts

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice may change teams next year.", importance_score=0.5)

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _record_importance(cur, fact_id, 0.5)
            ids = [r["id"] for r in _select_unscored_facts(cur, tenant, limit=10_000)]
        conn.commit()
        assert fact_id in ids


def _select_ids(conn: Any, tenant: str) -> list[int]:
    from robothor.memory.lifecycle import _select_unscored_facts

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        ids = [r["id"] for r in _select_unscored_facts(cur, tenant, limit=10_000)]
    conn.commit()
    return ids


def _auto_score(conn: Any, tenant: str) -> None:
    from robothor.memory.lifecycle import _auto_score_old_events

    with conn.cursor() as cur:
        _auto_score_old_events(cur, tenant)
    conn.commit()


def _judge(conn: Any, fact_id: int, score: float) -> None:
    from robothor.memory.lifecycle import _record_importance

    with conn.cursor() as cur:
        _record_importance(cur, fact_id, score)
    conn.commit()


def _set(conn: Any, fact_id: int, **cols: Any) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE memory_facts SET {sets} WHERE id = %s", [*cols.values(), fact_id])
    conn.commit()


class TestImportanceKeepsDeliberateScores:
    """A fact is an importance candidate only if it was never judged or set:
    its score is still the 0.5 default plus what reinforcement alone added.
    Rows scored before the marker existed carry no marker, so the marker alone
    cannot tell a judged fact from an unjudged one."""

    def test_a_reinforced_old_event_judged_high_keeps_its_score(self, facts_db) -> None:
        from robothor.memory.conflicts import _reinforce_fact

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice closed the security review.", importance_score=0.75)
        _reinforce_fact(fact_id, tenant_id=tenant)  # judged 0.75, reinforced to 0.8

        _auto_score(conn, tenant)

        assert _row(conn, fact_id)["importance_score"] == pytest.approx(0.8)
        assert fact_id not in _select_ids(conn, tenant)

    def test_a_reinforced_resolution_keeps_its_lifted_score(self, facts_db) -> None:
        conn, tenant = facts_db
        fact_id = _seed(
            conn,
            tenant,
            "Resolved: Bob's invoice was reissued.",
            category="resolution",
            importance_score=0.85,
        )
        _set(conn, fact_id, reinforcement_count=1)

        _auto_score(conn, tenant)

        assert fact_id not in _select_ids(conn, tenant)
        assert _row(conn, fact_id)["importance_score"] == pytest.approx(0.85)

    def test_a_consolidated_fact_keeps_inherited_importance_after_reinforcement(
        self, facts_db, monkeypatch
    ) -> None:
        from robothor.memory.conflicts import _reinforce_fact
        from robothor.memory.lifecycle import _apply_consolidation_supersession

        monkeypatch.setenv("MEMORY_CONSOLIDATION_GUARD", "1")
        conn, tenant = facts_db
        source = _seed(conn, tenant, "Alice runs the Monday sync.", importance_score=0.7)
        merged = _seed(
            conn, tenant, "Alice runs the Monday sync and its notes.", importance_score=0.5
        )
        _apply_consolidation_supersession(merged, [source])
        conn.commit()
        _reinforce_fact(merged, tenant_id=tenant)

        _auto_score(conn, tenant)

        assert merged not in _select_ids(conn, tenant)
        assert _row(conn, merged)["importance_score"] == pytest.approx(0.75)

    def test_a_reinforced_unjudged_fact_is_judged_and_keeps_its_reinforcement(
        self, facts_db
    ) -> None:
        from robothor.memory.conflicts import _reinforce_fact

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Bob prefers async updates.", importance_score=0.5)
        _set(conn, fact_id, created_at=psycopg2.extensions.AsIs("NOW()"))
        _reinforce_fact(fact_id, tenant_id=tenant)
        _reinforce_fact(fact_id, tenant_id=tenant)  # 0.6, never judged

        assert fact_id in _select_ids(conn, tenant)
        _judge(conn, fact_id, 0.7)

        assert _row(conn, fact_id)["importance_score"] == pytest.approx(0.8)
        assert fact_id not in _select_ids(conn, tenant)

    def test_a_reinforced_unjudged_old_event_is_auto_scored_with_its_reinforcement(
        self, facts_db
    ) -> None:
        from robothor.memory.conflicts import _reinforce_fact

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice attended the offsite.", importance_score=0.5)
        plain = _seed(conn, tenant, "Bob attended the offsite.", importance_score=0.5)
        _reinforce_fact(fact_id, tenant_id=tenant)

        _auto_score(conn, tenant)

        assert _row(conn, fact_id)["importance_score"] == pytest.approx(0.35)
        assert _row(conn, plain)["importance_score"] == pytest.approx(0.3)
        ids = _select_ids(conn, tenant)
        assert fact_id not in ids
        assert plain not in ids

    def test_a_neutral_verdict_on_a_reinforced_fact_is_not_rewritten_nightly(
        self, facts_db
    ) -> None:
        from robothor.memory.conflicts import _reinforce_fact

        conn, tenant = facts_db
        fact_id = _seed(conn, tenant, "Alice might join the panel.", importance_score=0.5)
        _set(conn, fact_id, created_at=psycopg2.extensions.AsIs("NOW()"))
        _reinforce_fact(fact_id, tenant_id=tenant)

        nights_judged = 0
        for _night in range(3):
            if fact_id in _select_ids(conn, tenant):
                nights_judged += 1
                _judge(conn, fact_id, 0.5)
            _reinforce_fact(fact_id, tenant_id=tenant)

        assert nights_judged == 1
        # 0.5 verdict + four reinforcements, none of them reset.
        assert _row(conn, fact_id)["importance_score"] == pytest.approx(0.7)


class TestWarmupRecordsPersonLinkedFacts:
    """Stored hashes cover person_id; a hash without it never matches a
    person-linked fact, so warm-up silently recorded none of them."""

    def _seed_person_fact(self, conn: Any, tenant: str) -> int:
        from robothor.memory.drift import compute_fact_hash

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_people (first_name, last_name, tenant_id) "
                "VALUES ('Alice', 'Example', %s) RETURNING id",
                (tenant,),
            )
            person = str(cur.fetchone()[0])
        conn.commit()
        text = "Alice prefers morning meetings."
        return _seed(
            conn,
            tenant,
            text,
            category="preference",
            importance_score=0.8,
            entities=["Alice"],
            person_id=person,
            content_hash=compute_fact_hash(
                text, tenant_id=tenant, category="preference", person_id=person
            ),
        )

    @pytest.mark.parametrize("v2", ["0", "1"], ids=["legacy", "v2"])
    def test_a_person_linked_fact_injected_by_warmup_is_recorded(
        self, facts_db, monkeypatch, v2
    ) -> None:
        from robothor.engine import warmup

        monkeypatch.setenv("MEMORY_WARMUP_RECALL_V2", v2)
        conn, tenant = facts_db
        fact_id = self._seed_person_fact(conn, tenant)

        text = warmup._build_entity_context("Ask Alice about Thursday", tenant_id=tenant)
        conn.commit()
        assert warmup.wait_for_deferred_writes(timeout=5.0)

        assert "Alice prefers morning meetings." in text
        assert _row(conn, fact_id)["access_count"] == 1

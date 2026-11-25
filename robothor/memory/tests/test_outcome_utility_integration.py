"""Outcome utility end to end, on a database migrated through the full chain.

Runs recall facts (the per-run access log), the run ends (credit or penalty at
delivery), the nightly per-tenant decay pass turns the counts into one utility
scalar, and ranking and prune read it. The decay pass and prune are per tenant
but rescore every row of that tenant, so they run on a throwaway database.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

pytestmark = pytest.mark.integration

_OLD = "NOW() - INTERVAL '400 days'"


def _new_tenant(conn: Any) -> str:
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant, "Test Tenant"),
        )
    conn.commit()
    return tenant


@pytest.fixture
def facts_db(scratch_db):
    from robothor.db.migrate import _discover
    from tests import conftest_integration as ci

    last = list(_discover())[-1].migration_id
    _, dsn = scratch_db(through=last)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    tenant = _new_tenant(conn)

    ci._current_holder["conn"] = conn
    ci._current_holder["wrapper"] = conn
    try:
        yield conn, tenant
    finally:
        ci._current_holder["conn"] = None
        ci._current_holder["wrapper"] = None
        conn.close()


def _seed(conn: Any, tenant: str, text: str, *, used: str = _OLD, **cols: Any) -> int:
    """One old, low-importance fact; ``used`` is its last_accessed SQL."""
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
            f"VALUES ({marks}, {_OLD}, {used}) RETURNING id",
            list(base.values()),
        )
        fact_id = int(cur.fetchone()[0])
    conn.commit()
    return fact_id


def _get(conn: Any, fact_id: int) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM memory_facts WHERE id = %s", (fact_id,))
        row = dict(cur.fetchone())
    conn.commit()
    return row


def _utility(conn: Any, fact_id: int) -> float:
    return float(_get(conn, fact_id)["utility"] or 0.0)


def _succeed(tenant: str, fact_ids: list[int], *, output: str = "Done.", n: int = 1) -> None:
    from robothor.memory.outcomes import credit_success_for_run, log_fact_access

    for _ in range(n):
        run_id = str(uuid.uuid4())
        log_fact_access(run_id, fact_ids, "worker", tenant)
        credit_success_for_run(run_id, tenant, output_text=output, task_text="Check it.")


def _fail(tenant: str, fact_ids: list[int], *, n: int = 1) -> None:
    from robothor.memory.outcomes import bump_failure_for_run, log_fact_access

    for _ in range(n):
        run_id = str(uuid.uuid4())
        log_fact_access(run_id, fact_ids, "worker", tenant)
        bump_failure_for_run(run_id, tenant)


def _ranked(conn: Any, ids: list[int]) -> list[int]:
    """Blend-rank the stored rows at equal relevance, as recall would."""
    from robothor.memory.facts import _FACT_COLUMNS, _blend_rank

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {_FACT_COLUMNS} FROM memory_facts WHERE id = ANY(%s)", (ids,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    for r in rows:
        r["rerank_score"] = 0.7
        r["age_seconds"] = 3600.0  # equal recency: only utility differs
    with (
        patch("robothor.memory.facts._dedup_enabled", return_value=False),
        patch("robothor.memory.facts._temporal_coherence_enabled", return_value=False),
        patch("robothor.memory.facts._rerank_wide_enabled", return_value=False),
    ):
        return [r["id"] for r in _blend_rank(rows, 10)]


class TestUtilityFromOutcomes:
    def test_i_a_fact_used_in_successful_runs_gains_utility_and_ranks_higher(
        self, facts_db
    ) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        helped = _seed(conn, tenant, "Alice approves vendor contract VC-3307 above the threshold.")
        other = _seed(conn, tenant, "Bob reviews vendor contracts below the threshold.")

        # "Used": the answer carries the fact's distinctive value.
        _succeed(tenant, [helped], output="Approved under VC-3307.", n=3)
        _run_decay_pass_sync(tenant_id=tenant)

        assert _get(conn, helped)["outcome_successes"] == 3
        assert _get(conn, helped)["outcome_citations"] == 3
        assert _utility(conn, helped) > 0.0
        assert _utility(conn, other) == 0.0
        # `other` has the higher id, so the newest-wins tie-break favours it.
        assert _ranked(conn, [helped, other]) == [helped, other]

    def test_i_a_cited_fact_gains_more_than_one_merely_returned_in_success(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        cited = _seed(conn, tenant, "Invoice INV-20417 for Alice was paid in March.")
        returned = _seed(conn, tenant, "Invoice handling is owned by the finance team.")

        _succeed(tenant, [cited, returned], output="INV-20417 is settled.", n=2)
        _run_decay_pass_sync(tenant_id=tenant)

        assert _get(conn, cited)["outcome_citations"] == 2
        assert (_get(conn, returned)["outcome_citations"] or 0) == 0
        assert _utility(conn, cited) > 0.0
        assert _utility(conn, returned) == 0.0

    def test_ii_a_fact_in_failed_runs_loses_utility(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        blamed = _seed(conn, tenant, "Bob's office is on the third floor.")
        _succeed(tenant, [blamed], n=1)
        _run_decay_pass_sync(tenant_id=tenant)
        before = _utility(conn, blamed)

        _fail(tenant, [blamed], n=3)
        _run_decay_pass_sync(tenant_id=tenant)

        assert _utility(conn, blamed) < before
        assert _utility(conn, blamed) < 0.0

    def test_iii_returned_200_times_without_success_does_not_rise(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync
        from robothor.memory.outcomes import log_fact_access, record_fact_access

        conn, tenant = facts_db
        popular = _seed(conn, tenant, "The office plants are watered on Mondays.")
        never = _seed(conn, tenant, "The office printer is on the second floor.")

        for _ in range(200):
            log_fact_access(str(uuid.uuid4()), [popular], "worker", tenant)
            record_fact_access(tenant, fact_ids=[popular])
        _run_decay_pass_sync(tenant_id=tenant)

        assert _get(conn, popular)["access_count"] == 200
        assert _utility(conn, popular) == _utility(conn, never) == 0.0
        # Equal relevance and utility: popularity does not lift it over the
        # never-returned fact (which wins the newest-wins tie-break).
        assert _ranked(conn, [popular, never]) == [never, popular]

    def test_iii_returned_in_many_successful_runs_but_never_used_does_not_rise(
        self, facts_db
    ) -> None:
        """Criterion 3 in the ordinary case: an off-topic fact that keeps
        matching broad queries rides along in successful runs it never used."""
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        ridealong = _seed(conn, tenant, "The office plants are watered on Mondays.")
        never = _seed(conn, tenant, "The office printer is on the second floor.")
        before = _ranked(conn, [ridealong, never])

        _succeed(tenant, [ridealong], output="All 3 reports were sent.", n=50)
        _run_decay_pass_sync(tenant_id=tenant)

        assert _get(conn, ridealong)["outcome_successes"] == 50
        assert _utility(conn, ridealong) == _utility(conn, never) == 0.0
        assert _ranked(conn, [ridealong, never]) == before == [never, ridealong]

    def test_iii_returned_200_times_in_failed_runs_falls_no_further_than_the_cap(
        self, facts_db
    ) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync
        from robothor.memory.utility import MAX_FAILURE_PENALTY

        conn, tenant = facts_db
        # Recent and important, so the penalty is visible above the 0 floor.
        blamed = _seed(
            conn,
            tenant,
            "Alice's standing order is renewed yearly.",
            used="NOW()",
            importance_score=0.9,
        )
        clean = _seed(
            conn,
            tenant,
            "Bob's standing order is renewed monthly.",
            used="NOW()",
            importance_score=0.9,
        )
        _fail(tenant, [blamed], n=200)
        _run_decay_pass_sync(tenant_id=tenant)

        u = _utility(conn, blamed)
        assert -1.0 <= u < -0.95
        drop = float(_get(conn, clean)["decay_score"]) - float(_get(conn, blamed)["decay_score"])
        assert 0.0 < drop <= MAX_FAILURE_PENALTY + 1e-6


class TestDecayPassWrites:
    def test_a_row_without_last_use_is_not_rewritten_when_nothing_changed(self, facts_db) -> None:
        """Rows with a NULL last_accessed get no decay score; their utility is
        written only when it changes, not every night."""
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        fact = _seed(conn, tenant, "Bob keeps the spare keys in the top drawer.", used="NULL")

        def _xmin() -> str:
            with conn.cursor() as cur:
                cur.execute("SELECT xmin::text FROM memory_facts WHERE id = %s", (fact,))
                value = str(cur.fetchone()[0])
            conn.commit()
            return value

        _run_decay_pass_sync(tenant_id=tenant)
        first = _xmin()
        _run_decay_pass_sync(tenant_id=tenant)
        assert _xmin() == first
        assert _get(conn, fact)["utility"] == 0.0


class TestPruneWithFadingUse:
    @pytest.mark.asyncio
    async def test_iv_old_single_recall_is_pruned_recent_or_useful_is_kept(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync, prune_low_quality_facts

        conn, tenant = facts_db
        long_ago = _seed(conn, tenant, "Bob once asked where the stapler was.", access_count=1)
        last_week = _seed(
            conn,
            tenant,
            "Bob asked where the spare monitors are kept.",
            access_count=1,
            used="NOW() - INTERVAL '7 days'",
        )
        useful = _seed(
            conn, tenant, "Alice's purchase order PO-77120 needs two signatures.", access_count=1
        )
        _succeed(tenant, [useful], output="PO-77120 has both signatures.", n=6)
        with conn.cursor() as cur:  # credit landed now; make its last use old again
            cur.execute(f"UPDATE memory_facts SET last_accessed = {_OLD} WHERE id = %s", (useful,))
        conn.commit()

        _run_decay_pass_sync(tenant_id=tenant)
        await prune_low_quality_facts(tenant_id=tenant)

        assert _get(conn, long_ago)["is_active"] is False
        assert _get(conn, last_week)["is_active"] is True
        assert _get(conn, useful)["is_active"] is True

    @pytest.mark.asyncio
    async def test_iv_protected_facts_stay_protected(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync, prune_low_quality_facts

        conn, tenant = facts_db
        decision = _seed(
            conn, tenant, "We decided to keep the old vendor for now.", category="decision"
        )
        _fail(tenant, [decision], n=10)
        _run_decay_pass_sync(tenant_id=tenant)
        await prune_low_quality_facts(tenant_id=tenant)
        assert _get(conn, decision)["is_active"] is True


class TestUtilitySurvivesRollup:
    def test_v_utility_survives_the_access_log_rollup(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync
        from robothor.memory.outcomes import cleanup_old_access_logs

        conn, tenant = facts_db
        helped = _seed(conn, tenant, "Alice's travel budget code is TRV-5521.")
        blamed = _seed(conn, tenant, "Bob's travel budget is unlimited.")
        _succeed(tenant, [helped], output="Use TRV-5521.", n=3)
        _fail(tenant, [blamed], n=2)
        _run_decay_pass_sync(tenant_id=tenant)
        before = (_utility(conn, helped), _utility(conn, blamed))

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fact_access_log SET accessed_at = NOW() - INTERVAL '90 days' "
                "WHERE tenant_id = %s",
                (tenant,),
            )
        conn.commit()
        assert cleanup_old_access_logs(days=30, tenant_id=tenant) == 5
        _run_decay_pass_sync(tenant_id=tenant)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM fact_access_log WHERE tenant_id = %s", (tenant,))
            assert cur.fetchone()[0] == 0
        conn.commit()
        assert (_utility(conn, helped), _utility(conn, blamed)) == pytest.approx(before)
        assert before[0] > 0.0 > before[1]


class TestTenantIsolation:
    def test_vi_runs_in_one_tenant_never_change_anothers_utility(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync
        from robothor.memory.outcomes import bump_failure_for_run, credit_success_for_run

        conn, tenant_a = facts_db
        tenant_b = _new_tenant(conn)
        text = "Alice's badge number is BDG-31337."
        in_a = _seed(conn, tenant_a, text)
        in_b = _seed(conn, tenant_b, text)

        _succeed(tenant_a, [in_a], output="Badge BDG-31337.", n=3)
        _fail(tenant_a, [in_a], n=1)
        # A run id from tenant A, credited under tenant B, touches nothing in B.
        from robothor.memory.outcomes import log_fact_access

        run_id = str(uuid.uuid4())
        log_fact_access(run_id, [in_a, in_b], "worker", tenant_a)
        credit_success_for_run(run_id, tenant_b, output_text="BDG-31337", task_text="")
        bump_failure_for_run(run_id, tenant_b)
        _run_decay_pass_sync(tenant_id=tenant_a)
        _run_decay_pass_sync(tenant_id=tenant_b)

        b = _get(conn, in_b)
        assert (b["outcome_successes"] or 0) == 0
        assert (b["outcome_citations"] or 0) == 0
        assert (b["outcome_failures"] or 0) == 0
        assert _utility(conn, in_b) == 0.0
        assert _utility(conn, in_a) != 0.0


class TestBenchmarkRunsCreateNoUtility:
    def test_vii_a_benchmark_run_neither_logs_nor_credits(self, facts_db) -> None:
        from robothor.engine.run_context import benchmark_run_scope
        from robothor.memory.lifecycle import _run_decay_pass_sync
        from robothor.memory.outcomes import credit_success_for_run, log_fact_access

        conn, tenant = facts_db
        fact = _seed(conn, tenant, "Alice's desk phone extension is EXT-4410.")
        # Even a run whose access rows exist (logged outside the benchmark)
        # earns nothing when it is credited from inside one.
        logged_run = str(uuid.uuid4())
        log_fact_access(logged_run, [fact], "worker", tenant)

        with benchmark_run_scope(True, agent_id="bench-child", run_id="bench-1"):
            bench_run = str(uuid.uuid4())
            log_fact_access(bench_run, [fact], "bench-child", tenant)
            out1 = credit_success_for_run(bench_run, tenant, output_text="EXT-4410")
            out2 = credit_success_for_run(logged_run, tenant, output_text="EXT-4410")
        _run_decay_pass_sync(tenant_id=tenant)

        assert out1 == out2 == {"facts_credited": 0, "facts_cited": 0}
        row = _get(conn, fact)
        assert (row["outcome_successes"] or 0) == 0
        assert (row["outcome_citations"] or 0) == 0
        assert _utility(conn, fact) == 0.0
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM fact_access_log WHERE run_id = %s", (bench_run,))
            assert cur.fetchone()[0] == 0
        conn.commit()


class TestOriginGatesReinforcement:
    def test_viii_external_or_inferred_origin_earns_no_positive_utility(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        # The graph schema adds this column; until it lands here, add it locally.
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS origin TEXT")
        conn.commit()
        external = _seed(conn, tenant, "A web page says PRJ-8812 ships in May.", origin="external")
        inferred = _seed(conn, tenant, "PRJ-8812 is probably led by Bob.", origin="inferred")
        stated = _seed(conn, tenant, "Alice said PRJ-8812 ships in June.", origin="user_stated")
        legacy = _seed(conn, tenant, "PRJ-8812 has a budget review.")
        blamed = _seed(conn, tenant, "A web page says PRJ-8812 is cancelled.", origin="external")

        _succeed(tenant, [external, inferred, stated, legacy], output="PRJ-8812 is due.", n=4)
        _fail(tenant, [blamed], n=2)
        _run_decay_pass_sync(tenant_id=tenant)

        assert _utility(conn, external) == 0.0
        assert _utility(conn, inferred) == 0.0
        assert _utility(conn, stated) > 0.0
        assert _utility(conn, legacy) > 0.0
        assert _utility(conn, blamed) < 0.0

    def test_viii_without_the_origin_column_every_fact_is_eligible(self, facts_db) -> None:
        from robothor.memory.lifecycle import _run_decay_pass_sync

        conn, tenant = facts_db
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'memory_facts' AND column_name = 'origin'"
            )
            assert cur.fetchone() is None
        conn.commit()
        fact = _seed(conn, tenant, "Alice's locker is LKR-0921.")
        _succeed(tenant, [fact], output="Locker LKR-0921.", n=2)
        _run_decay_pass_sync(tenant_id=tenant)
        assert _utility(conn, fact) > 0.0

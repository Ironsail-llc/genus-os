"""Seeding the synthetic world into the eval tenants, and removing every trace.

Unit tests prove the tenant guard fires before any database call and that the
world cases are scored by the existing retrieval scorers. The integration test
seeds a small world into real PostgreSQL under freshly-named eval tenants with
a fake embedder, and proves that every touched table holds rows for those
tenants only, and that cleanup returns every count to where it started.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from typing import Any

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.memory import eval_world as ew
from robothor.memory import eval_world_seed as seed
from robothor.memory.eval import EVAL_TENANT, EvalPreconditionError

EMBED_DIM = 1024


async def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
    """Deterministic 1024-d vectors; no model."""
    out = []
    for t in texts:
        digest = hashlib.sha256(t.encode()).digest()
        out.append([(digest[i % 32] - 128) / 128.0 for i in range(EMBED_DIM)])
    return out


def _case(stratum: str, **kw: Any) -> ew.Case:
    base: dict[str, Any] = {
        "id": f"{stratum}:t",
        "stratum": stratum,
        "question": "q?",
        "gold": ("Kelvaro",),
        "supporting_ids": ("fct-00001",),
        "as_of": None,
        "split": "dev",
        "subject_id": "per-0001",
    }
    base.update(kw)
    return ew.Case(**base)


# --------------------------------------------------------------------------- #
# Tenant guard
# --------------------------------------------------------------------------- #


class TestEvalTenantGuard:
    @pytest.mark.parametrize(
        "tenant", [EVAL_TENANT, f"{EVAL_TENANT}-shadow", f"{EVAL_TENANT}-t1234"]
    )
    def test_eval_tenants_are_recognised(self, tenant):
        assert seed.is_eval_tenant(tenant)

    @pytest.mark.parametrize(
        "tenant", ["", DEFAULT_TENANT, "default", "memory-evaluation", "prod-memory-eval", None]
    )
    def test_everything_else_is_not(self, tenant):
        assert not seed.is_eval_tenant(tenant)

    def test_shadow_tenant_is_itself_an_eval_tenant(self):
        assert seed.is_eval_tenant(seed.shadow_tenant_for(EVAL_TENANT))
        assert seed.shadow_tenant_for(EVAL_TENANT) != EVAL_TENANT

    @staticmethod
    def _forbid_db(monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("the guard must fire before any database call")

        monkeypatch.setattr("robothor.db.connection.get_connection", _boom)
        monkeypatch.setattr(seed, "get_connection", _boom, raising=False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant", [DEFAULT_TENANT, "", "robothor-primary"])
    async def test_seed_refuses_a_non_eval_tenant_before_touching_the_db(self, monkeypatch, tenant):
        self._forbid_db(monkeypatch)
        world = ew.generate_world(1, "small")
        with pytest.raises(EvalPreconditionError):
            await seed.seed_world(world, tenant, embed_batch=_fake_embed_batch)

    @pytest.mark.asyncio
    async def test_seed_refuses_a_non_eval_shadow_tenant(self, monkeypatch):
        self._forbid_db(monkeypatch)
        world = ew.generate_world(1, "small")
        with pytest.raises(EvalPreconditionError):
            await seed.seed_world(
                world, EVAL_TENANT, shadow_tenant_id=DEFAULT_TENANT, embed_batch=_fake_embed_batch
            )

    @pytest.mark.parametrize("tenants", [[DEFAULT_TENANT], [EVAL_TENANT, "default"], [""]])
    def test_cleanup_refuses_a_non_eval_tenant_before_touching_the_db(self, monkeypatch, tenants):
        self._forbid_db(monkeypatch)
        with pytest.raises(EvalPreconditionError):
            seed.cleanup_world(tenants)

    @pytest.mark.asyncio
    async def test_run_world_refuses_the_production_tenant(self, monkeypatch):
        self._forbid_db(monkeypatch)
        with pytest.raises(EvalPreconditionError):
            await seed.run_world(1, "small", "dev", tenant_id=DEFAULT_TENANT)


# --------------------------------------------------------------------------- #
# Scoring through the existing scorers
# --------------------------------------------------------------------------- #


class TestWorldScoring:
    def test_recall_strata_pass_when_any_gold_is_in_top_k(self):
        case = _case("multi_hop", gold=("Ana Quill", "ana.quill@example.com"))
        r = seed.score_world_case(case, ["x", "met ana.quill@example.com"], k=5)
        assert r is not None and r.passed

    def test_recall_strata_fail_when_no_gold_is_in_top_k(self):
        r = seed.score_world_case(_case("point_in_time"), ["nothing here"], k=5)
        assert r is not None and not r.passed
        assert r.kind == "point_in_time"

    def test_knowledge_update_requires_the_current_value_ranked_first(self):
        case = _case("knowledge_update")
        assert seed.score_world_case(case, ["Kelvaro now", "old"], k=5).passed
        assert not seed.score_world_case(case, ["old", "Kelvaro now"], k=5).passed

    def test_cross_tenant_fails_when_the_other_tenants_answer_leaks(self):
        case = _case("cross_tenant", forbidden=("Zelmoor",))
        assert seed.score_world_case(case, ["Kelvaro"], k=5).passed
        leaked = seed.score_world_case(case, ["Kelvaro", "Zelmoor"], k=5)
        assert not leaked.passed
        assert "leak" in leaked.detail

    def test_abstention_is_unscored_by_retrieval(self):
        case = _case("abstention", gold=("unknown",), supporting_ids=(), probe="blood type")
        assert seed.score_world_case(case, ["anything"], k=5) is None

    def test_report_aggregates_per_stratum_and_counts_unscored(self):
        cases = [_case("multi_hop"), _case("abstention", gold=("unknown",), supporting_ids=())]
        results = [seed.score_world_case(cases[0], ["Kelvaro"], k=5), None]
        report = seed.build_world_report(cases, results, suite_id="memory-world-test")
        assert report["total"] == 1 and report["passed"] == 1
        assert report["by_kind"]["multi_hop"] == {"passed": 1, "total": 1}
        assert report["unscored"] == {"abstention": 1}


# --------------------------------------------------------------------------- #
# Integration: real PostgreSQL
# --------------------------------------------------------------------------- #


def _db_reachable() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — any failure means "no database here"
        return False


@pytest.fixture
def eval_tenants():
    if not _db_reachable():
        pytest.skip("no PostgreSQL reachable for the integration test")
    primary = f"{EVAL_TENANT}-t{uuid.uuid4().hex[:10]}"
    tenants = [primary, seed.shadow_tenant_for(primary)]
    yield tenants
    # Never leave residue, even when an assertion failed halfway.
    with contextlib.suppress(Exception):
        seed.cleanup_world(tenants)


@pytest.mark.integration
class TestSeedAndCleanup:
    @pytest.mark.asyncio
    async def test_seed_populates_every_table_for_eval_tenants_only_and_cleanup_removes_it(
        self, eval_tenants, monkeypatch
    ):
        # No model may be called while seeding: the world already knows its
        # facts and entities, and the embedder is injected.
        async def _no_llm(*_a, **_k):
            raise AssertionError("seeding must not call a model")

        from robothor.llm import ollama

        for name in ("get_embedding_async", "get_embeddings_batch_async", "generate"):
            if hasattr(ollama, name):
                monkeypatch.setattr(ollama, name, _no_llm)

        primary, shadow = eval_tenants
        world = ew.generate_world(3, "small")
        before = seed.world_row_counts(eval_tenants)
        assert set(before) == set(seed.WORLD_TABLES)

        seeded = await seed.seed_world(
            world, primary, shadow_tenant_id=shadow, embed_batch=_fake_embed_batch
        )
        during = seed.world_row_counts(eval_tenants)

        for table in (
            "memory_facts",
            "memory_entities",
            "memory_relations",
            "crm_people",
            "crm_companies",
            "kb_documents",
            "kb_chunks",
            "chat_sessions",
            "chat_messages",
        ):
            assert during[table] > before[table], table

        assert len(seeded.fact_ids) == len(world.facts) + len(world.shadow.facts)
        # Every seeded row is in an eval tenant, and the per-tenant split is right.
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT tenant_id, count(*) FROM memory_facts WHERE id = ANY(%s) GROUP BY 1",
                (list(seeded.fact_ids.values()),),
            )
            per_tenant = dict(cur.fetchall())
            cur.execute(
                "SELECT count(*) FROM memory_facts WHERE id = ANY(%s) AND embedding IS NULL",
                (list(seeded.fact_ids.values()),),
            )
            unembedded = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM memory_facts WHERE tenant_id = %s AND is_active = FALSE",
                (primary,),
            )
            inactive = cur.fetchone()[0]
        assert per_tenant == {primary: len(world.facts), shadow: len(world.shadow.facts)}
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT tenant_id FROM memory_entities WHERE id = ANY(%s)",
                (list(seeded.entity_ids.values()),),
            )
            assert {r[0] for r in cur.fetchall()} == {primary, shadow}
            cur.execute(
                "SELECT DISTINCT tenant_id FROM crm_people WHERE id = ANY(%s::uuid[])",
                (list(seeded.crm_person_ids.values()),),
            )
            assert {r[0] for r in cur.fetchall()} == {primary, shadow}
        assert unembedded == 0
        assert inactive == sum(1 for f in world.facts if f.superseded_by)

        deleted = seed.cleanup_world(eval_tenants)
        after = seed.world_row_counts(eval_tenants)
        assert after == before
        assert sum(deleted.values()) > 0

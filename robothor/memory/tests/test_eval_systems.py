"""Systems under test: the interface, budget filling, and each baseline on a
small seeded world (integration, real PostgreSQL, fake embedder)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.memory import eval_systems as es
from robothor.memory import eval_world as ew
from robothor.memory import eval_world_seed as seed
from robothor.memory.eval import EVAL_TENANT, EvalPreconditionError

DIM = 1024


async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        d = hashlib.sha256(t.encode()).digest()
        out.append([(d[i % 32] - 128) / 128.0 for i in range(DIM)])
    return out


async def fake_embed(text: str) -> list[float]:
    return (await fake_embed_batch([text]))[0]


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


class TestFillBudget:
    def _cands(self, sizes: list[int]) -> list[tuple[str, str, str]]:
        return [(f"id{i}", "fact", "x" * (4 * n)) for i, n in enumerate(sizes)]

    @pytest.mark.parametrize("budget", [0, 1, 5, 10, 17, 100])
    def test_never_exceeds_the_budget(self, budget):
        items = es.fill_budget(self._cands([3, 7, 2, 9, 4]), budget)
        assert sum(i.tokens for i in items) <= budget

    def test_fills_in_rank_order_skipping_what_does_not_fit(self):
        items = es.fill_budget(self._cands([3, 7, 2, 9, 4]), 10)
        assert [i.id for i in items] == ["id0", "id1"]
        items = es.fill_budget(self._cands([3, 9, 2, 1]), 6)
        assert [i.id for i in items] == ["id0", "id2", "id3"]
        assert [i.rank for i in items] == sorted(i.rank for i in items)

    def test_duplicates_are_dropped(self):
        cands = [("a", "fact", "one"), ("a", "fact", "one"), ("b", "fact", "two")]
        assert [i.id for i in es.fill_budget(cands, 100)] == ["a", "b"]

    def test_unbounded(self):
        assert len(es.fill_budget(self._cands([5] * 50), None)) == 50

    def test_token_estimate_is_the_repo_utility(self):
        from robothor.memory.block_budget import estimate_tokens

        assert es.estimate_tokens("abcdefgh" * 10) == estimate_tokens("abcdefgh" * 10)

    def test_deterministic(self):
        c = self._cands([3, 7, 2, 9, 4])
        assert es.fill_budget(c, 12) == es.fill_budget(c, 12)


# --------------------------------------------------------------------------- #
# Interface and registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_the_four_baselines_and_the_unbounded_variant_exist(self):
        for name in ("recall", "recall_budget", "keyword_only", "raw_verbatim", "full_context"):
            assert es.make_systems([name])[0].name == name
        unbounded = es.make_systems(["full_context_unbounded"])[0]
        assert unbounded.equal_budget is False
        assert all(s.equal_budget for s in es.make_systems(["recall", "keyword_only"]))

    def test_recall_variants_switch_the_segment_and_graph_legs_off(self):
        on, no_seg, no_graph = es.make_systems(["recall", "recall_no_segments", "recall_no_graph"])
        # "recall" is the product's default: the graph leg as search_facts decides.
        assert (on.include_segments, on.include_graph) == (True, None)
        (with_graph,) = es.make_systems(["recall_with_graph"])
        assert with_graph.include_graph is True
        assert (no_seg.name, no_seg.include_segments, no_seg.include_graph) == (
            "recall_no_segments",
            False,
            None,
        )
        assert (no_graph.name, no_graph.include_segments, no_graph.include_graph) == (
            "recall_no_graph",
            True,
            False,
        )

    def test_unknown_system_is_refused(self):
        with pytest.raises(ValueError, match="unknown system"):
            es.make_systems(["oracle"])

    def test_a_new_system_plugs_in_without_touching_the_runner(self):
        class Echo:
            name = "echo_test"
            equal_budget = True

            async def retrieve(self, case, *, world_handle, token_budget):
                return es.ContextBundle(
                    system=self.name,
                    items=es.fill_budget([("x", "fact", case.question)], token_budget),
                    budget=token_budget,
                    equal_budget=True,
                    abstained=False,
                    retrieval_ms=0.0,
                )

        es.register_system("echo_test", Echo)
        try:
            (sys_,) = es.make_systems(["echo_test"])
            assert isinstance(sys_, es.MemorySystem)
        finally:
            es.unregister_system("echo_test")

    def test_world_handle_refuses_non_eval_tenants(self):
        w = ew.generate_world(1, "small")
        with pytest.raises(EvalPreconditionError):
            es.WorldHandle(w, seed.SeededWorld(tenant_ids=()), DEFAULT_TENANT, "x", fake_embed)
        with pytest.raises(EvalPreconditionError):
            es.WorldHandle(
                w, seed.SeededWorld(tenant_ids=()), EVAL_TENANT, DEFAULT_TENANT, fake_embed
            )


# --------------------------------------------------------------------------- #
# Integration: each system on a seeded world
# --------------------------------------------------------------------------- #


def _db_reachable() -> bool:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — any failure means "no database here"
        return False


DECOY_TEXT = "DECOYMARKER the name of the dog is DECOYMARKER and Velmere bay"


@pytest.fixture
async def seeded(monkeypatch):
    if not _db_reachable():
        pytest.skip("no PostgreSQL reachable for the integration test")
    from robothor.db.connection import get_connection
    from robothor.llm import ollama

    async def one(text, *a, **k):
        return await fake_embed(text)

    monkeypatch.setattr(ollama, "get_embedding_async", one)
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "0")

    primary = f"{EVAL_TENANT}-s{uuid.uuid4().hex[:10]}"
    shadow = seed.shadow_tenant_for(primary)
    decoy = f"test-{uuid.uuid4().hex}"
    world = ew.generate_world(5, "small")
    # A non-eval tenant holding text that matches every question: no system
    # may ever return it.
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (decoy, "decoy"))
        cur.execute(
            "INSERT INTO memory_facts (fact_text, category, tenant_id, embedding) "
            "VALUES (%s, 'event', %s, %s)",
            (DECOY_TEXT, decoy, await fake_embed(DECOY_TEXT)),
        )
        cur.execute(
            "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES (%s, %s, 'eval') "
            "RETURNING id",
            (decoy, "decoy:1"),
        )
        sid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO chat_messages (session_id, message, embedding) VALUES (%s, %s, %s)",
            (
                sid,
                json.dumps({"role": "user", "content": DECOY_TEXT}),
                await fake_embed(DECOY_TEXT),
            ),
        )
    try:
        handle_seed = await seed.seed_world(
            world, primary, shadow_tenant_id=shadow, embed_batch=fake_embed_batch
        )
        yield es.WorldHandle(world, handle_seed, primary, shadow, fake_embed)
    finally:
        with contextlib.suppress(Exception):
            seed.cleanup_world([primary, shadow])
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM chat_messages WHERE session_id IN "
                "(SELECT id FROM chat_sessions WHERE tenant_id = %s)",
                (decoy,),
            )
            cur.execute("DELETE FROM chat_sessions WHERE tenant_id = %s", (decoy,))
            cur.execute("DELETE FROM memory_facts WHERE tenant_id = %s", (decoy,))
            cur.execute("DELETE FROM crm_tenants WHERE id = %s", (decoy,))


def _ids(world: ew.World) -> dict[str, set[str]]:
    return {
        "fact": {f.id for f in world.facts},
        "turn": {t.id for c in world.conversations for s in c.sessions for t in s.turns},
        "document": {d.id for d in world.documents},
    }


def _check_bundle(bundle: es.ContextBundle, world: ew.World, budget: int | None) -> None:
    ids = _ids(world)
    if budget is not None:
        assert bundle.tokens <= budget
    for item in bundle.items:
        assert DECOY_TEXT not in item.text
        assert not item.id.startswith("sh-"), item.id
        if item.kind == "document":
            assert item.id.split("#", 1)[0] in ids["document"], item.id
        else:
            assert item.id in ids[item.kind], item.id
    ranks = [i.rank for i in bundle.items]
    assert ranks == sorted(ranks)
    assert bundle.retrieval_ms >= 0


@pytest.mark.integration
class TestSystemsOnASeededWorld:
    @pytest.mark.asyncio
    async def test_every_system_stays_in_budget_and_in_its_own_tenant(self, seeded):
        world = seeded.world
        cases = [next(c for c in world.cases if c.stratum == s) for s in ew.STRATA]
        for system in es.make_systems(
            ["recall", "keyword_only", "raw_verbatim", "full_context", "full_context_unbounded"]
        ):
            for case in cases:
                bundle = await system.retrieve(case, world_handle=seeded, token_budget=600)
                _check_bundle(bundle, world, 600 if system.equal_budget else None)
                assert bundle.system == system.name

    @pytest.mark.asyncio
    async def test_fact_systems_return_facts_and_raw_systems_return_raw(self, seeded):
        case = next(c for c in seeded.world.cases if c.stratum == "knowledge_update")
        kinds: dict[str, set[str]] = {}
        for system in es.make_systems(["recall", "keyword_only", "raw_verbatim", "full_context"]):
            bundle = await system.retrieve(case, world_handle=seeded, token_budget=800)
            assert bundle.items, system.name
            kinds[system.name] = {i.kind for i in bundle.items}
        # Recall reads the reader's conversation segments too, as the turns
        # they copy; it never returns a document.
        assert "fact" in kinds["recall"]
        assert kinds["recall"] <= {"fact", "turn"}
        assert kinds["keyword_only"] == {"fact"}
        assert kinds["raw_verbatim"] <= {"turn", "document"}
        assert kinds["full_context"] == {"turn"}

    @pytest.mark.asyncio
    async def test_keyword_only_finds_the_subjects_facts(self, seeded):
        world = seeded.world
        case = next(c for c in world.cases if c.stratum == "knowledge_update")
        person = next(p for p in world.people if p.id == case.subject_id)
        (system,) = es.make_systems(["keyword_only"])
        bundle = await system.retrieve(case, world_handle=seeded, token_budget=800)
        assert any(person.last_name in i.text for i in bundle.items)

    @pytest.mark.asyncio
    async def test_raw_verbatim_reaches_a_transcript_only_detail(self, seeded):
        world = seeded.world
        (system,) = es.make_systems(["raw_verbatim"])
        hits = 0
        cases = [c for c in world.cases if c.stratum == "raw_detail"][:5]
        for case in cases:
            bundle = await system.retrieve(case, world_handle=seeded, token_budget=1500)
            if case.supporting_ids[0] in {i.id for i in bundle.items}:
                hits += 1
        assert hits >= 1

    @pytest.mark.asyncio
    async def test_recall_reaches_a_transcript_only_detail_through_its_segment_leg(self, seeded):
        """Recall searches as the world's reader, so its conversation segments
        count; a segment is the transcript turn it starts with."""
        world = seeded.world
        on, off = es.make_systems(["recall", "recall_no_segments"])
        cases = [c for c in world.cases if c.stratum == "raw_detail"][:5]
        hits = {"recall": 0, "recall_no_segments": 0}
        for system in (on, off):
            for case in cases:
                bundle = await system.retrieve(case, world_handle=seeded, token_budget=1500)
                _check_bundle(bundle, world, 1500)
                if system is off:
                    assert {i.kind for i in bundle.items} <= {"fact"}
                hits[system.name] += any(
                    case.gold[0].lower() in i.text.lower() for i in bundle.items
                )
        assert hits["recall"] >= 1
        assert hits["recall"] >= hits["recall_no_segments"]

    @pytest.mark.asyncio
    async def test_full_context_is_the_newest_history_in_chronological_order(self, seeded):
        world = seeded.world
        (system,) = es.make_systems(["full_context"])
        bundle = await system.retrieve(world.cases[0], world_handle=seeded, token_budget=500)
        turns = {t.id: t.at for c in world.conversations for s in c.sessions for t in s.turns}
        stamps = [turns[i.id] for i in bundle.items]
        assert stamps == sorted(stamps)
        newest = max(turns.values())
        assert stamps[-1] == newest

    @pytest.mark.asyncio
    async def test_unbounded_holds_more_than_the_equal_budget(self, seeded):
        case = seeded.world.cases[0]
        (small,) = es.make_systems(["full_context"])
        (big,) = es.make_systems(["full_context_unbounded"])
        a = await small.retrieve(case, world_handle=seeded, token_budget=300)
        b = await big.retrieve(case, world_handle=seeded, token_budget=300)
        assert b.tokens > a.tokens and b.equal_budget is False


@pytest.mark.integration
class TestRecallAtEqualBudget:
    @pytest.mark.asyncio
    async def test_budget_variant_keeps_the_product_ranking_and_fills_further(
        self, seeded, monkeypatch
    ):
        # The product reranks and its reranker pool is capped
        # (MEMORY_RERANK_MAX_CANDIDATES, default 16). A fake reranker keeps
        # that cap without a model, so the product returns a short list.
        async def capped_rerank(query, results, top_k=10):
            return results[: min(top_k, 16)]

        monkeypatch.setenv("MEMORY_RERANK_ENABLED", "1")
        monkeypatch.setattr("robothor.rag.reranker.rerank_with_fallback", capped_rerank)
        case = next(c for c in seeded.world.cases if c.stratum == "knowledge_update")
        (product,) = es.make_systems(["recall"])
        (budget,) = es.make_systems(["recall_budget"])
        a = await product.retrieve(case, world_handle=seeded, token_budget=2048)
        b = await budget.retrieve(case, world_handle=seeded, token_budget=2048)
        _check_bundle(b, seeded.world, 2048)
        # Facts, and the eval reader's conversation turns the segment leg adds.
        assert {i.kind for i in b.items} <= {"fact", "turn"}
        assert [i.id for i in b.items][: len(a.items)] == [i.id for i in a.items]
        # The product now fills past the reranked head itself, so the variant
        # adds at most what is left of the budget.
        assert len(a.items) > 16
        assert b.tokens >= a.tokens


class TestRecallBudgetReadsAsTheWorldReader:
    @pytest.mark.asyncio
    async def test_budget_variant_searches_as_the_eval_reader(self, monkeypatch):
        """The world's conversations are private to its eval reader; the budget
        variant must search as that reader, as ``recall`` does, or the two
        compare different stores."""
        from types import SimpleNamespace

        calls: list[dict] = []

        async def fake_search(query, **kwargs):
            calls.append(kwargs)
            return []

        monkeypatch.setattr("robothor.memory.facts.search_facts", fake_search)
        handle = SimpleNamespace(
            tenant_id=EVAL_TENANT,
            seeded=SimpleNamespace(reader_person_id="p-1"),
            fact_id=lambda _i: None,
            turn_id=lambda _i: None,
        )
        case = SimpleNamespace(question="Where does Alice work?")
        (budget,) = es.make_systems(["recall_budget"])
        await budget.retrieve(case, world_handle=handle, token_budget=2048)
        assert calls
        assert all(c.get("reader_person_id") == "p-1" for c in calls)


class TestRecallToday:
    """``recall_today``: recall as main serves it, for a before/after row.

    Main has no conversation segments, no graph leg and no budget fill: the
    reranked head is all it returns. On this tree the same call with those legs
    off, keeping only the rows the fill did not add, is that recall.
    """

    @pytest.mark.asyncio
    async def test_reads_facts_only_and_keeps_the_reranked_head(self, monkeypatch):
        from types import SimpleNamespace

        calls: list[dict] = []

        async def fake_search(query, **kwargs):
            calls.append(kwargs)
            return [
                {"id": 1, "kind": "fact", "fact_text": "Alice works at Example Co."},
                {
                    "id": 2,
                    "kind": "fact",
                    "fact_text": "Bob lives in Brelford.",
                    "budget_tail": True,
                },
                {"id": 3, "kind": "fact", "fact_text": "Alice likes tea."},
            ]

        monkeypatch.setattr("robothor.memory.facts.search_facts", fake_search)
        handle = SimpleNamespace(
            tenant_id=EVAL_TENANT,
            seeded=SimpleNamespace(reader_person_id="p-1"),
            fact_id=lambda i: f"f{i}",
            turn_id=lambda _i: None,
        )
        (today,) = es.make_systems(["recall_today"])
        assert today.name == "recall_today" and today.equal_budget
        bundle = await today.retrieve(
            SimpleNamespace(question="Where does Alice work?"),
            world_handle=handle,
            token_budget=2048,
        )
        assert [i.id for i in bundle.items] == ["f1", "f3"]
        assert calls and all(
            c.get("include_segments") is False and c.get("include_graph") is False for c in calls
        )

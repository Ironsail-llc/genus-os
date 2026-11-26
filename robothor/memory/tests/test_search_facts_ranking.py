"""search_facts ranking, end to end over a fake database and a fake reranker.

No Postgres and no Ollama: the cursor answers by which leg's SQL it was given,
and the reranker's per-pair scorer is replaced by a function of the document
text. What is asserted is the ranking contract — which rows survive and in what
order — never an exact score.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.memory import facts
from robothor.rag import reranker


def _row(fid: int, text: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": fid,
        "fact_text": text,
        "category": "event",
        "entities": ["Alice"],
        "confidence": 0.9,
        "source_type": "conversation",
        "metadata": {},
        "created_at": None,
        "importance_score": 0.5,
        "access_count": 0,
        "superseded_by": None,
        "person_id": None,
        "age_seconds": 3600.0,
    }
    row.update(extra)
    return row


def _dense(n: int) -> list[dict[str, Any]]:
    return [
        _row(i, f"unrelated note {i} on subject {i * 13}", similarity=0.8 - i / 100)
        for i in range(1, n + 1)
    ]


class _Cursor:
    def __init__(
        self,
        vector: list[dict[str, Any]],
        keyword: list[dict[str, Any]],
        expansion: list[dict[str, Any]] | None = None,
    ) -> None:
        self.vector, self.keyword, self.expansion = vector, keyword, expansion or []
        self.sql: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(sql)

    def _last(self) -> str:
        return self.sql[-1] if self.sql else ""

    def fetchall(self) -> list[dict[str, Any]]:
        s = self._last()
        if "<=>" in s:
            return [dict(r) for r in self.vector]
        if "tsv @@" in s:
            return [dict(r) for r in self.keyword]
        if "ANY(entities)" in s:
            out, self.expansion = self.expansion, []
            return [dict(r) for r in out]
        return []

    def fetchone(self) -> None:
        return None


def _conn(cur: _Cursor) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _scorer(scores: dict[str, float], default: float = 0.2):
    async def fake_pair(client: Any, query: str, doc: str) -> float:
        for needle, score in scores.items():
            if needle in doc:
                return score
        return default

    return fake_pair


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for flag in (
        "MEMORY_RERANK_WIDE",
        "MEMORY_EPISODE_MERGE",
        "MEMORY_TEMPORAL_COHERENCE",
        "MEMORY_RERANK_MAX_CANDIDATES",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("MEMORY_RANK_BLEND", "1")
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    return monkeypatch


async def _search(cur: _Cursor, query: str = "where is the passport", **kw: Any):
    kw.setdefault("limit", 5)
    kw.setdefault("use_reranker", True)
    with (
        patch.object(facts.llm_client, "get_embedding_async", AsyncMock(return_value=[0.1] * 8)),
        patch.object(facts, "get_connection", return_value=_conn(cur)),
        patch.object(facts, "apply_hnsw_session"),
    ):
        return await facts.search_facts(query, tenant_id="test-tenant", **kw)


@pytest.mark.asyncio
async def test_keyword_only_best_answer_ranks_in_the_top(env: pytest.MonkeyPatch) -> None:
    """Acceptance: a fact matched only by keyword, when it is the best answer,
    ranks in the top results — it has no cosine similarity to sort by."""
    answer = _row(999, "Alice keeps the passport in the blue folder", kw_score=0.6)
    cur = _Cursor(vector=_dense(20), keyword=[answer])
    env.setattr(reranker, "rerank_pair", _scorer({"passport": 0.97}))

    results = await _search(cur)

    assert 999 in [r["id"] for r in results[:3]]


@pytest.mark.asyncio
async def test_returned_rows_carry_relevance_rerank_score_and_leg_ranks(
    env: pytest.MonkeyPatch,
) -> None:
    # Similarity 0.9: the vector leg's first row is its most similar one.
    both = _row(1, "Alice keeps the passport in the blue folder", similarity=0.9)
    cur = _Cursor(vector=[both, *_dense(5)[1:]], keyword=[both])
    env.setattr(reranker, "rerank_pair", _scorer({"passport": 0.9}))

    results = await _search(cur)

    top = results[0]
    assert top["id"] == 1
    assert top["leg_ranks"] == {"vector": 0, "keyword": 0}
    assert top["rerank_score"] == 0.9
    assert 0.0 <= top["relevance"] <= 1.0
    assert all("relevance" in r and "leg_ranks" in r for r in results)


@pytest.mark.asyncio
async def test_reranker_failure_returns_the_fused_order(env: pytest.MonkeyPatch) -> None:
    """Acceptance: with the reranker failing, recall still returns the fused order."""
    env.setenv("MEMORY_RANK_BLEND", "0")
    dense = _dense(8)
    keyword = [dense[5], dense[2], _row(50, "keyword only fact about folders")]

    async def boom(client: Any, query: str, doc: str) -> float:
        raise RuntimeError("reranker down")

    env.setattr(reranker, "rerank_pair", boom)
    failed = await _search(_Cursor(vector=dense, keyword=keyword))
    plain = await _search(_Cursor(vector=dense, keyword=keyword), use_reranker=False)

    assert failed, "a reranker failure must not empty the result"
    assert [r["id"] for r in failed] == [r["id"] for r in plain]


@pytest.mark.asyncio
async def test_search_embeds_the_query_with_the_query_instruction(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("ROBOTHOR_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    seen: list[str] = []

    async def capture(text: str, *a: Any, **k: Any) -> list[float]:
        seen.append(text)
        return [0.1] * 8

    cur = _Cursor(vector=_dense(3), keyword=[])
    with (
        patch.object(facts.llm_client, "get_embedding_async", capture),
        patch.object(facts, "get_connection", return_value=_conn(cur)),
        patch.object(facts, "apply_hnsw_session"),
    ):
        await facts.search_facts("tea?", use_reranker=False, tenant_id="test-tenant")

    assert seen and seen[0].startswith("Instruct: ") and seen[0].endswith("Query:tea?")


def _graph_hit(fid: int, text: str, score: float) -> Any:
    from robothor.memory.recall import Candidate

    payload = _row(fid, text, source="graph", origin="user_stated", graph_hops=2)
    return Candidate(id=fid, kind="fact", text=text, leg_scores={"graph": score}, payload=payload)


@pytest.mark.asyncio
async def test_the_graph_leg_keeps_its_reserved_places(env: pytest.MonkeyPatch) -> None:
    """The graph leg finds facts that are related, not similar. A
    query-document reranker scores them 0; the reserved places keep two, chosen
    by the leg's own score. (Replaces the one-hop entity_expansion leg's test.)"""
    hops = [
        _graph_hit(700, "Bob runs the shipping team", 0.9),
        _graph_hit(701, "Carol drives the shipping van", 0.5),
        _graph_hit(702, "Dan shipping weakest link", 0.1),
    ]
    cur = _Cursor(vector=_dense(20), keyword=[])
    env.setattr(reranker, "rerank_pair", _scorer({"shipping": 0.0}, default=0.6))

    with patch("robothor.memory.activation.graph_candidates_for_query", return_value=hops):
        results = await _search(cur)

    ids = [r["id"] for r in results]
    assert len(results) == 5
    assert 700 in ids and 701 in ids and 702 not in ids
    row = next(r for r in results if r["id"] == 700)
    assert "graph" in row["leg_ranks"]
    assert row["origin"] == "user_stated"


@pytest.mark.asyncio
async def test_include_graph_false_runs_no_graph_leg(env: pytest.MonkeyPatch) -> None:
    hops = [_graph_hit(700, "Bob runs the shipping team", 0.9)]
    cur = _Cursor(vector=_dense(20), keyword=[])
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    graph = MagicMock(return_value=hops)

    with patch("robothor.memory.activation.graph_candidates_for_query", graph):
        results = await _search(cur, include_graph=False)

    graph.assert_not_called()
    assert 700 not in [r["id"] for r in results]
    assert not any("graph" in r["leg_ranks"] for r in results)


@pytest.mark.asyncio
async def test_expand_entities_is_still_accepted(env: pytest.MonkeyPatch) -> None:
    """Callers that pass the superseded argument keep working: it now means
    "graph leg on"."""
    hops = [_graph_hit(700, "Bob runs the shipping team", 0.9)]
    cur = _Cursor(vector=_dense(20), keyword=[])
    env.setattr(reranker, "rerank_pair", _scorer({"shipping": 0.0}, default=0.6))

    with patch("robothor.memory.activation.graph_candidates_for_query", return_value=hops):
        results = await _search(cur, expand_entities=True, include_graph=False)

    assert 700 in [r["id"] for r in results]


@pytest.mark.asyncio
async def test_the_one_hop_expansion_answers_when_the_graph_returns_nothing(
    env: pytest.MonkeyPatch,
) -> None:
    """Nothing writes memory_mentions yet, so on existing data the graph leg is
    empty; the one-hop expansion keeps answering until it is not. (Replaces a
    test that pinned the expansion leg as removed.)"""
    hop = _row(700, "Bob runs the shipping team", entities=["Bob"], importance_score=0.9)
    cur = _Cursor(vector=_dense(20), keyword=[], expansion=[hop])
    env.setattr(reranker, "rerank_pair", _scorer({"shipping": 0.01}, default=0.6))
    get_entity = AsyncMock(return_value={"relations": [{"target_name": "Bob"}]})

    with (
        patch("robothor.memory.activation.graph_candidates_for_query", return_value=[]),
        patch("robothor.memory.entities.get_entity", get_entity),
    ):
        results = await _search(cur, expand_entities=True)

    row = next((r for r in results if r["id"] == 700), None)
    assert row is not None and "entity_expansion" in row["leg_ranks"]
    assert facts.RESERVED_PLACES == {"graph": 2, "entity_expansion": 1}


@pytest.mark.asyncio
async def test_the_one_hop_expansion_does_not_run_beside_a_graph_result(
    env: pytest.MonkeyPatch,
) -> None:
    hop = _row(800, "Dan runs the night shift", entities=["Dan"], importance_score=0.9)
    cur = _Cursor(vector=_dense(20), keyword=[], expansion=[hop])
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    get_entity = AsyncMock(return_value={"relations": [{"target_name": "Dan"}]})
    graph = [_graph_hit(700, "Bob runs the shipping team", 0.9)]

    with (
        patch("robothor.memory.activation.graph_candidates_for_query", return_value=graph),
        patch("robothor.memory.entities.get_entity", get_entity),
    ):
        results = await _search(cur, expand_entities=True)

    get_entity.assert_not_called()
    assert 800 not in [r["id"] for r in results]


@pytest.mark.asyncio
async def test_without_expand_entities_an_empty_graph_adds_nothing(
    env: pytest.MonkeyPatch,
) -> None:
    hop = _row(700, "Bob runs the shipping team", entities=["Bob"], importance_score=0.9)
    cur = _Cursor(vector=_dense(20), keyword=[], expansion=[hop])
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    get_entity = AsyncMock(return_value={"relations": [{"target_name": "Bob"}]})

    with (
        patch("robothor.memory.activation.graph_candidates_for_query", return_value=[]),
        patch("robothor.memory.entities.get_entity", get_entity),
    ):
        results = await _search(cur)

    get_entity.assert_not_called()
    assert 700 not in [r["id"] for r in results]


@pytest.mark.asyncio
async def test_a_failing_graph_leg_does_not_fail_the_search(env: pytest.MonkeyPatch) -> None:
    cur = _Cursor(vector=_dense(6), keyword=[])
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))

    with patch(
        "robothor.memory.activation.graph_candidates_for_query",
        side_effect=RuntimeError("function memory_activate does not exist"),
    ):
        results = await _search(cur)

    assert len(results) == 5


@pytest.mark.asyncio
async def test_episodes_are_fused_on_the_reranked_path(env: pytest.MonkeyPatch) -> None:
    """Episodes were dropped whenever the reranker ran (the default) unless a
    flag was on. They are now one more leg, ranked with everything else."""
    episode = {
        "id": 31,
        "source": "episode",
        "title": "Trip planning",
        "summary": "Alice packed the passport for the trip",
        "fact_text": "Trip planning: Alice packed the passport for the trip",
        "category": "episode",
        "confidence": 0.8,
        "similarity": 0.4,
    }
    cur = _Cursor(vector=_dense(10), keyword=[])
    env.setattr(reranker, "rerank_pair", _scorer({"passport": 0.95}))

    with patch("robothor.memory.episodes.search_episodes", AsyncMock(return_value=[episode])):
        results = await _search(cur, include_episodes=True)

    assert results[0].get("source") == "episode"
    assert len(results) <= 5, "fused legs share the limit instead of being appended past it"


@pytest.mark.asyncio
async def test_restricted_scope_leaves_out_legs_that_cannot_scope_by_person(
    env: pytest.MonkeyPatch,
) -> None:
    """Insights, episodes and chat turns carry no person_id, so a restricted
    caller would read summaries built from other people's facts. Like the
    router, search_facts leaves an unscopable store out rather than return it
    unfiltered."""
    from robothor.identity.scope import DataScope

    restricted = DataScope(tenant_id="test-tenant", person_id="person-1", restricted=True)
    insights = AsyncMock(return_value=[])
    episodes = AsyncMock(return_value=[])
    chat_turns = MagicMock(return_value=[])
    env.setattr(reranker, "rerank_pair", _scorer({}))

    with (
        patch.object(facts, "search_insights", insights),
        patch("robothor.memory.episodes.search_episodes", episodes),
        patch("robothor.engine.chat_store.search_chat_turns", chat_turns),
    ):
        results = await _search(
            _Cursor(vector=_dense(5), keyword=[]),
            include_insights=True,
            include_episodes=True,
            include_chat_turns=True,
            scope=restricted,
        )

    assert results, "the scoped fact legs still answer"
    insights.assert_not_called()
    episodes.assert_not_called()
    chat_turns.assert_not_called()


@pytest.mark.asyncio
async def test_unrestricted_scope_still_queries_the_auxiliary_legs(
    env: pytest.MonkeyPatch,
) -> None:
    from robothor.identity.scope import DataScope

    open_scope = DataScope(tenant_id="test-tenant", person_id="person-1", restricted=False)
    insights = AsyncMock(return_value=[])
    env.setattr(reranker, "rerank_pair", _scorer({}))
    with patch.object(facts, "search_insights", insights):
        await _search(
            _Cursor(vector=_dense(5), keyword=[]), include_insights=True, scope=open_scope
        )
    insights.assert_called_once()


@pytest.mark.asyncio
async def test_a_reserved_pick_never_brings_back_a_duplicate(env: pytest.MonkeyPatch) -> None:
    """The blend drops near-duplicates; a reserved place must not re-add one."""
    dense = _dense(20)
    copy = _row(701, dense[0]["fact_text"], entities=["Bob"], importance_score=0.9)
    cur = _Cursor(vector=dense, keyword=[], expansion=[copy])
    # The original is the best answer, so it is certain to be in the result.
    env.setattr(reranker, "rerank_pair", _scorer({dense[0]["fact_text"]: 0.95}, default=0.6))
    get_entity = AsyncMock(return_value={"relations": [{"target_name": "Bob"}]})

    with patch("robothor.memory.entities.get_entity", get_entity):
        results = await _search(cur, expand_entities=True)

    assert results[0]["id"] == dense[0]["id"]
    texts = [r["fact_text"] for r in results]
    assert len(texts) == len(set(texts)), f"duplicate text in results: {texts}"


@pytest.mark.asyncio
async def test_vector_leg_ranks_follow_similarity_under_relaxed_order(
    env: pytest.MonkeyPatch,
) -> None:
    """Pooled connections scan with hnsw.iterative_scan = relaxed_order, which
    may return rows slightly out of distance order. The leg's RRF rank must be
    its similarity order, so the leg re-sorts what the index returns."""
    env.setenv("MEMORY_RANK_BLEND", "0")
    rows = [
        _row(1, "first fact about apples", similarity=0.70),
        _row(2, "second fact about pears", similarity=0.90),
        _row(3, "third fact about plums", similarity=0.80),
    ]
    results = await _search(_Cursor(vector=rows, keyword=[]), use_reranker=False)
    ranks = {r["id"]: r["leg_ranks"]["vector"] for r in results}
    assert ranks == {2: 0, 3: 1, 1: 2}


@pytest.mark.asyncio
async def test_reserved_dedupe_holds_when_the_head_has_its_own_duplicate(
    env: pytest.MonkeyPatch,
) -> None:
    """With MEMORY_RANK_BLEND=0 the head is never deduped, so it can carry a
    near-duplicate pair. The exclusion must compare against the deduped head:
    a unique reserved pick is let in, a duplicate one is kept out."""
    env.setenv("MEMORY_RANK_BLEND", "0")
    dense = _dense(20)
    dense[1]["fact_text"] = dense[0]["fact_text"]  # an internal duplicate pair at the head
    unique = _row(702, "Bob runs the shipping team", entities=["Bob"], importance_score=0.9)
    copy = _row(703, dense[2]["fact_text"], entities=["Bob"], importance_score=0.95)
    cur = _Cursor(vector=dense, keyword=[], expansion=[copy, unique])
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    get_entity = AsyncMock(return_value={"relations": [{"target_name": "Bob"}]})

    with patch("robothor.memory.entities.get_entity", get_entity):
        results = await _search(cur, expand_entities=True)

    ids = [r["id"] for r in results]
    assert 703 not in ids, "a pick duplicating a head row must stay out"
    assert 702 in ids, "a unique pick must not be mistaken for a duplicate"

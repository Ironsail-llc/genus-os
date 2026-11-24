"""The recall-leg contract: any number of named legs, one fusion, reserved places.

A recall leg is anything that turns a query into a ranked list of
``Candidate`` objects — the vector and keyword legs over facts today, raw
conversation segments and graph activation tomorrow. ``fuse`` merges them by
reciprocal rank, and ``apply_reserved`` guarantees a leg a number of places in
the final top-k. The second matters because a multi-hop (graph) fact is by
definition not similar to the query, so a query-document reranker will
otherwise always discard it.
"""

from __future__ import annotations

from robothor.memory.recall import (
    Candidate,
    apply_reserved,
    fuse,
    relevance_from_rank,
)


def _leg(name: str, ids: list[int], kind: str = "fact") -> list[Candidate]:
    return [
        Candidate(id=i, kind=kind, text=f"{name} item {i}", leg_scores={name: 1.0 - n / 10})
        for n, i in enumerate(ids)
    ]


class TestFuse:
    def test_three_legs_are_fused_and_ranks_recorded_per_leg(self) -> None:
        legs = {
            "vector": _leg("vector", [1, 2, 3]),
            "keyword": _leg("keyword", [3, 4]),
            "graph": _leg("graph", [5, 3]),
        }
        fused = fuse(legs)

        by_id = {c.id: c for c in fused}
        assert set(by_id) == {1, 2, 3, 4, 5}
        # Item 3 was returned by all three legs; it must lead and know each rank.
        assert fused[0].id == 3
        assert by_id[3].leg_ranks == {"vector": 2, "keyword": 0, "graph": 1}
        assert by_id[5].leg_ranks == {"graph": 0}
        assert by_id[3].rrf_score > by_id[1].rrf_score > 0

    def test_fused_rank_is_the_output_position(self) -> None:
        fused = fuse({"a": _leg("a", [1, 2]), "b": _leg("b", [2, 3])})
        assert [c.fused_rank for c in fused] == list(range(len(fused)))

    def test_k_and_weights_change_the_score(self) -> None:
        legs = {"a": _leg("a", [1]), "b": _leg("b", [2])}
        even = {c.id: c.rrf_score for c in fuse(legs)}
        assert even[1] == even[2]
        weighted = fuse(legs, weights={"a": 0.5})
        assert weighted[0].id == 2, "an unweighted leg counts 1.0, so b now leads"
        assert fuse(legs, k=10)[0].rrf_score > fuse(legs, k=60)[0].rrf_score

    def test_same_id_different_kind_do_not_collide(self) -> None:
        fused = fuse({"a": _leg("a", [7], kind="fact"), "b": _leg("b", [7], kind="episode")})
        assert len(fused) == 2

    def test_similarity_and_payload_survive_the_merge(self) -> None:
        a = [Candidate(id=1, kind="fact", text="t", similarity=0.8, payload={"fact_text": "t"})]
        b = [Candidate(id=1, kind="fact", text="t", leg_scores={"keyword": 0.4})]
        (only,) = fuse({"vector": a, "keyword": b})
        assert only.similarity == 0.8
        assert only.payload["fact_text"] == "t"
        assert only.leg_scores["keyword"] == 0.4

    def test_empty(self) -> None:
        assert fuse({}) == []
        assert fuse({"vector": []}) == []

    def test_to_row_carries_the_contract_fields_and_the_payload(self) -> None:
        (c,) = fuse({"vector": [Candidate(id=1, kind="fact", text="t", payload={"x": 1})]})
        row = c.to_row()
        assert row["x"] == 1
        assert row["id"] == 1
        assert row["kind"] == "fact"
        for key in ("leg_ranks", "leg_scores", "rrf_score", "fused_rank", "rerank_score"):
            assert key in row


class TestRelevanceFromRank:
    def test_bounded_and_monotone(self) -> None:
        values = [relevance_from_rank(r) for r in range(50)]
        assert values[0] == 1.0
        assert all(0.0 < v <= 1.0 for v in values)
        assert values == sorted(values, reverse=True)


def _rows(fused: list[Candidate]) -> list[dict]:
    return [c.to_row() for c in fused]


class TestReservedPlaces:
    """A fake third leg ('graph') whose candidates the reranker would drop."""

    def _pool(self) -> list[dict]:
        legs = {
            "vector": _leg("vector", list(range(1, 11))),
            "graph": [
                Candidate(id=101, kind="fact", text="hop a", leg_scores={"graph": 0.2}),
                Candidate(id=102, kind="fact", text="hop b", leg_scores={"graph": 0.9}),
                Candidate(id=103, kind="fact", text="hop c", leg_scores={"graph": 0.5}),
            ],
        }
        return _rows(fuse(legs))

    def test_reserved_leg_gets_its_places_chosen_by_its_own_score(self) -> None:
        pool = self._pool()
        # The ranked list a reranker produced: graph candidates nowhere in it.
        ranked = [r for r in pool if r["id"] <= 10]
        out = apply_reserved(ranked, pool, limit=5, reserved={"graph": 2})

        assert len(out) == 5
        graph_ids = [r["id"] for r in out if "graph" in r["leg_ranks"]]
        assert graph_ids == [102, 103], "the leg's own score picks which hops survive"
        # The non-reserved head of the ranked list keeps its order.
        assert [r["id"] for r in out[:3]] == [r["id"] for r in ranked[:3]]

    def test_no_change_when_the_quota_is_already_met(self) -> None:
        pool = self._pool()
        ranked = [next(r for r in pool if r["id"] == 102)] + [r for r in pool if r["id"] <= 10]
        out = apply_reserved(ranked, pool, limit=5, reserved={"graph": 1})
        assert [r["id"] for r in out] == [r["id"] for r in ranked[:5]]

    def test_no_candidates_from_the_leg_means_nothing_reserved(self) -> None:
        pool = _rows(fuse({"vector": _leg("vector", [1, 2, 3])}))
        out = apply_reserved(pool, pool, limit=2, reserved={"graph": 2})
        assert [r["id"] for r in out] == [pool[0]["id"], pool[1]["id"]]

    def test_fewer_candidates_than_reserved_takes_what_exists(self) -> None:
        pool = self._pool()
        ranked = [r for r in pool if r["id"] <= 10]
        # limit 9 leaves room for four reserved places; only three candidates exist.
        out = apply_reserved(ranked, pool, limit=9, reserved={"graph": 10})
        assert len(out) == 9
        assert {r["id"] for r in out if "graph" in r["leg_ranks"]} == {101, 102, 103}


class TestReservedPlacesNeverTakeTheHead:
    """Reserved places are a minority of a result, never its top.

    At a small ``limit`` a reserved pick used to evict every ranked row — with
    ``limit=1`` the single best answer was replaced by a graph neighbour chosen
    by importance alone. Reserved picks now take at most ``(limit - 1) // 2``
    places in total, so the ranked head always keeps the majority.
    """

    def _pool_and_ranked(self) -> tuple[list[dict], list[dict]]:
        pool = TestReservedPlaces()._pool()
        return pool, [r for r in pool if r["id"] <= 10]

    def test_limit_one_returns_the_best_ranked_row(self) -> None:
        pool, ranked = self._pool_and_ranked()
        out = apply_reserved(ranked, pool, limit=1, reserved={"graph": 2, "entity": 1})
        assert [r["id"] for r in out] == [ranked[0]["id"]]

    def test_limit_two_keeps_both_ranked_rows(self) -> None:
        pool, ranked = self._pool_and_ranked()
        out = apply_reserved(ranked, pool, limit=2, reserved={"graph": 2})
        assert [r["id"] for r in out] == [r["id"] for r in ranked[:2]]

    def test_reserved_total_is_capped_below_half(self) -> None:
        pool, ranked = self._pool_and_ranked()
        for limit in range(1, 11):
            out = apply_reserved(ranked, pool, limit=limit, reserved={"graph": 10})
            reserved_rows = [r for r in out if "graph" in r["leg_ranks"]]
            assert len(reserved_rows) <= (limit - 1) // 2, limit
            assert out[0]["id"] == ranked[0]["id"], limit

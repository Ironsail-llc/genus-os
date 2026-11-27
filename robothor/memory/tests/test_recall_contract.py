"""The recall-leg contract: any number of named legs, one fusion, reserved places.

A recall leg is anything that turns a query into a ranked list of
``Candidate`` objects — the vector and keyword legs over facts, conversation
segments, graph activation. ``fuse`` merges them by
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


class TestRowText:
    """What a model reads for a recall row (``recall.row_text``, the one
    formatter every surface uses). A row only the graph found is about an
    entity related to the one asked about, and says so; the answer model
    otherwise took an employer's address for the person's."""

    def _row(self, legs: set[str], **kw: object) -> dict:
        return {
            "id": 1,
            "kind": "fact",
            "fact_text": "Example Co is headquartered in Brelford.",
            "leg_ranks": dict.fromkeys(legs, 0),
            "graph_path": ["Alice Smith", "Example Co"],
            **kw,
        }

    def test_a_graph_only_row_names_its_relation_path(self) -> None:
        from robothor.memory.recall import row_text

        assert row_text(self._row({"graph"})) == (
            "Related via Alice Smith → Example Co: Example Co is headquartered in Brelford."
        )

    def test_a_row_the_head_legs_also_found_keeps_its_plain_text(self) -> None:
        from robothor.memory.recall import row_text

        for legs in ({"graph", "vector"}, {"keyword"}, {"vector", "graph", "segment"}):
            assert row_text(self._row(legs)) == "Example Co is headquartered in Brelford."

    def test_without_a_path_the_text_is_plain(self) -> None:
        from robothor.memory.recall import row_text

        assert row_text(self._row({"graph"}, graph_path=[])) == (
            "Example Co is headquartered in Brelford."
        )
        assert row_text({"insight_text": "An insight", "leg_ranks": {"insight": 0}}) == "An insight"


def _base_apply_reserved(ranked, pool, limit, reserved, exclude=None):
    """``apply_reserved`` exactly as the base branch has it (before the graph's
    best-rows rule), for legs other than the graph."""
    from robothor.memory.recall import _leg_order, _row_key, max_reserved_picks

    out = list(ranked[:limit])
    protected: set = set()
    picks: list = []
    budget = max_reserved_picks(limit)
    for leg, want in reserved.items():
        if want <= 0:
            continue
        have = [r for r in out if leg in (r.get("leg_ranks") or {})][:want]
        protected.update(_row_key(r) for r in have)
        need = min(want - len(have), budget - len(picks))
        if need <= 0:
            continue
        taken = {_row_key(r) for r in out} | {_row_key(r) for r in picks}
        extra = sorted(
            (
                r
                for r in pool
                if leg in (r.get("leg_ranks") or {})
                and _row_key(r) not in taken
                and not (exclude is not None and exclude(r))
            ),
            key=_leg_order(leg),
        )[:need]
        for r in extra:
            r["reserved_leg"] = leg
            protected.add(_row_key(r))
        picks.extend(extra)
    for pick in picks:
        while len(out) + 1 > limit:
            victim = next(
                (i for i in range(len(out) - 1, 0, -1) if _row_key(out[i]) not in protected),
                None,
            )
            if victim is None:
                break
            out.pop(victim)
        if len(out) < limit:
            out.append(pick)
    return out


class TestTheDefaultPathIsUnchanged:
    """The graph leg is off by default; what a default search does must be
    exactly what the base branch does. The best-rows rule is the graph's
    alone: ``entity_expansion`` (and any other reserved leg) keeps the
    previous rule, compared here row for row with a frozen copy of it."""

    def _pool(self, expansion_scores: list[float]) -> list[dict]:
        legs = {
            "vector": _leg("vector", list(range(1, 11))),
            "entity_expansion": [
                Candidate(
                    id=500 + i, kind="fact", text=f"exp {i}",
                    leg_scores={"entity_expansion": s},
                )
                for i, s in enumerate(expansion_scores)
            ],
        }  # fmt: skip
        return _rows(fuse(legs))

    def _cases(self):
        import copy

        pool = self._pool([0.9, 0.6, 0.3])
        by_id = {r["id"]: r for r in pool}
        head = [r for r in pool if r["id"] <= 10]
        weakest_in_head = [*head[:2], by_id[502], *head[2:]]
        best_in_head = [*head[:2], by_id[500], *head[2:]]
        for ranked in (head, weakest_in_head, best_in_head):
            for limit in (3, 5, 10):
                for reserved in ({"entity_expansion": 1}, {"graph": 2, "entity_expansion": 1}):
                    keys = {(r["kind"], r["id"]) for r in ranked[:limit]}
                    for exclude in (None, lambda r, k=keys: (r["kind"], r["id"]) in k):
                        yield copy.deepcopy(ranked), copy.deepcopy(pool), limit, reserved, exclude

    def test_entity_expansion_reservations_match_the_previous_rule(self) -> None:
        n = 0
        for ranked, pool, limit, reserved, exclude in self._cases():
            import copy

            new = apply_reserved(
                copy.deepcopy(ranked), copy.deepcopy(pool), limit, reserved, exclude
            )
            old = _base_apply_reserved(ranked, pool, limit, reserved, exclude)
            assert [(r["id"], r.get("reserved_leg")) for r in new] == [
                (r["id"], r.get("reserved_leg")) for r in old
            ], (limit, reserved, [r["id"] for r in ranked[:limit]])
            n += 1
        assert n == 36


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

        # The head is untouched; the graph's rows come after it.
        assert [r["id"] for r in out[:5]] == [r["id"] for r in ranked[:5]]
        assert [r["id"] for r in out[5:]] == [102, 103], "the leg's own score picks the hops"

    def test_no_change_when_the_quota_is_already_met(self) -> None:
        pool = self._pool()
        ranked = [next(r for r in pool if r["id"] == 102)] + [r for r in pool if r["id"] <= 10]
        out = apply_reserved(ranked, pool, limit=5, reserved={"graph": 1})
        assert [r["id"] for r in out] == [r["id"] for r in ranked[:5]]

    def test_the_places_go_to_the_legs_best_rows_not_to_any_row_it_found(self) -> None:
        """A weak graph row that the ranked head holds anyway (another leg
        found it too) does not use up the leg's place: the leg's best row by
        its own score is what the place is for. On the eval world the wrong
        organisation's address sat in the head and kept the right one out."""
        pool = self._pool()
        weakest = next(r for r in pool if r["id"] == 101)
        ranked = (
            [r for r in pool if r["id"] <= 2] + [weakest] + [r for r in pool if 3 <= r["id"] <= 10]
        )
        out = apply_reserved(ranked, pool, limit=5, reserved={"graph": 1})

        ids = [r["id"] for r in out]
        assert ids[:5] == [r["id"] for r in ranked[:5]]
        assert ids[5:] == [102], ids

    def test_a_best_row_the_head_holds_counts_toward_the_places(self) -> None:
        """``search_facts`` passes an exclude that is true for any row already
        in the head (it duplicates itself). That must not drop the leg's best
        row from its places: the head holds it, so one place is used, and only
        the second-best row is added after the head."""
        pool = self._pool()
        best = next(r for r in pool if r["id"] == 102)
        ranked = (
            [r for r in pool if r["id"] <= 2] + [best] + [r for r in pool if 3 <= r["id"] <= 10]
        )
        head = {(r["kind"], r["id"]) for r in ranked[:5]}

        out = apply_reserved(
            ranked, pool, limit=5, reserved={"graph": 2},
            exclude=lambda r: (r["kind"], r["id"]) in head,
        )  # fmt: skip

        ids = [r["id"] for r in out]
        assert ids[:5] == [r["id"] for r in ranked[:5]], ids
        assert ids[5:] == [103], "only the second-best graph row is added"

    def test_no_candidates_from_the_leg_means_nothing_reserved(self) -> None:
        pool = _rows(fuse({"vector": _leg("vector", [1, 2, 3])}))
        out = apply_reserved(pool, pool, limit=2, reserved={"graph": 2})
        assert [r["id"] for r in out] == [pool[0]["id"], pool[1]["id"]]

    def test_fewer_candidates_than_reserved_takes_what_exists(self) -> None:
        pool = self._pool()
        ranked = [r for r in pool if r["id"] <= 10]
        # Ten places asked for; only three candidates exist.
        out = apply_reserved(ranked, pool, limit=9, reserved={"graph": 10})
        assert len(out) == 9 + 3
        assert {r["id"] for r in out if "graph" in r["leg_ranks"]} == {101, 102, 103}


class TestReservedPlacesNeverTakeTheHead:
    """Reserved places never take the head.

    At a small ``limit`` a reserved pick used to evict every ranked row — with
    ``limit=1`` the single best answer was replaced by a graph neighbour chosen
    by importance alone. Picks of other legs take at most ``(limit - 1) // 2``
    places in total; the graph's rows are added after the head and evict
    nothing (on the held-out split a reserved graph row pushed the right
    point-in-time fact out of a five-row result in 4 of 6 losses).
    """

    def _pool_and_ranked(self) -> tuple[list[dict], list[dict]]:
        pool = TestReservedPlaces()._pool()
        return pool, [r for r in pool if r["id"] <= 10]

    def test_limit_one_keeps_the_best_ranked_row_first(self) -> None:
        pool, ranked = self._pool_and_ranked()
        out = apply_reserved(ranked, pool, limit=1, reserved={"graph": 2, "entity": 1})
        assert [r["id"] for r in out] == [ranked[0]["id"], 102, 103]

    def test_limit_two_keeps_both_ranked_rows(self) -> None:
        pool, ranked = self._pool_and_ranked()
        out = apply_reserved(ranked, pool, limit=2, reserved={"graph": 2})
        assert [r["id"] for r in out[:2]] == [r["id"] for r in ranked[:2]]

    def test_two_reserved_legs_never_evict_rank_zero(self) -> None:
        """With graph rows protected by their own quota, the next leg's pick
        found rank 0 as the only unprotected row. The victim search now never
        reaches index 0."""
        legs = {
            "vector": _leg("vector", [1]),
            "graph": [
                Candidate(id=201, kind="fact", text="g1", leg_scores={"graph": 0.9}),
                Candidate(id=202, kind="fact", text="g2", leg_scores={"graph": 0.8}),
            ],
            "entity": [Candidate(id=301, kind="fact", text="e1", leg_scores={"entity": 0.7})],
        }
        pool = _rows(fuse(legs))
        by_id = {r["id"]: r for r in pool}
        ranked = [by_id[1], by_id[201], by_id[202]]
        out = apply_reserved(ranked, pool, limit=3, reserved={"graph": 2, "entity": 1})
        assert out[0]["id"] == 1
        assert len(out) == 3

    def test_graph_rows_follow_the_head_and_never_replace_it(self) -> None:
        """At most ``limit`` + the graph's reserved count rows come back, and
        the first ``limit`` are the ranked head exactly."""
        pool, ranked = self._pool_and_ranked()
        for limit in range(1, 11):
            for want in (1, 2, 10):
                out = apply_reserved(ranked, pool, limit=limit, reserved={"graph": want})
                assert [r["id"] for r in out[:limit]] == [r["id"] for r in ranked[:limit]]
                assert len(out) <= limit + want, (limit, want)
                assert all(r.get("reserved_leg") == "graph" for r in out[limit:])

    def test_other_legs_still_take_at_most_a_minority(self) -> None:
        pool = _rows(fuse({
            "vector": _leg("vector", list(range(1, 11))),
            "entity_expansion": [
                Candidate(id=300 + i, kind="fact", text=f"e{i}",
                          leg_scores={"entity_expansion": 1 - i / 10})
                for i in range(5)
            ],
        }))  # fmt: skip
        ranked = [r for r in pool if r["id"] <= 10]
        for limit in range(1, 11):
            out = apply_reserved(ranked, pool, limit=limit, reserved={"entity_expansion": 5})
            picked = [r for r in out if "entity_expansion" in r["leg_ranks"]]
            assert len(out) <= limit and len(picked) <= (limit - 1) // 2, limit
            assert out[0]["id"] == ranked[0]["id"], limit

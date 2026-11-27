"""A changed value, end to end on a real database.

The slot lookup reads the subject's entity index (``lower_entities`` and
``memory_mentions``), not the similarity search, so a change scored below the
classify threshold ("Alice lives in Boston" vs "Alice moved to Denver": 0.692)
is still found. Nothing is deleted: the old row is closed, the new one is
current, and the row count only ever grows. A decision only ever compares
facts of one tenant and one scope.

Vectors are one-hot so no two facts are similar: only the slot lookup can
connect them. No model is called.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT = "changed-values-test"
OTHER_TENANT = "changed-values-other"
_DIM = 1024


def _vec(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i % _DIM] = 1.0
    return v


@pytest.fixture
def tenants():
    from robothor.db.connection import get_connection

    def _wipe() -> None:
        with get_connection() as conn:
            cur = conn.cursor()
            # A test may point one tenant's row at another's; unlink first.
            cur.execute(
                "UPDATE memory_facts SET superseded_by = NULL WHERE tenant_id IN (%s, %s)",
                (TENANT, OTHER_TENANT),
            )
            for t in (TENANT, OTHER_TENANT):
                cur.execute("DELETE FROM memory_conflict_decisions WHERE tenant_id = %s", (t,))
                cur.execute("DELETE FROM memory_mentions WHERE tenant_id = %s", (t,))
                cur.execute("DELETE FROM memory_relation_evidence WHERE tenant_id = %s", (t,))
                cur.execute("DELETE FROM memory_relations WHERE tenant_id = %s", (t,))
                cur.execute("DELETE FROM memory_facts WHERE tenant_id = %s", (t,))
                cur.execute("DELETE FROM memory_entities WHERE tenant_id = %s", (t,))
            conn.commit()

    with get_connection() as conn:
        cur = conn.cursor()
        for t in (TENANT, OTHER_TENANT):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (t, f"{t} tenant"),
            )
        conn.commit()
    _wipe()
    yield
    _wipe()


class _Verdict:
    """The classifier and the slot judge, mocked: every pair gets ``kind``.

    ``kind`` is a classifier answer (update, new, duplicate, contradiction),
    ``earlier``, or ``unavailable``; the judge hears it as its own outcome.
    """

    _OUTCOME = {
        "update": "changed",
        "new": "coexist",
        "duplicate": "same",
        "contradiction": "conflict",
        "earlier": "earlier",
    }

    def __init__(self) -> None:
        self.kind = "update"
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, new: str, old: str) -> dict[str, Any]:
        self.calls.append((new, old))
        if self.kind == "unavailable":
            return {"classification": "new", "reasoning": "down", "unavailable": True}
        return {"classification": self.kind, "reasoning": "mocked"}

    async def judge(self, new: str, old: str, **_context: Any) -> dict[str, Any]:
        self.calls.append((new, old))
        if self.kind == "unavailable":
            return {"outcome": None, "reasoning": "down", "unavailable": True}
        return {"outcome": self._OUTCOME[self.kind], "reasoning": "mocked"}


@pytest.fixture(autouse=True)
def verdict(monkeypatch):
    import robothor.memory.conflicts as cf

    v = _Verdict()
    monkeypatch.setattr(cf, "classify_relationship", v)
    monkeypatch.setattr(cf, "judge_slot_change", v.judge)
    return v


_counter = iter(range(1, 10_000))


async def _write(text: str, *, tenant: str = TENANT, **kw: Any) -> dict[str, Any]:
    from robothor.memory.conflicts import resolve_and_store
    from robothor.memory.facts import parse_extraction_response

    parsed = parse_extraction_response(
        json.dumps(
            {
                "facts": [
                    {
                        "fact_text": text,
                        "category": kw.pop("category", "personal"),
                        "confidence": 0.9,
                        "event_time": kw.get("event_time"),
                        "mentions": [
                            {"surface_form": n, "entity_type": "person"}
                            for n in kw.pop("names", ["Alice"])
                        ],
                        "relations": kw.pop("relations", []),
                    }
                ]
            }
        )
    )
    assert parsed, text
    kw.setdefault("embedding", _vec(next(_counter)))
    return await resolve_and_store(parsed[0], "src", "conversation", tenant_id=tenant, **kw)


def _rows(tenant: str = TENANT) -> dict[int, dict[str, Any]]:
    from psycopg2.extras import RealDictCursor

    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, fact_text, is_active, superseded_by, valid_from, valid_to, metadata, "
            "reinforcement_count FROM memory_facts WHERE tenant_id = %s",
            (tenant,),
        )
        return {int(r["id"]): dict(r) for r in cur.fetchall()}


def _decisions() -> list[dict[str, Any]]:
    from psycopg2.extras import RealDictCursor

    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT classification, action, new_fact_id, existing_fact_id, reasoning "
            "FROM memory_conflict_decisions WHERE tenant_id = %s ORDER BY id",
            (TENANT,),
        )
        return [dict(r) for r in cur.fetchall()]


class TestChangeEndToEnd:
    async def test_a_move_closes_the_old_value_and_loses_no_fact(self, tenants):
        first = await _write("Alice lives in Boston.")
        assert first["action"] == "stored"
        before = len(_rows())

        out = await _write("Alice moved to Denver.", names=["Alice", "Denver"])
        assert out["action"] == "superseded", out

        rows = _rows()
        assert len(rows) == before + 1  # nothing deleted
        old, new = rows[first["new_id"]], rows[out["new_id"]]
        assert old["is_active"] is False
        assert old["superseded_by"] == out["new_id"]
        assert old["valid_to"] is not None
        assert old["metadata"]["superseded_reason"] == "changed"
        assert new["is_active"] is True
        assert new["valid_from"] is not None
        assert first["new_id"] in new["metadata"]["supersedes"]
        assert [d["action"] for d in _decisions()] == ["superseded"]

    async def test_a_reversed_preference_is_current_and_the_old_one_kept(self, tenants):
        first = await _write("Bob prefers tea over coffee.", names=["Bob"])
        out = await _write("Bob prefers coffee over tea.", names=["Bob"])
        assert out["action"] == "superseded"
        rows = _rows()
        assert rows[first["new_id"]]["is_active"] is False
        assert rows[out["new_id"]]["is_active"] is True

    async def test_a_true_repeat_reinforces_and_stores_nothing(self, tenants):
        # The same statement again embeds the same: one shared vector.
        first = await _write("Alice lives in Boston.", embedding=_vec(900))
        before = len(_rows())
        out = await _write("Alice now lives in Boston.", embedding=_vec(900))
        assert out["action"] == "reinforced"
        rows = _rows()
        assert len(rows) == before
        assert rows[first["new_id"]]["reinforcement_count"] == 1
        assert rows[first["new_id"]]["is_active"] is True

    async def test_a_multi_valued_attribute_keeps_both_active(self, tenants):
        a = await _write("Alice has visited Paris.", names=["Alice", "Paris"])
        b = await _write("Alice has visited Rome.", names=["Alice", "Rome"])
        assert b["action"] == "stored"
        rows = _rows()
        assert rows[a["new_id"]]["is_active"] is True
        assert rows[b["new_id"]]["is_active"] is True

    async def test_a_resolved_entity_joins_two_names_for_one_subject(self, tenants):
        from robothor.db.connection import get_connection

        first = await _write("Ally lives in Boston.", names=["Ally"])
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO memory_entities (name, entity_type, tenant_id) "
                "VALUES ('Alice', 'person', %s) RETURNING id",
                (TENANT,),
            )
            eid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form) "
                "VALUES (%s, %s, %s, 'Ally')",
                (TENANT, first["new_id"], eid),
            )
            conn.commit()
        out = await _write("Alice moved to Denver.")
        assert out["action"] == "superseded", out
        assert _rows()[first["new_id"]]["superseded_by"] == out["new_id"]


class TestAuthorityEndToEnd:
    async def test_an_agent_value_does_not_replace_a_user_stated_one(self, tenants):
        first = await _write("Alice lives in Boston.", origin="user_stated")
        before = len(_rows())
        out = await _write("Alice moved to Denver.", origin="agent_generated")
        assert out["action"] == "kept_authority"
        rows = _rows()
        assert len(rows) == before + 1  # both kept
        assert rows[first["new_id"]]["is_active"] is True
        assert rows[out["new_id"]]["is_active"] is False
        assert rows[out["new_id"]]["superseded_by"] == first["new_id"]
        assert [d["action"] for d in _decisions()] == ["kept_authority"]


class TestIsolation:
    async def test_another_tenants_value_is_never_touched(self, tenants):
        theirs = await _write("Alice lives in Boston.", tenant=OTHER_TENANT)
        out = await _write("Alice moved to Denver.")
        assert out["action"] == "stored"
        assert _rows(OTHER_TENANT)[theirs["new_id"]]["is_active"] is True

    async def test_another_persons_private_value_is_never_touched(self, tenants):
        theirs = await _write(
            "Alice lives in Boston.", scope_kind="user", scope_id="user:someone-else"
        )
        out = await _write("Alice moved to Denver.", scope_kind="user", scope_id="user:reader")
        assert out["action"] == "stored"
        assert _rows()[theirs["new_id"]]["is_active"] is True

    async def test_a_private_value_does_not_close_a_shared_one_nor_the_reverse(self, tenants):
        shared = await _write("Alice lives in Boston.")
        mine = await _write("Alice moved to Denver.", scope_kind="user", scope_id="user:reader")
        assert mine["action"] == "stored"
        private = await _write(
            "Bob lives in Lisbon.", names=["Bob"], scope_kind="user", scope_id="user:reader"
        )
        public = await _write("Bob moved to Porto.", names=["Bob"])
        assert public["action"] == "stored"
        rows = _rows()
        assert rows[shared["new_id"]]["is_active"] is True
        assert rows[private["new_id"]]["is_active"] is True


class TestBoundedLookup:
    async def test_the_lookup_reads_at_most_its_limit(self, tenants):
        from robothor.memory.conflicts import fact_slot, find_slot_candidates

        for i in range(30):
            await _write(f"Alice has visited City{i}.", names=["Alice", f"City{i}"])
        slot = fact_slot({"fact_text": "Alice has visited Rome."})
        found = await find_slot_candidates(slot, tenant_id=TENANT, limit=10)
        assert len(found.rows) == 10


class TestRecall:
    """search_facts, keyword and dense legs only, the query vector patched."""

    async def _search(self, monkeypatch, vector_of: int, **kw: Any) -> list[dict[str, Any]]:
        import robothor.memory.facts as facts

        async def _embed(_q: str) -> list[float]:
            return _vec(vector_of)

        monkeypatch.setattr(facts, "_embed_query", _embed)
        return await facts.search_facts(
            "Where does Alice live?",
            limit=5,
            use_reranker=False,
            include_segments=False,
            tenant_id=TENANT,
            **kw,
        )

    async def _boston_then_denver(self) -> tuple[int, int, int, int]:
        global _counter
        _counter = iter(range(500, 10_000))
        first = await _write("Alice lives in Boston.")
        second = await _write("Alice moved to Denver.", names=["Alice", "Denver"])
        assert second["action"] == "superseded"
        return first["new_id"], 500, second["new_id"], 501

    async def test_default_recall_puts_the_current_value_first_and_labels_the_old(
        self, tenants, monkeypatch
    ):
        old_id, _old_vec, new_id, new_vec = await self._boston_then_denver()
        rows = await self._search(monkeypatch, new_vec)
        ids = [r["id"] for r in rows]
        assert new_id in ids and old_id in ids
        assert ids.index(new_id) < ids.index(old_id)
        old = rows[ids.index(old_id)]
        assert old["superseded"] is True
        assert old["superseded_reason"] == "changed"
        assert old["superseded_by"] == new_id
        assert old["superseded_at"]
        assert not rows[ids.index(new_id)].get("superseded")

    async def test_history_recall_never_ranks_the_old_value_first(self, tenants, monkeypatch):
        old_id, old_vec, new_id, _new_vec = await self._boston_then_denver()
        rows = await self._search(monkeypatch, old_vec, active_only=False)
        ids = [r["id"] for r in rows]
        assert ids.index(new_id) < ids.index(old_id)

    async def test_an_earlier_row_of_another_tenant_is_never_read(self, tenants, monkeypatch):
        from robothor.db.connection import get_connection

        theirs = await _write("Alice lives in Boston.", tenant=OTHER_TENANT)
        global _counter
        _counter = iter(range(700, 10_000))
        mine = await _write("Alice moved to Denver.", names=["Alice", "Denver"])
        with get_connection() as conn:
            cur = conn.cursor()
            # A forged pointer: the read must still stay in the reader's tenant.
            cur.execute(
                "UPDATE memory_facts SET metadata = jsonb_build_object('supersedes', "
                "jsonb_build_array(%s::int)) WHERE id = %s",
                (theirs["new_id"], mine["new_id"]),
            )
            cur.execute(
                "UPDATE memory_facts SET superseded_by = %s WHERE id = %s",
                (mine["new_id"], theirs["new_id"]),
            )
            conn.commit()
        rows = await self._search(monkeypatch, 700)
        assert theirs["new_id"] not in [r["id"] for r in rows]


# ── The verifier's findings, as behaviour ──────────────────────────────────


def _active(text_part: str) -> list[int]:
    return [i for i, r in _rows().items() if r["is_active"] and text_part in r["fact_text"]]


class TestNothingTrueIsClosed:
    @pytest.mark.parametrize(
        ("old", "new", "names"),
        [
            ("Alice works at Acme.", "Alice also works at Globex.", ["Alice"]),
            ("Alice lives in Boston.", "Alice moved to the sales team.", ["Alice"]),
            ("Alice works at Acme.", "Alice works at night.", ["Alice"]),
            ("Alice lives in Boston.", "Alice moved to Denver last year but is back in Boston.",
             ["Alice"]),
            ("Alice's manager is Bob.", "Alice's manager is on vacation.", ["Alice", "Bob"]),
            ("Alice's email is alice@example.com.", "Alice's email is also alice@work.example.",
             ["Alice"]),
            ("Alice lives in Boston.", "Alice lives in Denver?", ["Alice"]),
        ],
    )  # fmt: skip
    async def test_a_sentence_that_is_not_a_plain_change_closes_nothing(
        self, tenants, verdict, old, new, names
    ):
        first = await _write(old, names=names)
        await _write(new, names=names)
        assert _rows()[first["new_id"]]["is_active"] is True

    async def test_a_pronoun_never_reaches_another_persons_fact(self, tenants):
        first = await _write("She lives in Boston.", names=["Carol"], embedding=_vec(800))
        await _write("She lives in Denver.", names=["Dana"], embedding=_vec(800))
        assert _rows()[first["new_id"]]["is_active"] is True

    async def test_an_older_event_becomes_the_earlier_value(self, tenants, verdict):
        verdict.kind = "earlier"
        first = await _write("Alice lives in Boston.", event_time="2026-11-01")
        out = await _write("Alice moved to Denver in 2019.", event_time="2019-06-01")
        assert out["action"] == "stored_history"
        rows = _rows()
        old, new = rows[first["new_id"]], rows[out["new_id"]]
        assert old["is_active"] is True
        assert new["is_active"] is False and new["superseded_by"] == first["new_id"]
        assert new["valid_from"] < new["valid_to"]
        assert out["new_id"] in old["metadata"]["supersedes"]

    async def test_without_a_classifier_both_stay(self, tenants, verdict):
        verdict.kind = "unavailable"
        first = await _write("Alice lives in Boston.")
        out = await _write("Alice moved to Denver.", names=["Alice", "Denver"])
        assert out["action"] == "stored"
        assert len(_active("Alice")) == 2
        assert _rows()[first["new_id"]]["is_active"] is True
        assert "conflict_unresolved" in [d["action"] for d in _decisions()]

    async def test_a_legacy_fact_is_not_closed_by_a_tool(self, tenants):
        first = await _write("Alice lives in Boston.", origin=None)
        out = await _write("Alice moved to Denver.", origin="tool")
        assert out["action"] == "kept_authority"
        assert _rows()[first["new_id"]]["is_active"] is True


class TestSelfAndRepeats:
    _REL = [{"source": "Alice", "relation": "lives in", "target": "New York City"}]

    async def test_the_same_fact_twice_never_closes_itself(self, tenants):
        a = await _write("Alice is based in NYC.", relations=self._REL)
        b = await _write("Alice is based in NYC.", relations=self._REL)
        row = _rows()[a["new_id"]]
        assert row["is_active"] is True
        assert row["superseded_by"] is None
        assert b.get("new_id", a["new_id"]) == a["new_id"]

    async def test_history_recall_survives_a_fact_stored_twice(self, tenants, monkeypatch):
        import robothor.memory.facts as facts

        await _write("Alice is based in NYC.", relations=self._REL, embedding=_vec(820))
        await _write("Alice is based in NYC.", relations=self._REL, embedding=_vec(821))

        async def _embed(_q: str) -> list[float]:
            return _vec(820)

        monkeypatch.setattr(facts, "_embed_query", _embed)
        rows = await facts.search_facts(
            "Where is Alice based?",
            use_reranker=False,
            include_segments=False,
            tenant_id=TENANT,
            active_only=False,
        )
        assert rows

    async def test_an_overruled_sentence_seen_again_adds_no_row(self, tenants):
        await _write("Alice lives in Boston.", origin="user_stated")
        for _ in range(4):
            await _write("Alice moved to Denver.", origin="external", embedding=_vec(830))
        assert len([r for r in _rows().values() if "Denver" in r["fact_text"]]) == 1


class TestBusySubject:
    async def test_many_other_facts_about_the_subject_do_not_hide_the_slot(self, tenants):
        first = await _write("Alice lives in Boston.")
        for i in range(30):
            await _write(f"Alice has visited City{i}.", names=["Alice", f"City{i}"])
        out = await _write("Alice moved to Denver.", names=["Alice", "Denver"])
        assert out["action"] == "superseded", out
        assert _rows()[first["new_id"]]["is_active"] is False


class TestDeterministicEndToEnd:
    async def test_a_resolved_different_value_still_asks_the_judge(self, tenants, verdict):
        """Resolved entities never close a fact on their own (containment, two
        nodes for one place, a second value all look like a change)."""
        from robothor.db.connection import get_connection

        rel = [{"source": "Alice", "relation": "lives in", "target": "Boston"}]
        first = await _write(
            "Alice lives in Boston.",
            names=["Alice", "Boston"],
            relations=rel,
            origin="agent_generated",
        )
        with get_connection() as conn:
            cur = conn.cursor()
            ids = {}
            for name, kind in (("Alice", "person"), ("Boston", "location"), ("Denver", "location")):
                cur.execute(
                    "INSERT INTO memory_entities (name, entity_type, tenant_id) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (name, kind, TENANT),
                )
                ids[name] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
                "predicate, tenant_id, fact_id, origin) "
                "VALUES (%s, %s, 'lives in', 'lives_in', %s, %s, 'agent_generated') RETURNING id",
                (ids["Alice"], ids["Boston"], TENANT, first["new_id"]),
            )
            cur.execute(
                "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) "
                "VALUES (%s, %s, %s)",
                (TENANT, cur.fetchone()[0], first["new_id"]),
            )
            conn.commit()
        out = await _write(
            "Alice moved to Denver.",
            names=["Alice", "Denver"],
            relations=[{"source": "Alice", "relation": "moved to", "target": "Denver"}],
            origin="agent_generated",
        )
        assert len(verdict.calls) == 1
        assert out["action"] == "superseded"  # the judge (mocked) said changed


class TestConcurrency:
    async def test_two_different_values_at_once_leave_one_current(self, tenants):
        import asyncio

        await _write("Alice lives in Boston.")
        await asyncio.gather(
            _write("Alice lives in Denver.", names=["Alice", "Denver"]),
            _write("Alice lives in Chicago.", names=["Alice", "Chicago"]),
        )
        assert len(_active("Alice lives")) == 1

    async def test_the_same_value_twice_at_once_is_one_fact_listed_once(self, tenants):
        import asyncio

        first = await _write("Alice lives in Boston.")
        await asyncio.gather(
            _write("Alice lives in Denver.", names=["Alice", "Denver"]),
            _write("Alice lives in Denver.", names=["Alice", "Denver"]),
        )
        current = _active("Denver")
        assert len(current) == 1
        assert _rows()[current[0]]["metadata"]["supersedes"] == [first["new_id"]]


class TestRecallContract:
    """Earlier values are extra rows after selection: at most
    ``bitemporal.MAX_EARLIER_ROWS``, never in place of a ranked row, and only
    ever read through the reader's own filters."""

    async def _search(self, monkeypatch, vector_of: int, query: str, **kw: Any):
        import robothor.memory.facts as facts

        async def _embed(_q: str) -> list[float]:
            return _vec(vector_of)

        monkeypatch.setattr(facts, "_embed_query", _embed)
        return await facts.search_facts(
            query, use_reranker=False, include_segments=False, tenant_id=TENANT, **kw
        )

    async def _six_moves(self) -> None:
        for i in range(6):
            await _write(f"Person{i} lives in Boston.", names=[f"Person{i}"], embedding=_vec(600))
            await _write(
                f"Person{i} moved to Denver.", names=[f"Person{i}"], embedding=_vec(610 + i)
            )

    async def test_the_limit_bounds_the_ranked_rows_and_earlier_rows_are_bounded_extras(
        self, tenants, monkeypatch
    ):
        from robothor.memory.bitemporal import MAX_EARLIER_ROWS

        await self._six_moves()
        for limit in (1, 3, 6):
            rows = await self._search(monkeypatch, 610, "Denver moved", limit=limit)
            ranked = [r for r in rows if r.get("earlier_of") is None]
            assert len(ranked) <= limit
            assert len(rows) - len(ranked) <= MAX_EARLIER_ROWS
            assert not ranked[0].get("superseded")

    async def test_history_recall_keeps_the_same_contract(self, tenants, monkeypatch):
        from robothor.memory.bitemporal import MAX_EARLIER_ROWS

        await self._six_moves()
        rows = await self._search(monkeypatch, 600, "Boston", limit=3, active_only=False)
        ranked = [r for r in rows if r.get("earlier_of") is None]
        assert len(ranked) <= 3
        assert len(rows) <= 3 + MAX_EARLIER_ROWS
        for i, r in enumerate(rows):
            if r.get("earlier_of") is not None:
                assert rows[i - 1]["id"] == r["earlier_of"]

    async def test_a_private_change_is_read_by_its_reader_only(self, tenants, monkeypatch):
        a = "person:11111111-1111-1111-1111-111111111111"
        b = "person:22222222-2222-2222-2222-222222222222"
        old = await _write("Alice lives in Boston.", scope_kind="user", scope_id=a)
        new = await _write("Alice moved to Denver.", scope_kind="user", scope_id=a,
                           embedding=_vec(640))  # fmt: skip
        assert new["action"] == "superseded"
        ids_a = [r["id"] for r in await self._search(monkeypatch, 640, "Alice", reader_scope_id=a)]
        assert ids_a.index(new["new_id"]) + 1 == ids_a.index(old["new_id"])
        for reader in (b, None):
            rows = await self._search(monkeypatch, 640, "Alice", reader_scope_id=reader)
            assert {old["new_id"], new["new_id"]}.isdisjoint(r["id"] for r in rows)
        history = await self._search(
            monkeypatch, 640, "Alice", reader_scope_id=b, active_only=False
        )
        assert {old["new_id"], new["new_id"]}.isdisjoint(r["id"] for r in history)

    async def test_a_forgotten_earlier_value_is_not_shown(self, tenants, monkeypatch):
        from robothor.db.connection import get_connection

        old = await _write("Alice lives in Boston.")
        new = await _write("Alice moved to Denver.", embedding=_vec(650))
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE memory_facts SET metadata = metadata || "
                "jsonb_build_object('forgotten', jsonb_build_object('at', now())) WHERE id = %s",
                (old["new_id"],),
            )
            conn.commit()
        rows = await self._search(monkeypatch, 650, "Alice")
        assert new["new_id"] in [r["id"] for r in rows]
        assert old["new_id"] not in [r["id"] for r in rows]


class TestManyWriters:
    async def test_five_values_at_once_end_with_one_current_or_recorded_conflicts(self, tenants):
        import asyncio

        await _write("Alice lives in Boston.")
        await asyncio.gather(
            *[_write(f"Alice lives in Town{c}.", names=["Alice"]) for c in "ABCDE"]
        )
        active = _active("Alice lives")
        if len(active) > 1:
            # Every value left current beside another is in a recorded open
            # conflict, as either side, for the next write to resolve.
            unresolved = set()
            for d in _decisions():
                if d["action"] == "conflict_unresolved":
                    unresolved |= {d["new_fact_id"], d["existing_fact_id"]}
            assert set(active) <= unresolved, (active, unresolved)
        assert len(active) >= 1

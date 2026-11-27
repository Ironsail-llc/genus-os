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
                        "event_time": None,
                        "mentions": [
                            {"surface_form": n, "entity_type": "person"}
                            for n in kw.pop("names", ["Alice"])
                        ],
                        "relations": [],
                    }
                ]
            }
        )
    )
    assert parsed, text
    return await resolve_and_store(
        parsed[0], "src", "conversation", tenant_id=tenant, embedding=_vec(next(_counter)), **kw
    )


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
        first = await _write("Alice lives in Boston.")
        before = len(_rows())
        out = await _write("Alice now lives in Boston.")
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
        slot = fact_slot({"fact_text": "Alice lives in Denver."})
        _ids, rows = await find_slot_candidates(slot, tenant_id=TENANT, limit=10)
        assert len(rows) == 10


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

"""A changed value is a change, not a repeat.

Two facts fill the same slot when they share a subject and an attribute. A
repeat needs the same value; a different value for a single-valued attribute
(where someone lives, who they work for, which of two things they prefer) is a
change; a different value for a multi-valued one (likes, owns, has visited)
is one more value. Similarity alone cannot tell these apart: "Bob prefers tea
over coffee" and "Bob prefers coffee over tea" embed at about 0.975 cosine.
"""

from __future__ import annotations

import pytest

from robothor.memory.conflicts import compare_slots, fact_slot, values_differ


def _slot(text: str, **kw):
    return fact_slot({"fact_text": text, **kw})


class TestFactSlot:
    @pytest.mark.parametrize(
        ("text", "subject", "attribute", "value"),
        [
            ("Alice lives in Boston.", "alice", "residence", "boston"),
            ("Alice moved to Denver last year.", "alice", "residence", "denver"),
            ("Alice works at Example Co as a designer.", "alice", "employer", "example co"),
            ("Alice's favorite color is blue.", "alice", "favorite:color", "blue"),
            ("Bob's manager is Carol.", "bob", "manager", "carol"),
            ("Alice has visited Paris.", "alice", "visited", "paris"),
            ("Bob likes jazz.", "bob", "likes", "jazz"),
        ],
    )
    def test_a_slot_is_subject_attribute_value(self, text, subject, attribute, value):
        slot = _slot(text)
        assert slot is not None
        assert f"name:{subject}" in slot.subject_keys
        assert slot.attribute == attribute
        assert slot.value == value

    def test_a_pairwise_preference_is_one_slot_whichever_way_round(self):
        a = _slot("Bob prefers tea over coffee.")
        b = _slot("Bob prefers coffee over tea.")
        assert a is not None and b is not None
        assert a.attribute == b.attribute
        assert (a.value, b.value) == ("tea", "coffee")

    def test_a_canonical_predicate_gives_the_slot(self):
        slot = _slot(
            "Alice is now settled in Denver.",
            relations=[{"source": "Alice", "relation": "lives in", "target": "Denver"}],
        )
        assert slot is not None
        assert (slot.attribute, slot.value) == ("residence", "denver")
        assert "name:alice" in slot.subject_keys

    def test_no_frame_no_slot(self):
        assert _slot("The Helios launch went well.") is None
        assert _slot("") is None

    def test_single_several_and_multi_valued(self):
        # Single: one value at a time. Several: people often have more than one
        # (employers, phones, emails, addresses), so only the classifier may
        # call a new one a replacement. Multi: a new value never replaces.
        for text in (
            "Alice lives in Boston.",
            "Bob prefers tea over coffee.",
            "Bob's manager is Carol.",
        ):
            assert _slot(text).cardinality == "single", text
        for text in (
            "Alice works for Acme Corp.",
            "Alice's phone number is 555-0199.",
            "Alice's email is alice@example.com.",
            "Alice's address is 1 Main St.",
        ):
            assert _slot(text).cardinality == "several", text
        for text in ("Bob likes jazz.", "Alice owns a bike.", "Alice has visited Rome."):
            assert _slot(text).cardinality == "multi", text

    def test_a_mention_resolved_to_an_entity_keys_the_subject(self):
        slot = _slot(
            "Ally lives in Boston.",
            mention_entities=[{"surface_form": "Ally", "entity_id": 12}],
        )
        assert slot is not None
        assert {"name:ally", "entity:12"} <= slot.subject_keys


class TestCompareSlots:
    def test_a_reversed_preference_is_a_change(self):
        assert (
            compare_slots(
                _slot("Bob prefers coffee over tea."), _slot("Bob prefers tea over coffee.")
            )
            == "change"
        )

    def test_a_move_is_a_change_however_low_the_similarity(self):
        assert compare_slots(_slot("Alice moved to Denver."), _slot("Alice lives in Boston.")) == (
            "change"
        )

    def test_the_same_value_is_a_repeat(self):
        assert compare_slots(
            _slot("Alice lives in Boston."), _slot("Alice now lives in Boston.")
        ) == ("repeat")
        # More detail about the same value is still the same value.
        assert (
            compare_slots(
                _slot("Alice lives in Boston, Massachusetts."), _slot("Alice lives in Boston.")
            )
            == "repeat"
        )

    def test_a_multi_valued_attribute_gains_a_value(self):
        assert compare_slots(
            _slot("Alice has visited Rome."), _slot("Alice has visited Paris.")
        ) == ("coexist")
        assert compare_slots(_slot("Bob likes hiking."), _slot("Bob likes jazz.")) == "coexist"

    def test_another_subject_is_unrelated(self):
        assert compare_slots(_slot("Bob lives in Denver."), _slot("Alice lives in Boston.")) == (
            "unrelated"
        )

    def test_another_attribute_is_unrelated(self):
        assert compare_slots(
            _slot("Alice works at Acme Corp."), _slot("Alice lives in Boston.")
        ) == ("unrelated")

    def test_a_shared_entity_is_the_same_subject_under_another_name(self):
        new = _slot("Alice moved to Denver.").with_entity_ids({12})
        old = _slot(
            "Ally lives in Boston.",
            mention_entities=[{"surface_form": "Ally", "entity_id": 12}],
        )
        assert compare_slots(new, old) == "change"

    def test_an_entity_resolved_subject_needs_the_same_entity(self):
        # Two people named Alice in one scope never share a slot.
        old = _slot(
            "Alice lives in Boston.",
            mention_entities=[{"surface_form": "Alice", "entity_id": 12}],
        )
        assert compare_slots(_slot("Alice moved to Denver.").with_entity_ids({13}), old) == (
            "unrelated"
        )
        # Nor does an unresolved one borrow a resolved fact's slot by name.
        assert compare_slots(_slot("Alice moved to Denver."), old) == "unrelated"
        assert compare_slots(_slot("Alice moved to Denver.").with_entity_ids({12}), old) == (
            "change"
        )

    def test_a_missing_slot_is_unrelated(self):
        assert compare_slots(None, _slot("Alice lives in Boston.")) == "unrelated"
        assert compare_slots(_slot("Alice lives in Boston."), None) == "unrelated"


class TestNoSlotForUnsafeSentences:
    """A frame match may only ever nominate a pair, and these must not even do
    that: hedged, negated, conditional, quoted, plural or questioned sentences,
    pronoun or generic subjects, and values that are not of the attribute's
    type. Each of these closed a true fact in the verifier's run."""

    @pytest.mark.parametrize(
        "text",
        [
            "Alice also works at Globex.",
            "Alice also lives in Denver.",
            "Alice's email is also alice@work.example.",
            "Alice no longer lives in Boston.",
            "Alice never lives in Denver.",
            "Alice's manager is not Carol.",
            "Alice moved to Denver last year but is back in Boston.",
            "Alice lives in Denver if the deal closes.",
            "If Alice moved to Denver she would save money.",
            "Alice might move to Denver.",
            "Alice lives in Denver?",
            "Alice used to work at Globex.",
            "Bob said Alice lives in Denver.",
            'Alice said "I live in Denver".',
            "Alice moved to Denver temporarily for a project.",
            "She lives in Denver.",
            "He works at Globex.",
            "The user lives in Denver.",
            "The company is based in Denver.",
        ],
    )
    def test_a_hedged_or_unattributed_sentence_has_no_slot(self, text):
        assert _slot(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "Alice moved to the sales team.",
            "Alice moved in with Bob.",
            "Alice lives in a house with two dogs.",
            "Alice lives at 12 Main Street.",
            "Alice lives in fear of layoffs.",
            "Alice is based in the engineering org.",
            "Alice recently moved to a new apartment.",
            "Alice works at night.",
            "Alice works at home on Fridays.",
            "Alice works for free on weekends.",
            "Alice works at the front desk.",
            "Alice drives to work every day.",
            "Alice drives the product roadmap.",
            "Alice is married to her job.",
            "Alice's manager is unhappy with the Q3 numbers.",
            "Alice's manager is on vacation.",
            "Alice's phone is broken.",
            "Alice's address is unknown.",
            "Alice's age is a sensitive topic.",
            "Alice's role is to approve budgets.",
            "Alice's birthday is next week.",
        ],
    )
    def test_a_value_of_the_wrong_type_has_no_slot(self, text):
        assert _slot(text) is None

    @pytest.mark.parametrize(
        ("text", "attribute", "value"),
        [
            ("Alice moved to Denver in 2019.", "residence", "denver"),
            ("Alice has moved from Boston to Denver.", "residence", "denver"),
            ("Alice lives in New York.", "residence", "new york"),
            ("Alice works at Acme Labs.", "employer", "acme labs"),
            ("Alice's phone number is 555-0199.", "phone", "555-0199"),
            ("Alice's email is alice@work.example.", "email", "alice@work.example"),
            ("Alice's birthday is March 3.", "birthday", "march 3"),
            ("Alice's age is 30.", "age", "30"),
            ("Alice's timezone is EST.", "timezone", "est"),
            ("Alice's address is 1 Main St.", "address", "1 main st"),
            ("Alice's title is CTO.", "title", "cto"),
            ("Alice drives a Tesla.", "car", "tesla"),
        ],
    )
    def test_a_plain_statement_of_a_typed_value_still_has_a_slot(self, text, attribute, value):
        slot = _slot(text)
        assert slot is not None, text
        assert (slot.attribute, slot.value) == (attribute, value)


class TestValuesDiffer:
    """The reinforce shortcut's guard: a near-identical text is a repeat only
    when it carries the same values."""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Bob prefers tea over coffee.", "Bob prefers coffee over tea."),
            ("Alice lives in Boston.", "Bob lives in Boston."),
            ("Alice drives a Honda Civic.", "Alice drives a Toyota Corolla."),
            ("FakeVendorCo charges $100.", "FakeVendorCo charges $150."),
        ],
    )
    def test_a_different_value_differs(self, a, b):
        assert values_differ(a, b) is True

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("X happened", "X occurred"),
            ("Alice prefers dark mode.", "Alice prefers dark mode for coding."),
            ("The standup is at 9am.", "The standup is at 9am."),
        ],
    )
    def test_a_rewording_does_not(self, a, b):
        assert values_differ(a, b) is False


# ── Store time ──────────────────────────────────────────────────────────────

from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import robothor.memory.conflicts as cf  # noqa: E402


def _row(
    fid: int,
    text: str,
    *,
    similarity: float | None = None,
    origin: str | None = None,
    metadata: Any = None,
    category: str = "personal",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": fid,
        "fact_text": text,
        "category": category,
        "entities": [],
        "origin": origin,
        "metadata": metadata,
    }
    if similarity is not None:
        row["similarity"] = similarity
    return row


class _Store:
    """resolve_and_store with every collaborator mocked; records what it did."""

    def __init__(self, monkeypatch: Any, *, similar=(), slot_rows=(), entity_ids=()):
        self.similar = AsyncMock(return_value=list(similar))
        self.lookup = AsyncMock(return_value=(list(entity_ids), list(slot_rows)))
        self.store = AsyncMock(return_value=99)
        self.classify = AsyncMock(return_value={"classification": "new", "reasoning": "r"})
        self.supersede = MagicMock(return_value=True)
        self.overrule = MagicMock(return_value=True)
        self.record = MagicMock()
        self.reinforced: list[int] = []
        monkeypatch.setattr(cf, "find_similar_facts", self.similar)
        monkeypatch.setattr(cf, "find_slot_candidates", self.lookup, raising=False)
        monkeypatch.setattr(cf, "store_fact", self.store)
        monkeypatch.setattr(cf, "classify_relationship", self.classify)
        monkeypatch.setattr(cf, "supersede_with_validity", self.supersede)
        monkeypatch.setattr(cf, "keep_overruled", self.overrule)
        monkeypatch.setattr(cf, "record_conflict_decision", self.record)
        monkeypatch.setattr(cf, "_reinforce_fact", lambda fid, **_k: self.reinforced.append(fid))

    async def __call__(self, text: str, **kw: Any) -> dict[str, Any]:
        fact = {"fact_text": text, "category": "personal", "entities": [], "confidence": 0.9}
        kw.setdefault("tenant_id", "test-tenant")
        return await cf.resolve_and_store(fact, "src", "conversation", **kw)

    def actions(self) -> list[str]:
        return [c.kwargs["action"] for c in self.record.call_args_list]


class TestStoreTimeChange:
    @pytest.mark.asyncio
    async def test_a_reversed_preference_supersedes_instead_of_reinforcing(self, monkeypatch):
        old = _row(7, "Bob prefers tea over coffee.", similarity=0.975)
        s = _Store(monkeypatch, similar=[old])
        out = await s("Bob prefers coffee over tea.")
        assert out["action"] == "superseded"
        assert out["new_id"] == 99 and out["old_id"] == 7
        assert s.reinforced == []
        s.store.assert_awaited_once()
        assert s.supersede.call_args.args[:2] == (7, 99)
        assert s.supersede.call_args.kwargs["reason"] == "changed"
        s.classify.assert_not_awaited()  # the slot decided; no model call
        assert s.actions() == ["superseded"]

    @pytest.mark.asyncio
    async def test_a_move_below_the_similarity_threshold_is_still_found(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.")
        s = _Store(monkeypatch, similar=[], slot_rows=[old])
        out = await s("Alice moved to Denver.")
        assert out["action"] == "superseded"
        assert s.supersede.call_args.args[:2] == (7, 99)
        lookup_kw = s.lookup.call_args.kwargs
        assert lookup_kw["tenant_id"] == "test-tenant"

    @pytest.mark.asyncio
    async def test_every_active_value_of_the_slot_is_closed(self, monkeypatch):
        a = _row(7, "Alice lives in Boston.")
        b = _row(8, "Alice lives in Seattle.")
        s = _Store(monkeypatch, slot_rows=[a, b])
        out = await s("Alice moved to Denver.")
        assert out["action"] == "superseded"
        assert sorted(c.args[0] for c in s.supersede.call_args_list) == [7, 8]
        assert sorted(out["old_ids"]) == [7, 8]

    @pytest.mark.asyncio
    async def test_a_true_repeat_is_reinforced(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", similarity=0.95)
        s = _Store(monkeypatch, similar=[old], slot_rows=[_row(7, "Alice lives in Boston.")])
        out = await s("Alice now lives in Boston.")
        assert out["action"] == "reinforced"
        assert out["existing_id"] == 7
        assert s.reinforced == [7]
        s.store.assert_not_awaited()
        s.supersede.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_restatement_below_the_reinforce_threshold_is_not_swallowed(self, monkeypatch):
        # Same value, said differently (and perhaps by a more trusted source):
        # the slot does not reinforce it away; the similarity rules decide.
        old = _row(7, "Alice lives in Boston.", origin="external")
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice now lives in Boston.", origin="crm")
        assert out["action"] == "stored"
        assert s.reinforced == []
        s.supersede.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_multi_valued_attribute_keeps_both(self, monkeypatch):
        old = _row(7, "Alice has visited Paris.", similarity=0.95)
        s = _Store(monkeypatch, similar=[old], slot_rows=[old])
        out = await s("Alice has visited Rome.")
        assert out["action"] == "stored"
        assert out["new_id"] == 99
        assert s.reinforced == []
        s.supersede.assert_not_called()
        s.classify.assert_not_awaited()
        assert s.record.call_args.kwargs["classification"] == "new"

    @pytest.mark.asyncio
    async def test_a_fact_with_no_slot_costs_no_lookup(self, monkeypatch):
        s = _Store(monkeypatch)
        out = await s("The Helios launch went well.")
        assert out["action"] == "stored"
        s.lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_lookup_reads_the_facts_own_scope(self, monkeypatch):
        s = _Store(monkeypatch)
        await s("Alice moved to Denver.", scope_kind="user", scope_id="person:p-1")
        kw = s.lookup.call_args.kwargs
        assert (kw["tenant_id"], kw["scope_kind"], kw["scope_id"]) == (
            "test-tenant",
            "user",
            "person:p-1",
        )

    @pytest.mark.asyncio
    async def test_a_failed_lookup_falls_back_to_similarity(self, monkeypatch):
        s = _Store(monkeypatch)
        s.lookup.side_effect = RuntimeError("db down")
        out = await s("Alice moved to Denver.")
        assert out["action"] == "stored"


class TestAuthority:
    @pytest.mark.asyncio
    async def test_a_lower_authority_value_does_not_replace_a_user_stated_one(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="user_stated")
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", origin="agent_generated")
        assert out["action"] == "kept_authority"
        assert out["new_id"] == 99 and out["existing_id"] == 7
        s.supersede.assert_not_called()
        assert s.overrule.call_args.args[:2] == (99, 7)  # stored, overruled, not deleted
        assert s.actions() == ["kept_authority"]

    @pytest.mark.asyncio
    async def test_external_does_not_replace_an_agent_value(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin=None)  # legacy: agent-generated
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", origin="external")
        assert out["action"] == "kept_authority"
        s.supersede.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_operator_confirmed_value_is_kept(self, monkeypatch):
        confirmed = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "operator"}}
        old = _row(7, "Alice lives in Boston.", origin="agent_generated", metadata=confirmed)
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", origin="user_stated")
        assert out["action"] == "kept_confirmed"
        s.supersede.assert_not_called()
        assert s.overrule.call_args.args[:2] == (99, 7)

    @pytest.mark.asyncio
    async def test_a_user_statement_replaces_an_agent_value(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="agent_generated")
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", origin="user_stated")
        assert out["action"] == "superseded"

    @pytest.mark.asyncio
    async def test_the_classifier_path_respects_authority_too(self, monkeypatch):
        old = _row(7, "Alice leads the Helios project.", similarity=0.8, origin="user_stated")
        s = _Store(monkeypatch, similar=[old])
        s.classify.return_value = {"classification": "update", "reasoning": "r"}
        out = await s("Bob leads the Helios project.", origin="agent_generated")
        assert out["action"] == "kept_authority"
        s.supersede.assert_not_called()
        assert s.overrule.call_args.args[:2] == (99, 7)


class TestReinforceGuard:
    @pytest.mark.asyncio
    async def test_a_near_identical_text_with_another_name_is_not_reinforced(self, monkeypatch):
        # No frame: the similarity path decides, and it must not treat a
        # different name as the same fact said again.
        old = _row(7, "Alice leads the Helios project.", similarity=0.95)
        s = _Store(monkeypatch, similar=[old])
        out = await s("Bob leads the Helios project.")
        assert out["action"] != "reinforced"
        s.classify.assert_awaited_once()


# ── Read time ───────────────────────────────────────────────────────────────

from datetime import UTC, datetime  # noqa: E402

from robothor.memory.bitemporal import place_superseded  # noqa: E402

_WHEN = datetime(2026, 5, 1, tzinfo=UTC)


def _cur(fid: int, text: str, **kw: Any) -> dict[str, Any]:
    return {"id": fid, "kind": "fact", "fact_text": text, "superseded_by": None, **kw}


def _old(fid: int, text: str, by: int) -> dict[str, Any]:
    return {
        "id": fid,
        "kind": "fact",
        "fact_text": text,
        "superseded_by": by,
        "valid_to": _WHEN,
        "metadata": {"superseded_reason": "changed"},
    }


class TestPlaceSuperseded:
    def test_the_earlier_value_follows_the_current_one_labelled(self):
        rows = [_cur(2, "Alice moved to Denver.", relevance=0.9), _cur(5, "Other fact.")]
        out = place_superseded(rows, earlier={2: [_old(1, "Alice lives in Boston.", 2)]})
        assert [r["id"] for r in out] == [2, 1, 5]
        old = out[1]
        assert old["superseded"] is True
        assert old["superseded_at"] == _WHEN.isoformat()
        assert old["superseded_reason"] == "changed"
        assert old["superseded_by"] == 2
        assert old["relevance"] <= 0.9
        assert "superseded" not in out[0] or out[0]["superseded"] is False

    def test_a_superseded_row_never_outranks_its_current_row(self):
        rows = [_old(1, "Alice lives in Boston.", 2), _cur(5, "Other."), _cur(2, "Alice moved.")]
        out = place_superseded(rows)
        assert [r["id"] for r in out] == [5, 2, 1]

    def test_a_missing_current_row_is_brought_in_ahead_of_the_old_one(self):
        rows = [_old(1, "Alice lives in Boston.", 2), _cur(5, "Other.")]
        out = place_superseded(rows, current={1: _cur(2, "Alice moved to Denver.")})
        assert [r["id"] for r in out] == [2, 1, 5]
        assert out[1]["superseded"] is True

    def test_an_old_row_without_a_visible_current_is_still_labelled(self):
        out = place_superseded([_old(1, "Alice lives in Boston.", 2)])
        assert [r["id"] for r in out] == [1]
        assert out[0]["superseded"] is True

    def test_rows_without_supersession_are_untouched(self):
        rows = [_cur(1, "A."), _cur(2, "B.")]
        assert place_superseded(rows) == rows

    def test_an_earlier_row_already_present_is_not_repeated(self):
        rows = [_cur(2, "Alice moved."), _old(1, "Alice lives in Boston.", 2)]
        out = place_superseded(rows, earlier={2: [_old(1, "Alice lives in Boston.", 2)]})
        assert [r["id"] for r in out] == [2, 1]

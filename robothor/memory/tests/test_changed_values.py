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

from datetime import UTC, datetime  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import robothor.memory.conflicts as cf  # noqa: E402

_LEARNED = datetime(2026, 11, 20, tzinfo=UTC)


def _row(
    fid: int,
    text: str,
    *,
    similarity: float | None = None,
    origin: str | None = "agent_generated",
    metadata: Any = None,
    category: str = "personal",
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": fid,
        "fact_text": text,
        "category": category,
        "entities": [],
        "origin": origin,
        "metadata": metadata,
        "created_at": _LEARNED,
        **extra,
    }
    if similarity is not None:
        row["similarity"] = similarity
    return row


_CONFIRMED = {"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "operator"}}


class _Store:
    """resolve_and_store with every collaborator mocked; records what it did.

    ``store_fact`` runs the insert's ``after_insert`` hook on a fake cursor, as
    the real one does inside its transaction, and returns ``new_id``. The slot
    judge answers ``outcome``; the similarity path's classifier ``verdict``.
    """

    def __init__(
        self,
        monkeypatch: Any,
        *,
        similar=(),
        slot_rows=(),
        subject_id=None,
        value_id=None,
        outcome="changed",
        verdict="update",
        new_id=99,
        newcomers=(),
    ):
        self.cur = MagicMock()
        self.similar = AsyncMock(return_value=list(similar))
        self.lookup = AsyncMock(
            return_value=cf.SlotLookup(subject_id, value_id, [dict(r) for r in slot_rows])
        )

        async def _store(*_a: Any, after_insert: Any = None, **_k: Any) -> int:
            if after_insert is not None:
                after_insert(self.cur, new_id)
            return new_id

        self.store = AsyncMock(side_effect=_store)
        self.classify = AsyncMock(return_value={"classification": verdict, "reasoning": "r"})
        self.judge = AsyncMock(return_value={"outcome": outcome, "reasoning": "r"})
        self.supersede = MagicMock(return_value=True)
        self.overrule = MagicMock(return_value=True)
        self.earlier = MagicMock(return_value=True)
        self.record = MagicMock()
        self.recheck = MagicMock(side_effect=list(newcomers) or None, return_value=[])
        self.overruled_before = MagicMock(return_value=0)
        self.reinforced: list[int] = []
        monkeypatch.setattr(cf, "find_similar_facts", self.similar)
        monkeypatch.setattr(cf, "find_slot_candidates", self.lookup)
        monkeypatch.setattr(cf, "store_fact", self.store)
        monkeypatch.setattr(cf, "classify_relationship", self.classify)
        monkeypatch.setattr(cf, "judge_slot_change", self.judge, raising=False)
        monkeypatch.setattr(cf, "supersede_with_validity", self.supersede)
        monkeypatch.setattr(cf, "keep_overruled", self.overrule)
        monkeypatch.setattr(cf, "close_on_arrival", self.earlier)
        monkeypatch.setattr(cf, "record_conflict_decision", self.record)
        monkeypatch.setattr(cf, "_slot_newcomers", self.recheck)
        monkeypatch.setattr(cf, "_find_overruled", self.overruled_before)
        monkeypatch.setattr(cf, "_reinforce_fact", lambda fid, **_k: self.reinforced.append(fid))

    async def __call__(self, text: str, **kw: Any) -> dict[str, Any]:
        fact = {
            "fact_text": text,
            "category": "personal",
            "entities": kw.pop("entities", []),
            "confidence": 0.9,
            "relations": kw.pop("relations", []),
        }
        kw.setdefault("tenant_id", "test-tenant")
        kw.setdefault("origin", "agent_generated")
        return await cf.resolve_and_store(fact, "src", "conversation", **kw)

    def actions(self) -> list[str]:
        return [c.kwargs["action"] for c in self.record.call_args_list]

    def closed(self) -> list[tuple[int, int]]:
        return [c.args[:2] for c in self.supersede.call_args_list]


_LIVES_IN = [{"source": "Alice", "relation": "lives in", "target": "Denver"}]


def _resolved_old(fid: int = 7, *, predicate: str = "lives_in", **kw: Any) -> dict[str, Any]:
    """A stored 'Alice lives in Boston' whose predicate, subject and value resolved."""
    return _row(
        fid,
        kw.pop("text", "Alice lives in Boston."),
        mention_entities=[{"surface_form": "Alice", "entity_id": 5}],
        relations=[{"predicate": predicate, "source_entity_id": 5, "target_entity_id": 8}],
        **kw,
    )


class TestTheDecisionTable:
    """``slot_action``: what one judged pair does, given the judge's outcome,
    the attribute's cardinality, the new text's wording and the time order."""

    @pytest.mark.parametrize(
        ("outcome", "cardinality", "wording", "order", "action"),
        [
            ("changed", "single", False, "later", "close"),
            ("changed", "single", False, "earlier", "unresolved"),
            ("earlier", "single", False, "earlier", "history"),
            ("earlier", "single", False, "later", "unresolved"),
            ("same", "single", False, "later", "skip"),
            ("coexist", "single", False, "later", "keep"),
            ("unrelated", "single", False, "later", "keep"),
            ("conflict", "single", False, "later", "unresolved"),
            (None, "single", False, "later", "unresolved"),
            ("changed", "several", False, "later", "keep"),
            ("changed", "several", True, "later", "close"),
        ],
    )
    def test_each_outcome(self, outcome, cardinality, wording, order, action):
        assert cf.slot_action(outcome, cardinality, replacement_wording=wording, order=order) == (
            action
        )

    @pytest.mark.parametrize(
        ("text", "wording"),
        [
            ("Alice's email is now alice@new.example.", True),
            ("Alice switched to alice@new.example.", True),
            ("Alice's email is alice@work.example.", False),
        ],
    )
    def test_replacement_wording(self, text, wording):
        assert cf.replacement_wording(text) is wording


class TestOnlyTheJudgeCloses:
    @pytest.mark.asyncio
    async def test_a_reversed_preference_goes_to_the_judge(self, monkeypatch):
        old = _row(7, "Bob prefers tea over coffee.", similarity=0.975)
        s = _Store(monkeypatch, similar=[old])
        out = await s("Bob prefers coffee over tea.")
        s.judge.assert_awaited_once()
        assert out["action"] == "superseded"
        assert s.closed() == [(7, 99)]
        assert s.supersede.call_args.kwargs["reason"] == "changed"

    @pytest.mark.asyncio
    async def test_the_judge_is_told_the_slot_the_dates_and_the_sources(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="user_stated", event_time="2026-01-01")
        s = _Store(monkeypatch, slot_rows=[old])
        await s("Alice moved to Denver.", origin="user_stated", event_time="2026-06-01")
        kw = s.judge.call_args.kwargs
        assert kw["attribute"] == "residence" and kw["several"] is False
        assert (kw["new_origin"], kw["old_origin"]) == ("user_stated", "user_stated")
        assert kw["new_event"] == "2026-06-01"
        assert str(kw["old_event"]).startswith("2026-01-01")
        assert kw["old_recorded"] is not None

    @pytest.mark.parametrize(
        ("outcome", "action", "closed"),
        [
            ("coexist", "stored", False),
            ("unrelated", "stored", False),
            ("same", "skipped", False),
            ("conflict", "stored", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_only_changed_closes(self, monkeypatch, outcome, action, closed):
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")], outcome=outcome)
        out = await s("Alice lives in Massachusetts.")
        assert out["action"] == action
        assert bool(s.closed()) is closed

    @pytest.mark.asyncio
    async def test_a_conflict_or_no_answer_keeps_both_and_is_recorded(self, monkeypatch):
        for outcome in ("conflict", None):
            s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")], outcome=outcome)
            if outcome is None:
                s.judge.return_value = {"outcome": None, "unavailable": True, "reasoning": "down"}
            await s("Alice moved to Denver.")
            assert s.closed() == []
            assert s.actions() == ["conflict_unresolved"]

    @pytest.mark.asyncio
    async def test_a_judge_timeout_keeps_both(self, monkeypatch):
        import asyncio

        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")])

        async def _slow(*_a: Any, **_k: Any) -> dict[str, Any]:
            await asyncio.sleep(1)
            return {"outcome": "changed", "reasoning": "r"}

        s.judge.side_effect = _slow
        out = await s("Alice moved to Denver.", classify_timeout=0.01)
        assert out["action"] == "stored"
        assert s.closed() == []
        assert s.actions() == ["conflict_unresolved"]

    @pytest.mark.asyncio
    async def test_a_second_email_is_kept_unless_the_text_says_it_replaced_one(self, monkeypatch):
        old = _row(7, "Alice's email is alice@example.com.")
        s = _Store(monkeypatch, slot_rows=[old])
        assert (await s("Alice's email is alice@work.example."))["action"] == "stored"
        assert s.closed() == []
        s = _Store(monkeypatch, slot_rows=[old])
        assert (await s("Alice's email is now alice@new.example."))["action"] == "superseded"

    @pytest.mark.asyncio
    async def test_every_value_the_judge_replaces_is_closed(self, monkeypatch):
        a = _row(7, "Alice lives in Boston.")
        b = _row(8, "Alice lives in Seattle.")
        s = _Store(monkeypatch, slot_rows=[a, b])
        out = await s("Alice moved to Denver.")
        assert sorted(c[0] for c in s.closed()) == [7, 8]
        assert sorted(out["old_ids"]) == [7, 8]


class TestNoModelFreeClose:
    """Resolved entities may only ever say 'same value'; every different value
    goes to the judge. Each case closed a true fact without a model before."""

    @pytest.mark.parametrize(
        ("old_pred", "new_rel", "value_id"),
        [
            ("lives_in", "lives in", 9),  # Boston -> Massachusetts / America (containment)
            ("lives_in", "based in", 9),  # home vs work base
            ("lives_in", "lives in", 10),  # NYC vs New York City, two nodes
            ("reports_to", "reports to", 9),  # a second (dotted-line) manager
            ("drives", "drives", 9),  # a second car
        ],
    )
    @pytest.mark.asyncio
    async def test_a_different_resolved_value_still_goes_to_the_judge(
        self, monkeypatch, old_pred, new_rel, value_id
    ):
        s = _Store(
            monkeypatch,
            slot_rows=[_resolved_old(predicate=old_pred)],
            subject_id=5,
            value_id=value_id,
            outcome="coexist",
        )
        await s(
            "Alice moved to Denver.",
            relations=[{"source": "Alice", "relation": new_rel, "target": "Somewhere"}],
        )
        assert s.closed() == []

    @pytest.mark.asyncio
    async def test_the_same_resolved_value_is_a_repeat_without_a_model(self, monkeypatch):
        old = _resolved_old(similarity=0.95)
        s = _Store(monkeypatch, similar=[old], slot_rows=[old], subject_id=5, value_id=8)
        out = await s(
            "Alice lives in Beantown.",
            relations=[{"source": "Alice", "relation": "lives in", "target": "Beantown"}],
        )
        s.judge.assert_not_awaited()
        assert out["action"] == "reinforced"


class TestEventTime:
    @pytest.mark.asyncio
    async def test_an_undated_current_value_is_not_closed_by_an_old_dated_one(self, monkeypatch):
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")])
        out = await s("Alice moved to Denver in 2019.", event_time="2019-06-01")
        assert s.closed() == []
        assert out["action"] == "stored"
        assert s.actions() == ["conflict_unresolved"]

    @pytest.mark.asyncio
    async def test_a_bad_future_date_on_the_old_value_is_ignored(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", event_time="2031-01-01")
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", event_time="2026-11-25")
        assert out["action"] == "superseded"
        s.earlier.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_older_event_the_judge_calls_earlier_becomes_history(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", event_time="2026-11-01")
        s = _Store(monkeypatch, slot_rows=[old], outcome="earlier")
        out = await s("Alice moved to Denver in 2019.", event_time="2019-06-01")
        assert out["action"] == "stored_history"
        assert s.earlier.call_args.args[:2] == (99, 7)
        assert s.closed() == []

    @pytest.mark.asyncio
    async def test_dates_and_judge_disagreeing_keep_both(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", event_time="2026-11-01")
        s = _Store(monkeypatch, slot_rows=[old], outcome="changed")
        out = await s("Alice moved to Denver in 2019.", event_time="2019-06-01")
        assert out["action"] == "stored"
        assert s.closed() == [] and not s.earlier.called
        assert s.actions() == ["conflict_unresolved"]


class TestStoreTimeOther:
    @pytest.mark.asyncio
    async def test_a_true_repeat_is_reinforced(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", similarity=0.95)
        s = _Store(monkeypatch, similar=[old], slot_rows=[_row(7, "Alice lives in Boston.")])
        out = await s("Alice now lives in Boston.")
        assert out["action"] == "reinforced"
        assert s.reinforced == [7]
        s.store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_lower_authority_repeat_reinforces_nothing(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", similarity=0.95, origin="user_stated")
        s = _Store(monkeypatch, similar=[old], slot_rows=[_row(7, "Alice lives in Boston.",
                   origin="user_stated")])  # fmt: skip
        out = await s("Alice now lives in Boston.", origin="external")
        assert s.reinforced == []
        s.store.assert_not_awaited()
        assert out["action"] == "skipped"
        assert s.actions() == ["sighted"]

    @pytest.mark.asyncio
    async def test_a_lower_authority_repeat_on_the_similarity_path_reinforces_nothing(
        self, monkeypatch
    ):
        old = _row(7, "X happened at the launch.", similarity=0.97, origin="user_stated",
                   category="event")  # fmt: skip
        s = _Store(monkeypatch, similar=[old])
        fact = {"fact_text": "X happened at the launch!", "category": "event", "entities": [],
                "confidence": 1.0}  # fmt: skip
        out = await cf.resolve_and_store(fact, "src", "conversation", origin="external")
        assert s.reinforced == []
        assert out["action"] == "skipped"

    @pytest.mark.asyncio
    async def test_a_restatement_below_the_reinforce_threshold_is_not_swallowed(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="external")
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice now lives in Boston.", origin="crm")
        assert out["action"] == "stored"
        assert s.reinforced == []

    @pytest.mark.asyncio
    async def test_a_multi_valued_attribute_keeps_both_without_a_model(self, monkeypatch):
        old = _row(7, "Alice has visited Paris.", similarity=0.95)
        s = _Store(monkeypatch, similar=[old], slot_rows=[old])
        out = await s("Alice has visited Rome.")
        assert out["action"] == "stored"
        assert s.closed() == []
        s.judge.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_a_fact_never_closes_itself(self, monkeypatch):
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")], new_id=7)
        out = await s("Alice moved to Denver.")
        assert (7, 7) not in s.closed()
        assert out["action"] != "superseded"

    @pytest.mark.asyncio
    async def test_a_value_written_meanwhile_restarts_the_decision(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.")
        meanwhile = _row(8, "Alice lives in Chicago.")
        s = _Store(monkeypatch, slot_rows=[old], newcomers=[[meanwhile], []])
        await s("Alice moved to Denver.")
        assert s.lookup.await_count == 2

    @pytest.mark.asyncio
    async def test_restarts_never_ask_the_judge_twice_about_one_pair_nor_too_often(
        self, monkeypatch
    ):
        old = _row(7, "Alice lives in Boston.")
        rows = [_row(8 + i, f"Alice lives in Town{i}.") for i in range(6)]
        s = _Store(monkeypatch, slot_rows=[old], newcomers=[[r] for r in rows] + [[]])
        s.lookup.side_effect = [
            cf.SlotLookup(None, None, [old, *rows[:i]]) for i in range(len(rows) + 1)
        ]
        await s("Alice moved to Denver.")
        asked = [c.args[1] for c in s.judge.call_args_list]
        assert len(asked) == len(set(asked))
        assert s.judge.await_count <= cf.SLOT_CLASSIFY_TOTAL

    @pytest.mark.asyncio
    async def test_the_slot_lock_is_on_the_resolved_entity(self, monkeypatch):
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")], subject_id=5)
        await s("Alice moved to Denver.")
        assert s.recheck.call_args.kwargs["subject_key"] == "entity:5"
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")])
        await s("Alice moved to Denver.")
        assert s.recheck.call_args.kwargs["subject_key"] == "name:alice"


class TestDifferentSubjectsOnTheSimilarityPath:
    @pytest.mark.asyncio
    async def test_two_names_sharing_a_token_still_reach_the_classifier(self, monkeypatch):
        old = _row(7, "Alice Smith's budget is $100k.", similarity=0.8, entities=["Alice Smith"])
        s = _Store(monkeypatch, similar=[old])
        await s("Alice's budget is now $150k.", entities=["Alice"])
        s.classify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disjoint_names_skip_the_classifier(self, monkeypatch):
        old = _row(7, "She lives in Boston.", similarity=0.8, entities=["Carol"])
        s = _Store(monkeypatch, similar=[old])
        monkeypatch.setattr(cf, "_same_resolved_entity", lambda *a, **k: False)
        out = await s("She lives in Denver.", entities=["Dana"])
        s.classify.assert_not_awaited()
        assert out["action"] == "stored"

    @pytest.mark.asyncio
    async def test_disjoint_names_resolved_to_one_entity_reach_the_classifier(self, monkeypatch):
        old = _row(7, "Ally's budget is $100k.", similarity=0.8, entities=["Ally"])
        s = _Store(monkeypatch, similar=[old])
        monkeypatch.setattr(cf, "_same_resolved_entity", lambda *a, **k: True)
        await s("Alice's budget is now $150k.", entities=["Alice"])
        s.classify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_facts_without_entities_still_reach_the_classifier(self, monkeypatch):
        old = _row(7, "I live in Boston.", similarity=0.8)
        s = _Store(monkeypatch, similar=[old])
        await s("I moved to Denver.")
        s.classify.assert_awaited_once()


class TestAuthority:
    @pytest.mark.asyncio
    async def test_a_lower_authority_value_does_not_replace_a_user_stated_one(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="user_stated")
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", origin="agent_generated")
        assert out["action"] == "kept_authority"
        assert s.closed() == []
        assert s.overrule.call_args.args[:2] == (99, 7)

    @pytest.mark.asyncio
    async def test_a_lower_authority_source_cannot_plant_history_either(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="user_stated", event_time="2026-11-01")
        s = _Store(monkeypatch, slot_rows=[old], outcome="earlier")
        out = await s("Alice moved to Denver.", origin="external", event_time="2015-01-01")
        s.earlier.assert_not_called()
        assert out["action"] == "kept_authority"

    @pytest.mark.asyncio
    async def test_an_overruled_sentence_seen_again_reuses_its_row(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", origin="user_stated")
        s = _Store(monkeypatch, slot_rows=[old])
        s.overruled_before.return_value = 55
        out = await s("Alice moved to Denver.", origin="agent_generated")
        assert out["new_id"] == 55
        s.store.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin", ["tool", "agent_generated", "external", "crm"])
    async def test_a_legacy_fact_is_closed_only_by_a_user_or_operator(self, monkeypatch, origin):
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.", origin=None)])
        out = await s("Alice moved to Denver.", origin=origin)
        assert out["action"] == "kept_authority"
        assert s.closed() == []

    @pytest.mark.asyncio
    async def test_an_operator_confirmed_value_is_kept(self, monkeypatch):
        old = _row(7, "Alice lives in Boston.", metadata=_CONFIRMED)
        s = _Store(monkeypatch, slot_rows=[old])
        out = await s("Alice moved to Denver.", origin="user_stated")
        assert out["action"] == "kept_confirmed"
        assert s.closed() == []

    @pytest.mark.asyncio
    async def test_a_user_statement_replaces_an_agent_value(self, monkeypatch):
        s = _Store(monkeypatch, slot_rows=[_row(7, "Alice lives in Boston.")])
        out = await s("Alice moved to Denver.", origin="user_stated")
        assert out["action"] == "superseded"

    @pytest.mark.asyncio
    async def test_the_classifier_path_respects_authority_too(self, monkeypatch):
        old = _row(7, "Alice leads the Helios project.", similarity=0.8, origin="user_stated")
        s = _Store(monkeypatch, similar=[old])
        out = await s("Bob leads the Helios project.", origin="agent_generated")
        assert out["action"] == "kept_authority"
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

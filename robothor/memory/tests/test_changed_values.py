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

    def test_single_and_multi_valued(self):
        assert _slot("Alice lives in Boston.").single_valued is True
        assert _slot("Alice works for Acme Corp.").single_valued is True
        assert _slot("Bob prefers tea over coffee.").single_valued is True
        for text in ("Bob likes jazz.", "Alice owns a bike.", "Alice has visited Rome."):
            assert _slot(text).single_valued is False, text

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

    def test_a_missing_slot_is_unrelated(self):
        assert compare_slots(None, _slot("Alice lives in Boston.")) == "unrelated"
        assert compare_slots(_slot("Alice lives in Boston."), None) == "unrelated"


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

"""The identity resolution rules, as a pure function over fake candidate sets.

``decide`` is the whole rule table with no database: the integration tests in
``test_identity_integration.py`` prove that the database layer hands it the
right candidates. Here every rule, and every way it must refuse to merge, is
pinned on its own.

Precision first: whenever the rules cannot single out one entity on strong
evidence, the outcome is *unresolved* — never a guess.
"""

from __future__ import annotations

import pytest

from robothor.memory.contact_matching import first_name_variants, name_similarity
from robothor.memory.identity import (
    Candidate,
    MentionInput,
    StrongHit,
    decide,
    normalize_surface,
)


def _person(eid: int, name: str, match: str = "name", **kw) -> Candidate:
    return Candidate(entity_id=eid, name=name, entity_type="person", match=match, **kw)


def _org(eid: int, name: str, match: str = "name", **kw) -> Candidate:
    return Candidate(entity_id=eid, name=name, entity_type="organization", match=match, **kw)


# ─── normalisation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw", ["Dana Reyes", "dana reyes", "  DANA   Reyes ", "Dana\tReyes", "Dana Reyes"]
)
def test_case_and_whitespace_variants_normalise_alike(raw: str) -> None:
    assert normalize_surface(raw) == "dana reyes"


def test_the_nickname_table_gives_variants_both_ways() -> None:
    assert first_name_variants("bob") == frozenset({"bob", "rob", "robert"})
    assert first_name_variants("Robert") == frozenset({"bob", "rob", "robert"})
    assert first_name_variants("zelda") == frozenset({"zelda"})


# ─── (a) strong keys ─────────────────────────────────────────────────────


def test_a_strong_key_resolves_with_full_confidence() -> None:
    out = decide(
        MentionInput("Dana Reyes", "person", strong_keys={"email": "dana@example.com"}),
        [_person(1, "Dana Reyes")],
        strong_hits=[StrongHit("email", 1, "person", "Dana Reyes")],
    )
    assert (out.entity_id, out.method, out.confidence) == (1, "strong_key", 1.0)


def test_a_strong_key_beats_the_name() -> None:
    """The email belongs to Bob even though the name says Dana."""
    out = decide(
        MentionInput("Dana Reyes", "person", strong_keys={"email": "bob@example.com"}),
        [_person(1, "Dana Reyes")],
        strong_hits=[StrongHit("email", 2, "person", "Bob Rivera")],
    )
    assert (out.entity_id, out.method) == (2, "strong_key")


def test_strong_keys_that_disagree_leave_it_unresolved() -> None:
    out = decide(
        MentionInput("Dana Reyes", "person"),
        [_person(1, "Dana Reyes")],
        strong_hits=[
            StrongHit("email", 1, "person", "Dana Reyes"),
            StrongHit("crm_person_id", 2, "person", "Bob Rivera"),
        ],
    )
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "strong_key_conflict"
    assert sorted(out.evidence["candidates"]) == [1, 2]


def test_a_strong_key_of_the_wrong_type_does_not_resolve() -> None:
    out = decide(
        MentionInput("Northwind", "organization"),
        [],
        strong_hits=[StrongHit("crm_person_id", 5, "person", "Dana Reyes")],
    )
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "type_mismatch"


# ─── (b) exact full name ─────────────────────────────────────────────────


def test_a_unique_full_name_resolves_by_full_name() -> None:
    out = decide(MentionInput("Dana Reyes", "person"), [_person(1, "Dana Reyes")])
    assert (out.entity_id, out.method) == (1, "full_name")
    assert 0 < out.confidence < 1


def test_a_full_name_alias_counts_as_a_full_name() -> None:
    out = decide(MentionInput("Dana Reyes", "person"), [_person(1, "Dana Reyes #1a2b", "alias")])
    assert (out.entity_id, out.method) == (1, "full_name")


def test_an_exact_name_beats_a_nickname_variant() -> None:
    out = decide(
        MentionInput("Robert Rivera", "person"),
        [_person(1, "Robert Rivera"), _person(2, "Bob Rivera", "nickname")],
    )
    assert (out.entity_id, out.method) == (1, "full_name")


# ─── (c) ambiguity needs supporting context ──────────────────────────────


def test_two_people_with_one_full_name_and_no_context_stay_unresolved() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person"),
        [_person(1, "Alex Morgan"), _person(2, "Alex Morgan #9f9f", "alias")],
    )
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "ambiguous"
    assert sorted(out.evidence["candidates"]) == [1, 2]


def test_the_same_organisation_singles_out_one_of_them() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person", context={"organisation": "Northwind Traders"}),
        [
            _person(1, "Alex Morgan", organisations=frozenset({"northwind traders"})),
            _person(2, "Alex Morgan #9f9f", "alias", organisations=frozenset({"globex"})),
        ],
    )
    assert (out.entity_id, out.method) == (1, "context")
    assert out.confidence < 1
    assert out.evidence["signals"] == {"1": ["organisation"]}


def test_an_organisation_named_in_the_same_fact_is_context() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person", context={"co_mentions": ["Globex", "Friday"]}),
        [
            _person(1, "Alex Morgan", organisations=frozenset({"northwind traders"})),
            _person(2, "Alex Morgan #9f9f", "alias", organisations=frozenset({"globex"})),
        ],
    )
    assert (out.entity_id, out.method) == (2, "context")


def test_the_same_email_domain_singles_out_one_of_them() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person", context={"email_domain": "Globex.example"}),
        [
            _person(1, "Alex Morgan", email_domains=frozenset({"northwind.example"})),
            _person(2, "Alex Morgan #9f9f", "alias", email_domains=frozenset({"globex.example"})),
        ],
    )
    assert (out.entity_id, out.method) == (2, "context")


def test_a_free_mail_domain_is_not_context() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person", context={"email_domain": "gmail.com"}),
        [
            _person(1, "Alex Morgan", email_domains=frozenset({"gmail.com"})),
            _person(2, "Alex Morgan #9f9f", "alias", email_domains=frozenset({"globex.example"})),
        ],
    )
    assert out.entity_id is None


def test_context_that_fits_both_candidates_is_no_context() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person", context={"organisation": "Globex"}),
        [
            _person(1, "Alex Morgan", organisations=frozenset({"globex"})),
            _person(2, "Alex Morgan #9f9f", "alias", organisations=frozenset({"globex"})),
        ],
    )
    assert out.entity_id is None and out.reason == "ambiguous"


def test_a_nickname_variant_without_context_stays_unresolved_and_creates_nothing() -> None:
    out = decide(MentionInput("Bob Rivera", "person"), [_person(7, "Robert Rivera", "nickname")])
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "needs_context"
    assert out.evidence["candidates"] == [7]


def test_a_nickname_variant_with_context_resolves_by_context() -> None:
    out = decide(
        MentionInput("Bob Rivera", "person", context={"organisation": "Globex"}),
        [_person(7, "Robert Rivera", "nickname", organisations=frozenset({"globex"}))],
    )
    assert (out.entity_id, out.method) == (7, "context")
    assert out.confidence < 1


def test_the_same_conversation_singles_out_a_full_name() -> None:
    out = decide(
        MentionInput("Alex Morgan", "person", context={"conversation_ref": "s-1"}),
        [_person(1, "Alex Morgan", in_conversation=True), _person(2, "Alex Morgan #9f", "alias")],
    )
    assert (out.entity_id, out.method) == (1, "context")


# ─── (d) a single token never merges by itself ───────────────────────────


@pytest.mark.parametrize("surface", ["Alex", "alex", "A.", "AM"])
def test_a_single_token_never_merges_even_with_one_exact_candidate(surface: str) -> None:
    out = decide(MentionInput(surface, "person"), [_person(1, surface, "name")])
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "single_token"
    assert out.evidence["candidates"] == [1]


def test_two_people_sharing_a_first_name_are_not_merged() -> None:
    out = decide(
        MentionInput("Alex", "person"),
        [_person(1, "Alex Morgan", "first_name"), _person(2, "Alex Chen", "first_name")],
    )
    assert out.entity_id is None
    assert sorted(out.evidence["candidates"]) == [1, 2]


def test_a_nickname_shared_by_two_people_is_not_merged() -> None:
    """The fuzzy matcher scores "Sam" at 0.8 against both — above its threshold."""
    assert name_similarity("Sam", "Samantha Lee") >= 0.75
    assert name_similarity("Sam", "Samantha Cole") >= 0.75
    out = decide(
        MentionInput("Sam", "person"),
        [_person(1, "Samantha Lee", "nickname"), _person(2, "Samantha Cole", "nickname")],
    )
    assert out.entity_id is None


def test_a_single_token_resolves_only_when_one_candidate_is_in_the_conversation() -> None:
    out = decide(
        MentionInput("Alex", "person", context={"conversation_ref": "s-1"}),
        [
            _person(1, "Alex Morgan", "first_name", in_conversation=True),
            _person(2, "Alex Chen", "first_name"),
        ],
    )
    assert (out.entity_id, out.method) == (1, "context")
    assert out.confidence < 1


def test_a_single_token_with_two_candidates_in_the_conversation_stays_unresolved() -> None:
    out = decide(
        MentionInput("Alex", "person", context={"conversation_ref": "s-1"}),
        [
            _person(1, "Alex Morgan", "first_name", in_conversation=True),
            _person(2, "Alex Chen", "first_name", in_conversation=True),
        ],
    )
    assert out.entity_id is None


def test_organisation_context_does_not_resolve_a_single_token() -> None:
    out = decide(
        MentionInput("Alex", "person", context={"organisation": "Globex"}),
        [_person(1, "Alex Morgan", "first_name", organisations=frozenset({"globex"}))],
    )
    assert out.entity_id is None


def test_the_operator_is_never_guessed_from_a_first_name() -> None:
    out = decide(
        MentionInput("Alex", "person", context={"conversation_ref": "s-1"}),
        [_person(1, "Alex Morgan", "first_name", in_conversation=True, is_operator=True)],
    )
    assert out.entity_id is None


def test_the_operator_and_a_namesake_both_in_the_conversation_stay_unresolved() -> None:
    """Dropping the operator first would leave one candidate: the namesake."""
    out = decide(
        MentionInput("Casey", "person", context={"conversation_ref": "s-1"}),
        [
            _person(1, "Casey Quinn", "first_name", in_conversation=True, is_operator=True),
            _person(2, "Casey Lin", "first_name", in_conversation=True),
        ],
    )
    assert out.entity_id is None


def test_the_operator_still_resolves_by_strong_key() -> None:
    out = decide(
        MentionInput("Alex", "person", strong_keys={"crm_person_id": "x"}),
        [_person(1, "Alex Morgan", "first_name", is_operator=True)],
        strong_hits=[StrongHit("crm_person_id", 1, "person", "Alex Morgan")],
    )
    assert (out.entity_id, out.method) == (1, "strong_key")


@pytest.mark.parametrize("surface", ["A. Morgan", "A Morgan", "Alex M.", "J. R."])
def test_a_name_with_an_initial_is_not_a_full_name(surface: str) -> None:
    out = decide(MentionInput(surface, "person"), [_person(1, "Alex Morgan", "initial")])
    assert out.entity_id is None and out.create_type is None
    assert decide(MentionInput(surface, "person"), []).create_type is None


def test_a_single_token_person_with_no_candidate_creates_nothing() -> None:
    out = decide(MentionInput("Quill", "person"), [])
    assert out.entity_id is None and out.create_type is None


# ─── (e) provisional nodes ───────────────────────────────────────────────


def test_an_unknown_full_name_becomes_a_provisional_person() -> None:
    out = decide(MentionInput("Priya Natarajan", "person"), [])
    assert out.entity_id is None
    assert (out.create_type, out.method, out.reason) == ("person", "exact", "create")


def test_an_unknown_single_token_organisation_becomes_a_provisional_node() -> None:
    out = decide(MentionInput("Globex", "organization"), [])
    assert (out.create_type, out.method) == ("organization", "exact")


def test_a_name_of_unknown_type_with_no_candidate_creates_nothing() -> None:
    out = decide(MentionInput("Priya Natarajan", None), [])
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "no_type"


def test_a_blocked_name_creates_nothing() -> None:
    """A candidate whose CRM record is gone blocks a duplicate from being made."""
    out = decide(MentionInput("Dana Reyes", "person"), [], blocked=True)
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "blocked"


@pytest.mark.parametrize("surface", ["", " ", "x", "3f7c1e9a-0000-4000-8000-000000000000"])
def test_a_junk_name_is_never_resolved_or_created(surface: str) -> None:
    out = decide(MentionInput(surface, "person"), [_person(1, "Dana Reyes")])
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "junk"


# ─── single-token names of other types ───────────────────────────────────


def test_a_unique_single_token_organisation_resolves_exactly() -> None:
    out = decide(MentionInput("Globex", "organization"), [_org(3, "Globex")])
    assert (out.entity_id, out.method) == (3, "exact")


# ─── type compatibility ──────────────────────────────────────────────────


def test_a_person_never_resolves_to_an_organisation_of_the_same_name() -> None:
    out = decide(MentionInput("Jordan Lee", "person"), [_org(3, "Jordan Lee")])
    assert out.entity_id is None
    assert out.create_type == "person"  # a person node of that name does not exist yet


def test_an_organisation_never_resolves_to_a_person_of_the_same_name() -> None:
    out = decide(
        MentionInput("Jordan Lee", "organization"),
        [_person(1, "Jordan Lee"), _org(3, "Jordan Lee")],
    )
    assert (out.entity_id, out.method) == (3, "full_name")


def test_an_untyped_name_shared_by_a_person_and_an_organisation_stays_unresolved() -> None:
    out = decide(
        MentionInput("Jordan Lee", None), [_person(1, "Jordan Lee"), _org(3, "Jordan Lee")]
    )
    assert out.entity_id is None and out.create_type is None
    assert out.reason == "type_ambiguous"


def test_an_untyped_full_name_with_one_candidate_takes_its_type() -> None:
    out = decide(MentionInput("Dana Reyes", None), [_person(1, "Dana Reyes")])
    assert (out.entity_id, out.method) == (1, "full_name")


def test_an_untyped_single_token_matching_one_organisation_resolves() -> None:
    out = decide(MentionInput("Globex", None), [_org(3, "Globex")])
    assert (out.entity_id, out.method) == (3, "exact")


def test_an_untyped_single_token_that_is_also_a_first_name_stays_unresolved() -> None:
    out = decide(
        MentionInput("Jordan", None), [_org(3, "Jordan"), _person(1, "Jordan Lee", "first_name")]
    )
    assert out.entity_id is None


# ─── the basis a decision is reused by ───────────────────────────────────


def test_equal_inputs_give_equal_bases() -> None:
    cands = [_person(1, "Alex Morgan"), _person(2, "Alex Morgan #9f", "alias")]
    a = decide(MentionInput("Alex Morgan", "person"), cands)
    b = decide(MentionInput("alex  morgan", "person"), list(reversed(cands)))
    assert a.basis == b.basis


def test_a_new_candidate_changes_an_unresolved_basis() -> None:
    a = decide(MentionInput("Alex", "person"), [_person(1, "Alex Morgan", "first_name")])
    b = decide(
        MentionInput("Alex", "person"),
        [_person(1, "Alex Morgan", "first_name"), _person(2, "Alex Chen", "first_name")],
    )
    assert a.basis != b.basis

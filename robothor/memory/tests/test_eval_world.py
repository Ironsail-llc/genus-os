"""The synthetic memory-eval world: determinism, difficulty, ground truth, split.

Pure tests — no database, no model. They assert properties of the generated
world rather than exact text, so the word lists and phrasings can change
without rewriting the suite, while every property the eval depends on stays
pinned: the same seed is the same world byte for byte, the hard cases the
world exists to produce are actually present, and every generated case can be
answered from (or, for abstention, is provably absent from) the world itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

from robothor.memory import eval_world as ew
from robothor.memory.facts import VALID_CATEGORIES

REPO = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=4)
def _world(seed: int = 7, size: str = "small") -> ew.World:
    return ew.generate_world(seed, size)


def _full() -> ew.World:
    return _world(7, "full")


def _all_text(world: ew.World) -> list[str]:
    """Every piece of prose the world stores anywhere."""
    out = [f.text for f in world.facts]
    out += [t.text for c in world.conversations for s in c.sessions for t in s.turns]
    out += [d.title for d in world.documents]
    out += [sec.heading + " " + sec.body for d in world.documents for sec in d.sections]
    return out


def _segment_ids(world: ew.World) -> set[str]:
    ids = {t.id for c in world.conversations for s in c.sessions for t in s.turns}
    ids |= {sec.id for d in world.documents for sec in d.sections}
    ids |= {p.id for p in world.crm_people} | {c.id for c in world.crm_companies}
    return ids


def _entity_ids(world: ew.World) -> set[str]:
    return (
        {p.id for p in world.people}
        | {o.id for o in world.organisations}
        | {p.id for p in world.projects}
        | {loc.id for loc in world.locations}
        | {e.id for e in world.events}
    )


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


class TestDeterminism:
    def test_same_seed_is_byte_identical(self):
        a = ew.world_to_json(ew.generate_world(11, "small"))
        b = ew.world_to_json(ew.generate_world(11, "small"))
        assert a == b

    def test_different_seed_is_a_different_world(self):
        a = ew.world_to_json(ew.generate_world(11, "small"))
        b = ew.world_to_json(ew.generate_world(12, "small"))
        assert a != b

    def test_serialisation_is_valid_json_with_the_schema_version(self):
        data = json.loads(ew.world_to_json(_world()))
        assert data["schema_version"] == ew.WORLD_SCHEMA_VERSION
        assert data["seed"] == 7
        assert data["size"] == "small"

    def test_independent_of_hash_randomisation(self):
        """Unordered iteration (a set, a dict built from one) would leak here."""
        code = (
            "import hashlib,sys;from robothor.memory import eval_world as ew;"
            "sys.stdout.write(hashlib.sha256(ew.world_to_json("
            "ew.generate_world(5,'small')).encode()).hexdigest())"
        )
        digests = set()
        for hash_seed in ("0", "1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(REPO)}
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO),
                check=True,
            )
            digests.add(out.stdout.strip())
        assert len(digests) == 1

    def test_size_is_parameterised(self):
        small, full = _world(), _full()
        assert len(full.people) > len(small.people)
        assert len(full.facts) > len(small.facts)
        custom = ew.generate_world(3, ew.WORLD_SIZES["small"])
        assert custom.size == "small"

    def test_unknown_size_is_refused(self):
        with pytest.raises(ValueError, match="size"):
            ew.generate_world(1, "enormous")


# --------------------------------------------------------------------------- #
# Shape at full size
# --------------------------------------------------------------------------- #


class TestFullSize:
    def test_hundreds_of_people_dozens_of_orgs_thousands_of_facts(self):
        w = _full()
        assert 200 <= len(w.people) <= 999
        assert 24 <= len(w.organisations) <= 99
        assert len(w.facts) >= 2000
        assert len(w.projects) >= 12
        assert len(w.events) >= 12

    def test_at_least_150_cases_per_stratum(self):
        counts = Counter(c.stratum for c in _full().cases)
        for stratum in ew.STRATA:
            assert counts[stratum] >= 150, (stratum, counts[stratum])

    def test_histories_change_over_time(self):
        w = _full()
        per_person = Counter(e.person_id for e in w.employments)
        assert max(per_person.values()) >= 3
        assert sum(1 for n in per_person.values() if n >= 2) >= 150
        assert any(r.end is not None for r in w.reporting_lines)


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #


class TestFacts:
    def test_categories_and_origins_are_valid_and_all_origins_occur(self):
        w = _world()
        assert {f.category for f in w.facts} <= set(VALID_CATEGORIES)
        origins = {f.origin for f in w.facts}
        assert origins <= set(ew.ORIGINS)
        assert origins == set(ew.ORIGINS)

    def test_mentions_name_real_entities_by_the_surface_used_in_the_text(self):
        w = _world()
        ids = _entity_ids(w)
        for f in w.facts:
            assert f.mentions, f.id
            for m in f.mentions:
                assert m.entity_id in ids, (f.id, m.entity_id)
                assert m.surface in f.text, (f.id, m.surface, f.text)

    def test_event_dates_are_iso_dates(self):
        for f in _world().facts:
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.event_date), f.event_date

    def test_every_source_reference_resolves(self):
        w = _world()
        segments = _segment_ids(w)
        conv_ids = {c.id for c in w.conversations}
        doc_ids = {d.id for d in w.documents}
        for f in w.facts:
            assert f.source.kind in ("conversation", "document", "crm"), f.id
            assert f.source.segment_id in segments, (f.id, f.source)
            if f.source.kind == "conversation":
                assert f.source.container_id in conv_ids
            elif f.source.kind == "document":
                assert f.source.container_id in doc_ids

    def test_supersession_is_linked_both_ways_and_forward_in_time(self):
        w = _world()
        by_id = {f.id: f for f in w.facts}
        superseded = [f for f in w.facts if f.superseded_by]
        assert superseded
        for old in superseded:
            new = by_id[old.superseded_by]
            assert new.supersedes == old.id
            assert new.event_date >= old.event_date
            assert old.valid_to == new.event_date
        for f in w.facts:
            if f.supersedes:
                assert by_id[f.supersedes].superseded_by == f.id

    def test_fact_text_is_unique_per_category(self):
        """memory_facts dedups on (tenant, text, category): a collision would drop a row."""
        w = _world()
        keys = Counter((f.text, f.category) for f in w.facts)
        assert max(keys.values()) == 1
        if w.shadow is not None:
            keys = Counter((f.text, f.category) for f in w.shadow.facts)
            assert max(keys.values()) == 1


# --------------------------------------------------------------------------- #
# Difficulty properties
# --------------------------------------------------------------------------- #


class TestIdentityDifficulty:
    def test_people_share_first_names(self):
        counts = Counter(p.first_name for p in _world().people)
        assert any(n >= 2 for n in counts.values())

    def test_full_names_are_unique_within_a_tenant(self):
        counts = Counter(p.full_name for p in _full().people)
        assert max(counts.values()) == 1

    def test_mentions_use_nicknames_initials_emails_first_names_and_case_variants(self):
        w = _world()
        people = {p.id: p for p in w.people}
        kinds: set[str] = set()
        for f in w.facts:
            for m in f.mentions:
                p = people.get(m.entity_id)
                if p is None:
                    continue
                if m.surface == p.full_name:
                    kinds.add("full")
                elif p.nickname and m.surface == p.nickname:
                    kinds.add("nickname")
                elif m.surface == p.initials:
                    kinds.add("initials")
                elif m.surface == p.email:
                    kinds.add("email")
                elif m.surface == p.first_name:
                    kinds.add("first")
                elif m.surface.lower() == p.full_name.lower():
                    kinds.add("case")
        assert kinds >= {"full", "nickname", "initials", "email", "first", "case"}

    def test_an_organisation_and_a_project_share_a_name(self):
        w = _world()
        assert {o.name for o in w.organisations} & {p.name for p in w.projects}


class TestHub:
    @staticmethod
    def _person_shares(world: ew.World) -> dict[str, float]:
        neighbours: dict[str, set[str]] = {}
        for f in world.facts:
            ids = sorted({m.entity_id for m in f.mentions})
            for a in ids:
                neighbours.setdefault(a, set()).update(b for b in ids if b != a)
        total = len(_entity_ids(world)) - 1
        people = {p.id for p in world.people}
        return {pid: len(neighbours.get(pid, ())) / total for pid in sorted(people)}

    @pytest.mark.parametrize("size", ["small", "full"])
    def test_exactly_one_hub_person(self, size):
        w = _world(7, size)
        shares = self._person_shares(w)
        hubs = [pid for pid, s in shares.items() if s >= ew.HUB_MIN_SHARE]
        assert hubs == [w.hub_id]
        assert [p.id for p in w.people if p.is_hub] == [w.hub_id]
        others = [s for pid, s in shares.items() if pid != w.hub_id]
        assert max(others) < ew.HUB_MIN_SHARE / 2


class TestSourcesAndRecords:
    def test_some_entities_have_crm_rows_and_some_do_not(self):
        w = _world()
        with_crm = {c.person_id for c in w.crm_people}
        assert with_crm and with_crm < {p.id for p in w.people}
        orgs_crm = {c.org_id for c in w.crm_companies}
        assert orgs_crm and orgs_crm < {o.id for o in w.organisations}
        assert all(p.has_crm_row == (p.id in with_crm) for p in w.people)

    def test_crm_people_point_at_real_people_and_companies(self):
        w = _world()
        people = {p.id: p for p in w.people}
        crm_orgs = {c.org_id for c in w.crm_companies}
        for row in w.crm_people:
            assert people[row.person_id].email == row.email
            assert row.company_org_id is None or row.company_org_id in crm_orgs

    def test_documents_have_titled_sections(self):
        w = _world()
        assert w.documents
        for d in w.documents:
            assert d.title and d.sections
            assert all(s.id.startswith(d.id) and s.body for s in d.sections)

    def test_conversations_have_several_dated_sessions(self):
        w = _world()
        assert w.conversations
        assert max(len(c.sessions) for c in w.conversations) >= 2
        for c in w.conversations:
            for s in c.sessions:
                assert s.turns
                assert {t.role for t in s.turns} <= {"user", "assistant"}
                stamps = [t.at for t in s.turns]
                assert stamps == sorted(stamps)
                assert all(t.at.startswith(s.date) for t in s.turns)

    def test_transcripts_hold_details_that_no_fact_or_document_holds(self):
        w = _world()
        assert w.raw_details
        turns = {t.id: t for c in w.conversations for s in c.sessions for t in s.turns}
        fact_text = " ".join(f.text for f in w.facts).lower()
        doc_text = " ".join(s.body for d in w.documents for s in d.sections).lower()
        for rd in w.raw_details:
            assert rd.value in turns[rd.turn_id].text
            assert rd.value.lower() not in fact_text
            assert rd.value.lower() not in doc_text

    def test_planted_untrusted_documents_assert_false_relations(self):
        w = _world()
        planted = [d for d in w.documents if not d.trusted]
        assert planted
        assert {d.id for d in planted} == {c.document_id for c in w.planted_claims}
        for claim in w.planted_claims:
            assert claim.claimed_object != claim.true_object
        planted_ids = {d.id for d in planted}
        for f in w.facts:
            from_planted = f.source.kind == "document" and f.source.container_id in planted_ids
            assert from_planted == (f.origin == "external"), f.id


class TestShadowTenant:
    def test_second_tenant_shares_person_names_but_not_organisations(self):
        w = _world()
        assert w.shadow is not None
        assert w.shadow.shadow is None
        shared = {p.full_name for p in w.people} & {p.full_name for p in w.shadow.people}
        assert len(shared) >= 3
        assert not ({o.name for o in w.organisations} & {o.name for o in w.shadow.organisations})
        assert not ({loc.name for loc in w.locations} & {loc.name for loc in w.shadow.locations})

    def test_ids_do_not_collide_across_tenants(self):
        w = _world()
        assert w.shadow is not None
        a = _entity_ids(w) | {f.id for f in w.facts} | _segment_ids(w)
        b = _entity_ids(w.shadow) | {f.id for f in w.shadow.facts} | _segment_ids(w.shadow)
        assert not (a & b)


class TestGenericData:
    _EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")

    def test_names_come_from_the_module_word_lists(self):
        for w in (_full(), _full().shadow):
            assert w is not None
            for p in w.people:
                assert p.first_name in ew.FIRST_NAMES
                assert p.last_name in ew.LAST_NAMES

    def test_every_address_is_example_com(self):
        blob = ew.world_to_json(_full())
        domains = set(self._EMAIL.findall(blob))
        assert domains
        assert all(d == "example.com" or d.endswith(".example.com") for d in domains), domains
        for o in _full().organisations:
            assert o.domain.endswith(".example.com")


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #


class TestCases:
    def test_every_stratum_occurs_and_ids_are_unique(self):
        w = _world()
        assert {c.stratum for c in w.cases} == set(ew.STRATA)
        ids = [c.id for c in _full().cases]
        assert len(ids) == len(set(ids))

    def test_case_ids_are_stable_across_runs(self):
        a = [c.id for c in ew.generate_world(7, "small").cases]
        assert a == [c.id for c in _world().cases]

    def test_every_supporting_id_exists(self):
        for w in (_world(), _full()):
            known = {f.id for f in w.facts} | _segment_ids(w)
            for c in w.cases:
                for sid in c.supporting_ids:
                    assert sid in known, (c.id, sid)

    def test_answerable_cases_carry_gold_and_support(self):
        for c in _full().cases:
            if c.stratum == "abstention":
                continue
            assert c.gold and all(g.strip() for g in c.gold), c.id
            assert c.supporting_ids, c.id
            assert c.question.strip()

    def test_gold_is_reachable_from_its_support(self):
        w = _full()
        texts = {f.id: f.text.lower() for f in w.facts}
        texts |= {
            t.id: t.text.lower() for cv in w.conversations for s in cv.sessions for t in s.turns
        }
        for c in w.cases:
            if c.stratum == "abstention":
                continue
            support = " ".join(texts[s] for s in c.supporting_ids if s in texts)
            assert any(g.lower() in support for g in c.gold), c.id

    def test_abstention_cases_truly_have_no_support(self):
        w = _full()
        corpus = " ".join(_all_text(w)).lower()
        cases = [c for c in w.cases if c.stratum == "abstention"]
        assert cases
        for c in cases:
            assert c.gold == ("unknown",)
            assert c.supporting_ids == ()
            assert c.probe and c.probe.lower() not in corpus, c.id

    def test_raw_detail_gold_lives_only_in_a_transcript(self):
        w = _full()
        facts = " ".join(f.text for f in w.facts).lower()
        turns = {t.id for cv in w.conversations for s in cv.sessions for t in s.turns}
        for c in (c for c in w.cases if c.stratum == "raw_detail"):
            assert all(s in turns for s in c.supporting_ids)
            assert not any(g.lower() in facts for g in c.gold), c.id

    def test_point_in_time_cases_ask_inside_a_superseded_validity_window(self):
        w = _full()
        by_id = {f.id: f for f in w.facts}
        for c in (c for c in w.cases if c.stratum == "point_in_time"):
            assert c.as_of
            fact = by_id[c.supporting_ids[0]]
            assert fact.superseded_by is not None
            assert fact.event_date <= c.as_of < (fact.valid_to or "9999")

    def test_knowledge_update_support_is_the_current_fact_after_a_change(self):
        w = _full()
        by_id = {f.id: f for f in w.facts}
        for c in (c for c in w.cases if c.stratum == "knowledge_update"):
            fact = by_id[c.supporting_ids[0]]
            assert fact.superseded_by is None
            assert fact.supersedes is not None

    def test_long_tenure_spans_several_history_lengths(self):
        cases = [c for c in _full().cases if c.stratum == "long_tenure"]
        assert len({c.history_length for c in cases}) >= 3
        assert all(c.history_length and c.history_length >= 1 for c in cases)

    def test_entity_confusion_names_a_confuser_with_the_same_first_name(self):
        w = _full()
        people = {p.id: p for p in w.people}
        for c in (c for c in w.cases if c.stratum == "entity_confusion"):
            assert c.confuser_id and c.confuser_id != c.subject_id
            assert people[c.confuser_id].first_name == people[c.subject_id].first_name

    def test_multi_hop_needs_two_or_three_links(self):
        for c in (c for c in _full().cases if c.stratum == "multi_hop"):
            assert 2 <= len(c.supporting_ids) <= 3, c.id

    def test_cross_tenant_forbids_the_other_tenants_answer(self):
        w = _full()
        assert w.shadow is not None
        shadow_text = " ".join(f.text for f in w.shadow.facts).lower()
        primary_names = {p.full_name for p in w.people}
        cases = [c for c in w.cases if c.stratum == "cross_tenant"]
        assert cases
        for c in cases:
            assert c.forbidden
            assert not set(c.forbidden) & set(c.gold)
            assert all(f.lower() in shadow_text for f in c.forbidden)
            assert any(n in c.question for n in primary_names)


class TestSplit:
    def test_every_case_is_dev_or_held_out_by_its_id(self):
        for c in _full().cases:
            assert c.split in ew.SPLITS
            assert c.split == ew.split_for(c.id)

    def test_split_is_a_stable_hash_of_the_case_id(self):
        cid = "multi_hop:per-0001:manager-of-manager"
        expected = "dev" if hashlib.sha256(cid.encode()).digest()[0] % 2 == 0 else "held_out"
        assert ew.split_for(cid) == expected
        assert ew.split_for(cid) == ew.split_for(cid)

    def test_roughly_half_and_half(self):
        cases = _full().cases
        dev = sum(1 for c in cases if c.split == "dev")
        assert 0.4 <= dev / len(cases) <= 0.6

    def test_world_cases_filters_by_split(self):
        w = _full()
        dev = ew.world_cases(w, "dev")
        held = ew.world_cases(w, "held_out")
        assert dev and held
        assert {c.split for c in dev} == {"dev"}
        assert len(dev) + len(held) == len(ew.world_cases(w, "all")) == len(w.cases)
        with pytest.raises(ValueError):
            ew.world_cases(w, "train")

"""The ``conversation_buried`` stratum: a conversation answer under a pile of facts.

Pure tests — no database, no model. Each subject is a company with about a
hundred extracted facts, every one naming it, so a question about the company
finds those facts on both fact legs. The answer exists only in one of its
conversations: a detail beside the question's words (``near``), one far from
them in a long note (``far``), or a value the conversation changed while the
fact layer still holds the old one (``update``). This is the shape in which
facts fill the context and the conversation never reaches it.

Pins that adding it left every other stratum's cases, every earlier fact and
every earlier conversation untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from functools import lru_cache

import pytest

from robothor.memory import eval_world as ew
from robothor.memory.eval_answer import Answer, judge_rules, known_kinds, known_values
from robothor.memory.segments import rerank_excerpt

STRATUM = "conversation_buried"
KINDS = ("near", "far", "update")


@lru_cache(maxsize=4)
def _world(seed: int, size: str) -> ew.World:
    return ew.generate_world(seed, size)


def _cases(w: ew.World) -> list[ew.Case]:
    return [c for c in w.cases if c.stratum == STRATUM]


def _kind(case: ew.Case) -> str:
    return case.id.rsplit(":", 1)[1]


def _details(w: ew.World) -> dict[str, ew.ConversationRecall]:
    return {f"{STRATUM}:{r.id}": r for r in w.conversation_buried}


def _buried_convs(w: ew.World) -> set[str]:
    return {r.conversation_id for r in w.conversation_buried}


def _subject_of(w: ew.World, case: ew.Case) -> ew.Organisation:
    return next(o for o in w.organisations if o.id == case.subject_id)


def _turns(w: ew.World) -> dict[str, tuple[ew.Conversation, ew.ChatSession, ew.Turn]]:
    return {t.id: (c, s, t) for c in w.conversations for s in c.sessions for t in s.turns}


def _session_text(session: ew.ChatSession) -> str:
    labels = {"user": "User", "assistant": "Assistant"}
    return "\n".join(f"{labels[t.role]}: {t.text}" for t in session.turns)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Everything that existed before is untouched
# --------------------------------------------------------------------------- #

#: The conversation_recall stratum's cases and conversations before this one.
_RECALL_BEFORE = {
    (1, "full"): (
        "5bb45be0613112089bf041e9ac43bae012b09dd31167426aa99786ea97997b1c",
        "b803e4486e9fccb6275e35edfaa028c5569f69ac20bff8a8468a1a772c5da397",
    ),
    (7, "full"): (
        "6e4281af802755d4c1e570d601385c3aa12fb18e1fee4d7bc6781f9192c2bedc",
        "e3393a39b7f47b2a9b51a2a7586c027425c5d6b0273b257a6b81e384fce0b4fd",
    ),
    (7, "small"): (
        "dd5710eb27145c9d37899fe4a98156b62a34fd63c29af597ef468124dd2fb3f6",
        "e4c3f2dbf1279765921a0d43a760fc87db5b0c7e93b8d2b4770c83ef15df48c2",
    ),
}


class TestEverythingEarlierUnchanged:
    @pytest.mark.parametrize(("seed", "size"), sorted(_RECALL_BEFORE))
    def test_the_conversation_recall_stratum_is_byte_identical(self, seed, size):
        w = _world(seed, size)
        cases = [
            [c.id, c.question, list(c.gold), list(c.supporting_ids), c.split]
            for c in w.cases
            if c.stratum == "conversation_recall"
        ]
        recall = {r.conversation_id for r in w.conversation_recall}
        convs = [
            [t.id, t.text, t.at]
            for c in w.conversations
            if c.id in recall
            for s in c.sessions
            for t in s.turns
        ]
        assert (_digest(cases), _digest(convs)) == _RECALL_BEFORE[(seed, size)]

    def test_the_stratum_comes_last(self):
        assert ew.STRATA[-1] == STRATUM
        assert ew.STRATA[-2] == "conversation_recall"

    def test_the_schema_version_was_bumped_for_the_new_field(self):
        assert ew.WORLD_SCHEMA_VERSION == 3
        assert json.loads(ew.world_to_json(_world(7, "small")))["conversation_buried"]

    def test_earlier_organisations_keep_their_ids_and_order(self):
        w = _world(1, "full")
        subjects = {c.subject_id for c in _cases(w)}
        earlier = [o.id for o in w.organisations if o.id not in subjects]
        assert earlier == [o.id for o in w.organisations][: len(earlier)]


# --------------------------------------------------------------------------- #
# Size and split
# --------------------------------------------------------------------------- #


class TestSize:
    def test_forty_cases_in_each_split_at_full_size(self):
        split = Counter(c.split for c in _cases(_world(1, "full")))
        assert split["dev"] >= 40 and split["held_out"] >= 40, split

    def test_every_kind_in_both_splits(self):
        cases = _cases(_world(1, "full"))
        for split in ew.SPLITS:
            kinds = Counter(_kind(c) for c in cases if c.split == split)
            assert set(kinds) == set(KINDS), (split, kinds)
            assert min(kinds.values()) >= 10, (split, kinds)

    def test_present_in_the_small_world_too(self):
        assert {_kind(c) for c in _cases(_world(7, "small"))} == set(KINDS)

    def test_cases_are_deterministic(self):
        a = [(c.id, c.question, c.gold) for c in _cases(ew.generate_world(1, "full"))]
        assert a == [(c.id, c.question, c.gold) for c in _cases(_world(1, "full"))]


# --------------------------------------------------------------------------- #
# The pile of facts
# --------------------------------------------------------------------------- #


class TestThePile:
    def test_each_subject_is_named_by_at_least_a_hundred_facts(self):
        w = _world(1, "full")
        for case in _cases(w):
            org = _subject_of(w, case)
            naming = [f for f in w.facts if any(m.entity_id == org.id for m in f.mentions)]
            assert len(naming) >= 100, (case.id, len(naming))
            assert all(org.name in f.text for f in naming), case.id

    def test_the_pile_is_not_one_fact_repeated(self):
        """Near-duplicate removal must not collapse the pile to a few rows."""
        w = _world(1, "full")
        org = _subject_of(w, _cases(w)[0])
        texts = [f.text for f in w.facts if any(m.entity_id == org.id for m in f.mentions)]
        assert len(set(texts)) == len(texts)
        tokens = [frozenset(re.findall(r"[a-z0-9]+", t.lower())) for t in texts]
        overlapping = sum(
            1
            for i, a in enumerate(tokens)
            for b in tokens[i + 1 :]
            if len(a & b) / len(a | b) >= 0.75 or a <= b or b <= a
        )
        assert overlapping == 0

    def test_subject_names_share_no_word_with_the_rest_of_the_world(self):
        w = _world(1, "full")
        subjects = {c.subject_id for c in _cases(w)}
        names = {o.name for o in w.organisations if o.id in subjects}
        words = {x.lower() for n in names for x in n.split()}
        buried = _buried_convs(w)
        rest = " ".join(
            [f.text for f in w.facts if not any(m.entity_id in subjects for m in f.mentions)]
            + [
                t.text
                for c in w.conversations
                if c.id not in buried
                for s in c.sessions
                for t in s.turns
            ]
            + [s.heading + " " + s.body for d in w.documents for s in d.sections]
            + [o.name for o in w.organisations if o.id not in subjects]
            + [p.full_name for p in w.people]
        ).lower()
        rest_words = set(re.findall(r"[a-z]+", rest))
        assert not words & rest_words, words & rest_words


# --------------------------------------------------------------------------- #
# Ground truth lives only in the conversation
# --------------------------------------------------------------------------- #


class TestOnlyInConversation:
    @pytest.mark.parametrize(("seed", "size"), [(1, "full"), (7, "small")])
    def test_gold_is_in_no_fact_document_or_other_conversation(self, seed, size):
        w = _world(seed, size)
        buried = _buried_convs(w)
        elsewhere = " ".join(
            [f.text for f in w.facts]
            + [s.heading + " " + s.body for d in w.documents for s in d.sections]
            + [
                t.text
                for c in w.conversations
                if c.id not in buried
                for s in c.sessions
                for t in s.turns
            ]
        ).lower()
        cases = _cases(w)
        assert cases
        for c in cases:
            assert not any(g.lower() in elsewhere for g in c.gold), c.id

    def test_support_is_a_turn_of_the_subjects_conversation_and_holds_the_gold(self):
        w = _world(1, "full")
        turns = _turns(w)
        details = _details(w)
        for c in _cases(w):
            (sid,) = c.supporting_ids
            conv, _session, turn = turns[sid]
            assert conv.id == details[c.id].conversation_id, c.id
            assert any(g.lower() in turn.text.lower() for g in c.gold), c.id

    def test_conversations_are_user_assistant_exchanges(self):
        w = _world(1, "full")
        for c in (c for c in w.conversations if c.id in _buried_convs(w)):
            assert len(c.sessions) >= 3
            for s in c.sessions:
                roles = [t.role for t in s.turns]
                assert roles == ["user", "assistant"] * (len(roles) // 2), s.id

    def test_no_abstention_probe_appears_in_the_new_text(self):
        w = _world(1, "full")
        buried = _buried_convs(w)
        text = " ".join(
            [t.text for c in w.conversations if c.id in buried for s in c.sessions for t in s.turns]
            + [f.text for f in w.facts if f.source.container_id in buried]
        ).lower()
        for probe, _ in ew.ABSTENTION_PROBES:
            assert probe not in text


# --------------------------------------------------------------------------- #
# The shapes
# --------------------------------------------------------------------------- #


class TestShapes:
    def test_a_far_answer_lies_outside_the_window_around_the_question_words(self):
        w = _world(1, "full")
        turns = _turns(w)
        for c in _cases(w):
            if _kind(c) not in ("near", "far"):
                continue
            (sid,) = c.supporting_ids
            assert len(turns[sid][2].text) > 1200, c.id
            window = rerank_excerpt(_session_text(turns[sid][1]), c.question).lower()
            inside = any(g.lower() in window for g in c.gold)
            assert inside == (_kind(c) == "near"), (c.id, c.question)

    def test_an_update_is_new_in_conversation_and_old_in_an_active_fact(self):
        w = _world(1, "full")
        turns = _turns(w)
        details = _details(w)
        for c in (c for c in _cases(w) if _kind(c) == "update"):
            (old,) = details[c.id].rivals
            org = _subject_of(w, c)
            holding = [
                f
                for f in w.facts
                if old in f.text and any(m.entity_id == org.id for m in f.mentions)
            ]
            assert holding and all(f.superseded_by is None for f in holding), c.id
            (sid,) = c.supporting_ids
            text = turns[sid][2].text
            assert any(g in text for g in c.gold) and old in text, c.id
            assert all(f.event_date < turns[sid][1].date for f in holding), c.id


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #


class TestJudging:
    def test_the_old_value_the_facts_hold_is_judged_wrong(self):
        w = _world(7, "small")
        known, kinds = known_values(w), known_kinds(w)
        details = _details(w)
        for c in (c for c in _cases(w) if _kind(c) == "update"):
            (old,) = details[c.id].rivals
            verdict = judge_rules(c, Answer(old, False, ()), known, kinds)
            assert verdict is not None and not verdict.correct, (c.id, verdict)
            right = judge_rules(c, Answer(c.gold[0], False, ()), known, kinds)
            assert right is not None and right.correct, (c.id, right)

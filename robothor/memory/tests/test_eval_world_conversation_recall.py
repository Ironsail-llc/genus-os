"""The ``conversation_recall`` stratum: answers that live only in conversation text.

Pure tests — no database, no model. The world's own transcripts are short
(one detail per exchange), so no world question needed a long conversation
segment, an update said in a later session, or several sessions at once. This
stratum adds multi-session conversations written for exactly that, seeded
through the same chat path as the rest of the world, and pins that adding it
left every existing stratum's cases, and every fact, untouched.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from functools import lru_cache

import pytest

from robothor.memory import eval_world as ew
from robothor.memory.eval_answer import Answer, judge_rules, known_kinds, known_values
from robothor.memory.segments import rerank_excerpt

STRATUM = "conversation_recall"
KINDS = ("near", "far", "update", "multi_session")


@lru_cache(maxsize=4)
def _world(seed: int, size: str) -> ew.World:
    return ew.generate_world(seed, size)


def _cases(w: ew.World) -> list[ew.Case]:
    return [c for c in w.cases if c.stratum == STRATUM]


def _kind(case: ew.Case) -> str:
    return case.id.rsplit(":", 1)[1]


def _turns(w: ew.World) -> dict[str, tuple[ew.Conversation, ew.ChatSession, ew.Turn]]:
    return {t.id: (c, s, t) for c in w.conversations for s in c.sessions for t in s.turns}


def _session_text(session: ew.ChatSession) -> str:
    """The session as a conversation segment holds it (``segments._line``)."""
    labels = {"user": "User", "assistant": "Assistant"}
    return "\n".join(f"{labels[t.role]}: {t.text}" for t in session.turns)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# The existing strata are untouched
# --------------------------------------------------------------------------- #

#: Digests of the world as it was before this stratum existed (seed, size):
#: every other stratum's cases (id, stratum, question, gold, support, split,
#: as_of), every fact, and the original conversations' turns.
_BEFORE = {
    (1, "full"): (
        "e8df55a6938b9eeed54f83b5e2fd86ee0f4f04484a11c4d6221cff7f87e8d84b",
        "b401cc3c432b74ef46819a37395fe203049faaf3e5fb493d76a1f0b026486463",
        "f8b565841ec4c28e6d58977b626d175c5c0be0094f939e82f150cb1430eff43e",
    ),
    (7, "full"): (
        "387e60bb4a25ed0cc665fbeb7f2c872b20560fce2d7a25114d2f2ad00d4e5a02",
        "dbb662593f9eb55e84fb49fe31d70bb8cdd3af843325b023991872d9164b6b96",
        "b6f02d7c1d1d850d9d4a3b891ea00af6466759e8f888656aff12040c41187e4f",
    ),
    (7, "small"): (
        "1833d723866a1b796d4439e6762eb7ef00cc36896535383728ad3b42cf65ea93",
        "23835fa1f2ae13c96d6848297a72a088af64bf8239bbf0d9ba27a739ee93f01e",
        "45f63146778b0db2c06d4320ddedf7e4db7d5ffedec1247bc0c0f92301f3c767",
    ),
}


class TestExistingStrataUnchanged:
    @pytest.mark.parametrize(("seed", "size"), sorted(_BEFORE))
    def test_other_strata_facts_and_original_conversations_are_byte_identical(self, seed, size):
        w = _world(seed, size)
        # Later strata (conversation_buried) add their own cases, facts and
        # conversations; what existed before them is pinned here.
        later = {r.conversation_id for r in getattr(w, "conversation_buried", ())}
        cases = [
            [c.id, c.stratum, c.question, list(c.gold), list(c.supporting_ids), c.split, c.as_of]
            for c in w.cases
            if c.stratum not in (STRATUM, "conversation_buried")
        ]
        facts = [
            [f.id, f.text, f.event_date, f.source.segment_id]
            for f in w.facts
            if f.source.container_id not in later
        ]
        original = [
            [t.id, t.text, t.at]
            for c in w.conversations
            if c.id not in {r.conversation_id for r in w.conversation_recall} | later
            for s in c.sessions
            for t in s.turns
        ]
        assert (_digest(cases), _digest(facts), _digest(original)) == _BEFORE[(seed, size)]

    def test_the_stratum_follows_the_others_so_their_case_order_holds(self):
        at = ew.STRATA.index(STRATUM)
        assert ew.STRATA[at + 1 :] == ("conversation_buried",)
        assert ew.STRATA[:at] == (
            "multi_hop",
            "entity_confusion",
            "point_in_time",
            "knowledge_update",
            "abstention",
            "raw_detail",
            "long_tenure",
            "cross_tenant",
            "multi_hop_paraphrase",
        )

    def test_the_schema_version_was_bumped_for_the_new_field(self):
        assert ew.WORLD_SCHEMA_VERSION >= 2
        assert json.loads(ew.world_to_json(_world(7, "small")))["conversation_recall"]


# --------------------------------------------------------------------------- #
# Size and split
# --------------------------------------------------------------------------- #


class TestSize:
    def test_forty_cases_in_each_split_at_full_size(self):
        cases = _cases(_world(1, "full"))
        split = Counter(c.split for c in cases)
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
# Ground truth lives only in the conversations
# --------------------------------------------------------------------------- #


class TestOnlyInConversation:
    @pytest.mark.parametrize(("seed", "size"), [(1, "full"), (7, "small")])
    def test_gold_is_in_no_fact_document_or_original_conversation(self, seed, size):
        w = _world(seed, size)
        recall_convs = {r.conversation_id for r in w.conversation_recall}
        elsewhere = " ".join(
            [f.text for f in w.facts]
            + [s.heading + " " + s.body for d in w.documents for s in d.sections]
            + [
                t.text
                for c in w.conversations
                if c.id not in recall_convs
                for s in c.sessions
                for t in s.turns
            ]
        ).lower()
        cases = _cases(w)
        assert cases
        for c in cases:
            assert not any(g.lower() in elsewhere for g in c.gold), c.id

    def test_support_is_turns_of_the_new_conversations_and_holds_the_gold(self):
        w = _world(1, "full")
        turns = _turns(w)
        recall_convs = {r.conversation_id for r in w.conversation_recall}
        for c in _cases(w):
            assert c.supporting_ids and all(s in turns for s in c.supporting_ids), c.id
            assert {turns[s][0].id for s in c.supporting_ids} <= recall_convs, c.id
            support = " ".join(turns[s][2].text for s in c.supporting_ids).lower()
            assert any(g.lower() in support for g in c.gold), c.id

    def test_conversations_have_several_sessions_of_user_assistant_pairs(self):
        w = _world(1, "full")
        recall_convs = {r.conversation_id for r in w.conversation_recall}
        convs = [c for c in w.conversations if c.id in recall_convs]
        assert len(convs) >= 30
        for c in convs:
            assert len(c.sessions) >= 3
            for s in c.sessions:
                roles = [t.role for t in s.turns]
                # Seeding saves turns as exchanges (chat_store.save_exchange).
                assert roles == ["user", "assistant"] * (len(roles) // 2), s.id
                assert all(t.at.startswith(s.date) for t in s.turns)

    def test_no_abstention_probe_appears_in_the_new_text(self):
        w = _world(1, "full")
        recall_convs = {r.conversation_id for r in w.conversation_recall}
        text = " ".join(
            t.text
            for c in w.conversations
            if c.id in recall_convs
            for s in c.sessions
            for t in s.turns
        ).lower()
        for probe, _ in ew.ABSTENTION_PROBES:
            assert probe not in text


# --------------------------------------------------------------------------- #
# The shapes the stratum exists for
# --------------------------------------------------------------------------- #


class TestShapes:
    def test_near_and_far_answers_sit_in_long_conversation_text(self):
        """Over 1,200 characters: a segment the reranker and an excerpt must cut."""
        w = _world(1, "full")
        turns = _turns(w)
        for c in _cases(w):
            if _kind(c) in ("near", "far"):
                (sid,) = c.supporting_ids
                assert len(turns[sid][2].text) > 1200, c.id

    def test_a_far_answer_lies_outside_the_window_around_the_question_words(self):
        w = _world(1, "full")
        turns = _turns(w)
        for c in _cases(w):
            (sid,) = c.supporting_ids if _kind(c) in ("near", "far") else (None,)
            if sid is None:
                continue
            session = _session_text(turns[sid][1])
            window = rerank_excerpt(session, c.question).lower()
            inside = any(g.lower() in window for g in c.gold)
            assert inside == (_kind(c) == "near"), (c.id, c.question)

    def test_an_update_changes_the_value_in_a_later_session(self):
        w = _world(1, "full")
        turns = _turns(w)
        details = {f"{STRATUM}:{r.id}": r for r in w.conversation_recall}
        for c in (c for c in _cases(w) if _kind(c) == "update"):
            (new_turn,) = c.supporting_ids
            conv, new_session, _ = turns[new_turn]
            (old,) = details[c.id].rivals
            assert not any(old.lower() == g.lower() for g in c.gold)
            earlier = [
                s for s in conv.sessions if s.date < new_session.date and old in _session_text(s)
            ]
            assert earlier, c.id
            later = [s for s in conv.sessions if s.date > new_session.date]
            assert not any(old in _session_text(s) for s in later), c.id

    def test_a_multi_session_question_needs_three_sessions(self):
        w = _world(1, "full")
        turns = _turns(w)
        details = {f"{STRATUM}:{r.id}": r for r in w.conversation_recall}
        for c in (c for c in _cases(w) if _kind(c) == "multi_session"):
            sessions = {turns[s][1].id for s in c.supporting_ids}
            assert len(sessions) == 3, c.id
            rivals = details[c.id].rivals
            assert len(rivals) == 2 and not set(rivals) & set(c.gold)
            # Each session holds one of the three candidates, and only one.
            for sid in c.supporting_ids:
                text = turns[sid][2].text
                named = [v for v in (*c.gold, *rivals) if v in text]
                assert len(named) == 1, (c.id, sid)


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #


class TestJudging:
    def _details(self) -> tuple[ew.World, dict[str, ew.ConversationRecall]]:
        w = _world(7, "small")
        return w, {f"{STRATUM}:{r.id}": r for r in w.conversation_recall}

    def test_the_superseded_value_is_judged_wrong(self):
        w, details = self._details()
        known, kinds = known_values(w), known_kinds(w)
        for c in (c for c in _cases(w) if _kind(c) == "update"):
            (old,) = details[c.id].rivals
            verdict = judge_rules(c, Answer(old, False, ()), known, kinds)
            assert verdict is not None and not verdict.correct, (c.id, verdict)
            hedge = judge_rules(
                c, Answer(f"{c.gold[0]} (previously {old})", False, ()), known, kinds
            )
            assert hedge is not None and not hedge.correct, (c.id, hedge)
            right = judge_rules(c, Answer(c.gold[0], False, ()), known, kinds)
            assert right is not None and right.correct, (c.id, right)

    def test_a_rival_supplier_is_judged_wrong(self):
        w, details = self._details()
        known, kinds = known_values(w), known_kinds(w)
        for c in (c for c in _cases(w) if _kind(c) == "multi_session"):
            for rival in details[c.id].rivals:
                verdict = judge_rules(c, Answer(rival, False, ()), known, kinds)
                assert verdict is not None and not verdict.correct, (c.id, verdict)

"""Evidence at the level of the turns that hold the answer.

A session can be "present" while the turn holding the answer was excerpted
or packed away, so session-level evidence over-credits retrieval. LongMemEval
marks the answer-bearing turns (``has_answer``); a turn counts as delivered
when its text is in the context the answer model actually read.
"""

from __future__ import annotations

import json

import pytest

from robothor.memory import eval_bench as eb
from robothor.memory import eval_bench_data as bd
from robothor.memory.tests.test_eval_bench import _fake_ingest, _FakeChat
from robothor.memory.tests.test_eval_bench_data import _lme_entry

_ANSWER_TURN = "After the repair, the office kettle in Brelford boils in ninety seconds flat."


def _q(*turns: str, abstention: bool = False) -> bd.BenchQuestion:
    return bd.BenchQuestion(
        "q_abs" if abstention else "q",
        "h",
        "single-session-user",
        "How fast does the office kettle boil now?",
        "ninety seconds",
        abstention=abstention,
        evidence_sessions=("s1",),
        evidence_turns=tuple(turns),
    )


def test_loading_keeps_the_answer_bearing_turns(tmp_path):
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps([_lme_entry("q1", "single-session-user", "Q?", "A")]))
    (q,) = bd.load_benchmark("longmemeval", path).questions
    assert q.evidence_turns == ("I took up the cello.",)


def test_a_turn_whose_text_was_delivered_counts():
    items = [{"id": "s1#0u", "text": f"User: Hi.\nUser: {_ANSWER_TURN}\nAssistant: Nice."}]
    ev = eb.answer_turn_evidence(_q(_ANSWER_TURN), items)
    assert ev.recall == 1.0 and ev.complete is True


def test_a_session_present_without_its_answer_turn_does_not_count():
    # The excerpt kept the sentence that matches the question, not the value.
    items = [
        {
            "id": "s1#0u",
            "text": "User: By the way, the office kettle was repaired … it is quick now.",
        }
    ]
    q = _q(_ANSWER_TURN)
    assert eb.evidence_recall(q, items) == 1.0, "session-level evidence says present"
    ev = eb.answer_turn_evidence(q, items)
    assert ev.recall == 0.0 and ev.complete is False


def test_turn_recall_is_the_share_of_answer_turns_delivered():
    other = "Bob also orders oat milk and green tea for the office every Monday."
    items = [{"id": "s1#0u", "text": _ANSWER_TURN}]
    ev = eb.answer_turn_evidence(_q(_ANSWER_TURN, other), items)
    assert ev.recall == pytest.approx(0.5) and ev.complete is False


def test_a_turn_cut_below_half_is_not_present_and_formatting_is_ignored():
    long_turn = " ".join(f"word{i}" for i in range(100))
    half_minus = " ".join(f"word{i}" for i in range(40)) + " [truncated]"
    assert eb.answer_turn_evidence(_q(long_turn), [{"text": half_minus}]).recall == 0.0
    # Case, punctuation and line breaks do not decide presence.
    shouted = _ANSWER_TURN.upper().replace(",", "\n")
    assert eb.answer_turn_evidence(_q(_ANSWER_TURN), [{"text": shouted}]).recall == 1.0


def test_no_labelled_turns_and_abstention_are_not_measured():
    assert eb.answer_turn_evidence(_q(), [{"text": "x"}]).recall is None
    abst = eb.answer_turn_evidence(_q(_ANSWER_TURN, abstention=True), [{"text": _ANSWER_TURN}])
    assert abst.recall is None and abst.complete is None


def _rec(qid, score, turn_recall, complete, *, abstention=False, session=1.0):
    return bd.BenchRecord(
        "recall",
        qid,
        "multi-session",
        abstention,
        score,
        10,
        1.0,
        1.0,
        evidence_recall=None if abstention else session,
        answer_turn_recall=turn_recall,
        answer_turns_complete=complete,
    )


def test_losses_split_three_ways():
    recs = [
        _rec("right", 1.0, 1.0, True),
        _rec("cut", 0.0, 0.5, False),  # every session present, an answer turn cut
        _rec("missing", 0.0, 0.0, False, session=0.0),
        _rec("reasoning", 0.0, 1.0, True),
        _rec("x_abs", 0.0, None, None, abstention=True),
    ]
    m = bd.summarise_bench(recs, benchmark="longmemeval")["recall"]["overall"]
    assert m["losses"] == {
        "answer_turns_absent": 2,
        "answer_turns_present_answer_wrong": 1,
        "abstention": 1,
    }
    assert m["answer_turn_recall"] == pytest.approx((1.0 + 0.5 + 0.0 + 1.0) / 4)
    assert m["answer_turns_complete"] == 2
    # The session-level number stays beside it, labelled as such.
    assert m["evidence_recall"] == pytest.approx(0.75)
    assert m["wrong_with_evidence"] == 2


def test_bucket_of_one_record():
    assert bd.loss_bucket(_rec("a", 1.0, 1.0, True)) is None
    assert bd.loss_bucket(_rec("a", 0.0, 0.2, False)) == "answer_turns_absent"
    assert bd.loss_bucket(_rec("a", 0.0, 1.0, True)) == "answer_turns_present_answer_wrong"
    assert bd.loss_bucket(_rec("a_abs", 0.0, None, None, abstention=True)) == "abstention"


class _CutSystem:
    """Delivers the evidence session with its answer turn cut out."""

    name = "cut_system"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        from robothor.memory import eval_systems as es

        return es.ContextBundle(
            system=self.name,
            items=es.fill_budget(
                [("answer_1#0u", "turn", "User: I started a new hobby.")], token_budget
            ),
            budget=token_budget,
            equal_budget=True,
            abstained=False,
            retrieval_ms=1.0,
        )


@pytest.mark.asyncio
async def test_records_and_report_carry_answer_turn_evidence(tmp_path):
    from robothor.memory import eval_systems as es

    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps([_lme_entry("q1", "single-session-user", "Q?", "A")]))
    bench = bd.load_benchmark("longmemeval", path)
    es.register_system("cut_system", _CutSystem)
    try:
        report = await eb.run_benchmark(
            bench,
            systems=["cut_system"],
            ingest_fn=_fake_ingest([]),
            cleanup_fn=lambda _t: {},
            chat=_FakeChat("I don't know."),
            validate_judge=False,
        )
    finally:
        es.unregister_system("cut_system")
    (rec,) = report["records"]
    assert rec["evidence_recall"] == 1.0
    assert rec["answer_turn_recall"] == 0.0 and rec["answer_turns_complete"] is False
    md = eb.format_bench_markdown(report)
    assert "Answer-turn evidence" in md and "Session evidence" in md
    assert "answer turns absent 1" in md

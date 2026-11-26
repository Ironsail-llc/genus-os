"""Public long-memory benchmarks: loading and their own published scoring.

Tiny hand-written fixtures in each benchmark's schema; no dataset file and no
network. The judge prompts and metrics are checked against the official
evaluation code they reproduce (cited in ``eval_bench_data``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from robothor.memory import eval_bench_data as bd

# --------------------------------------------------------------------------- #
# Fixtures in the benchmarks' own schemas
# --------------------------------------------------------------------------- #


def _lme_entry(qid: str, qtype: str, question: str, answer: str) -> dict:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": question,
        "question_date": "2023/05/30 (Tue) 23:40",
        "answer": answer,
        "answer_session_ids": ["answer_1"],
        # Out of order on purpose: ingestion must follow the clock.
        "haystack_dates": ["2023/05/20 (Sat) 02:57", "2023/05/20 (Sat) 02:21"],
        "haystack_session_ids": ["s-later", "answer_1"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "Bob asked about tea."},
                {"role": "assistant", "content": "Green tea is mild."},
            ],
            [
                {
                    "role": "user",
                    "content": "I graduated in Business Administration.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Congratulations, Alice!"},
            ],
        ],
    }


@pytest.fixture
def lme_path(tmp_path):
    data = [
        _lme_entry(
            "q1",
            "single-session-user",
            "What degree did I graduate with?",
            "Business Administration",
        ),
        _lme_entry(
            "q2_abs", "multi-session", "What is my dog's name?", "You did not mention a dog."
        ),
        _lme_entry("q3", "temporal-reasoning", "How many days ago did I graduate?", "10 days"),
        _lme_entry("q4", "knowledge-update", "Where do I live now?", "Brelford"),
        _lme_entry(
            "q5", "single-session-preference", "Suggest a drink.", "The user would prefer tea."
        ),
        _lme_entry("q6", "single-session-assistant", "What tea did you call mild?", "Green tea"),
    ]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def locomo_path(tmp_path):
    data = [
        {
            "sample_id": "conv-1",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_1": [
                    {"speaker": "Alice", "dia_id": "D1:1", "text": "I ran a charity race."},
                    {
                        "speaker": "Bob",
                        "dia_id": "D1:2",
                        "text": "Look at this!",
                        "img_url": ["x"],
                        "blip_caption": "a photo of a dog",
                    },
                ],
                "session_2_date_time": "10:37 am on 27 June, 2023",
                "session_2": [
                    {"speaker": "Bob", "dia_id": "D2:1", "text": "I moved to Brelford."},
                ],
            },
            "qa": [
                {
                    "question": "When did Alice run?",
                    "answer": "7 May 2023",
                    "evidence": ["D1:1"],
                    "category": 2,
                },
                {
                    "question": "Where did Bob move?",
                    "answer": "Brelford",
                    "evidence": ["D2:1"],
                    "category": 4,
                },
                {
                    "question": "What do they like?",
                    "answer": "dogs, races",
                    "evidence": [],
                    "category": 1,
                },
                {
                    "question": "What might Bob study?",
                    "answer": "biology; maybe",
                    "evidence": [],
                    "category": 3,
                },
                {
                    "question": "What did Bob realise?",
                    "adversarial_answer": "self-care matters",
                    "evidence": ["D1:2"],
                    "category": 5,
                },
            ],
        }
    ]
    path = tmp_path / "locomo10.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


class TestLongMemEvalLoading:
    def test_one_history_per_question_sessions_in_clock_order(self, lme_path):
        bench = bd.load_benchmark("longmemeval", lme_path)
        assert bench.name == "longmemeval" and bench.variant == "s"
        assert len(bench.questions) == 6
        q = bench.questions[0]
        history = bench.histories[q.history_id]
        assert [s.id for s in history.sessions] == ["answer_1", "s-later"]
        assert history.sessions[0].at == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
        assert [t.role for t in history.sessions[0].turns] == ["user", "assistant"]
        # Turns keep their order and carry the session's clock, never later
        # than the next session.
        turns = history.sessions[0].turns
        assert turns[0].at >= history.sessions[0].at and turns[1].at > turns[0].at
        assert turns[-1].at < history.sessions[1].at

    def test_question_fields(self, lme_path):
        bench = bd.load_benchmark("longmemeval", lme_path)
        by_id = {q.id: q for q in bench.questions}
        assert by_id["q1"].question_type == "single-session-user"
        assert by_id["q1"].answer == "Business Administration"
        assert by_id["q1"].as_of == "2023/05/30 (Tue) 23:40"
        assert by_id["q2_abs"].abstention and not by_id["q1"].abstention

    def test_variant_comes_from_the_file_name(self, tmp_path, lme_path):
        oracle = tmp_path / "longmemeval_oracle.json"
        oracle.write_text(lme_path.read_text(encoding="utf-8"), encoding="utf-8")
        assert bd.load_benchmark("longmemeval", oracle).variant == "oracle"

    def test_dates(self):
        assert bd.parse_longmemeval_date("2023/05/20 (Sat) 02:21") == datetime(
            2023, 5, 20, 2, 21, tzinfo=UTC
        )


class TestLoCoMoLoading:
    def test_one_history_per_conversation_speakers_and_images(self, locomo_path):
        bench = bd.load_benchmark("locomo", locomo_path)
        assert list(bench.histories) == ["conv-1"]
        history = bench.histories["conv-1"]
        assert history.speakers == ("Alice", "Bob")
        s1 = history.sessions[0]
        assert s1.at == datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
        # speaker_a is the user side, speaker_b the other; each line names its speaker.
        assert [t.role for t in s1.turns] == ["user", "assistant"]
        assert s1.turns[0].text == "Alice: I ran a charity race."
        # As the official context builder renders a shared image.
        assert s1.turns[1].text == "Bob: Look at this! [shares a photo of a dog]"

    def test_questions_and_the_adversarial_answer(self, locomo_path):
        bench = bd.load_benchmark("locomo", locomo_path)
        assert all(q.history_id == "conv-1" for q in bench.questions)
        cats = [q.category for q in bench.questions]
        assert cats == [2, 4, 1, 3, 5]
        assert bench.questions[0].answer == "7 May 2023"
        assert bench.questions[0].question_type == "temporal"
        adv = bench.questions[4]
        assert adv.question_type == "adversarial" and adv.answer == "self-care matters"

    def test_adversarial_dropped_by_default_convention(self, locomo_path):
        bench = bd.load_benchmark("locomo", locomo_path)
        kept = bd.select_questions(bench.questions, drop_types=bd.LOCOMO_DEFAULT_DROP)
        assert sorted(q.category for q in kept) == [1, 2, 3, 4]

    def test_dates(self):
        assert bd.parse_locomo_date("10:37 am on 27 June, 2023") == datetime(
            2023, 6, 27, 10, 37, tzinfo=UTC
        )
        assert bd.parse_locomo_date("12:05 am on 1 Jan, 2024") == datetime(
            2024, 1, 1, 0, 5, tzinfo=UTC
        )

    def test_unknown_benchmark_is_refused(self, locomo_path):
        with pytest.raises(ValueError, match="unknown benchmark"):
            bd.load_benchmark("beam", locomo_path)


class TestSelection:
    def test_stratified_deterministic_and_extendable(self, lme_path):
        bench = bd.load_benchmark("longmemeval", lme_path)
        one = bd.select_questions(bench.questions, limit_per_type=1)
        assert len(one) == 6
        assert one == bd.select_questions(bench.questions, limit_per_type=1)
        two = bd.select_questions(bench.questions, limit=3)
        assert len(two) == 3 and len({q.question_type for q in two}) == 3
        assert {q.id for q in two} <= {q.id for q in bd.select_questions(bench.questions, limit=6)}

    def test_no_limit_keeps_everything(self, lme_path):
        bench = bd.load_benchmark("longmemeval", lme_path)
        assert len(bd.select_questions(bench.questions)) == 6


# --------------------------------------------------------------------------- #
# LongMemEval's own judge
# --------------------------------------------------------------------------- #


class TestLongMemEvalJudge:
    def _q(self, lme_path, qid):
        bench = bd.load_benchmark("longmemeval", lme_path)
        return next(q for q in bench.questions if q.id == qid)

    def test_standard_prompt_is_the_official_text(self, lme_path):
        q = self._q(lme_path, "q1")
        prompt = bd.longmemeval_judge_prompt(q, "You graduated in Business Administration.")
        assert prompt == (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "If the response is equivalent to the correct answer or contains all the intermediate "
            "steps to get the correct answer, you should also answer yes. If the response only "
            "contains a subset of the information required by the answer, answer no. \n\n"
            "Question: What degree did I graduate with?\n\nCorrect Answer: Business Administration"
            "\n\nModel Response: You graduated in Business Administration.\n\n"
            "Is the model response correct? Answer yes or no only."
        )

    def test_type_specific_prompts(self, lme_path):
        assert "off-by-one errors" in bd.longmemeval_judge_prompt(self._q(lme_path, "q3"), "9 days")
        assert "updated answer" in bd.longmemeval_judge_prompt(self._q(lme_path, "q4"), "x")
        pref = bd.longmemeval_judge_prompt(self._q(lme_path, "q5"), "Try tea.")
        assert "Rubric: The user would prefer tea." in pref
        assert "satisfies the desired response" in pref
        ssa = bd.longmemeval_judge_prompt(self._q(lme_path, "q6"), "Green tea")
        assert ssa.startswith("I will give you a question, a correct answer")

    def test_abstention_is_decided_by_the_id_suffix(self, lme_path):
        prompt = bd.longmemeval_judge_prompt(self._q(lme_path, "q2_abs"), "I don't know.")
        assert prompt.startswith("I will give you an unanswerable question, an explanation")
        assert "Explanation: You did not mention a dog." in prompt
        assert prompt.endswith(
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )

    @pytest.mark.parametrize(
        ("raw", "label"),
        [("yes", True), ("Yes.", True), ("no", False), ("No, it does not.", False), ("YES", True)],
    )
    def test_label_is_yes_anywhere_in_the_output(self, raw, label):
        assert bd.longmemeval_label(raw) is label

    def test_label_ignores_a_think_block(self):
        assert bd.longmemeval_label("<think>yes or no? no</think>no") is False


class TestLongMemEvalMetrics:
    def _rec(self, system, qid, qtype, correct, abstention=False):
        return bd.BenchRecord(
            system=system,
            question_id=qid,
            question_type=qtype,
            abstention=abstention,
            score=1.0 if correct else 0.0,
            tokens=100,
            retrieval_ms=10.0,
            answer_ms=20.0,
        )

    def test_overall_task_averaged_per_type_and_abstention(self):
        recs = [
            self._rec("recall", "a", "single-session-user", True),
            self._rec("recall", "b", "single-session-user", True),
            self._rec("recall", "c", "multi-session", False),
            self._rec("recall", "d_abs", "multi-session", True, abstention=True),
        ]
        m = bd.summarise_bench(recs, benchmark="longmemeval")["recall"]
        assert m["overall"]["n"] == 4 and m["overall"]["accuracy"] == pytest.approx(0.75)
        assert m["by_type"]["single-session-user"]["accuracy"] == 1.0
        assert m["by_type"]["multi-session"]["accuracy"] == 0.5
        # The mean of the per-type accuracies, as print_qa_metrics.py reports it.
        assert m["task_averaged"] == pytest.approx(0.75)
        assert m["abstention"]["n"] == 1 and m["abstention"]["accuracy"] == 1.0
        assert m["overall"]["mean_tokens"] == 100
        assert m["overall"]["errors"] == 0

    def test_errors_count_as_wrong_and_are_reported(self):
        recs = [self._rec("recall", "a", "single-session-user", True)]
        recs.append(
            bd.BenchRecord(
                system="recall",
                question_id="b",
                question_type="single-session-user",
                abstention=False,
                score=0.0,
                tokens=0,
                retrieval_ms=0.0,
                answer_ms=0.0,
                error="boom",
            )
        )
        m = bd.summarise_bench(recs, benchmark="longmemeval")["recall"]
        assert m["overall"]["accuracy"] == 0.5 and m["overall"]["errors"] == 1


class TestPlantedJudgeAnswers:
    def test_planted_right_and_wrong_per_question(self, lme_path):
        bench = bd.load_benchmark("longmemeval", lme_path)
        planted = bd.longmemeval_planted(bench.questions, seed=1)
        assert planted
        kinds = {p.kind for p in planted}
        assert {"gold", "other_answer", "abstained", "abstention_answered"} <= kinds
        for p in planted:
            if p.kind == "other_answer":
                assert p.response != p.question.answer and not p.expect_yes
            if p.kind == "gold":
                assert p.question.answer in p.response and p.expect_yes
        # The preference rubric is not an answer; it is not planted.
        assert all(p.question.question_type != "single-session-preference" for p in planted)


# --------------------------------------------------------------------------- #
# Answer prompts
# --------------------------------------------------------------------------- #


class TestAnswerPrompts:
    def test_longmemeval_reader_prompt_is_the_official_template(self):
        items = [bd.PromptItem("turn", "User: I graduated.", "2023/05/20 (Sat) 02:21")]
        prompt = bd.longmemeval_answer_prompt("What degree?", "2023/05/30 (Tue) 23:40", items)
        assert prompt.startswith(
            "I will give you several history chats between you and a user. Please answer the "
            "question based on the relevant chat history.\n\n\nHistory Chats:\n\n"
        )
        assert prompt.endswith(
            "\n\nCurrent Date: 2023/05/30 (Tue) 23:40\nQuestion: What degree?\nAnswer:"
        )
        assert "Session Date: 2023/05/20 (Sat) 02:21" in prompt
        assert "User: I graduated." in prompt

    def test_facts_get_the_official_facts_template(self):
        facts_only = [bd.PromptItem("fact", "Alice graduated.", None)]
        p = bd.longmemeval_answer_prompt("Q?", "2023/05/30 (Tue) 23:40", facts_only)
        assert p.startswith("I will give you several facts extracted from history chats")
        mixed = [*facts_only, bd.PromptItem("turn", "User: hi", "2023/05/20 (Sat) 02:21")]
        p = bd.longmemeval_answer_prompt("Q?", "2023/05/30 (Tue) 23:40", mixed)
        assert "as well as the relevant user facts extracted from the chat history" in p

    def test_locomo_prompt_and_temporal_suffix(self, locomo_path):
        bench = bd.load_benchmark("locomo", locomo_path)
        q = bench.questions[0]
        text, key = bd.locomo_question_text(q, seed=1)
        assert text.endswith(" Use DATE of CONVERSATION to answer with an approximate date.")
        assert key is None
        prompt = bd.locomo_answer_prompt(
            ("Alice", "Bob"), [bd.PromptItem("turn", "Alice: hi", "x")], text, q
        )
        assert prompt.startswith("Below is a conversation between two people: Alice and Bob.")
        assert "Based on the above context, write an answer in the form of a short phrase" in prompt
        assert prompt.rstrip().endswith("Short answer:")

    def test_locomo_adversarial_is_a_two_way_choice(self, locomo_path):
        bench = bd.load_benchmark("locomo", locomo_path)
        q = bench.questions[4]
        text, key = bd.locomo_question_text(q, seed=1)
        assert (
            "Select the correct answer: (a)" in text and "Not mentioned in the conversation" in text
        )
        assert set(key.values()) == {"Not mentioned in the conversation", "self-care matters"}
        not_mentioned = next(k for k, v in key.items() if v.startswith("Not mentioned"))
        assert bd.locomo_cat5_answer(not_mentioned, key) == "Not mentioned in the conversation"
        assert bd.locomo_cat5_answer(f"({not_mentioned})", key).startswith("Not mentioned")


# --------------------------------------------------------------------------- #
# LoCoMo's own metric (task_eval/evaluation.py)
# --------------------------------------------------------------------------- #


def _identity(word: str) -> str:
    return word


class TestLoCoMoScore:
    def test_normalise_drops_articles_and_punctuation(self):
        assert bd.locomo_normalize_answer("The Dog, and a Cat!") == "dog cat"

    def test_f1_single_answer(self):
        assert bd.locomo_f1_score("Brelford", "Brelford", stem=_identity) == 1.0
        assert bd.locomo_f1_score("in Brelford town", "Brelford", stem=_identity) == pytest.approx(
            0.5
        )
        assert bd.locomo_f1_score("Paris", "Brelford", stem=_identity) == 0.0

    def test_scores_by_category(self, locomo_path):
        bench = bd.load_benchmark("locomo", locomo_path)
        temporal, single, multi, open_domain, adversarial = bench.questions
        assert bd.locomo_score(single, "Brelford", stem=_identity) == 1.0
        # Multi-hop: the mean over gold parts of the best prediction part.
        assert bd.locomo_score(multi, "dogs, races", stem=_identity) == 1.0
        assert bd.locomo_score(multi, "dogs", stem=_identity) == pytest.approx(0.5)
        # Open-domain: only the gold before the first ';'.
        assert bd.locomo_score(open_domain, "biology", stem=_identity) == 1.0
        # Adversarial: right only when the answer says it is not mentioned.
        assert (
            bd.locomo_score(adversarial, "Not mentioned in the conversation", stem=_identity) == 1.0
        )
        assert bd.locomo_score(adversarial, "self-care matters", stem=_identity) == 0.0
        assert bd.locomo_score(adversarial, "No information available", stem=_identity) == 1.0

    def test_default_stemmer_is_porter_from_nltk(self):
        nltk = pytest.importorskip("nltk")
        assert nltk is not None
        assert bd.locomo_f1_score("running dogs", "run dog") == 1.0

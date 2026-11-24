"""The answer step and the judge: prompt, parsing, every judge rule, validation.

The model is always injected here. Real model calls live in
``test_eval_answer_llm.py`` under the ``llm`` marker.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pytest

from robothor.memory import eval_answer as ea
from robothor.memory import eval_world as ew
from robothor.memory.eval import EvalPreconditionError
from robothor.memory.eval_systems import ContextBundle, ContextItem


@lru_cache(maxsize=1)
def _world() -> ew.World:
    return ew.generate_world(7, "small")


def _case(stratum: str = "knowledge_update", **kw: Any) -> ew.Case:
    base: dict[str, Any] = {
        "id": f"{stratum}:per-0002:x",
        "stratum": stratum,
        "question": "Which organisation does Alice Ashby work for now?",
        "gold": ("Kelvaro Systems",),
        "supporting_ids": ("fct-00001",),
        "as_of": None,
        "split": "dev",
        "subject_id": "per-0002",
    }
    base.update(kw)
    return ew.Case(**base)


def _answer(text: str, abstained: bool = False) -> ea.Answer:
    return ea.Answer(text=text, abstained=abstained, cited_ids=())


def _bundle(*texts: str, budget: int = 1000) -> ContextBundle:
    items = tuple(
        ContextItem(id=f"fct-{i:05d}", kind="fact", text=t, tokens=len(t) // 4, rank=i)
        for i, t in enumerate(texts, start=1)
    )
    return ContextBundle(
        system="test",
        items=items,
        budget=budget,
        equal_budget=True,
        abstained=False,
        retrieval_ms=1.0,
    )


KNOWN = frozenset({"Kelvaro Systems", "Brivane Labs", "Velmere", "Tharholt", "Bob Ashby"})


# --------------------------------------------------------------------------- #
# Prompt and parsing
# --------------------------------------------------------------------------- #


class TestPrompt:
    def test_prompt_carries_items_question_instruction_and_date(self):
        msgs = ea.build_answer_messages(
            "Where?",
            _bundle("Alice works at Kelvaro.", "Bob lives in Velmere."),
            as_of="2021-03-04",
        )
        blob = "\n".join(m["content"] for m in msgs)
        assert msgs[0]["role"] == "system"
        assert "fct-00001" in blob and "fct-00002" in blob
        assert "Alice works at Kelvaro." in blob
        assert "Where?" in blob
        assert "2021-03-04" in blob
        assert "unknown" in msgs[0]["content"].lower()
        assert "only" in msgs[0]["content"].lower()

    def test_prompt_is_deterministic(self):
        b = _bundle("a", "b")
        assert ea.build_answer_messages("q", b) == ea.build_answer_messages("q", b)

    def test_empty_context_still_asks(self):
        msgs = ea.build_answer_messages("q?", _bundle())
        assert "q?" in msgs[-1]["content"]


class TestParse:
    def test_json_answer(self):
        raw = json.dumps({"answer": "Kelvaro Systems", "unknown": False, "cited_ids": ["fct-1"]})
        assert ea.parse_answer(raw) == ("Kelvaro Systems", False, ("fct-1",))

    def test_json_unknown(self):
        raw = json.dumps({"answer": "unknown", "unknown": True, "cited_ids": []})
        text, abstained, cited = ea.parse_answer(raw)
        assert abstained and cited == ()

    def test_plain_text_unknown_is_an_abstention(self):
        assert ea.parse_answer("Unknown.")[1] is True
        assert ea.parse_answer("The context does not say; unknown")[1] is True

    def test_garbage_is_not_an_abstention(self):
        text, abstained, _ = ea.parse_answer("Kelvaro Systems")
        assert text == "Kelvaro Systems" and not abstained

    def test_think_blocks_are_stripped(self):
        text, _, _ = ea.parse_answer("<think>hmm</think>Velmere")
        assert text == "Velmere"


class TestAnswer:
    @pytest.mark.asyncio
    async def test_answer_uses_the_injected_model_and_reports_it(self):
        seen: dict[str, Any] = {}

        async def chat(messages, **kw):
            seen.update(kw)
            seen["messages"] = messages
            return json.dumps({"answer": "Velmere", "unknown": False, "cited_ids": ["fct-00001"]})

        out = await ea.answer("Where?", _bundle("x"), model="m-test", chat=chat)
        assert out.text == "Velmere" and not out.abstained
        assert out.cited_ids == ("fct-00001",)
        assert out.model == "m-test"
        assert seen["model"] == "m-test"
        assert seen["temperature"] == 0.0
        assert out.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_answer_refuses_a_bundle_over_its_budget(self):
        async def chat(messages, **kw):
            raise AssertionError("must not be called")

        over = _bundle("x" * 4000, budget=10)
        with pytest.raises(ValueError, match="budget"):
            await ea.answer("q", over, chat=chat)

    @pytest.mark.asyncio
    async def test_model_failure_is_an_error_not_an_abstention(self):
        async def chat(messages, **kw):
            raise RuntimeError("model down")

        with pytest.raises(RuntimeError):
            await ea.answer("q", _bundle("x"), chat=chat)


# --------------------------------------------------------------------------- #
# Judge rules
# --------------------------------------------------------------------------- #


class TestJudgeRules:
    def _rule(self, case, answer):
        return ea.judge_rules(case, answer, KNOWN)

    def test_abstention_case_abstained_is_correct(self):
        v = self._rule(_case("abstention", gold=("unknown",)), _answer("unknown", True))
        assert v.correct and v.abstained and not v.confident_wrong

    def test_abstention_case_answered_is_wrong_and_confident(self):
        v = self._rule(_case("abstention", gold=("unknown",)), _answer("O negative"))
        assert not v.correct and v.confident_wrong and not v.abstained

    def test_unknown_on_an_answerable_case_is_wrong_but_not_confident(self):
        v = self._rule(_case(), _answer("unknown", True))
        assert not v.correct and not v.confident_wrong and v.abstained

    def test_forbidden_value_is_wrong_even_beside_the_gold(self):
        case = _case("cross_tenant", forbidden=("Zelthane Works",))
        v = self._rule(case, _answer("Kelvaro Systems or maybe Zelthane Works"))
        assert not v.correct and v.confident_wrong
        assert "forbidden" in v.reason

    @pytest.mark.parametrize(
        "text",
        [
            "Kelvaro Systems",
            "kelvaro systems.",
            "KELVARO SYSTEMS",
            "She works for Kelvaro Systems these days.",
            "Kelvaro  Systems!",
        ],
    )
    def test_gold_match_is_normalised(self, text):
        v = self._rule(_case(), _answer(text))
        assert v.correct and v.method == "rule"

    def test_any_alias_counts(self):
        case = _case("multi_hop", gold=("Bob Ashby", "bob.ashby@example.com"))
        assert self._rule(case, _answer("bob.ashby@example.com")).correct

    def test_gold_must_match_whole_words(self):
        case = _case("long_tenure", gold=("Vel",))
        v = self._rule(case, _answer("Velmere"))
        assert v is None or not v.correct

    def test_another_known_value_without_gold_is_confident_wrong(self):
        v = self._rule(_case(), _answer("Brivane Labs"))
        assert v is not None and not v.correct and v.confident_wrong and v.method == "rule"

    def test_free_text_paraphrase_is_left_to_the_llm(self):
        assert self._rule(_case(), _answer("the company with the blue logo")) is None


class TestJudge:
    @pytest.mark.asyncio
    async def test_llm_judge_settles_what_rules_cannot(self):
        calls = []

        async def llm(case, answer):
            calls.append(answer.text)
            return True, "paraphrase of gold"

        v = await ea.judge(_case(), _answer("that Kelvaro outfit"), known_values=KNOWN, llm=llm)
        assert v.correct and v.method == "llm" and calls == ["that Kelvaro outfit"]

    @pytest.mark.asyncio
    async def test_llm_wrong_is_confident_wrong(self):
        async def llm(case, answer):
            return False, "different organisation"

        v = await ea.judge(_case(), _answer("some other outfit"), known_values=KNOWN, llm=llm)
        assert not v.correct and v.confident_wrong and v.method == "llm"

    @pytest.mark.asyncio
    async def test_llm_is_not_called_when_rules_decide(self):
        async def llm(case, answer):
            raise AssertionError("rules should have decided")

        v = await ea.judge(_case(), _answer("Kelvaro Systems"), known_values=KNOWN, llm=llm)
        assert v.correct

    @pytest.mark.asyncio
    async def test_without_an_llm_an_unsettled_answer_is_wrong(self):
        v = await ea.judge(_case(), _answer("the blue-logo company"), known_values=KNOWN)
        assert not v.correct and v.reason.startswith("unsettled")

    def test_judge_prompt_is_fixed_in_the_module(self):
        assert "{question}" in ea.JUDGE_PROMPT and "{gold}" in ea.JUDGE_PROMPT
        assert "{answer}" in ea.JUDGE_PROMPT


class TestCrossTenantFailsALeakingSystem:
    @pytest.mark.asyncio
    async def test_answer_built_from_the_shadow_tenant_is_wrong(self):
        w = _world()
        case = next(c for c in w.cases if c.stratum == "cross_tenant")
        leaked = case.forbidden[0]

        async def chat(messages, **kw):
            # A model that repeats whatever the (leaking) context said.
            return json.dumps({"answer": leaked, "unknown": False, "cited_ids": ["sh-fct-00001"]})

        bundle = _bundle(f"{case.question} — {leaked}")
        ans = await ea.answer(case.question, bundle, chat=chat)
        verdict = await ea.judge(case, ans, known_values=ea.known_values(w))
        assert not verdict.correct and verdict.confident_wrong


# --------------------------------------------------------------------------- #
# Judge validation
# --------------------------------------------------------------------------- #


class TestPlantedAnswers:
    def test_all_kinds_are_planted_and_deterministic(self):
        a = ea.planted_answers(_world())
        b = ea.planted_answers(_world())
        assert [(p.case.id, p.answer.text, p.expect_correct) for p in a] == [
            (p.case.id, p.answer.text, p.expect_correct) for p in b
        ]
        kinds = {p.kind for p in a}
        assert {
            "confuser",
            "superseded",
            "other_tenant",
            "random_entity",
            "paraphrase",
        } <= kinds
        assert any(p.expect_correct for p in a) and any(not p.expect_correct for p in a)

    def test_planted_wrong_answers_never_contain_a_gold(self):
        for p in ea.planted_answers(_world()):
            if not p.expect_correct:
                norm = ea.normalise(p.answer.text)
                assert not any(ea.normalise(g) in norm for g in p.case.gold), p


class TestJudgeValidation:
    @pytest.mark.asyncio
    async def test_the_rule_judge_passes_validation(self):
        w = _world()
        result = await ea.validate_judge(w, ea.make_judge(w, llm=None))
        assert result.passed
        assert result.wrong_marked_wrong >= 0.95
        assert result.right_marked_right >= 0.95
        assert result.n_wrong > 0 and result.n_right > 0

    @pytest.mark.asyncio
    async def test_a_judge_that_accepts_everything_fails(self):
        async def lenient(case, answer):
            return ea.Verdict(True, False, False, "always", "rule")

        result = await ea.validate_judge(_world(), lenient)
        assert not result.passed
        assert result.wrong_marked_wrong < 0.95

    @pytest.mark.asyncio
    async def test_a_judge_that_rejects_everything_fails(self):
        async def harsh(case, answer):
            return ea.Verdict(False, False, True, "never", "rule")

        result = await ea.validate_judge(_world(), harsh)
        assert not result.passed and result.right_marked_right < 0.95

    @pytest.mark.asyncio
    async def test_require_valid_judge_aborts_with_a_clear_message(self):
        async def lenient(case, answer):
            return ea.Verdict(True, False, False, "always", "rule")

        with pytest.raises(EvalPreconditionError, match="judge failed validation"):
            await ea.require_valid_judge(_world(), lenient)


class TestCaseSelection:
    def test_limit_per_stratum_is_deterministic_and_bounded(self):
        w = _world()
        a = ea.select_cases(w, "all", limit_per_stratum=3)
        b = ea.select_cases(w, "all", limit_per_stratum=3)
        assert [c.id for c in a] == [c.id for c in b]
        counts: dict[str, int] = {}
        for c in a:
            counts[c.stratum] = counts.get(c.stratum, 0) + 1
        assert set(counts) == set(ew.STRATA)
        assert max(counts.values()) <= 3

    def test_split_is_respected(self):
        assert {c.split for c in ea.select_cases(_world(), "dev", None)} == {"dev"}

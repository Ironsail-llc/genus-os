"""Metrics summary, the deciding comparison, and the answer-arm runner wired
with fakes (no database, no model)."""

from __future__ import annotations

from typing import Any

import pytest

from robothor.constants import DEFAULT_TENANT
from robothor.memory import eval_answer as ea
from robothor.memory import eval_systems as es
from robothor.memory import eval_world as ew
from robothor.memory.eval import EvalPreconditionError


def _rec(system: str, case_id: str, stratum: str, correct: bool, **kw: Any) -> ea.CaseRecord:
    base: dict[str, Any] = {
        "system": system,
        "case_id": case_id,
        "stratum": stratum,
        "repeat": 0,
        "correct": correct,
        "abstained": False,
        "confident_wrong": not correct,
        "tokens": 100,
        "retrieval_ms": 10.0,
        "answer_ms": 100.0,
        "equal_budget": True,
        "judge_method": "rule",
    }
    base.update(kw)
    return ea.CaseRecord(**base)


class TestSummarise:
    def test_hand_computed_metrics(self):
        recs = [
            _rec("a", "m1", "multi_hop", True, tokens=100, retrieval_ms=10),
            _rec("a", "m2", "multi_hop", False, tokens=300, retrieval_ms=30),
            _rec(
                "a",
                "ab1",
                "abstention",
                True,
                abstained=True,
                confident_wrong=False,
                tokens=200,
                retrieval_ms=20,
            ),
            _rec(
                "a",
                "ab2",
                "abstention",
                False,
                abstained=False,
                confident_wrong=True,
                tokens=200,
                retrieval_ms=40,
            ),
            _rec(
                "a",
                "m3",
                "multi_hop",
                False,
                abstained=True,
                confident_wrong=False,
                tokens=200,
                retrieval_ms=50,
            ),
        ]
        out = ea.summarise(recs)["a"]
        mh = out["multi_hop"]
        assert mh["n"] == 3
        assert mh["accuracy"] == pytest.approx(1 / 3)
        assert mh["confident_wrong_rate"] == pytest.approx(1 / 3)
        assert mh["mean_tokens"] == pytest.approx(200.0)
        assert mh["retrieval_p50_ms"] == 30.0
        assert mh["retrieval_p95_ms"] == 50.0
        assert mh["wilson"][0] < mh["accuracy"] < mh["wilson"][1]
        pooled = out["all"]
        # abstained: ab1 (abstention) + m3 (answerable) → precision 1/2
        assert pooled["abstention_precision"] == pytest.approx(0.5)
        # abstention cases: ab1 abstained, ab2 not → recall 1/2
        assert pooled["abstention_recall"] == pytest.approx(0.5)
        assert pooled["n"] == 5
        assert out["hard"]["n"] == 5  # both strata are hard strata
        assert mh["mde"] == pytest.approx(
            __import__("robothor.memory.eval_stats", fromlist=["x"]).minimum_detectable_effect(
                3, p=1 / 3
            )
        )

    def test_spread_across_repeats(self):
        recs = [
            _rec("a", "c1", "raw_detail", True, repeat=0),
            _rec("a", "c2", "raw_detail", True, repeat=0),
            _rec("a", "c1", "raw_detail", True, repeat=1),
            _rec("a", "c2", "raw_detail", False, repeat=1),
            _rec("a", "c1", "raw_detail", False, repeat=2),
            _rec("a", "c2", "raw_detail", False, repeat=2),
        ]
        spread = ea.summarise(recs)["a"]["raw_detail"]["spread"]
        assert spread["runs"] == 3
        assert spread["min"] == 0.0 and spread["max"] == 1.0
        assert spread["range"] == pytest.approx(1.0)

    def test_deterministic(self):
        recs = [_rec("a", f"c{i}", "multi_hop", i % 2 == 0) for i in range(9)]
        assert ea.summarise(recs) == ea.summarise(list(recs))


class TestComparison:
    def test_deciding_comparison_uses_hard_strata_and_the_existing_verdict(self):
        recs = []
        for i in range(30):
            recs.append(_rec("ref", f"c{i}", "multi_hop", False))
            recs.append(_rec("sys", f"c{i}", "multi_hop", True))
        recs.append(_rec("ref", "x", "cross_tenant", True))
        recs.append(_rec("sys", "x", "cross_tenant", False))
        cmp_ = ea.compare(recs, system="sys", reference="ref")
        assert cmp_["strata"] == list(ea.HARD_STRATA)
        assert cmp_["paired"]["n"] == 30  # cross_tenant is not a hard stratum
        assert cmp_["paired"]["improved"] == 30
        assert cmp_["promote"] is True
        assert cmp_["non_inferiority"]["non_inferior"] is True

    def test_missing_reference_is_reported_not_crashed(self):
        cmp_ = ea.compare([_rec("sys", "c", "multi_hop", True)], system="sys", reference="ref")
        assert cmp_["paired"]["n"] == 0 and cmp_["promote"] is False


class _Echo:
    """A system that hands back the gold for half the cases."""

    name = "echo_run"
    equal_budget = True

    async def retrieve(self, case, *, world_handle, token_budget):
        keep = int(case.id.encode().hex(), 16) % 2 == 0
        text = f"Answer: {case.gold[0]}" if keep else "nothing useful"
        return es.ContextBundle(
            system=self.name,
            items=es.fill_budget([(case.id, "fact", text)], token_budget),
            budget=token_budget,
            equal_budget=True,
            abstained=False,
            retrieval_ms=1.0,
        )


class TestRunner:
    @pytest.fixture
    def fakes(self, monkeypatch):
        calls: dict[str, int] = {"seed": 0, "cleanup": 0}

        async def fake_seed(world, tenant, **kw):
            calls["seed"] += 1
            from robothor.memory.eval_world_seed import SeededWorld

            return SeededWorld(tenant_ids=(tenant,))

        def fake_cleanup(tenants):
            calls["cleanup"] += 1
            return {}

        async def fake_answer(question, bundle, *, as_of=None):
            text = bundle.items[0].text.removeprefix("Answer: ") if bundle.items else "unknown"
            if text == "nothing useful":
                text = "unknown"
            return ea.Answer(text=text, abstained=text == "unknown", cited_ids=())

        monkeypatch.setattr(ea, "seed_world", fake_seed)
        monkeypatch.setattr(ea, "cleanup_world", fake_cleanup)
        es.register_system("echo_run", _Echo)
        yield calls, fake_answer
        es.unregister_system("echo_run")

    @pytest.mark.asyncio
    async def test_every_stratum_scored_for_every_system_with_tokens_and_latency(self, fakes):
        calls, fake_answer = fakes
        report = await ea.run_answers(
            1,
            "small",
            "all",
            systems=["echo_run"],
            reference="echo_run",
            token_budget=200,
            repeats=3,
            limit_per_stratum=4,
            answer_fn=fake_answer,
            embed_batch=None,
        )
        strata = report["systems"]["echo_run"]
        for stratum in ew.STRATA:
            s = strata[stratum]
            assert s["n"] > 0
            assert "accuracy" in s and "mean_tokens" in s
            assert "retrieval_p50_ms" in s and "answer_p50_ms" in s
            assert s["spread"]["runs"] == 3
        assert report["judge_validation"]["passed"] is True
        assert report["params"]["token_budget"] == 200
        assert report["params"]["repeats"] == 3
        assert calls == {"seed": 1, "cleanup": 2}
        assert "comparison" in report
        md = ea.format_answers_markdown(report)
        assert "echo_run" in md and "multi_hop" in md and "|" in md

    @pytest.mark.asyncio
    async def test_abstention_answered_counts_as_confident_wrong(self, fakes):
        _, _ = fakes

        async def always_answers(question, bundle, *, as_of=None):
            return ea.Answer(text="Velmere", abstained=False, cited_ids=())

        report = await ea.run_answers(
            1,
            "small",
            "all",
            systems=["echo_run"],
            reference="echo_run",
            token_budget=200,
            limit_per_stratum=3,
            answer_fn=always_answers,
        )
        ab = report["systems"]["echo_run"]["abstention"]
        assert ab["accuracy"] == 0.0 and ab["confident_wrong_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_a_failing_judge_aborts_before_seeding(self, fakes, monkeypatch):
        calls, fake_answer = fakes

        def lenient_factory(world, *, llm):
            async def j(case, answer):
                return ea.Verdict(True, False, False, "always", "rule")

            return j

        monkeypatch.setattr(ea, "make_judge", lenient_factory)
        with pytest.raises(EvalPreconditionError, match="judge failed validation"):
            await ea.run_answers(
                1, "small", "all", systems=["echo_run"], answer_fn=fake_answer, reference="echo_run"
            )
        assert calls["seed"] == 0

    @pytest.mark.asyncio
    async def test_refuses_a_non_eval_tenant(self, fakes):
        _, fake_answer = fakes
        with pytest.raises(EvalPreconditionError):
            await ea.run_answers(
                1,
                "small",
                "all",
                systems=["echo_run"],
                tenant_id=DEFAULT_TENANT,
                answer_fn=fake_answer,
            )

    @pytest.mark.asyncio
    async def test_unknown_reference_is_refused_up_front(self, fakes):
        _, fake_answer = fakes
        with pytest.raises(ValueError, match="reference"):
            await ea.run_answers(
                1, "small", "all", systems=["echo_run"], reference="nope", answer_fn=fake_answer
            )

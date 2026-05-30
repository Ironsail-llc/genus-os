"""Tests for `robothor.memory.eval` — the memory recall eval harness.

These exercise the pure scoring/loading/reporting logic without a live
database or Ollama. The integration runner (`run_suite`) is tested with
the seed + retrieve steps patched out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory.eval import (
    CaseResult,
    EvalCase,
    format_report,
    load_suite,
    run_case,
    run_suite,
    score_case,
    score_recall,
    score_temporal,
    score_verbatim,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestScoreRecall:
    def test_hit_when_gold_substring_in_topk(self) -> None:
        top = ["Alice manages the Helios project at FakeVendorCo.", "Bob likes tea."]
        passed, score = score_recall(top, "Helios project", k=5)
        assert passed is True
        assert score == 1.0

    def test_miss_when_gold_absent(self) -> None:
        passed, score = score_recall(["Bob likes tea."], "Helios project", k=5)
        assert passed is False
        assert score == 0.0

    def test_case_insensitive(self) -> None:
        passed, _ = score_recall(["ALICE MANAGES HELIOS"], "helios", k=5)
        assert passed is True

    def test_respects_k_cutoff(self) -> None:
        top = ["miss1", "miss2", "Helios project"]
        # gold is at rank 3 (index 2); k=2 must not see it
        passed, _ = score_recall(top, "Helios project", k=2)
        assert passed is False

    def test_multiple_golds_all_required(self) -> None:
        top = ["Alice runs Helios", "Bob runs Athena"]
        passed, score = score_recall(top, ["Helios", "Athena"], k=5)
        assert passed is True
        assert score == 1.0
        passed2, score2 = score_recall(top, ["Helios", "Zeus"], k=5)
        assert passed2 is False
        assert score2 == 0.5


class TestScoreTemporal:
    def test_passes_only_when_latest_ranks_first(self) -> None:
        top = ["Alice switched storage to SQLite", "Alice chose Postgres"]
        passed, _ = score_temporal(top, "SQLite")
        assert passed is True

    def test_fails_when_stale_ranks_first(self) -> None:
        top = ["Alice chose Postgres", "Alice switched storage to SQLite"]
        passed, _ = score_temporal(top, "SQLite")
        assert passed is False

    def test_empty_results_fail(self) -> None:
        passed, score = score_temporal([], "SQLite")
        assert passed is False
        assert score == 0.0


class TestScoreVerbatim:
    def test_exact_case_sensitive_match_required(self) -> None:
        top = ["FakeVendorCo support line is 555-0142 ext 7."]
        passed, _ = score_verbatim(top, "555-0142 ext 7", k=5)
        assert passed is True

    def test_paraphrase_fails_verbatim(self) -> None:
        # The exact digits drifted — this is what the Knowledge Vault prevents.
        top = ["FakeVendorCo support line is 555 0142 extension seven."]
        passed, _ = score_verbatim(top, "555-0142 ext 7", k=5)
        assert passed is False

    def test_case_sensitive(self) -> None:
        passed, _ = score_verbatim(["sk-PROJ-ABC"], "sk-proj-abc", k=5)
        assert passed is False


class TestScoreCaseDispatch:
    def test_recall_kind(self) -> None:
        case = EvalCase(id="r1", kind="recall", query="q", gold="Helios", k=5)
        res = score_case(case, ["Alice runs Helios"])
        assert isinstance(res, CaseResult)
        assert res.passed is True
        assert res.kind == "recall"

    def test_verbatim_kind_uses_gold_exact(self) -> None:
        case = EvalCase(id="v1", kind="verbatim", query="q", gold_exact="555-0142", k=5)
        res = score_case(case, ["call 555-0142 now"])
        assert res.passed is True

    def test_persona_kind_scored_like_recall(self) -> None:
        case = EvalCase(id="p1", kind="persona", query="q", gold="async standups", k=3)
        res = score_case(case, ["Bob prefers async standups"])
        assert res.passed is True

    def test_unknown_kind_raises(self) -> None:
        case = EvalCase(id="x", kind="bogus", query="q", gold="z", k=5)
        with pytest.raises(ValueError):
            score_case(case, ["z"])


class TestLoadSuite:
    def test_loads_cases_and_meta(self, tmp_path: Path) -> None:
        suite = tmp_path / "suite.yaml"
        suite.write_text(
            """
id: memtest
description: tiny suite
k: 5
cases:
  - id: r1
    kind: recall
    query: "Who runs Helios?"
    gold: "Helios project"
    seed:
      - fact_text: "Alice manages the Helios project."
        category: project
        entities: [Alice, Helios]
  - id: v1
    kind: verbatim
    query: "support number"
    gold_exact: "555-0142"
    seed:
      - fact_text: "Support is 555-0142."
        category: contact
""",
            encoding="utf-8",
        )
        meta, cases = load_suite(suite)
        assert meta["id"] == "memtest"
        assert len(cases) == 2
        assert cases[0].kind == "recall"
        assert cases[0].k == 5  # inherits suite-level k
        assert cases[1].gold_exact == "555-0142"
        assert cases[0].seed[0]["fact_text"].startswith("Alice")

    def test_rejects_unknown_kind(self, tmp_path: Path) -> None:
        suite = tmp_path / "bad.yaml"
        suite.write_text(
            "id: x\ncases:\n  - id: c\n    kind: nope\n    query: q\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load_suite(suite)


class TestFormatReport:
    def test_text_report_has_totals_and_by_kind(self) -> None:
        report = {
            "suite_id": "memtest",
            "total": 2,
            "passed": 1,
            "by_kind": {"recall": {"passed": 1, "total": 1}, "verbatim": {"passed": 0, "total": 1}},
            "cases": [
                {"case_id": "r1", "kind": "recall", "passed": True, "score": 1.0, "detail": ""},
                {
                    "case_id": "v1",
                    "kind": "verbatim",
                    "passed": False,
                    "score": 0.0,
                    "detail": "miss",
                },
            ],
        }
        text = format_report(report)
        assert "memtest" in text
        assert "1/2" in text
        assert "recall" in text
        assert "verbatim" in text

    def test_json_report_roundtrips(self) -> None:
        import json

        report = {"suite_id": "x", "total": 0, "passed": 0, "by_kind": {}, "cases": []}
        out = format_report(report, as_json=True)
        assert json.loads(out)["suite_id"] == "x"


class TestRunCaseOrchestration:
    @pytest.mark.asyncio
    async def test_run_case_seeds_retrieves_and_scores(self) -> None:
        case = EvalCase(
            id="r1",
            kind="recall",
            query="Who runs Helios?",
            gold="Helios project",
            k=5,
            seed=[{"fact_text": "Alice manages the Helios project.", "category": "project"}],
        )
        with (
            patch("robothor.memory.eval._seed_case", new=AsyncMock(return_value=[1])) as seed,
            patch(
                "robothor.memory.eval._retrieve",
                new=AsyncMock(return_value=["Alice manages the Helios project."]),
            ) as retrieve,
        ):
            res = await run_case(case, tenant_id="memory-eval")

        seed.assert_awaited_once()
        retrieve.assert_awaited_once()
        assert res.passed is True
        assert res.case_id == "r1"

    @pytest.mark.asyncio
    async def test_run_suite_aggregates_and_cleans_up(self, tmp_path: Path) -> None:
        suite = tmp_path / "suite.yaml"
        suite.write_text(
            """
id: memtest
k: 5
cases:
  - id: r1
    kind: recall
    query: q
    gold: Helios
  - id: r2
    kind: recall
    query: q
    gold: Athena
""",
            encoding="utf-8",
        )

        async def fake_retrieve(case: EvalCase, tenant_id: str) -> list[str]:
            return ["Alice runs Helios"] if case.id == "r1" else ["nothing relevant"]

        with (
            patch("robothor.memory.eval._ensure_tenant") as ensure,
            patch("robothor.memory.eval._cleanup_tenant") as cleanup,
            patch("robothor.memory.eval._seed_case", new=AsyncMock(return_value=[])),
            patch("robothor.memory.eval._retrieve", new=fake_retrieve),
        ):
            report = await run_suite(suite, tenant_id="memory-eval")

        assert report["total"] == 2
        assert report["passed"] == 1
        assert report["by_kind"]["recall"]["total"] == 2
        ensure.assert_called_once()
        cleanup.assert_called_once()  # cleanup runs even with a partial pass

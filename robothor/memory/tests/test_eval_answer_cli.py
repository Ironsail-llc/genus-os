"""`memory-eval --world --answers`: flags, dispatch, report files, and the
existing modes left alone."""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.cli import _build_parser
from robothor.cli import memory_eval as cli


def _parse(*argv: str):
    return _build_parser().parse_args(["memory-eval", *argv])


_REPORT: dict[str, Any] = {
    "suite_id": "memory-world-answers-v1",
    "params": {"seed": 1, "size": "small", "split": "dev", "token_budget": 2048},
    "systems": {"recall": {"multi_hop": {"n": 2, "accuracy": 0.5}}},
    "judge_validation": {"passed": True},
    "comparison": {"promote": False},
}


class TestFlags:
    def test_answer_flags_parse(self):
        a = _parse(
            "--world",
            "--answers",
            "--systems",
            "recall,keyword_only",
            "--token-budget",
            "1024",
            "--repeats",
            "3",
            "--answer-model",
            "m-a",
            "--judge-model",
            "m-j",
            "--limit-per-stratum",
            "10",
            "--report",
            "/x/r.json",
            "--reference",
            "keyword_only",
            "--concurrency",
            "2",
        )
        assert a.answers is True
        assert a.systems == "recall,keyword_only"
        assert a.token_budget == 1024 and a.repeats == 3
        assert a.answer_model == "m-a" and a.judge_model == "m-j"
        assert a.limit_per_stratum == 10 and a.report == "/x/r.json"
        assert a.reference == "keyword_only" and a.concurrency == 2
        assert a.record_baseline is False

    def test_defaults(self):
        a = _parse("--world", "--answers")
        assert a.systems == "recall,keyword_only,raw_verbatim,full_context"
        assert a.token_budget == 2048 and a.repeats == 1
        assert a.answer_model is None and a.judge_model is None
        assert a.limit_per_stratum is None and a.report is None


class TestDispatch:
    @pytest.fixture
    def stubs(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_answers(seed, size, split, **kw):
            seen.update(seed=seed, size=size, split=split, **kw)
            return _REPORT

        async def no_world(*_a, **_k):
            raise AssertionError("retrieval-only world mode must not run with --answers")

        monkeypatch.setattr(cli, "run_answers", fake_answers)
        monkeypatch.setattr(cli, "run_world", no_world)
        monkeypatch.setattr(cli, "preflight", lambda _t: None)
        return seen

    def test_answers_mode_runs_the_answer_arm(self, stubs, capsys):
        rc = cli.cmd_memory_eval(
            _parse("--world", "--answers", "--systems", "recall,full_context", "--repeats", "2")
        )
        assert rc == 0
        assert stubs["systems"] == ["recall", "full_context"]
        assert stubs["repeats"] == 2 and stubs["token_budget"] == 2048
        assert stubs["tenant_id"] == "memory-eval-world"
        out = capsys.readouterr().out
        assert json.loads(out.split("\n<!-- markdown -->\n")[0])["suite_id"] == _REPORT["suite_id"]

    def test_report_path_gets_json_and_markdown(self, stubs, tmp_path):
        path = tmp_path / "r.json"
        cli.cmd_memory_eval(_parse("--world", "--answers", "--report", str(path)))
        assert json.loads(path.read_text())["suite_id"] == _REPORT["suite_id"]
        assert path.with_suffix(".md").read_text().strip()

    def test_record_baseline_writes_under_the_benchmark_dir(self, stubs, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "BASELINE_DIR", tmp_path)
        cli.cmd_memory_eval(_parse("--world", "--answers", "--record-baseline"))
        assert (tmp_path / "world-baseline.json").exists()
        assert (tmp_path / "world-baseline.md").exists()

    def test_world_without_answers_is_still_retrieval_scoring(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_world(seed, size, split, *, tenant_id, cleanup):
            seen["ran"] = True
            return {"suite_id": "w", "total": 1, "passed": 1, "by_kind": {}, "cases": []}

        async def no_answers(*_a, **_k):
            raise AssertionError("must not run")

        monkeypatch.setattr(cli, "run_world", fake_world)
        monkeypatch.setattr(cli, "run_answers", no_answers)
        monkeypatch.setattr(cli, "preflight", lambda _t: None)
        assert cli.cmd_memory_eval(_parse("--world")) == 0
        assert seen["ran"]

    def test_answers_without_world_is_refused(self, capsys):
        assert cli.cmd_memory_eval(_parse("--answers")) == 3
        assert "--world" in capsys.readouterr().out

    def test_bad_system_list_exits_three(self, stubs, capsys):
        assert (
            cli.cmd_memory_eval(_parse("--world", "--answers", "--systems", "recall,oracle")) == 3
        )

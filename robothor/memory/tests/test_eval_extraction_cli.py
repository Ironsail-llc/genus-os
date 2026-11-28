"""`memory-eval --extraction`: flags, dispatch, report files, and no database."""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.cli import _build_parser
from robothor.cli import memory_eval as cli
from robothor.memory.eval import EvalPreconditionError


def _parse(*argv: str):
    return _build_parser().parse_args(["memory-eval", *argv])


_REPORT: dict[str, Any] = {
    "suite_id": "conversation-facts-v1",
    "model": "m-x",
    "judge_model": "m-j",
    "conversations": 1,
    "repeats": 1,
    "metrics": {"precision": 1.0, "recall": 1.0},
    "by_category": {},
    "judge_validation": {"passed": True, "by_kind": {}},
    "per_repeat": [],
    "failures": [],
}


def test_extraction_flags_parse() -> None:
    a = _parse(
        "--extraction",
        "--fixtures",
        "/x/set.yaml",
        "--extraction-model",
        "m-x",
        "--judge-model",
        "m-j",
        "--repeats",
        "2",
        "--concurrency",
        "1",
        "--report",
        "/x/r.json",
    )
    assert a.extraction is True and a.fixtures == "/x/set.yaml"
    assert a.extraction_model == "m-x" and a.judge_model == "m-j"
    assert a.repeats == 2 and a.concurrency == 1 and a.report == "/x/r.json"


def test_extraction_defaults() -> None:
    a = _parse("--extraction")
    assert a.fixtures == "docs/benchmarks/memory/conversation-facts.yaml"
    assert a.extraction_model is None


@pytest.fixture
def stubs(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_run(path, **kw):
        seen.update(path=str(path), **kw)
        return _REPORT

    def no_db(_tenant):
        raise AssertionError("the extraction measurement needs no database")

    monkeypatch.setattr(cli, "run_extraction_eval", fake_run)
    monkeypatch.setattr(cli, "preflight", no_db)
    # The summary's own content is the harness's test; here only that it is written.
    monkeypatch.setattr(cli, "format_extraction_markdown", lambda r: f"## {r['model']}\n")
    return seen


def test_extraction_mode_runs_the_harness_without_a_database(stubs, capsys, tmp_path) -> None:
    fixtures = tmp_path / "set.yaml"
    fixtures.write_text("version: 1\nconversations: []\n")
    rc = cli.cmd_memory_eval(
        _parse(
            "--extraction",
            "--fixtures",
            str(fixtures),
            "--extraction-model",
            "m-x",
            "--judge-model",
            "m-j",
            "--repeats",
            "2",
        )
    )
    assert rc == 0
    assert stubs["path"] == str(fixtures)
    assert stubs["model"] == "m-x" and stubs["judge_model"] == "m-j"
    assert stubs["repeats"] == 2
    out = capsys.readouterr().out
    assert json.loads(out.split("\n<!-- markdown -->\n")[0])["suite_id"] == "conversation-facts-v1"


def test_report_path_gets_json_and_markdown(stubs, tmp_path) -> None:
    fixtures = tmp_path / "set.yaml"
    fixtures.write_text("version: 1\nconversations: []\n")
    path = tmp_path / "out" / "r.json"
    rc = cli.cmd_memory_eval(
        _parse("--extraction", "--fixtures", str(fixtures), "--report", str(path))
    )
    assert rc == 0
    assert json.loads(path.read_text())["model"] == "m-x"
    assert "m-x" in path.with_suffix(".md").read_text()


def test_missing_fixtures_exit_three(stubs, capsys, tmp_path) -> None:
    rc = cli.cmd_memory_eval(_parse("--extraction", "--fixtures", str(tmp_path / "none.yaml")))
    assert rc == 3
    assert "not found" in capsys.readouterr().out


def test_a_failed_judge_or_bad_labels_exit_three(monkeypatch, capsys, tmp_path) -> None:
    fixtures = tmp_path / "set.yaml"
    fixtures.write_text("version: 1\nconversations: []\n")

    async def refuse(path, **kw):
        raise EvalPreconditionError("judge failed validation: 50%")

    monkeypatch.setattr(cli, "run_extraction_eval", refuse)
    assert cli.cmd_memory_eval(_parse("--extraction", "--fixtures", str(fixtures))) == 3
    assert "judge failed validation" in capsys.readouterr().out

    async def bad(path, **kw):
        raise ValueError("pref-a#1: unknown trust")

    monkeypatch.setattr(cli, "run_extraction_eval", bad)
    assert cli.cmd_memory_eval(_parse("--extraction", "--fixtures", str(fixtures))) == 3


def test_extraction_with_world_is_refused(capsys) -> None:
    assert cli.cmd_memory_eval(_parse("--extraction", "--world")) == 3
    assert "--extraction" in capsys.readouterr().out

"""A failed live conversation must stay in the cohort, with its partial artifact."""

import json
from types import SimpleNamespace

import pytest
import yaml

from bench.runtime.chat_cohort import collect, main
from bench.runtime.native_journal import NativeJournal


def test_model_settings_are_frozen_before_first_conversation(tmp_path, monkeypatch):
    from pathlib import Path

    manifest = tmp_path / "main.yaml"
    selected = {
        "primary": "openrouter/selected/model",
        "fallbacks": ["openrouter/selected/fallback"],
        "temperature": 0.5,
    }
    manifest.write_text(yaml.safe_dump({"model": selected}))
    observed = []

    def invoke(command, **kwargs):
        settings = json.loads(kwargs["env"]["ROBOTHOR_RUNTIME_CHAT_LIVE"])
        observed.append(yaml.safe_load(Path(settings["manifest"]).read_text())["model"])
        manifest.write_text("model:\n  primary: openrouter/different/model\n")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("bench.runtime.chat_cohort.subprocess.run", invoke)
    directory = collect(manifest, tmp_path / "cohort")
    assert len(observed) == 30 and all(model == selected for model in observed)
    assert yaml.safe_load((directory / "model-settings.yaml").read_text()) == {"model": selected}


@pytest.mark.parametrize("factual_report", [False, True])
def test_failed_conversation_does_not_disappear_or_stop_later_samples(
    tmp_path, monkeypatch, factual_report
):
    manifest = tmp_path / "main.yaml"
    manifest.write_text("model:\n  primary: openrouter/selected/model\n")
    calls = []

    def invoke(command, **kwargs):
        settings = json.loads(kwargs["env"]["ROBOTHOR_RUNTIME_CHAT_LIVE"])
        assert settings["factual_report"] is factual_report
        from pathlib import Path

        path = Path(settings["output"])
        calls.append(path)
        if len(calls) == 1:
            path.write_text('{"transcript":[{"run":{"status":"timeout"}}]}')
            return SimpleNamespace(returncode=1)
        path.write_text('{"transcript":[{},{}]}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("bench.runtime.chat_cohort.subprocess.run", invoke)
    directory = collect(manifest, tmp_path / "cohort", factual_report=factual_report)
    assert json.loads((directory / "scenario.json").read_text()) == {
        "samples": 30,
        "factual_report": factual_report,
    }
    events = [
        json.loads(line) for line in (directory / "cohort.events.jsonl").read_text().splitlines()
    ]
    finished = [event for event in events if event["event"] == "sample_finished"]
    assert len(calls) == len(finished) == 30
    assert finished[0]["exit_code"] == 1 and finished[0]["artifact"] == "sample-000.json"
    assert all(event["exit_code"] == 0 for event in finished[1:])
    assert json.loads(calls[0].read_text())["transcript"][0]["run"]["status"] == "timeout"
    with pytest.raises(FileExistsError):
        collect(manifest, directory)
    assert len(calls) == 30


@pytest.mark.parametrize("case", ["passed", "child_failed", "missing_turn", "slow", "deadline"])
def test_command_reports_screening_failure_from_retained_evidence(tmp_path, monkeypatch, case):
    directory = tmp_path / "cohort"
    directory.mkdir()
    journal = NativeJournal(directory / "cohort.jsonl")
    for index in range(30):
        journal.record("sample_started", "selected", index)
        duration = 31000 if case == "slow" else 1000
        if case == "deadline" and index == 0:
            duration = 61000  # A single overrun can be hidden by p95.
        turns = [{"elapsed_ms": duration, "run": {"status": "completed"}}] * 2
        if case == "missing_turn" and index == 0:
            turns.pop()
        artifact = f"sample-{index:03d}.json"
        (directory / artifact).write_text(
            json.dumps(
                {"transcript": turns, "goal_status": "paused", "task_statuses": ["DONE", "TODO"]}
            )
        )
        journal.record(
            "sample_finished",
            "selected",
            index,
            exit_code=int(case == "child_failed" and index == 0),
            artifact=artifact,
        )
    monkeypatch.setattr("bench.runtime.chat_cohort.collect", lambda *args, **kwargs: directory)
    monkeypatch.setattr(
        "sys.argv",
        ["chat_cohort", "--manifest", "selected.yaml", "--output-directory", str(directory)],
    )
    assert main() == (0 if case == "passed" else 1)
    result = json.loads((directory / "summary.json").read_text())
    assert result["screening_passed"] is (case == "passed")
    assert result["manual_acceptance"] is False

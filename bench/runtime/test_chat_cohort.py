"""A failed live conversation must stay in the cohort, with its partial artifact."""

import json
from types import SimpleNamespace

import pytest

from bench.runtime.chat_cohort import collect


def test_failed_conversation_does_not_disappear_or_stop_later_samples(tmp_path, monkeypatch):
    manifest = tmp_path / "main.yaml"
    manifest.write_text("model:\n  primary: openrouter/selected/model\n")
    calls = []

    def invoke(command, **kwargs):
        settings = json.loads(kwargs["env"]["ROBOTHOR_RUNTIME_CHAT_LIVE"])
        from pathlib import Path

        path = Path(settings["output"])
        calls.append(path)
        if len(calls) == 1:
            path.write_text('{"transcript":[{"run":{"status":"timeout"}}]}')
            return SimpleNamespace(returncode=1)
        path.write_text('{"transcript":[{},{}]}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("bench.runtime.chat_cohort.subprocess.run", invoke)
    directory = collect(manifest, tmp_path / "cohort")
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

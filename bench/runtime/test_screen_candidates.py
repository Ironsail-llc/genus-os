"""Screening must preserve failures and independently check fixture outcomes."""

import pytest

from bench.runtime import screen_candidates


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["unverified", "no_write", "extra_dispatch", "timeout"])
async def test_screen_does_not_count_returned_or_failed_work_as_success(monkeypatch, behavior):
    class Candidate:
        def __init__(self, model):
            pass

        async def run(self, gateway, tenant):
            if behavior == "timeout":
                raise TimeoutError("Synthetic timeout")
            if behavior != "no_write":
                await gateway.dispatch(tenant, key="report", value="delivered")
            if behavior == "extra_dispatch":
                await gateway.dispatch(tenant, key="report", value="delivered")
            return {"verified": behavior != "unverified", "duration_ms": 1, "model_calls": 1}

    for name in ["PydanticCandidate", "DeepAgentsCandidate"]:
        monkeypatch.setattr(screen_candidates, name, Candidate)
    for name in ["pydantic_model", "deep_model"]:
        monkeypatch.setattr(screen_candidates, name, lambda: None)
    monkeypatch.setattr(screen_candidates.importlib.metadata, "version", lambda name: "fixture")
    events = []
    report = await screen_candidates.screen(2, record_event=events.append)
    finished = [event["sample"] for event in events if event["event"] == "sample_finished"]
    assert len(finished) == 6
    assert all(row["status"] != "completed" for row in finished)
    assert not report["correctness_passed"]
    assert len(report["samples"]) == 4
    assert len(report["warmups"]) == 2
    for row in report["samples"] + report["warmups"]:
        assert row["status"] == ("timeout" if behavior == "timeout" else "failed")
        assert row["execution_ms"] >= 0
        assert "dispatches" in row and "writes" in row


@pytest.mark.asyncio
async def test_verified_screen_retains_warmups_separately(monkeypatch):
    class Candidate:
        def __init__(self, model):
            pass

        async def run(self, gateway, tenant):
            await gateway.dispatch(tenant, key="report", value="delivered")
            return {"verified": True, "duration_ms": 1, "model_calls": 1}

    for name in ["PydanticCandidate", "DeepAgentsCandidate"]:
        monkeypatch.setattr(screen_candidates, name, Candidate)
    for name in ["pydantic_model", "deep_model"]:
        monkeypatch.setattr(screen_candidates, name, lambda: None)
    monkeypatch.setattr(screen_candidates.importlib.metadata, "version", lambda name: "fixture")
    report = await screen_candidates.screen(2)
    assert report["correctness_passed"]
    assert len(report["samples"]) == 4 and len(report["warmups"]) == 2
    assert all(row["warmup"] and row["repetition"] == 0 for row in report["warmups"])
    assert all(not row["warmup"] and row["repetition"] > 0 for row in report["samples"])


def test_cli_preserves_failure_report_and_fails_the_screen(monkeypatch, tmp_path):
    import json
    import sys

    output = tmp_path / "screen.json"
    report = {
        "samples": [],
        "warmups": [{"runtime": "pydantic-ai", "status": "failed"}],
        "correctness_passed": False,
    }

    async def screen(samples, record_event=None):
        assert samples == 30
        return report

    monkeypatch.setattr(screen_candidates, "screen", screen)
    monkeypatch.setattr(sys, "argv", ["screen", "--output", str(output)])
    with pytest.raises(SystemExit) as stopped:
        screen_candidates.main()
    assert stopped.value.code == 1
    assert json.loads(output.read_text()) == report
    with pytest.raises(SystemExit) as overwrite:
        screen_candidates.main()
    assert overwrite.value.code == 2
    assert json.loads(output.read_text()) == report

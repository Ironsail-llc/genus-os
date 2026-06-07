"""Goal-judge spine (Wave-2, W2-22)."""

from __future__ import annotations

import pytest

from robothor.engine import judge


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_JUDGE_ENABLED", "1")
    yield


def test_evidence_bundle_is_deterministic():
    run = {"agent_id": "a", "status": "completed", "output_text": "did the thing"}
    b1 = judge.build_evidence_bundle(run)
    b2 = judge.build_evidence_bundle(run)
    assert b1 == b2
    assert "did the thing" in b1


def test_parse_clamps_and_extracts():
    v = judge.parse_judge_response(
        '{"goal_achievement": 9, "rationale": "ok", "safety_regression": true}'
    )
    assert v["goal_achievement"] == 5  # clamped to 1-5
    assert v["safety_regression"] is True


def test_parse_bad_json_returns_none():
    assert judge.parse_judge_response("not json") is None


async def test_disabled_skips(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_JUDGE_ENABLED", raising=False)
    assert await judge.judge_run("r1", run_loader=lambda rid: {"agent_id": "a"}) is None


async def test_never_self_grades():
    async def _llm(s, u):
        raise AssertionError("should not call the LLM for a self-grade")

    out = await judge.judge_run(
        "r1", judge_agent_id="judge", run_loader=lambda rid: {"agent_id": "judge"}, llm=_llm
    )
    assert out is None


async def test_grades_and_writes():
    captured = {}

    async def _llm(system, user):
        assert "JUDGE" in system
        return '{"goal_achievement": 4, "rationale": "mostly", "safety_regression": false}'

    def _writer(run_id, agent_id, verdict):
        captured.update({"run_id": run_id, "agent_id": agent_id, **verdict})

    out = await judge.judge_run(
        "r1",
        run_loader=lambda rid: {"agent_id": "worker", "output_text": "x"},
        llm=_llm,
        writer=_writer,
    )
    assert out["goal_achievement"] == 4
    assert captured["agent_id"] == "worker"
    assert captured["run_id"] == "r1"

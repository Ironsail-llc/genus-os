"""Goal-judge spine (Wave-2, W2-22)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


# The real agent_reviews columns (031 + migration 080). The old INSERT used
# 'dimension'/'specific_issue' (nonexistent) and omitted NOT NULL agent_id/
# reviewer, so every judge verdict was silently dropped.
_REAL_COLUMNS = {
    "id",
    "tenant_id",
    "agent_id",
    "run_id",
    "reviewer",
    "reviewer_type",
    "rating",
    "categories",
    "feedback",
    "action_items",
    "created_at",
}
_PHANTOM_COLUMNS = {"dimension", "specific_issue"}


def test_write_review_uses_real_schema_columns():
    """_write_review must INSERT only columns that exist and supply the NOT NULL
    ones (agent_id, reviewer); reviewer_type must be 'judge'."""
    executed = {}
    cur = MagicMock()

    def _execute(sql, params=None):
        executed["sql"] = sql
        executed["params"] = params

    cur.execute.side_effect = _execute
    conn = MagicMock()
    conn.cursor.return_value = cur

    class _CM:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    with patch("robothor.db.connection.get_connection", return_value=_CM()):
        judge._write_review(
            "run-1",
            "worker",
            {"goal_achievement": 4, "rationale": "ok", "safety_regression": False},
        )

    sql = executed["sql"]
    # Only real columns are referenced between the parentheses of the INSERT.
    col_blob = sql.split("(", 1)[1].split(")", 1)[0]
    used = {c.strip() for c in col_blob.split(",")}
    assert used <= _REAL_COLUMNS, f"INSERT references unknown columns: {used - _REAL_COLUMNS}"
    assert not (used & _PHANTOM_COLUMNS), "INSERT still references phantom columns"
    assert {"agent_id", "reviewer", "reviewer_type", "rating"} <= used  # NOT NULLs supplied
    assert "'judge'" in sql  # reviewer_type='judge'
    conn.commit.assert_called_once()  # actually persisted

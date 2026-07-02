"""Test the accretion ledger's reward-hack divergence tripwire (Phase 4c)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from robothor.engine.tools.handlers.observability import _get_accretion_ledger


class _Cur:
    def __init__(self, judge_count, judged_agents):
        self._results = [(judge_count,), None]
        self._agents = judged_agents
        self._mode = None

    def execute(self, sql, params=None):
        self._mode = "count" if "COUNT(*)" in sql else "agents"

    def fetchone(self):
        return (5,)

    def fetchall(self):
        return [(a,) for a in self._agents]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, agents):
        self._agents = agents

    def cursor(self):
        return _Cur(5, self._agents)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Ctx:
    tenant_id = "robothor-primary"


def test_divergence_flags_benchmark_vs_judge_gap():
    metrics = {
        "crm-dedup": {"benchmark_pass_rate": 1.0, "goal_achievement": 0.0},  # gap 1.0
        "main": {"benchmark_pass_rate": 1.0, "goal_achievement": 0.9},  # gap 0.1 (below)
        "email-analyst": {"benchmark_pass_rate": 0.9, "goal_achievement": 0.5},  # gap 0.4
    }
    with (
        patch("robothor.engine.skills.load_skills", return_value={}),
        patch("robothor.db.connection.get_connection", return_value=_Conn(list(metrics))),
        patch("subprocess.run") as mock_run,
        patch(
            "robothor.engine.goals.compute_goal_metrics",
            side_effect=lambda a, **k: metrics[a],
        ),
    ):
        mock_run.return_value.stdout = ""
        result = asyncio.run(_get_accretion_ledger({}, _Ctx()))

    agents = [d["agent_id"] for d in result["divergent"]]
    assert agents == ["crm-dedup", "email-analyst"]  # sorted by gap desc; main excluded
    assert result["divergent"][0]["gap"] == 1.0
    assert result["judge_rows_7d"] == 5

"""Skill-accretion gate + ledger (Wave-2, W2-24)."""

from __future__ import annotations

import json

from robothor.engine import accretion
from robothor.engine.tools.schemas import get_engine_schemas


class TestTwoKeyGate:
    def test_promotes_when_clean_and_at_baseline(self):
        ok, reason = accretion.accretion_gate(
            has_safety_regression=False, judge_score=4.0, baseline_score=4.0
        )
        assert ok is True
        assert "promoted" in reason

    def test_blocks_on_safety_regression(self):
        ok, reason = accretion.accretion_gate(
            has_safety_regression=True, judge_score=5.0, baseline_score=1.0
        )
        assert ok is False
        assert "safety regression" in reason

    def test_blocks_below_baseline(self):
        ok, reason = accretion.accretion_gate(
            has_safety_regression=False, judge_score=3.0, baseline_score=4.0
        )
        assert ok is False
        assert "below baseline" in reason


class TestLedger:
    def test_ledger_lists_only_agent_authored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        sk = tmp_path / "agents" / "skills"
        (sk / "agent-skill").mkdir(parents=True)
        (sk / "agent-skill" / "meta.json").write_text(
            json.dumps({"created_by": "auto-agent", "usage_count": 3})
        )
        (sk / "human-skill").mkdir(parents=True)
        (sk / "human-skill" / "meta.json").write_text(json.dumps({"created_by": "operator"}))

        led = accretion.get_accretion_ledger()
        names = {e["skill"] for e in led["skills"]}
        assert "agent-skill" in names
        assert "human-skill" not in names  # operator-authored excluded
        # Legacy runtime keys still inside meta.json remain readable.
        by_name = {e["skill"]: e for e in led["skills"]}
        assert by_name["agent-skill"]["usage_count"] == 3

    def test_ledger_prefers_state_sidecar(self, tmp_path, monkeypatch):
        """usage telemetry comes from state.json when present (sidecar wins)."""
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        sk = tmp_path / "agents" / "skills"
        (sk / "agent-skill").mkdir(parents=True)
        (sk / "agent-skill" / "meta.json").write_text(
            json.dumps({"created_by": "auto-agent", "usage_count": 3})
        )
        (sk / "agent-skill" / "state.json").write_text(
            json.dumps({"usage_count": 11, "last_used": "2026-08-19T00:00:00+00:00"})
        )

        led = accretion.get_accretion_ledger()
        by_name = {e["skill"]: e for e in led["skills"]}
        assert by_name["agent-skill"]["usage_count"] == 11
        assert by_name["agent-skill"]["last_used"] == "2026-08-19T00:00:00+00:00"


def test_ledger_tool_registered():
    assert "get_accretion_ledger" in get_engine_schemas()

"""Tests for the goal-judge (self-improvement Phase 1).

Focus on the pure, deterministic core — evidence assembly, prompt rendering,
evidence-or-abstain parsing, and operator-anchored clamping — plus mocked tests
for the LLM call and the orchestration pass. No DB or live LLM required.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.judge import (
    Judgment,
    RunDigest,
    assemble_evidence_bundle,
    clamp_operator_satisfaction,
    judge_agent_run,
    parse_judgment,
    render_bundle_prompt,
    run_judgment_pass,
)


def _digest(run_id="run-1", status="completed", output="did the thing") -> RunDigest:
    return RunDigest(
        run_id=run_id, status=status, output_excerpt=output, tool_calls=3, tool_errors=0
    )


# ─── assemble_evidence_bundle ───────────────────────────────────────


class TestAssembleEvidenceBundle:
    def test_extracts_objective_and_criteria(self):
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={
                "objective": "drive the queue to zero",
                "success_criteria": ["no items in YOUR CALL", "operator not pinged"],
            },
        )
        assert b.objective == "drive the queue to zero"
        assert b.success_criteria == ["no items in YOUR CALL", "operator not pinged"]

    def test_tolerates_missing_or_malformed_meta(self):
        b = assemble_evidence_bundle(agent_id="main", run=_digest(), session_goal_meta=None)
        assert b.objective is None
        assert b.success_criteria == []
        b2 = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={"success_criteria": "oops-not-a-list"},
        )
        assert b2.success_criteria == []

    def test_truncates_operator_messages(self):
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            operator_messages=["x" * 5000] + [f"m{i}" for i in range(40)],
        )
        assert len(b.operator_messages) <= 12
        assert all(len(m) <= 600 for m in b.operator_messages)

    def test_includes_goal_evidence_when_present(self):
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={
                "objective": "ship it",
                "success_criteria": ["works"],
                "evidence": [
                    {
                        "kind": "test_run",
                        "reference": "pytest:passed:10",
                        "valid": True,
                    },
                    {
                        "kind": "commit",
                        "reference": "deadbeef",
                        "valid": False,
                    },
                ],
            },
        )
        assert b.goal_evidence == [
            {"kind": "test_run", "reference": "pytest:passed:10", "valid": True},
            {"kind": "commit", "reference": "deadbeef", "valid": False},
        ]

    def test_goal_evidence_empty_when_no_meta(self):
        b = assemble_evidence_bundle(agent_id="main", run=_digest(), session_goal_meta=None)
        assert b.goal_evidence == []

    def test_goal_evidence_tolerates_malformed_entries(self):
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={"evidence": ["not-a-dict", {"kind": "note"}, None]},
        )
        # Only the well-formed dict entry survives. A missing `valid` key must
        # NOT be assumed valid — default to False (fail closed).
        assert b.goal_evidence == [{"kind": "note", "reference": "", "valid": False}]

    def test_goal_evidence_missing_valid_key_defaults_false(self):
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={"evidence": [{"kind": "test_run", "reference": "pytest:passed:1"}]},
        )
        assert b.goal_evidence == [
            {"kind": "test_run", "reference": "pytest:passed:1", "valid": False}
        ]

    def test_goal_evidence_truncated_to_ten(self):
        evidence = [{"kind": "note", "reference": str(i), "valid": True} for i in range(20)]
        b = assemble_evidence_bundle(
            agent_id="main", run=_digest(), session_goal_meta={"evidence": evidence}
        )
        assert len(b.goal_evidence) == 10


# ─── render_bundle_prompt ───────────────────────────────────────────


class TestRenderBundlePrompt:
    def test_includes_goal_run_and_rubric(self):
        b = assemble_evidence_bundle(
            agent_id="email-analyst",
            run=_digest(output="classified 4 emails"),
            session_goal_meta={"objective": "triage inbound", "success_criteria": ["all routed"]},
        )
        text = render_bundle_prompt(b)
        assert "triage inbound" in text
        assert "classified 4 emails" in text
        assert "goal_achievement" in text  # rubric present
        assert "email-analyst" in text

    def test_shows_operator_verdict_when_present(self):
        b = assemble_evidence_bundle(agent_id="main", run=_digest(), operator_verdict=-2)
        assert "operator verdict" in render_bundle_prompt(b).lower()

    def test_includes_goal_evidence_section_when_present(self):
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={
                "objective": "ship it",
                "evidence": [
                    {"kind": "test_run", "reference": "pytest:passed:10", "valid": True},
                    {"kind": "commit", "reference": "deadbeef", "valid": False},
                ],
            },
        )
        text = render_bundle_prompt(b)
        assert "Goal evidence" in text
        assert "test_run" in text
        assert "pytest:passed:10" in text
        assert "deadbeef" in text
        assert "invalid" in text.lower()  # the unvalidated commit is flagged

    def test_no_goal_evidence_section_when_absent(self):
        b = assemble_evidence_bundle(agent_id="main", run=_digest())
        text = render_bundle_prompt(b)
        assert "Goal evidence" not in text

    def test_note_evidence_tagged_unverified_not_valid(self):
        # `note` evidence is self-reported and never independently verified
        # (session_goal.validate_evidence accepts any note with a summary) —
        # the judge prompt must not present it as validated.
        b = assemble_evidence_bundle(
            agent_id="main",
            run=_digest(),
            session_goal_meta={
                "objective": "ship it",
                "evidence": [
                    {"kind": "note", "reference": "left a note", "valid": True},
                    {"kind": "test_run", "reference": "pytest:passed:10", "valid": True},
                ],
            },
        )
        text = render_bundle_prompt(b)
        assert "note: left a note [unverified]" in text
        assert "note: left a note [valid]" not in text
        # Genuinely-validated kinds are unaffected.
        assert "test_run: pytest:passed:10 [valid]" in text

    def test_judges_against_role_and_trigger_when_no_objective(self):
        run = RunDigest(
            run_id="run-1",
            status="completed",
            output_excerpt="routed 4 emails",
            tool_calls=4,
            tool_errors=0,
            trigger_type="cron",
            trigger_detail="email-sweep",
        )
        b = assemble_evidence_bundle(
            agent_id="email-analyst", run=run, role="Analyze and route inbound email."
        )
        text = render_bundle_prompt(b)
        assert "Analyze and route inbound email." in text
        assert "email-sweep" in text
        assert "no operator-declared objective" in text


# ─── parse_judgment (evidence-or-abstain) ───────────────────────────


class TestParseJudgment:
    def test_parses_well_formed_judgment(self):
        raw = json.dumps(
            {
                "goal_achievement": 4,
                "operator_satisfaction": None,
                "obstacles_handled": 5,
                "honesty": 4,
                "confidence": 0.8,
                "evidence_refs": ["run-1", "step:tool_error"],
                "feedback": "solid",
            }
        )
        j = parse_judgment(raw, run_id="run-1")
        assert j is not None
        assert j.goal_achievement == 4
        assert j.confidence == pytest.approx(0.8)
        assert j.operator_satisfaction is None
        assert j.honesty == 4

    def test_abstains_when_no_evidence_refs(self):
        """A goal_achievement claim with no citations is a hallucination risk."""
        raw = json.dumps({"goal_achievement": 5, "confidence": 0.9, "evidence_refs": []})
        assert parse_judgment(raw, run_id="run-1") is None

    def test_abstains_when_goal_achievement_null(self):
        raw = json.dumps({"goal_achievement": None, "evidence_refs": ["run-1"]})
        assert parse_judgment(raw, run_id="run-1") is None

    def test_abstains_on_unparseable_json(self):
        assert parse_judgment("not json{", run_id="run-1") is None

    def test_rejects_out_of_range_goal_rating(self):
        raw = json.dumps({"goal_achievement": 9, "evidence_refs": ["run-1"]})
        assert parse_judgment(raw, run_id="run-1") is None

    def test_clamps_confidence_and_defaults_on_garbage(self):
        raw = json.dumps({"goal_achievement": 3, "evidence_refs": ["run-1"], "confidence": "high"})
        j = parse_judgment(raw, run_id="run-1")
        assert j is not None
        assert j.confidence == 0.5

    def test_accepts_dict_input(self):
        j = parse_judgment(
            {"goal_achievement": 2, "evidence_refs": ["run-1"], "confidence": 0.3}, run_id="run-1"
        )
        assert j is not None and j.goal_achievement == 2

    def test_rejects_boolean_ratings(self):
        """BUG-8: bool is an int subclass — True must NOT become rating 1."""
        raw = json.dumps({"goal_achievement": True, "confidence": True, "evidence_refs": ["run-1"]})
        assert parse_judgment(raw, run_id="run-1") is None

    def test_strips_markdown_code_fences(self):
        """openrouter/anthropic wraps JSON in ```json fences despite json_mode —
        a bare json.loads would make every real judgment abstain."""
        raw = '```json\n{"goal_achievement": 4, "confidence": 0.7, "evidence_refs": ["run-1"]}\n```'
        j = parse_judgment(raw, run_id="run-1")
        assert j is not None
        assert j.goal_achievement == 4

    def test_extracts_object_from_surrounding_prose(self):
        raw = 'Here is my verdict:\n{"goal_achievement": 3, "evidence_refs": ["run-1"]}\nDone.'
        j = parse_judgment(raw, run_id="run-1")
        assert j is not None and j.goal_achievement == 3


# ─── clamp_operator_satisfaction ────────────────────────────────────


class TestClampOperatorSatisfaction:
    def _j(self, sat):
        return Judgment(
            run_id="r",
            goal_achievement=4,
            confidence=0.8,
            evidence_refs=["r"],
            operator_satisfaction=sat,
        )

    def test_noop_without_verdict(self):
        j = clamp_operator_satisfaction(self._j(5), None)
        assert j.operator_satisfaction == 5

    def test_angry_verdict_caps_satisfaction(self):
        j = clamp_operator_satisfaction(self._j(5), -2)
        assert j.operator_satisfaction == 2

    def test_angry_verdict_sets_when_inferred_none(self):
        j = clamp_operator_satisfaction(self._j(None), -1)
        assert j.operator_satisfaction == 2

    def test_positive_verdict_floors_satisfaction(self):
        j = clamp_operator_satisfaction(self._j(1), 2)
        assert j.operator_satisfaction == 4


# ─── to_categories ──────────────────────────────────────────────────


def test_to_categories_carries_dimension_and_confidence():
    j = Judgment(run_id="r", goal_achievement=4, confidence=0.77, evidence_refs=["r"], honesty=3)
    cats = j.to_categories()
    assert cats["dimension"] == "goal_achievement"
    assert cats["confidence"] == pytest.approx(0.77)
    assert cats["honesty"] == 3


# ─── judge_agent_run (mocked LLM) ───────────────────────────────────


class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class TestJudgeAgentRun:
    @pytest.mark.asyncio
    async def test_returns_parsed_judgment(self):
        bundle = assemble_evidence_bundle(agent_id="main", run=_digest())
        payload = json.dumps({"goal_achievement": 4, "confidence": 0.8, "evidence_refs": ["run-1"]})
        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=_FakeResp(payload),
        ):
            j = await judge_agent_run(bundle)
        assert j is not None and j.goal_achievement == 4

    @pytest.mark.asyncio
    async def test_abstains_on_llm_error(self):
        bundle = assemble_evidence_bundle(agent_id="main", run=_digest())
        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            assert await judge_agent_run(bundle) is None

    @pytest.mark.asyncio
    async def test_applies_operator_clamp(self):
        bundle = assemble_evidence_bundle(agent_id="main", run=_digest(), operator_verdict=-2)
        payload = json.dumps(
            {
                "goal_achievement": 4,
                "operator_satisfaction": 5,
                "confidence": 0.8,
                "evidence_refs": ["run-1"],
            }
        )
        with patch(
            "robothor.engine.llm_client.llm_call",
            new_callable=AsyncMock,
            return_value=_FakeResp(payload),
        ):
            j = await judge_agent_run(bundle)
        assert j is not None and j.operator_satisfaction == 2


# ─── run_judgment_pass (flag gate) ──────────────────────────────────


class TestRunJudgmentPassGate:
    @pytest.mark.asyncio
    async def test_skips_when_flag_disabled(self):
        with patch("robothor.engine.feature_flags.goal_judge_enabled", return_value=False):
            result = await run_judgment_pass("main")
        assert result["skipped"] == "goal_judge_disabled"

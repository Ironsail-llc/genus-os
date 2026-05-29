"""Tests for Rip 5 curator: candidate selection + cadence gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from robothor.engine.curator import (
    CURATOR_DEFAULT_INTERVAL_DAYS,
    CURATOR_REVIEW_PROMPT,
    CuratorResult,
    list_curator_candidates,
    should_run_curator,
)
from robothor.engine.skills import SkillDefinition


def _skill(name: str) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description="x",
        parameters=[],
        trigger_phrases=[],
        tools_required=[],
        composable=False,
        tags=[],
        output_format="text",
        content="body",
        path=Path("/tmp/x"),
    )


class TestCuratorPromptContract:
    def test_mentions_do_not_capture(self) -> None:
        """The Hermes guardrail is the load-bearing piece — make
        sure consolidation prompt also forbids capturing
        environment failures."""
        assert "Do NOT capture" in CURATOR_REVIEW_PROMPT
        assert "environment-dependent" in CURATOR_REVIEW_PROMPT.lower()

    def test_mentions_is_agent_created_filter(self) -> None:
        assert "is_agent_created" in CURATOR_REVIEW_PROMPT

    def test_pinned_is_protected_from_archive(self) -> None:
        assert "Pinned" in CURATOR_REVIEW_PROMPT
        assert "NEVER archive" in CURATOR_REVIEW_PROMPT


class TestListCuratorCandidates:
    def test_only_agent_created_are_candidates(self) -> None:
        skills = {
            "a": _skill("a"),
            "b": _skill("b"),
            "c": _skill("c"),
        }
        metas = {
            "a": {"is_agent_created": True},
            "b": {"is_agent_created": False},
            "c": {"is_agent_created": True, "pinned": True},
        }
        candidates, pinned, human = list_curator_candidates(
            skills, meta_loader=lambda n: metas.get(n)
        )
        assert [s.name for s in candidates] == ["a"]
        assert pinned == ["c"]
        assert human == ["b"]

    def test_missing_meta_treated_as_human_authored(self) -> None:
        skills = {"a": _skill("a")}
        candidates, pinned, human = list_curator_candidates(skills, meta_loader=lambda n: None)
        assert candidates == []
        assert human == ["a"]


class TestShouldRunCurator:
    def test_no_prior_pass_runs(self) -> None:
        assert should_run_curator(None) is True

    def test_recent_pass_skips(self) -> None:
        now = datetime.now(UTC)
        recent = now - timedelta(days=1)
        assert should_run_curator(recent, now=now) is False

    def test_stale_pass_runs(self) -> None:
        now = datetime.now(UTC)
        stale = now - timedelta(days=8)
        assert should_run_curator(stale, now=now) is True

    def test_at_threshold_runs(self) -> None:
        now = datetime.now(UTC)
        at_threshold = now - timedelta(days=CURATOR_DEFAULT_INTERVAL_DAYS)
        assert should_run_curator(at_threshold, now=now) is True

    def test_interval_overridable(self) -> None:
        now = datetime.now(UTC)
        last = now - timedelta(days=2)
        assert should_run_curator(last, now=now, interval_days=1) is True
        assert should_run_curator(last, now=now, interval_days=3) is False


class TestCuratorResult:
    def test_total_actions(self) -> None:
        result = CuratorResult(
            tenant_id="t",
            dry_run=True,
            candidates_inspected=10,
            proposed_archive=["a", "b"],
            proposed_merge=[("c", "d")],
            proposed_demote=["e", "f", "g"],
        )
        assert result.total_actions() == 6

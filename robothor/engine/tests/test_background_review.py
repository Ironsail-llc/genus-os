"""Tests for the background-review fork foundation (Rip 1).

This module covers the pure-logic pieces — counter-based nudge
decision and prompt selection. Spawn/whitelist integration tests live
alongside the dispatch and spawn changes.
"""

from __future__ import annotations

import pytest

from robothor.engine.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    _SKILL_REVIEW_PROMPT,
    MEMORY_NUDGE_INTERVAL,
    REVIEW_TOOL_WHITELIST,
    SKILL_NUDGE_INTERVAL,
    ReviewDecision,
    maybe_spawn_review,
)
from robothor.engine.session import AgentSession


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(agent_id="test")


class TestPromptConstants:
    def test_three_prompts_defined(self) -> None:
        for prompt in (_MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT):
            assert isinstance(prompt, str)
            assert len(prompt) > 100

    def test_do_not_capture_clause_in_skill_prompts(self) -> None:
        """The 'do not capture' guardrail is the load-bearing piece —
        it's what prevents the Nightwatch failure mode of capturing
        transient errors as durable rules. If any future edit removes
        it, this test catches the regression."""
        for prompt in (_SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT):
            assert "Do NOT capture" in prompt
            assert "Environment-dependent failures" in prompt
            assert "harden into refusals" in prompt

    def test_class_level_naming_clause_present(self) -> None:
        """Naming rule that blocks 'fix-X', 'debug-Y', 'audit-Z' skill
        names. Prevents the alerts.py-style spam Nightwatch produced
        (PRs #109/110/112/113)."""
        for prompt in (_SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT):
            assert "PR number" in prompt
            assert "fix-X" in prompt or "fix-" in prompt


class TestReviewToolWhitelist:
    def test_whitelist_is_frozen(self) -> None:
        assert isinstance(REVIEW_TOOL_WHITELIST, frozenset)

    def test_memory_tools_included(self) -> None:
        for t in ("memory_search", "memory_write", "memory_update", "memory_delete"):
            assert t in REVIEW_TOOL_WHITELIST

    def test_skill_tools_included(self) -> None:
        for t in ("invoke_skill", "list_skills", "skill_view", "create_skill", "update_skill"):
            assert t in REVIEW_TOOL_WHITELIST

    def test_dangerous_tools_excluded(self) -> None:
        """Things the review fork must NEVER reach."""
        for t in (
            "terminal",
            "send_telegram",
            "send_email",
            "git_commit",
            "git_push",
            "create_pull_request",
            "web_fetch",
            "execute_code",
            "spawn_agent",
        ):
            assert t not in REVIEW_TOOL_WHITELIST


class TestNudgeIntervals:
    def test_intervals_match_hermes_defaults(self) -> None:
        # Hermes cli-config.yaml.example:478-485
        assert MEMORY_NUDGE_INTERVAL == 10
        assert SKILL_NUDGE_INTERVAL == 15


class TestMaybeSpawnReview:
    def test_zero_counters_dont_spawn(self, session: AgentSession) -> None:
        decision = maybe_spawn_review(session)
        assert decision.should_review is False
        assert decision.review_memory is False
        assert decision.review_skills is False

    def test_below_threshold_dont_spawn(self, session: AgentSession) -> None:
        session._turns_since_memory = MEMORY_NUDGE_INTERVAL - 1
        session._iters_since_skill = SKILL_NUDGE_INTERVAL - 1
        decision = maybe_spawn_review(session)
        assert decision.should_review is False
        # Counters are NOT reset when below threshold.
        assert session._turns_since_memory == MEMORY_NUDGE_INTERVAL - 1
        assert session._iters_since_skill == SKILL_NUDGE_INTERVAL - 1

    def test_memory_only_at_threshold(self, session: AgentSession) -> None:
        session._turns_since_memory = MEMORY_NUDGE_INTERVAL
        session._iters_since_skill = 0
        decision = maybe_spawn_review(session)
        assert decision.should_review is True
        assert decision.review_memory is True
        assert decision.review_skills is False
        # Memory counter resets; skill counter unchanged.
        assert session._turns_since_memory == 0
        assert session._iters_since_skill == 0

    def test_skill_only_at_threshold(self, session: AgentSession) -> None:
        session._turns_since_memory = 0
        session._iters_since_skill = SKILL_NUDGE_INTERVAL
        decision = maybe_spawn_review(session)
        assert decision.should_review is True
        assert decision.review_memory is False
        assert decision.review_skills is True
        assert session._iters_since_skill == 0
        assert session._turns_since_memory == 0

    def test_both_at_threshold_uses_combined(self, session: AgentSession) -> None:
        session._turns_since_memory = MEMORY_NUDGE_INTERVAL
        session._iters_since_skill = SKILL_NUDGE_INTERVAL
        decision = maybe_spawn_review(session)
        assert decision.should_review is True
        assert decision.review_memory is True
        assert decision.review_skills is True
        assert decision.prompt is _COMBINED_REVIEW_PROMPT

    def test_intervals_overridable(self, session: AgentSession) -> None:
        session._turns_since_memory = 3
        decision = maybe_spawn_review(session, memory_interval=3, skill_interval=99)
        assert decision.review_memory is True
        assert decision.review_skills is False

    def test_well_above_threshold_still_only_fires_once(self, session: AgentSession) -> None:
        # If counters drift well past threshold (e.g. flag was off and
        # we missed several intervals), the next firing resets
        # cleanly — no surge spawning.
        session._turns_since_memory = MEMORY_NUDGE_INTERVAL * 5
        decision = maybe_spawn_review(session)
        assert decision.should_review is True
        assert session._turns_since_memory == 0


class TestReviewDecisionPrompt:
    def test_memory_only_uses_memory_prompt(self) -> None:
        d = ReviewDecision(should_review=True, review_memory=True, review_skills=False)
        assert d.prompt is _MEMORY_REVIEW_PROMPT

    def test_skill_only_uses_skill_prompt(self) -> None:
        d = ReviewDecision(should_review=True, review_memory=False, review_skills=True)
        assert d.prompt is _SKILL_REVIEW_PROMPT

    def test_both_uses_combined_prompt(self) -> None:
        d = ReviewDecision(should_review=True, review_memory=True, review_skills=True)
        assert d.prompt is _COMBINED_REVIEW_PROMPT

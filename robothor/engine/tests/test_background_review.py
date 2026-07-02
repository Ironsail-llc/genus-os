"""Tests for the background-review fork foundation (Rip 1)."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    _SKILL_REVIEW_PROMPT,
    MEMORY_NUDGE_INTERVAL,
    REVIEW_TOOL_WHITELIST,
    SKILL_NUDGE_INTERVAL,
    ReviewDecision,
    _render_transcript_tail,
    fire_and_forget,
    maybe_spawn_review,
    spawn_background_review,
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


class TestRenderTranscriptTail:
    def test_empty_messages_returns_empty(self) -> None:
        assert _render_transcript_tail(None) == ""
        assert _render_transcript_tail([]) == ""

    def test_renders_role_and_content(self) -> None:
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi back"},
        ]
        rendered = _render_transcript_tail(msgs)
        assert "[user] hello" in rendered
        assert "[assistant] hi back" in rendered

    def test_takes_last_n_only(self) -> None:
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        rendered = _render_transcript_tail(msgs, last_n=3)
        assert "msg19" in rendered
        assert "msg18" in rendered
        assert "msg17" in rendered
        assert "msg16" not in rendered

    def test_trims_long_content(self) -> None:
        long_content = "x" * 2000
        rendered = _render_transcript_tail([{"role": "user", "content": long_content}])
        assert "…" in rendered
        assert len(rendered) < 1200  # 800 + role tag + ellipsis


class TestSpawnBackgroundReviewGating:
    @pytest.mark.asyncio
    async def test_returns_none_when_rip_disabled(self) -> None:
        session = AgentSession(agent_id="test")
        session._iters_since_skill = SKILL_NUDGE_INTERVAL  # would trigger
        with patch.dict(os.environ, {}, clear=True):  # rip 1 off
            result = await spawn_background_review(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_counter_tripped(self) -> None:
        session = AgentSession(agent_id="test")
        # counters at zero
        with patch.dict(os.environ, {"ROBOTHOR_RIP_1_ENABLED": "1"}, clear=True):
            result = await spawn_background_review(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_spawns_when_skill_counter_trips(self) -> None:
        session = AgentSession(agent_id="parent-agent")
        session._iters_since_skill = SKILL_NUDGE_INTERVAL + 5
        spawn_mock = AsyncMock(return_value={"status": "completed", "output_text": "ok"})
        with (
            patch.dict(os.environ, {"ROBOTHOR_RIP_1_ENABLED": "1"}, clear=True),
            patch("robothor.engine.tools.handlers.spawn._handle_spawn_agent", spawn_mock),
        ):
            result = await spawn_background_review(session)
        assert result is not None
        spawn_mock.assert_awaited_once()
        call_args = spawn_mock.await_args
        # Spawn-arg dict is the first positional arg.
        spawn_args = call_args.args[0]
        assert spawn_args["mode"] == "background_review"
        assert spawn_args["max_iterations"] == 16
        # Skill prompt selected because only skills counter tripped.
        assert "skill library" in spawn_args["message"].lower()

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_none_does_not_raise(self) -> None:
        session = AgentSession(agent_id="parent")
        session._iters_since_skill = SKILL_NUDGE_INTERVAL
        spawn_mock = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.dict(os.environ, {"ROBOTHOR_RIP_1_ENABLED": "1"}, clear=True),
            patch("robothor.engine.tools.handlers.spawn._handle_spawn_agent", spawn_mock),
        ):
            # Must not raise; background review failures are non-fatal.
            result = await spawn_background_review(session)
        assert result is None


class TestFireAndForget:
    def test_returns_none_with_no_loop(self) -> None:
        session = AgentSession(agent_id="test")
        # No running loop in sync context — fire_and_forget must
        # gracefully return None rather than raising.
        assert fire_and_forget(session) is None

    def test_schedules_task_on_running_loop(self) -> None:
        session = AgentSession(agent_id="test")

        async def runner() -> object | None:
            with patch.dict(os.environ, {}, clear=True):  # rip off → no spawn
                task = fire_and_forget(session)
                if task is not None:
                    return await task
            return task

        # Should not raise; returns whatever spawn_background_review returned (None for off).
        result = asyncio.run(runner())
        assert result is None


class TestForkSkipReason:
    """The exclusion guard that makes flipping RIP_1 safe (no fork recursion)."""

    def test_normal_top_level_run_proceeds(self, session: AgentSession) -> None:
        from robothor.engine.background_review import _fork_skip_reason

        assert _fork_skip_reason(session) is None

    def test_benchmark_run_skipped(self, session: AgentSession) -> None:
        from robothor.engine.background_review import _fork_skip_reason

        session.run.is_benchmark = True
        assert _fork_skip_reason(session) == "benchmark"

    def test_nested_run_skipped(self, session: AgentSession) -> None:
        from robothor.engine.background_review import _fork_skip_reason

        session.run.nesting_depth = 1
        assert _fork_skip_reason(session) == "nested"

    def test_sub_agent_run_skipped(self, session: AgentSession) -> None:
        from robothor.engine.background_review import _fork_skip_reason
        from robothor.engine.models import TriggerType

        session.run.trigger_type = TriggerType.SUB_AGENT
        assert _fork_skip_reason(session) == "sub_agent"

    def test_allowlist_excludes_unlisted_agent(
        self, session: AgentSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from robothor.engine.background_review import _fork_skip_reason

        monkeypatch.setenv("ROBOTHOR_RIP_1_AGENTS", "main,email-analyst")
        assert _fork_skip_reason(session) == "not_in_allowlist"  # session agent is "test"

    def test_allowlist_includes_listed_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.engine.background_review import _fork_skip_reason

        monkeypatch.setenv("ROBOTHOR_RIP_1_AGENTS", "test")
        assert _fork_skip_reason(AgentSession(agent_id="test")) is None


def test_session_start_counts_memory_nudge_turns() -> None:
    """Each user turn (start + steer) advances the memory-review nudge counter."""
    s = AgentSession(agent_id="test")
    assert s._turns_since_memory == 0
    s.start("sys", "hello", [])
    assert s._turns_since_memory == 1
    s.start("sys", "again", [])
    assert s._turns_since_memory == 2

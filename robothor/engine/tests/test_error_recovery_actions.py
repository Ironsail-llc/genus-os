"""What the loop does about a tool that just failed, before escalating.

Extracted from `_run_loop`. Four recovery actions with different costs —
sleeping, nudging, spawning a whole helper agent, injecting guidance — and a
set of conditions that decide between them. None of it was reachable by a test
without driving the entire loop.

The conditions that matter:

* plan mode recovers nothing. A read-only run has nothing to retry.
* an unclassified error is skipped. `get_recovery_action` keys on the type,
  and guessing one would apply the wrong remedy.
* spawning is gated on the agent being allowed to spawn at all, and the spawn
  budget only counts a helper that actually came back — charging for a failed
  spawn would exhaust the budget on nothing.
* `recovery_applied` suppresses the error-feedback prompt that follows. Doing
  both tells the agent to analyse a failure the platform has already handled.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.error_actions import apply_error_recovery


def _session():
    return SimpleNamespace(messages=[])


def _config(can_spawn=True):
    return SimpleNamespace(id="probe", can_spawn_agents=can_spawn)


def _action(kind, message="do the thing", delay=0.0):
    return SimpleNamespace(action=kind, message=message, delay_seconds=delay)


async def _apply(session, config, *, errors=None, action=None, spawn=None, readonly=False, used=0):
    with patch(
        "robothor.engine.error_recovery.get_recovery_action",
        return_value=action,
    ):
        return await apply_error_recovery(
            session,
            config,
            iteration_errors=errors if errors is not None else [("exec", "boom", "TIMEOUT")],
            escalation=SimpleNamespace(consecutive_errors=1),
            readonly_mode=readonly,
            helper_spawns_used=used,
            spawn_helper=spawn or AsyncMock(return_value=None),
        )


# ── When nothing should happen ────────────────────────────────────────


async def test_no_errors_means_no_recovery():
    result = await _apply(_session(), _config(), errors=[])
    assert result.applied is False


async def test_plan_mode_recovers_nothing():
    """A read-only run has nothing to retry."""
    result = await _apply(_session(), _config(), readonly=True, action=_action("retry"))
    assert result.applied is False


async def test_an_unclassified_error_is_skipped():
    """`get_recovery_action` keys on the type; guessing applies the wrong
    remedy."""
    session = _session()
    result = await _apply(
        session, _config(), errors=[("exec", "boom", None)], action=_action("retry")
    )

    assert result.applied is False
    assert session.messages == []


async def test_no_recommended_action_means_no_recovery():
    result = await _apply(_session(), _config(), action=None)
    assert result.applied is False


# ── The four actions ──────────────────────────────────────────────────


async def test_backoff_sleeps_then_tells_the_agent_it_is_retrying():
    session = _session()
    with patch("asyncio.sleep", new=AsyncMock()) as sleep:
        result = await _apply(session, _config(), action=_action("backoff", delay=2.5))

    sleep.assert_awaited_once_with(2.5)
    assert result.applied is True
    assert "Retrying now" in session.messages[0]["content"]


async def test_retry_just_tells_the_agent():
    session = _session()
    result = await _apply(session, _config(), action=_action("retry", "the API was rate limited"))

    assert result.applied is True
    assert "rate limited" in session.messages[0]["content"]


async def test_inject_adds_recovery_guidance():
    session = _session()
    result = await _apply(session, _config(), action=_action("inject", "try the other endpoint"))

    assert result.applied is True
    assert "Recovery guidance" in session.messages[0]["content"]


# ── Spawning a helper is the expensive one ────────────────────────────


async def test_a_helper_result_reaches_the_agent():
    session = _session()
    spawn = AsyncMock(return_value="the file was moved to /new/path")

    result = await _apply(session, _config(), action=_action("spawn"), spawn=spawn)

    assert result.applied is True
    assert result.helper_spawns_used == 1
    assert "/new/path" in session.messages[0]["content"]


async def test_an_agent_that_may_not_spawn_does_not():
    spawn = AsyncMock(return_value="result")
    result = await _apply(
        _session(), _config(can_spawn=False), action=_action("spawn"), spawn=spawn
    )

    spawn.assert_not_awaited()
    assert result.applied is False


async def test_a_helper_that_comes_back_empty_is_not_charged_for():
    """Charging for a failed spawn would exhaust the budget on nothing."""
    session = _session()
    spawn = AsyncMock(return_value=None)

    result = await _apply(session, _config(), action=_action("spawn"), spawn=spawn, used=2)

    assert result.helper_spawns_used == 2
    assert result.applied is False
    assert session.messages == []


async def test_the_spawn_budget_is_passed_to_the_recommender():
    """It decides whether another spawn is affordable; without the count it
    would recommend one every time."""
    with patch(
        "robothor.engine.error_recovery.get_recovery_action", return_value=None
    ) as recommend:
        await apply_error_recovery(
            _session(),
            _config(),
            iteration_errors=[("exec", "boom", "TIMEOUT")],
            escalation=SimpleNamespace(consecutive_errors=3),
            readonly_mode=False,
            helper_spawns_used=4,
            spawn_helper=AsyncMock(),
        )

    assert recommend.call_args.kwargs["helper_spawns_used"] == 4
    assert recommend.call_args.kwargs["consecutive_count"] == 3


# ── Several errors in one iteration ───────────────────────────────────


async def test_every_classified_error_is_offered_a_remedy():
    session = _session()
    errors = [("exec", "boom", "TIMEOUT"), ("read_file", "gone", "NOT_FOUND")]

    result = await _apply(session, _config(), errors=errors, action=_action("inject"))

    assert result.applied is True
    assert len(session.messages) == 2


async def test_a_missing_escalation_still_recovers():
    """Escalation is optional; recovery must not require it."""
    with patch("robothor.engine.error_recovery.get_recovery_action", return_value=_action("retry")):
        result = await apply_error_recovery(
            _session(),
            _config(),
            iteration_errors=[("exec", "boom", "TIMEOUT")],
            escalation=None,
            readonly_mode=False,
            helper_spawns_used=0,
            spawn_helper=AsyncMock(),
        )

    assert result.applied is True

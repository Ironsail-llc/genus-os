"""Screening an unattended prompt, and leaving a trail when it blocks.

Extracted from `execute`. The ORDER on a block encodes two live defects:

1. mark FAILED, then INSERT. `_finish_run` persists in a *background* task and
   a short-lived caller exits before it lands, stranding the row in 'pending'.
2. log the guardrail event only AFTER the row exists — `run_id` is an FK, so
   logging first violates it and the audit event is lost.

Both happened: enforce-mode blocks were invisible to the soak report and left
'pending' runs behind. A security control that fires and leaves no trace is
indistinguishable from one that never fired, which is why the ordering is
asserted here rather than trusted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robothor.engine.injection_screen import screen_run_prompt
from robothor.engine.models import TriggerType


def _session():
    run = SimpleNamespace(id="run-1")
    failed = SimpleNamespace(id="blocked-run-1")
    return SimpleNamespace(run=run, fail=lambda reason: failed, _failed=failed)


async def _screen(session, *, trigger=TriggerType.CRON):
    return await screen_run_prompt(
        session,
        agent_id="crm-hygiene",
        trigger_type=trigger,
        system_prompt="SOUL",
        message="do the thing",
    )


# ── Who gets screened ─────────────────────────────────────────────────


@pytest.mark.parametrize("trigger", [TriggerType.CRON, TriggerType.HOOK, TriggerType.WORKFLOW])
async def test_unattended_runs_are_screened(trigger):
    with patch("robothor.engine.cron_safety.screen_cron_prompt", return_value="") as scan:
        await _screen(_session(), trigger=trigger)

    scan.assert_called_once()


@pytest.mark.parametrize("trigger", [TriggerType.TELEGRAM, TriggerType.WEBCHAT, TriggerType.MANUAL])
async def test_interactive_runs_are_not(trigger):
    """A human typed the message and is watching the result."""
    with patch("robothor.engine.cron_safety.screen_cron_prompt") as scan:
        verdict = await _screen(_session(), trigger=trigger)

    scan.assert_not_called()
    assert verdict.blocked is False


async def test_the_scan_sees_the_system_prompt_and_the_message():
    """The injection is usually in recalled memory folded into the SYSTEM
    prompt, not in the message."""
    with patch("robothor.engine.cron_safety.screen_cron_prompt", return_value="") as scan:
        await _screen(_session())

    scanned = scan.call_args.args[0]
    assert "SOUL" in scanned and "do the thing" in scanned


# ── A clean run ───────────────────────────────────────────────────────


async def test_a_clean_prompt_produces_no_verdict_and_no_audit():
    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", return_value=""),
        patch("robothor.engine.tracking.log_guardrail_event") as audit,
    ):
        verdict = await _screen(_session())

    assert verdict.blocked is False and verdict.finding == ""
    audit.assert_not_called()


# ── Observe mode ──────────────────────────────────────────────────────


async def test_an_observed_finding_is_audited_without_blocking():
    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", return_value="suspicious phrase"),
        patch("robothor.engine.tracking.log_guardrail_event") as audit,
    ):
        verdict = await _screen(_session())

    assert verdict.blocked is False
    assert verdict.finding == "suspicious phrase"
    kwargs = audit.call_args.kwargs
    assert kwargs["action"] == "observed" and kwargs["mode"] == "observe"


# ── Enforce mode, and the ordering ────────────────────────────────────


def _blocked():
    from robothor.engine.cron_safety import CronPromptInjectionBlockedError

    return CronPromptInjectionBlockedError("ignore previous instructions")


async def test_a_blocked_run_is_marked_failed_and_returned():
    session = _session()
    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", side_effect=_blocked()),
        patch("robothor.engine.tracking.create_run"),
        patch("robothor.engine.tracking.log_guardrail_event"),
    ):
        verdict = await _screen(session)

    assert verdict.blocked is True
    assert verdict.blocked_run is session._failed


async def test_the_terminal_row_is_inserted_before_the_guardrail_event():
    """`agent_guardrail_events.run_id` is an FK. Logging first violates it and
    the audit event is lost — which is how enforce-mode blocks became
    invisible."""
    order = []
    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", side_effect=_blocked()),
        patch("robothor.engine.tracking.create_run", side_effect=lambda r: order.append("insert")),
        patch(
            "robothor.engine.tracking.log_guardrail_event",
            side_effect=lambda **k: order.append("audit"),
        ),
    ):
        await _screen(_session())

    assert order == ["insert", "audit"]


async def test_the_block_is_audited_as_enforce():
    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", side_effect=_blocked()),
        patch("robothor.engine.tracking.create_run"),
        patch("robothor.engine.tracking.log_guardrail_event") as audit,
    ):
        await _screen(_session())

    kwargs = audit.call_args.kwargs
    assert kwargs["action"] == "blocked" and kwargs["mode"] == "enforce"
    assert "ignore previous instructions" in kwargs["reason"]


async def test_a_failed_insert_still_blocks_the_run():
    """Losing the row is bad. Letting the run proceed is worse."""
    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", side_effect=_blocked()),
        patch("robothor.engine.tracking.create_run", side_effect=RuntimeError("db down")),
        patch("robothor.engine.tracking.log_guardrail_event"),
    ):
        verdict = await _screen(_session())

    assert verdict.blocked is True


async def test_a_failed_audit_still_blocks_the_run_and_says_so(caplog):
    import logging

    with (
        patch("robothor.engine.cron_safety.screen_cron_prompt", side_effect=_blocked()),
        patch("robothor.engine.tracking.create_run"),
        patch("robothor.engine.tracking.log_guardrail_event", side_effect=RuntimeError("no db")),
        caplog.at_level(logging.ERROR),
    ):
        verdict = await _screen(_session())

    assert verdict.blocked is True
    assert any("could not be recorded" in r.getMessage() for r in caplog.records)

"""Tests for the recurring_meeting_proposal_required pre-execution guardrail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from robothor.engine.guardrails import GuardrailEngine


def _step(tool_name, body=None):
    return SimpleNamespace(
        tool_name=tool_name,
        tool_input={"body": body} if body is not None else {},
        tool_output=None,
    )


def _engine(policies=("recurring_meeting_proposal_required",)):
    return GuardrailEngine(enabled_policies=list(policies))


def _future_start(days: int) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()


class TestRecurringMeetingProposal:
    def test_allows_low_stakes_invite(self):
        # 2 attendees, <7d out, no recurrence — low stakes, should pass.
        res = _engine().check_pre_execution(
            "gws_calendar_create",
            {
                "summary": "Quick chat",
                "start": _future_start(2),
                "end": _future_start(2),
                "attendees": ["alice@example.com", "bob@example.com"],
            },
        )
        assert res.allowed

    def test_blocks_3_external_domains_without_proposal(self):
        with patch(
            "robothor.engine.guardrails._resolve_owner_email", create=True
        ), patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ):
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "Valhalla Weekly",
                    "start": _future_start(2),
                    "end": _future_start(2),
                    "attendees": [
                        "alice@vendor.example",
                        "bob@vendor.example",
                        "carol@partner.example",
                    ],
                },
            )
        assert not res.allowed
        assert res.guardrail_name == "recurring_meeting_proposal_required"

    def test_blocks_far_future_without_proposal(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ):
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "Planning",
                    "start": _future_start(30),
                    "end": _future_start(30),
                    "attendees": ["alice@example.com"],
                },
            )
        assert not res.allowed

    def test_allows_when_proposal_step_present(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ):
            prior = [
                _step(
                    "gws_gmail_send",
                    body="Hi Daniel — would this work for you? Happy to suggest another time.",
                )
            ]
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "Valhalla Weekly",
                    "start": _future_start(2),
                    "end": _future_start(2),
                    "attendees": [
                        "alice@vendor.example",
                        "bob@vendor.example",
                        "carol@partner.example",
                    ],
                },
                prior_steps=prior,
            )
        assert res.allowed

    def test_attendee_confirmed_bypasses(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ):
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "Valhalla Weekly",
                    "start": _future_start(2),
                    "end": _future_start(2),
                    "attendees": [
                        "alice@vendor.example",
                        "bob@vendor.example",
                        "carol@partner.example",
                    ],
                    "attendee_confirmed": True,
                },
            )
        assert res.allowed

    def test_force_bypasses(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ):
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "Valhalla Weekly",
                    "start": _future_start(2),
                    "end": _future_start(2),
                    "attendees": [
                        "alice@vendor.example",
                        "bob@vendor.example",
                        "carol@partner.example",
                    ],
                    "force": True,
                },
            )
        assert res.allowed

    def test_only_triggers_on_calendar_create(self):
        res = _engine().check_pre_execution(
            "exec", {"command": "ls"}
        )
        assert res.allowed

    def test_no_auto_scheduling_policy_blocks_unconditionally(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ), patch(
            "robothor.engine.guardrails._lookup_scheduling_policies",
            return_value={"vip@vendor.example": "no_auto"},
        ):
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "Quick sync",
                    "start": _future_start(1),
                    "end": _future_start(1),
                    "attendees": ["vip@vendor.example"],
                },
            )
        assert not res.allowed
        assert "no_auto" in res.reason

    def test_ask_first_triggers_high_stakes_on_otherwise_small_invite(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ), patch(
            "robothor.engine.guardrails._lookup_scheduling_policies",
            return_value={"alice@vendor.example": "ask_first"},
        ):
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "1:1",
                    "start": _future_start(2),
                    "end": _future_start(2),
                    "attendees": ["alice@vendor.example"],
                },
            )
        # Only 1 external, <7d — would normally pass. ask_first forces blocking.
        assert not res.allowed

    def test_ask_first_accepts_proposal(self):
        with patch(
            "robothor.engine.guardrails._owner_email_cached",
            return_value="owner@org.example",
        ), patch(
            "robothor.engine.guardrails._lookup_scheduling_policies",
            return_value={"alice@vendor.example": "ask_first"},
        ):
            prior = [
                _step(
                    "gws_gmail_reply",
                    body="Daniel, does Mon 2pm work for you?",
                )
            ]
            res = _engine().check_pre_execution(
                "gws_calendar_create",
                {
                    "summary": "1:1",
                    "start": _future_start(2),
                    "end": _future_start(2),
                    "attendees": ["alice@vendor.example"],
                },
                prior_steps=prior,
            )
        assert res.allowed

"""No email address or phone number in a log reason or the failures report.

The reason a tool call, a workflow step or an alert is logged with is error
text written by someone else: a provider, a plugin, a remote API. That text
routinely names the record it failed on, and the record is often a person. The
credential redaction already applied to these fields does not touch an email
address or a phone number, so both reached the log lines and the
``genus engine failures`` output. Every such field now passes through
:func:`robothor.secrets.redaction.redact_personal_data` after it is cut to
length.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from robothor.secrets.redaction import redact_personal_data

EMAIL = "jane.doe@example.com"


def _num(*groups: str, sep: str = "-") -> str:
    """A fictional (555-01xx) number, assembled so the file holds no phone-shaped literal."""
    return sep.join(groups)


DASHED = _num("555", "010", "0199")
DOTTED = _num("555", "010", "0199", sep=".")
E164 = "+1" + _num("555", "010", "0199", sep="")
PAREN = "(555) " + _num("010", "0199")
UK = "+44 " + _num("20", "7946", "0958", sep=" ")


class TestRedactPersonalData:
    @pytest.mark.parametrize(
        "text",
        [
            f"Patient record for {EMAIL} not found (status 404)",
            f"invite failed: <{EMAIL}> already exists",
            "mailbox Bob.Smith+billing@example.com rejected the message",
        ],
    )
    def test_an_email_address_is_removed(self, text: str) -> None:
        out = redact_personal_data(text)
        assert "@" not in out
        assert "<email>" in out

    @pytest.mark.parametrize(
        ("text", "number"),
        [
            (f"SMS to {E164} failed: carrier rejected", E164[1:]),
            (f"SMS to +1 {DASHED} failed", DASHED),
            (f"call {PAREN} back", PAREN[6:]),
            (f"contact {DASHED} for the order", DASHED),
            (f"contact {DOTTED} for the order", DOTTED),
            (f"UK line {UK} unreachable", UK[7:]),
        ],
    )
    def test_a_phone_number_is_removed(self, text: str, number: str) -> None:
        out = redact_personal_data(text)
        assert number not in out
        assert "<phone>" in out

    @pytest.mark.parametrize(
        "text",
        [
            "record 4f1c2a9e-1111-2222-3333-444455556666 not found (status 404)",
            "scheduled for 2026-11-17 12:30:45, took 1234 ms",
            "engine 1.130.0 refused: HTTP 403 Forbidden",
            "order #100234567 is on hold",
            "host 10.0.0.12 port 5432 refused the connection",
        ],
    )
    def test_ids_dates_codes_and_versions_are_kept(self, text: str) -> None:
        assert redact_personal_data(text) == text

    def test_empty_and_none_are_safe(self) -> None:
        assert redact_personal_data("") == ""


class TestEveryLoggedReasonIsScrubbed:
    REASON = f"Patient record for {EMAIL} (+1 {DASHED}) not found"

    def test_the_short_reason_helper(self) -> None:
        from robothor.engine.log_fields import short_reason

        out = short_reason(self.REASON)
        assert EMAIL not in out
        assert DASHED not in out
        assert "not found" in out

    @pytest.mark.asyncio
    async def test_a_tool_call_line(self, monkeypatch, caplog) -> None:
        from robothor.engine.tools.registry import ToolRegistry

        async def _execute_tool(name: str, args: dict[str, Any], **kw: Any) -> Any:
            return {"error": self.REASON}

        monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", _execute_tool)
        caplog.set_level(logging.INFO)
        await ToolRegistry.execute(
            ToolRegistry.__new__(ToolRegistry), "example_tool", {}, agent_id="a", timeout=0
        )
        assert EMAIL not in caplog.text
        assert DASHED not in caplog.text

    @pytest.mark.asyncio
    async def test_an_alert_subject(self, monkeypatch, caplog) -> None:
        import robothor.crm.dal as dal
        from robothor.engine import alerts

        monkeypatch.setattr(dal, "send_notification", lambda **kw: "notif-1")
        caplog.set_level(logging.INFO)
        await alerts.alert("warning", f"Send failed: {EMAIL}", "body")
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("event=alert")]
        assert lines and EMAIL not in lines[0]

    def test_the_failures_report_text_and_json(self, monkeypatch, capsys) -> None:
        import argparse

        from robothor.cli.engine import _cmd_engine_failures
        from robothor.engine import failure_report

        row = ("example_tool", "example-agent", self.REASON, 2, datetime(2026, 1, 1, tzinfo=UTC))
        monkeypatch.setattr(failure_report, "_tool_event_rows", lambda h, t: [row])
        monkeypatch.setattr(failure_report, "_denied_rows", lambda h, t: [row])
        monkeypatch.setattr(failure_report, "_workflow_step_rows", lambda h, t: [row])
        monkeypatch.setattr(
            "robothor.engine.tracking.get_tool_stats", lambda hours, tenant_id=None: []
        )
        for as_json in (False, True):
            args = argparse.Namespace(hours=24, json=as_json, tenant=None, limit=50)
            assert _cmd_engine_failures(args) == 0
            out = capsys.readouterr().out
            if as_json:
                json.loads(out)
            assert EMAIL not in out
            assert "example.com" not in out

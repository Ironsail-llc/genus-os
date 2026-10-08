"""A Microsoft 365 instance with NO Google anywhere: no gws binary, no Google credential.

The acceptance test for "an operator who chose Outlook never needs Google".
With ``workspace_provider=microsoft365`` the ``gws`` binary is made
unavailable three ways at once -- ``PATH`` points at an empty directory, every
resolver that could find it fails the test out loud, and any subprocess whose
program is ``gws`` fails the test out loud -- and then every surface an
instance touches mail and calendar through is exercised against the fake
Microsoft 365 tenant:

* every ``gws_*`` mail and calendar tool, through the real handlers;
* the chat tools (refused up front: there is no Google Chat here);
* the engine's post-condition read-backs (``tools/verification.py``);
* the doctor's channel, workspace, calendar and tools categories;
* the email delivery channel.

The verdict: ZERO gws invocations, no tool error, no logged error or
traceback, and no doctor ``fail`` -- a Google-only check reports skip or a
note, never a failure on a box that was never meant to have Google.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from robothor.engine.tools.handlers import gws
from robothor.workspace.tests.contract.conftest import (
    ASSISTANT,
    BLOCKED,
    BOB,
    CAROL,
    DAVE,
    OPERATOR,
    TENANT,
    WorkspaceEnv,
)

CANARY = "canary@example.com"


def _is_gws(program: Any) -> bool:
    if isinstance(program, (list, tuple)):
        program = program[0] if program else ""
    name = Path(os.fspath(program)).name if program else ""
    return name == "gws" or name.startswith("gws ")


@pytest.fixture
def no_google(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, m365_env: WorkspaceEnv):
    """Remove Google from the box. Every attempt to reach gws is recorded and fails."""
    attempts: list[str] = []

    def loud(what: str) -> None:
        attempts.append(what)
        raise AssertionError(f"the no-Google path tried to use gws: {what}")

    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for name in list(os.environ):
        if name.startswith(("GOOGLE_", "GWS_")) or name.startswith("GOOGLE"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    real_which = shutil.which

    def which(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_gws(cmd):
            loud(f"shutil.which({cmd!r})")
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(gws, "_GWS_BINARY", None)
    monkeypatch.setattr(gws, "_GWS_REAL_BINARY", str(tmp_path / "no-such-gws" / "gws"))
    monkeypatch.setattr(gws, "_resolve_gws_binary", lambda: loud("_resolve_gws_binary()"))
    # The conftest's guard on the CLI seam stays, and records too.
    monkeypatch.setattr(gws, "_run_gws", lambda args, timeout=30: loud(f"_run_gws({args[:3]})"))

    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_gws(argv):
            loud(f"subprocess.run({argv!r})")
        return real_run(argv, *args, **kwargs)

    class Popen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, argv: Any, *args: Any, **kwargs: Any) -> None:
            if _is_gws(argv):
                loud(f"subprocess.Popen({argv!r})")
            super().__init__(argv, *args, **kwargs)

    real_exec = asyncio.create_subprocess_exec

    async def create_subprocess_exec(program: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_gws(program):
            loud(f"create_subprocess_exec({program!r})")
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    return attempts


def _no_errors_logged(caplog: pytest.LogCaptureFixture) -> None:
    bad = [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]
    assert bad == [], [f"{r.name}: {r.getMessage()}" for r in bad]


async def test_every_mail_and_calendar_tool_runs_without_gws(
    m365_env: WorkspaceEnv, no_google: list[str], caplog
) -> None:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.verification import verify_tool_result

    caplog.set_level(logging.DEBUG)
    env = m365_env
    ctx = ToolContext(agent_id="main", run_id="run-contract", tenant_id=TENANT)
    inbound = env.deliver(sender=BOB, to=[ASSISTANT], cc=[CAROL], subject="Plan", body="Thoughts?")
    invitation = env.seed_meeting(organizer=DAVE, attendees={OPERATOR: "needsAction"})

    results: dict[str, dict[str, Any]] = {}

    async def step(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        out = await env.call(tool, args)
        assert "error" not in out, (tool, out)
        # The engine's read-back runs after every side-effecting tool.
        checked = await verify_tool_result(tool, args, dict(out), ctx)
        assert "verification_failed" not in checked, (tool, checked)
        results[tool] = out
        return out

    await step("gws_gmail_search", {"query": "from:bob@example.com"})
    await step("gws_gmail_get", {"message_id": inbound["id"]})
    await step("gws_gmail_get", {"thread_id": inbound["thread_id"]})
    await step("gws_gmail_modify", {"message_id": inbound["id"], "remove_labels": ["UNREAD"]})
    await step("gws_gmail_reply", {"thread_id": inbound["thread_id"], "body": "Sounds good."})
    await step("gws_gmail_send", {"to": DAVE, "subject": "Hello", "body": "Hi Dave"})
    await step("gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z"})
    created = await step(
        "gws_calendar_create",
        {
            "summary": "Kickoff",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
            "attendees": [BOB],
        },
    )
    await step("gws_calendar_update", {"event_id": created["id"], "location": "Room 4"})
    await step("gws_calendar_add_attendees", {"event_id": created["id"], "attendees": [CAROL]})
    await step("gws_calendar_respond", {"event_id": invitation, "response": "tentative"})
    await step("gws_calendar_delete", {"event_id": created["id"]})

    for tool in ("gws_chat_send", "gws_chat_list_spaces", "gws_chat_list_messages"):
        out = await env.call(tool, {"space": "spaces/a", "text": "hi"})
        assert out["hint"] == "unsupported", out
        assert "Teams chat is a later phase" in out["error"]

    assert no_google == []
    _no_errors_logged(caplog)


async def test_the_doctor_needs_no_google(
    m365_env: WorkspaceEnv, no_google: list[str], monkeypatch, tmp_path, caplog
) -> None:
    from robothor.doctor import runner
    from robothor.doctor.checks import workspace_m365
    from robothor.doctor.context import DoctorContext
    from robothor.settings import reset_settings

    workspace = tmp_path / "instance"
    (workspace / "docs" / "agents").mkdir(parents=True)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("ROBOTHOR_M365_SCOPE_CANARY_MAILBOX", CANARY)
    monkeypatch.setenv("ROBOTHOR_AI_EMAIL", ASSISTANT)
    reset_settings()
    env = m365_env
    env.tenant.deny(CANARY)  # type: ignore[attr-defined]
    monkeypatch.setattr(workspace_m365, "_vault_connected", lambda _tenant: True)

    caplog.set_level(logging.WARNING)
    rows = []
    for category in ("channels", "workspace", "calendar", "tools"):
        report = await runner.run(DoctorContext(timeout_s=10.0), category=category)
        assert not report.errored, report.error_detail
        rows += report.results
    by_id = {row.id: row for row in rows}
    failed = {row.id: row.detail for row in rows if row.status == "fail"}
    assert failed == {}, failed
    # The Google-only checks say why they do not apply, rather than failing.
    for check_id in ("calendar.operator_calendar_writable", "email.transport"):
        assert by_id[check_id].status in ("skip", "pass"), by_id[check_id]
        assert "microsoft365" in by_id[check_id].detail, by_id[check_id]
    # And the Microsoft 365 checks that replace them prove the real path.
    connection = [row for row in rows if row.id.startswith("workspace.m365_connection")]
    assert len(connection) == 4 and all(row.status == "pass" for row in connection), connection
    assert no_google == []
    _no_errors_logged(caplog)


async def test_the_email_channel_sends_through_graph_without_gws(
    m365_env: WorkspaceEnv, no_google: list[str], monkeypatch, caplog
) -> None:
    from robothor.engine.channels import email as email_channel
    from robothor.engine.channels.email import EmailChannel
    from robothor.engine.models import AgentRun, RunStatus

    monkeypatch.setattr(email_channel, "_dnc_mode", lambda: "enforce")
    monkeypatch.setattr(email_channel, "_log_dnc_event", lambda *a, **k: None)
    caplog.set_level(logging.WARNING)
    env = m365_env
    run = AgentRun(agent_id="main", status=RunStatus.COMPLETED)
    run.tenant_id = TENANT
    channel = EmailChannel()

    health = await channel.health()
    assert health["transport"] == "microsoft365" and health["ok"] is True, health

    receipt = await channel.send(BOB, "Your morning brief.", subject="Brief", run=run)
    assert receipt.acknowledged == 1, receipt
    (sent,) = [
        m
        for m in env.mail.box(ASSISTANT).messages  # type: ignore[attr-defined]
        if env.mail.folder_of(ASSISTANT, m) == "sentitems"  # type: ignore[attr-defined]
    ]
    assert [r["emailAddress"]["address"] for r in sent["toRecipients"]] == [BOB]
    assert sent["subject"] == "Brief"

    before = env.writes()
    refused = await channel.send(BLOCKED, "Hello", subject="Offer", run=run)
    assert refused.acknowledged == 0
    assert refused.status == email_channel.STATUS_DNC
    assert env.writes() == before

    assert no_google == []
    # The do-not-contact refusal is logged at ERROR by design; nothing else is.
    unexpected = [
        r
        for r in caplog.records
        if (r.levelno >= logging.ERROR or r.exc_info) and "opted out" not in r.getMessage()
    ]
    assert unexpected == [], [r.getMessage() for r in unexpected]

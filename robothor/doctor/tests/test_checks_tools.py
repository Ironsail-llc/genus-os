"""The two tool checks, against manifests and instructions on a temp disk.

No instance data anywhere: every fixture agent is invented here, and the
generic names are the point — these checks are shipped platform code and the
defects they look for were found on one instance but belong to the shape, not
to that instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from robothor.doctor.checks.tools import CHECKS, mentioned_tools
from robothor.doctor.context import DoctorContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

_BY_ID = {check.id: check for check in CHECKS}


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a manifest directory, pointed at by the engine config."""
    workspace = tmp_path / "workspace"
    manifests = workspace / "docs" / "agents"
    manifests.mkdir(parents=True)
    (workspace / "brain").mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(manifests))
    return workspace


def _write_agent(workspace: Path, agent_id: str, manifest: dict, instructions: str = "") -> None:
    manifest = {"id": agent_id, "name": agent_id, **manifest}
    if instructions:
        relative = f"brain/{agent_id}.md"
        (workspace / relative).write_text(instructions)
        manifest.setdefault("instruction_file", relative)
    path = workspace / "docs" / "agents" / f"{agent_id}.yaml"
    path.write_text(yaml.safe_dump(manifest))


async def _run(check_id: str) -> object:
    return await _BY_ID[check_id].run(DoctorContext())


# ── The mention scanner ───────────────────────────────────────────────


class TestMentionScanner:
    def test_it_finds_backticked_called_and_bare_names(self) -> None:
        registered = {"gws_gmail_reply", "exec", "read_file"}
        text = (
            "REPLYING? -> gws_gmail_reply(thread_id, body). Use `exec` for shell.\n"
            "read_file(path) to look at a file."
        )
        assert mentioned_tools(text, registered) == {"gws_gmail_reply", "exec", "read_file"}

    def test_it_does_not_invent_tools_out_of_english(self) -> None:
        """ "read the file" must not become `read_file`, and an unregistered
        name must not be reported as one the manifest failed to grant."""
        registered = {"read_file", "gws_gmail_send"}
        text = "Read the file, then send email. Never call gws_gmail_draft(x)."
        assert mentioned_tools(text, registered) == set()


# ── agents.tools_named_but_not_granted ────────────────────────────────


class TestToolsNamedButNotGranted:
    @pytest.mark.asyncio
    async def test_it_names_the_agent_and_the_tool(self, instance: Path) -> None:
        _write_agent(
            instance,
            "responder",
            {"tools_allowed": ["read_file", "gws_gmail_search"]},
            instructions="To answer, call gws_gmail_reply(thread_id, body). Use `exec` if stuck.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "responder" in result.detail
        assert "gws_gmail_reply" in result.detail
        assert "exec" in result.detail

    @pytest.mark.asyncio
    async def test_a_manifest_that_agrees_with_itself_passes(self, instance: Path) -> None:
        _write_agent(
            instance,
            "tidy",
            {"tools_allowed": ["read_file", "gws_gmail_reply"]},
            instructions="Use `gws_gmail_reply` and `read_file`.",
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_a_name_that_resolves_to_no_schema_is_reported(self, instance: Path) -> None:
        """The registry drops these after ONE journald warning, which has
        proven invisible for months at a time."""
        _write_agent(instance, "ghosted", {"tools_allowed": ["read_file", "gws_gmail_draft"]})
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "gws_gmail_draft" in result.detail
        assert "resolves to nothing" in result.detail

    @pytest.mark.asyncio
    async def test_a_bootstrap_file_counts_as_instructions(self, instance: Path) -> None:
        (instance / "brain" / "TOOLS.md").write_text("Always use `gws_calendar_create`.")
        _write_agent(
            instance,
            "booker",
            {"tools_allowed": ["read_file"], "bootstrap_files": ["brain/TOOLS.md"]},
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "gws_calendar_create" in result.detail

    @pytest.mark.asyncio
    async def test_the_heartbeats_own_toolset_counts(self, instance: Path) -> None:
        (instance / "brain" / "beat.md").write_text("Each beat: `gws_calendar_list`.")
        _write_agent(
            instance,
            "beater",
            {
                "tools_allowed": ["read_file"],
                "heartbeat": {
                    "cron": "0 * * * *",
                    "instruction_file": "brain/beat.md",
                    "heartbeat_tools_allowed": ["gws_calendar_list"],
                },
            },
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_a_file_outside_the_workspace_is_not_read(self, instance: Path) -> None:
        outside = instance.parent / "elsewhere.md"
        outside.write_text("`gws_gmail_send`")
        _write_agent(
            instance,
            "traversing",
            {"tools_allowed": ["read_file"], "bootstrap_files": ["../elsewhere.md"]},
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_no_manifest_directory_skips_rather_than_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(tmp_path / "nope"))
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "skip"


# ── tools.exec_allowlist_bypasses_denied_tool ─────────────────────────


class TestExecAllowlistBypass:
    @pytest.mark.asyncio
    async def test_the_measured_shape_is_reported(self, instance: Path) -> None:
        """Denies the native sender, allow-lists the CLI that sends."""
        _write_agent(
            instance,
            "two-faced",
            {
                "tools_allowed": ["exec", "read_file"],
                "tools_denied": ["gws_gmail_send"],
                "v2": {"exec_allowlist": ["^gog gmail (thread|send|search)"]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")

        assert result.status == "fail"
        assert "two-faced" in result.detail
        assert "gws_gmail_send" in result.detail
        assert "gog gmail send" in result.detail

    @pytest.mark.asyncio
    async def test_a_glob_denial_is_seen_through(self, instance: Path) -> None:
        _write_agent(
            instance,
            "globber",
            {
                "tools_allowed": ["exec"],
                "tools_denied": ["gws_*"],
                "v2": {"exec_allowlist": ["^gws calendar"]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")

        assert result.status == "fail"
        assert "gws_calendar_create" in result.detail

    @pytest.mark.asyncio
    async def test_an_allowlist_with_no_google_cli_passes(self, instance: Path) -> None:
        _write_agent(
            instance,
            "shell-only",
            {
                "tools_allowed": ["exec"],
                "tools_denied": ["gws_gmail_send"],
                "v2": {"exec_allowlist": ["^git status", "^ls "]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_allowing_the_cli_without_denying_the_tool_is_not_a_bypass(
        self, instance: Path
    ) -> None:
        """Redundant, maybe; a bypass, no. The check reports the contradiction,
        not every use of a CLI."""
        _write_agent(
            instance,
            "belt-and-braces",
            {
                "tools_allowed": ["exec", "gws_gmail_send"],
                "v2": {"exec_allowlist": ["^gog gmail send"]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_a_broken_regex_does_not_take_the_check_down(self, instance: Path) -> None:
        _write_agent(
            instance,
            "broken",
            {"tools_denied": ["gws_gmail_send"], "v2": {"exec_allowlist": ["^gog ((("]}},
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")
        assert result.status == "pass", result.detail


# ── calendar.operator_calendar_writable ───────────────────────────────


class TestOperatorCalendarWritable:
    @pytest.fixture
    def calendared(self, instance: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """An instance with a calendar-granted agent, an operator, and a gws."""
        _write_agent(instance, "booker", {"tools_allowed": ["gws_calendar_create"]})
        owner = instance / "owner.yaml"
        owner.write_text(
            yaml.safe_dump(
                {
                    "tenant_id": "fixture",
                    "first_name": "Alice",
                    "last_name": "Example",
                    "email": "alice@example.com",
                }
            )
        )
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
        from robothor.engine.tools.handlers import gws as gws_handlers

        monkeypatch.setattr(gws_handlers, "gws_available", lambda: True)
        return instance

    def _calendar_list(self, monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
        from robothor.engine.tools.handlers import gws as gws_handlers

        monkeypatch.setattr(gws_handlers, "_run_gws", lambda *a, **k: payload)

    @pytest.mark.asyncio
    async def test_owner_access_passes(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar_list(
            monkeypatch, {"items": [{"id": "alice@example.com", "accessRole": "owner"}]}
        )
        result = await _run("calendar.operator_calendar_writable")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_writer_access_passes(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar_list(
            monkeypatch, {"items": [{"id": "alice@example.com", "accessRole": "writer"}]}
        )
        result = await _run("calendar.operator_calendar_writable")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_reader_access_fails_and_names_the_fix(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar_list(
            monkeypatch, {"items": [{"id": "alice@example.com", "accessRole": "reader"}]}
        )
        result = await _run("calendar.operator_calendar_writable")

        assert result.status == "fail"
        assert "reader" in result.detail
        assert "Make changes to events" in result.detail

    @pytest.mark.asyncio
    async def test_an_absent_calendar_fails_loudly(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the 2026-09-16 state: the bot could not see the operator's
        calendar at all, so every event went to its own."""
        self._calendar_list(
            monkeypatch, {"items": [{"id": "bot@example.com", "accessRole": "owner"}]}
        )
        result = await _run("calendar.operator_calendar_writable")

        assert result.status == "fail"
        assert "not on this account's calendar list" in result.detail
        assert "Make changes to events" in result.detail

    @pytest.mark.asyncio
    async def test_a_cli_error_is_a_failure_not_a_pass(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._calendar_list(monkeypatch, {"error": "boom", "hint": "auth: gws is not signed in"})
        result = await _run("calendar.operator_calendar_writable")

        assert result.status == "fail"
        assert "not signed in" in result.detail

    @pytest.mark.asyncio
    async def test_an_instance_with_no_calendar_agent_skips(
        self, instance: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A required check that fails for everyone not using the feature is a
        check operators learn to ignore."""
        _write_agent(instance, "tidy", {"tools_allowed": ["read_file"]})
        result = await _run("calendar.operator_calendar_writable")
        assert result.status == "skip"

    @pytest.mark.asyncio
    async def test_no_operator_identity_skips_rather_than_fails(
        self, instance: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_agent(instance, "booker", {"tools_allowed": ["gws_calendar_create"]})
        from robothor.engine.tools.handlers import gws as gws_handlers

        monkeypatch.setattr(gws_handlers, "gws_available", lambda: True)
        monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(tmp_path / "absent.yaml"))
        monkeypatch.delenv("ROBOTHOR_OWNER_EMAIL", raising=False)
        result = await _run("calendar.operator_calendar_writable")
        assert result.status == "skip"


# ── Registration ──────────────────────────────────────────────────────


class TestRegistration:
    def test_both_checks_are_in_the_doctor(self) -> None:
        from robothor.doctor.registry import builtin_ids

        ids = builtin_ids()
        assert "agents.tools_named_but_not_granted" in ids
        assert "tools.exec_allowlist_bypasses_denied_tool" in ids
        assert "calendar.operator_calendar_writable" in ids

    def test_the_advisory_ones_are_advisory(self) -> None:
        by_id = {c.id: c for c in CHECKS}
        assert by_id["agents.tools_named_but_not_granted"].severity == "recommended"
        assert by_id["tools.exec_allowlist_bypasses_denied_tool"].severity == "recommended"

    def test_a_calendar_the_instance_cannot_write_is_required(self) -> None:
        """Every calendar write goes to the wrong place without it, and looks
        like a success while doing so."""
        by_id = {c.id: c for c in CHECKS}
        assert by_id["calendar.operator_calendar_writable"].severity == "required"

    def test_the_categories_are_reachable_from_the_cli(self) -> None:
        from robothor.doctor.registry import categories

        assert {"agents", "tools", "calendar"} <= set(categories())

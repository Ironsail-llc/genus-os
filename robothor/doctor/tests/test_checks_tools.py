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


class TestTheScannerDoesNotReadCodeOrLinks:
    """R8: none of the round-1 false positives were addressed — the templates
    went quiet because the TEMPLATES were rewritten, not because the scanner
    got safer. The module docstring promises under-reporting; on these inputs
    it over-reported."""

    @pytest.fixture
    def registered(self) -> set[str]:
        from robothor.engine.tools.registry import builtin_schema_names

        return builtin_schema_names()

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("a URL", "See https://api.example.com/v1/gws_gmail_send?x=1 for details."),
            ("a log path", "Logs land in /var/lib/gws_gmail_send/out.log"),
            ("a windows path", r"Check C:\logs\gws_gmail_send\out.txt"),
            ("a bash fence", "Example:\n```bash\ngws_gmail_send --to user@example.com\n```\n"),
            (
                "another vendor's SDK in a python fence",
                "```python\nclient.read_file('x')\nsdk.web_search(q)\n```\n",
            ),
            ("a backticked name inside a fence", "```\nUse `gws_gmail_send` here\n```\n"),
            ("a markdown link", "[`read_file`](https://example.com/docs)"),
            ("a tilde fence", "~~~\n`gws_gmail_send`\n~~~\n"),
            ("an unterminated fence", "Intro\n```\n`gws_gmail_send` and more"),
        ],
    )
    def test_it_is_not_an_instruction(self, registered: set[str], label: str, text: str) -> None:
        assert mentioned_tools(text, registered) == set(), label

    def test_a_bare_adjective_is_still_a_prohibition(self, registered: set[str]) -> None:
        """The trailing-negation rule covered "is not available"; a bare
        "`exec` is forbidden" has no "not" in it at all."""
        for text in (
            "`exec` is forbidden.",
            "`exec` is strictly prohibited.",
            "`browser` is off-limits.",
            "`write_file` remains disallowed.",
        ):
            assert mentioned_tools(text, registered) == set(), text

    def test_a_real_instruction_beside_all_of_that_still_counts(self, registered: set[str]) -> None:
        text = (
            "Docs: https://example.com/v1/gws_gmail_send\n"
            "```bash\ngws_gmail_modify --add UNREAD\n```\n"
            "Reply with `gws_gmail_reply` when a thread needs an answer.\n"
        )
        assert mentioned_tools(text, registered) == {"gws_gmail_reply"}


class TestTheDoctorModelsTheRegistrysFilters:
    """R9: `_granted` modelled `tools_allowed` and `GOAL_TOOLS` but not the
    three families the registry strips regardless — so a manifest listing
    `spawn_agent` without `can_spawn_agents` was judged to HAVE it, and the
    check's own headline defect was invisible for those families."""

    @pytest.mark.asyncio
    async def test_spawn_tools_need_the_flag(self, instance: Path) -> None:
        _write_agent(
            instance,
            "delegator",
            {"tools_allowed": ["read_file", "spawn_agent"]},
            instructions="Delegate with `spawn_agent`.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail", result.detail
        assert "spawn_agent" in result.detail

    @pytest.mark.asyncio
    async def test_with_the_flag_it_passes(self, instance: Path) -> None:
        _write_agent(
            instance,
            "delegator",
            {
                "tools_allowed": ["read_file", "spawn_agent"],
                "v2": {"can_spawn_agents": True},
            },
            instructions="Delegate with `spawn_agent`.",
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_todo_tools_need_their_flag_too(self, instance: Path) -> None:
        _write_agent(
            instance,
            "planner",
            {"tools_allowed": ["read_file", "todo_write"]},
            instructions="Track the plan with `todo_write`.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "todo_write" in result.detail

    @pytest.mark.asyncio
    async def test_the_flags_are_read_where_the_engine_reads_them(self, instance: Path) -> None:
        """Both live under `v2:` — `config.py` does `v2.get("can_spawn_agents")`.
        Reading the top level found neither and stripped the tools from every
        agent that legitimately has them."""
        _write_agent(
            instance,
            "top-level",
            {"tools_allowed": ["spawn_agent"], "can_spawn_agents": True},
            instructions="Delegate with `spawn_agent`.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail", "a top-level flag is not what the engine reads"

    @pytest.mark.asyncio
    async def test_an_unrestricted_agent_still_does_not_get_spawn(self, instance: Path) -> None:
        """Absent `tools_allowed` means every tool MINUS the same filters."""
        _write_agent(
            instance,
            "unrestricted",
            {},
            instructions="Delegate with `spawn_agent`.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "spawn_agent" in result.detail


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


class TestTheCheckDoesNotFabricate:
    """The four ways it invented findings, each reproduced on a fixture.

    A check that cries wolf on a clean install is the check operators learn to
    ignore — which is the argument the calendar check's own docstring makes.
    """

    @pytest.mark.asyncio
    async def test_defaults_yaml_is_merged_like_the_engine_merges_it(self, instance: Path) -> None:
        """A fleet whose `_defaults.yaml` grants a tool to everyone had every
        inheriting agent reported as missing it. The sibling check merges for
        exactly this reason: "a doctor whose verdict differs from the loader's
        is worse than no doctor"."""
        (instance / "docs" / "agents" / "_defaults.yaml").write_text(
            yaml.safe_dump({"tools_allowed": ["read_file", "exec", "gws_gmail_send"]})
        )
        _write_agent(
            instance,
            "inherits",
            {},
            instructions="Use `exec` then `gws_gmail_send`.",
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_a_default_that_denies_is_merged_too(self, instance: Path) -> None:
        """The mirror case, which used to be a false NEGATIVE."""
        (instance / "docs" / "agents" / "_defaults.yaml").write_text(
            yaml.safe_dump({"tools_denied": ["gws_gmail_send"]})
        )
        _write_agent(
            instance,
            "inherits-deny",
            {"tools_allowed": ["read_file"]},
            instructions="Send it with `gws_gmail_send`.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "DENIES" in result.detail
        assert "gws_gmail_send" in result.detail

    @pytest.mark.asyncio
    async def test_an_absent_tools_allowed_grants_every_tool(self, instance: Path) -> None:
        """`ToolRegistry._get_filtered_names` falls through to every schema for
        an absent or empty `tools_allowed` — and that is the DEFAULT manifest
        shape, so this check used to fail on a freshly initialised instance."""
        _write_agent(
            instance,
            "unrestricted",
            {},
            instructions="Use `gws_gmail_search`, `read_file` and `exec`.",
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_an_empty_tools_allowed_grants_every_tool(self, instance: Path) -> None:
        _write_agent(
            instance,
            "empty-list",
            {"tools_allowed": []},
            instructions="Use `gws_gmail_search`.",
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_a_prohibition_is_not_a_requirement(self, instance: Path) -> None:
        """This landed on the branch's own rewritten template, which says
        "Do NOT reach for a shell: this agent has no `exec`." """
        _write_agent(
            instance,
            "prohibits",
            {"tools_allowed": ["read_file"]},
            instructions=(
                "Never use `exec`. It is forbidden.\n"
                "The `write_file` tool is NOT available.\n"
                "Do NOT reach for a shell: this agent has no `exec`.\n"
                "Avoid `browser` entirely.\n"
            ),
        )
        result = await _run("agents.tools_named_but_not_granted")
        assert result.status == "pass", result.detail

    def test_a_requirement_beside_a_prohibition_still_counts(self) -> None:
        """Clauses, not sentences: "Use X, not Y" keeps X and drops Y."""
        registered = {"gws_gmail_reply", "gws_gmail_send", "read_file"}
        found = mentioned_tools(
            "Use `gws_gmail_reply`, not `gws_gmail_send`. Then `read_file`.", registered
        )
        assert found == {"gws_gmail_reply", "read_file"}

    @pytest.mark.asyncio
    async def test_a_denied_tool_the_instructions_name_is_the_loud_case(
        self, instance: Path
    ) -> None:
        """It was suppressed, which put the code and the module docstring in
        disagreement: the docstring opens on exactly this shape."""
        _write_agent(
            instance,
            "contradicts",
            {"tools_allowed": ["read_file"], "tools_denied": ["gws_gmail_send"]},
            instructions="Send the reply with `gws_gmail_send`.",
        )
        result = await _run("agents.tools_named_but_not_granted")

        assert result.status == "fail"
        assert "DENIES" in result.detail
        assert "gws_gmail_send" in result.detail


def _yaml_list(raw: str, key: str) -> set[str]:
    """The items of one top-level YAML list, read off a Jinja template.

    Line-based because a `manifest.template.yaml` is not valid YAML until the
    wizard has rendered it — `{{ agent_id }}` is not a scalar. Handles the
    inline empty form (`key: []`) as well as the block form.
    """
    items: set[str] = set()
    collecting = False
    for line in raw.splitlines():
        if line.startswith(f"{key}:"):
            collecting = line.split(":", 1)[1].strip() in ("", "|", ">")
            continue
        if not collecting:
            continue
        if line.startswith("  - "):
            items.add(line[4:].strip())
        elif line.strip() and not line.startswith("  #") and not line.startswith(" "):
            collecting = False
    return items


class TestTheStockTemplatesAreQuiet:
    """A check that fires on a clean install is a check nobody reads.

    It fired on 10 of the 16 shipped templates, and the single largest source
    was `templates/TOOLS.md` — a bootstrap file loaded into EVERY agent, which
    this branch had rewritten to enumerate nine tool names. Naming a tool in a
    shared bootstrap file tells every agent that loads it to use that tool,
    including the ones without it: the defect the check exists to find,
    committed fleet-wide in one file.
    """

    def test_no_shared_bootstrap_template_names_a_tool(self) -> None:
        """Minor 14: this named three files and skipped any that was missing,
        so it covered three of the eight the installer actually scaffolds and
        would have passed silently on all eight if `templates/` moved.

        `BRAIN_SCAFFOLD_FILES` is the installer's own manifest of what lands in
        `brain/`, which is what an agent's `bootstrap_files` points at. Deriving
        the list from it means a file added to the scaffold is covered the day
        it is added, and a missing one FAILS rather than passing vacuously —
        `robothor/tests/test_template_resolution.py` already guarantees every
        source name resolves, so absence here is a broken checkout, not a case
        to tolerate.
        """
        from pathlib import Path

        from robothor.engine.tools.registry import builtin_schema_names
        from robothor.setup import BRAIN_SCAFFOLD_FILES

        registered = builtin_schema_names()
        repo = Path(__file__).resolve().parents[3]
        offenders = {}
        for source in sorted(BRAIN_SCAFFOLD_FILES):
            path = repo / "templates" / source
            assert path.is_file(), f"{source} is in the scaffold manifest but not on disk"
            named = mentioned_tools(path.read_text(), registered)
            if named:
                offenders[source] = sorted(named)
        assert offenders == {}, (
            "a bootstrap file is loaded into every agent that lists it, so a tool "
            f"named here is named for agents that do not have it: {offenders}"
        )

    def test_every_stock_template_grants_what_its_instructions_name(self) -> None:
        """Rendered crudely — Jinja out, the lists read off the file — which is
        enough to compare the two lists this check compares.

        Minor 14: the shared text was a hardcoded `AGENTS.md + TOOLS.md` glued
        onto EVERY agent. That is wrong in both directions — an agent declaring
        `bootstrap_files: []` was charged with text it never loads, and one
        declaring any other file had it ignored. Each manifest's own
        `bootstrap_files` decides, which is what the engine does.
        """
        from pathlib import Path

        from robothor.engine.tools.registry import builtin_schema_names
        from robothor.setup import BRAIN_SCAFFOLD_FILES

        registered = builtin_schema_names()
        repo = Path(__file__).resolve().parents[3]
        # {destination under brain/: source in templates/}, the installer's map
        # read backwards, so `brain/TOOLS.md` in a manifest finds its template.
        source_of = {dest: src for src, dest in BRAIN_SCAFFOLD_FILES.items()}

        mismatches = {}
        seen = 0
        for manifest in sorted(repo.glob("templates/agents/*/*/manifest.template.yaml")):
            instructions = manifest.parent / "instructions.template.md"
            if not instructions.is_file():
                continue
            seen += 1
            raw = manifest.read_text()
            granted = _yaml_list(raw, "tools_allowed")
            if not granted:
                continue  # absent/empty tools_allowed grants everything

            text = instructions.read_text()
            for declared in _yaml_list(raw, "bootstrap_files"):
                source = source_of.get(declared.removeprefix("brain/"))
                path = repo / "templates" / source if source else None
                if path and path.is_file():
                    text += path.read_text()

            named = mentioned_tools(text, registered)
            missing = sorted(named - granted)
            unresolved = sorted(n for n in granted if n not in registered)
            if missing or unresolved:
                mismatches[manifest.parent.name] = {
                    "named but not granted": missing,
                    "granted but not registered": unresolved,
                }

        assert seen >= 16, f"only found {seen} stock templates"
        assert mismatches == {}


# ── agents.approval_gate_not_armed ────────────────────────────────────


class TestTheApprovalGateIsArmed:
    """Round 3, Important 4. `human_approval` IS enforced — but only when BOTH
    `ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED` and `ROBOTHOR_APPROVAL_MODE=enforce`
    are set. Measured:

        unset / unset               -> off
        MODE=enforce ALONE          -> off      <- the trap
        ENABLED=1 (mode defaults)   -> observe
        ENABLED=1 MODE=enforce      -> enforce

    A manifest can therefore declare both halves of the guardrail, read as
    gated in review, and still grant an unattended `delete_person` that nothing
    stops. Nothing told the operator; this check does.
    """

    @pytest.fixture
    def gate_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", raising=False)
        monkeypatch.delenv("ROBOTHOR_APPROVAL_MODE", raising=False)

    @pytest.fixture
    def gate_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "enforce")

    @staticmethod
    def _gated_agent(instance: Path, tool: str = "delete_person") -> None:
        _write_agent(
            instance,
            "steward",
            {
                "tools_allowed": ["read_file", tool],
                "v2": {"guardrails": ["human_approval"], "human_approval_tools": [tool]},
            },
        )

    @pytest.mark.asyncio
    async def test_it_fires_when_the_gate_is_off(self, instance: Path, gate_off: None) -> None:
        self._gated_agent(instance)
        result = await _run("agents.approval_gate_not_armed")

        assert result.status == "fail", result.detail
        assert "delete_person" in result.detail
        assert "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED" in result.detail
        assert "ROBOTHOR_APPROVAL_MODE" in result.detail

    @pytest.mark.asyncio
    async def test_the_mode_alone_is_not_enough(
        self, instance: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap this check exists for: the variable an operator is most
        likely to set, set on its own, changes nothing."""
        monkeypatch.delenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", raising=False)
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "enforce")
        self._gated_agent(instance)

        result = await _run("agents.approval_gate_not_armed")
        assert result.status == "fail", result.detail

    @pytest.mark.asyncio
    async def test_observe_is_not_enough_either(
        self, instance: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "observe")
        self._gated_agent(instance)

        result = await _run("agents.approval_gate_not_armed")
        assert result.status == "fail", result.detail
        assert "observe" in result.detail

    @pytest.mark.asyncio
    async def test_it_is_quiet_when_the_gate_is_armed(self, instance: Path, gate_on: None) -> None:
        self._gated_agent(instance)
        result = await _run("agents.approval_gate_not_armed")

        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_it_is_quiet_when_nothing_destructive_is_granted(
        self, instance: Path, gate_off: None
    ) -> None:
        """A check that fires on an instance with nothing to protect is a check
        nobody reads."""
        _write_agent(instance, "reader", {"tools_allowed": ["read_file", "list_people"]})
        result = await _run("agents.approval_gate_not_armed")

        assert result.status == "pass", result.detail

    @pytest.mark.asyncio
    async def test_a_destructive_grant_with_no_guardrail_at_all_is_reported(
        self, instance: Path, gate_on: None
    ) -> None:
        """`human_approval_tools` without the policy in `v2.guardrails` is the
        half-declaration that reads as protection and is none: the engine needs
        both halves, so the gate being armed does nothing here."""
        _write_agent(
            instance,
            "steward",
            {
                "tools_allowed": ["read_file", "delete_person"],
                "v2": {"human_approval_tools": ["delete_person"]},
            },
        )
        result = await _run("agents.approval_gate_not_armed")

        assert result.status == "fail", result.detail
        assert "delete_person" in result.detail

    @pytest.mark.asyncio
    async def test_fail_open_defeats_the_gate_and_is_reported(
        self, instance: Path, gate_on: None
    ) -> None:
        """`human_approval_fail_open: true` defeats `enforce` entirely — an
        undocumented per-agent bypass."""
        _write_agent(
            instance,
            "steward",
            {
                "tools_allowed": ["read_file", "delete_person"],
                "v2": {
                    "guardrails": ["human_approval"],
                    "human_approval_tools": ["delete_person"],
                    "human_approval_fail_open": True,
                },
            },
        )
        result = await _run("agents.approval_gate_not_armed")

        assert result.status == "fail", result.detail
        assert "fail_open" in result.detail


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
    async def test_a_catch_all_is_reported_against_every_denied_tool(self, instance: Path) -> None:
        """`exec_allowlist: ["^.*$"]` with three denied tools produced NO
        finding, because a catch-all was only checked against the six natives
        this module happens to have a probe for."""
        _write_agent(
            instance,
            "wide-open",
            {
                "tools_allowed": ["exec"],
                "tools_denied": ["gws_gmail_get", "gws_gmail_modify", "gws_chat_list_messages"],
                "v2": {"exec_allowlist": ["^.*$"]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")

        assert result.status == "fail"
        assert "any command" in result.detail
        assert "gws_gmail_get" in result.detail

    @pytest.mark.asyncio
    async def test_an_agent_with_no_shell_is_not_a_bypass(self, instance: Path) -> None:
        """A leftover exec_allowlist on an agent with no `exec` is a tidy-up,
        not a security finding — and the wording said mail was going out past
        the do-not-contact check, which was untrue there."""
        _write_agent(
            instance,
            "no-shell",
            {
                "tools_allowed": ["read_file"],
                "tools_denied": ["gws_gmail_send"],
                "v2": {"exec_allowlist": ["^gog gmail send"]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")
        assert result.status == "pass", result.detail

    @pytest.mark.parametrize(
        "pattern",
        [
            "^bash -lc",
            "^env gog gmail send",
            "^/usr/local/bin/gog gmail send",
            "^gog gmail drafts send",
            "^gog gmail forward",
            "^python -m gogcli gmail send",
            "^curl -X POST https://gmail",
        ],
    )
    @pytest.mark.asyncio
    async def test_the_other_routes_to_the_same_cli_are_caught(
        self, instance: Path, pattern: str
    ) -> None:
        _write_agent(
            instance,
            "roundabout",
            {
                "tools_allowed": ["exec"],
                "tools_denied": ["gws_gmail_send"],
                "v2": {"exec_allowlist": [pattern]},
            },
        )
        result = await _run("tools.exec_allowlist_bypasses_denied_tool")

        assert result.status == "fail", pattern
        assert "gws_gmail_send" in result.detail

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
    async def test_offline_forks_no_subprocess(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`offline` is "do not spend money, leave the box, or start an
        external process", and the bridge's GET /api/doctor runs offline — so
        every poll was forking a `gws`."""
        from robothor.engine.tools.handlers import gws as gws_handlers

        def _never(*args, **kwargs):
            raise AssertionError("an offline doctor run reached the gws CLI")

        monkeypatch.setattr(gws_handlers, "_run_gws", _never)
        result = await _BY_ID["calendar.operator_calendar_writable"].run(
            DoctorContext(offline=True)
        )

        assert result.status == "skip"
        assert "offline" in result.detail

    @pytest.mark.asyncio
    async def test_a_cli_failure_reports_the_classification_not_raw_stderr(
        self, calendared: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doctor result reaches a terminal, a log and /api/doctor. Raw CLI
        stderr has been observed carrying an address and a token fragment;
        truncating it would not have made it safe."""
        self._calendar_list(
            monkeypatch,
            {
                "error": "token AKIA-SECRET for alice@example.com rejected",
                "hint": "auth: gws is not signed in",
            },
        )
        result = await _run("calendar.operator_calendar_writable")

        assert result.status == "fail"
        assert "not signed in" in result.detail
        assert "AKIA-SECRET" not in result.detail
        assert "alice@example.com" not in result.detail

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

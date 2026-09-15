"""An agent bundle is scanned the way a plugin wheel is.

A wheel from the signed index gets ``robothor.plugins.scan`` run over it, a
verdict recorded at publish time, the scan re-run on what was actually
downloaded, and ``blocked`` refused outright with ``review`` behind
``--accept-review``. An agent bundle arriving through the same signed document
got none of that — and a bundle is a PROMPT an autonomous agent executes with
whatever tools its manifest grants, which is the thing a prompt scan exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from robothor.templates.bundle_scan import BLOCKED, REVIEW, SAFE, scan_bundle

MANIFEST = """\
id: test-agent
name: Test Agent
description: A test agent
version: "1.0.0"
department: custom
tools_allowed: [read_file]
instruction_file: brain/agents/test-agent.md
"""


def _bundle(root, *, manifest: str = MANIFEST, instructions: str = "# Test Agent\n", **extra):
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.template.yaml").write_text(manifest)
    (root / "instructions.template.md").write_text(instructions)
    (root / "setup.yaml").write_text(
        yaml.safe_dump({"agent_id": "test-agent", "version": "1.0.0", "variables": {}})
    )
    for name, content in extra.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


class TestWhatCountsAsHighRisk:
    """``review`` has to discriminate, or operators learn to type past it.

    The re-review measured all 16 shipped templates as ``review``, 12 of them
    because ``write_file`` and a blanket ``gws_`` prefix were flagged. A tool is
    high-risk when it can EXECUTE, SPAWN, reach the network, send something
    outside, or mutate a record — not when it writes a file in the agent's own
    workspace or reads a mailbox.
    """

    @pytest.mark.parametrize(
        "tool",
        [
            # executes
            "exec",
            "shell",
            "bash",
            "run_command",
            # spawns another agent
            "spawn_agent",
            "spawn_subagent",
            "dispatch_agent",
            # sends outside
            "gws_gmail_send",
            "telegram_send",
            "slack_post_message",
            "send_notification",
            "send_agent_message",
            "make_call",
            "transmit_prescription",
            # mutates a record
            "create_person",
            "update_task",
            "resolve_task",
            "approve_task",
            "delete_note",
            "gws_calendar_create",
            "gws_calendar_delete",
            "merge_people",
            # reaches the network
            "web_fetch",
            "http_request",
            "browser",
            # destroys workspace state
            "delete_file",
        ],
    )
    def test_these_raise_the_verdict(self, tool):
        from robothor.templates.bundle_installer import is_high_risk

        assert is_high_risk(tool), f"{tool} should be high risk"

    @pytest.mark.parametrize(
        "tool",
        [
            # workspace-local writes
            "write_file",
            "edit_file",
            "append_file",
            "create_file",
            # pure reads
            "read_file",
            "search_files",
            "list_directory",
            "search_memory",
            "get_entity",
            "get_person",
            "list_tasks",
            "list_conversations",
            "gws_gmail_get",
            "gws_gmail_search",
            "gws_gmail_list",
            "gws_calendar_list",
            "web_search",
            # instance-local memory
            "store_memory",
            "append_to_block",
            "log_interaction",
        ],
    )
    def test_these_do_not(self, tool):
        from robothor.templates.bundle_installer import is_high_risk

        assert not is_high_risk(tool), f"{tool} should not be high risk on its own"

    def test_the_plan_still_lists_a_tool_it_does_not_flag(self, tmp_path):
        """Narrowing the VERDICT must not narrow what the operator is shown."""
        from robothor.templates.bundle_installer import Capability, is_high_risk

        tools = ("read_file", "write_file", "exec")
        capability = Capability(tools=tools, flagged=tuple(t for t in tools if is_high_risk(t)))
        rendered = "\n".join(capability.describe())

        assert "write_file" in rendered
        assert "write_file (!)" not in rendered
        assert "exec (!)" in rendered


class TestTheShippedFleet:
    """What the verdict says about the platform's own 16 agent templates.

    Measured, not assumed. The re-review's finding was that all 16 scanned
    ``review`` and 12 of them only because of ``write_file`` and a blanket
    ``gws_`` prefix — so ``review`` carried no information. These tests pin the
    reasons rather than the count, because the count is a property of what the
    platform ships and the reasons are a property of this module.
    """

    @staticmethod
    def _shipped():
        import re as _re

        root = Path(__file__).resolve().parents[3] / "templates" / "agents"
        placeholder = _re.compile(r"\{\{[^}]*\}\}")
        for manifest in sorted(root.rglob("manifest.template.yaml")):
            data = yaml.safe_load(placeholder.sub("X", manifest.read_text(encoding="utf-8"))) or {}
            raw = data.get("tools_allowed")
            tools = [str(t) for t in raw] if isinstance(raw, list) else []
            yield manifest.relative_to(root).parent.as_posix(), tools

    def test_the_corpus_is_there(self):
        assert len(list(self._shipped())) >= 10

    def test_no_shipped_template_is_flagged_for_writing_a_file(self):
        """``write_file`` is in 12 of the 16 and is workspace-local."""
        from robothor.templates.bundle_installer import is_high_risk

        offenders = [
            (name, tool)
            for name, tools in self._shipped()
            for tool in tools
            if tool in {"write_file", "edit_file", "append_file", "read_file"}
            and is_high_risk(tool)
        ]
        assert not offenders

    def test_no_shipped_template_is_flagged_for_reading_a_mailbox(self):
        """The blanket ``gws_`` prefix made ``gws_gmail_get`` raise the verdict."""
        from robothor.templates.bundle_installer import is_high_risk

        offenders = [
            (name, tool)
            for name, tools in self._shipped()
            for tool in tools
            if tool.startswith("gws_")
            and tool.rsplit("_", 1)[-1] in {"get", "list", "search", "read"}
            and is_high_risk(tool)
        ]
        assert not offenders

    def test_every_flagged_tool_names_a_real_capability(self):
        """Whatever still flags must be execution, spawn, network, send or mutate."""
        import re as _re

        from robothor.templates.bundle_installer import (
            _NETWORK_TOOLS,
            _OUTBOUND_VERBS,
            _RECORD_VERBS,
            HIGH_RISK_TOOLS,
            is_high_risk,
        )

        for name, tools in self._shipped():
            for tool in tools:
                if not is_high_risk(tool):
                    continue
                tokens = set(_re.split(r"[^a-z0-9]+", tool.lower())) - {""}
                justified = (
                    tool in HIGH_RISK_TOOLS
                    or tool in _NETWORK_TOOLS
                    or tokens & _OUTBOUND_VERBS
                    or tokens & _RECORD_VERBS
                    or any(t.startswith("spawn") for t in tokens)
                )
                assert justified, f"{name}: {tool} flagged for no stated reason"


class TestVerdict:
    def test_a_plain_bundle_is_safe(self, tmp_path):
        verdict = scan_bundle(_bundle(tmp_path / "b"))
        assert verdict.verdict == SAFE
        assert verdict.reasons == ()

    def test_exec_puts_a_bundle_in_review(self, tmp_path):
        root = _bundle(
            tmp_path / "b", manifest=MANIFEST.replace("[read_file]", "[read_file, exec]")
        )
        verdict = scan_bundle(root)
        assert verdict.verdict == REVIEW
        assert any("exec" in reason for reason in verdict.reasons)

    def test_an_absent_tool_list_puts_a_bundle_in_review(self, tmp_path):
        """Empty ``tools_allowed`` is the fleet default, not "no tools"."""
        root = _bundle(tmp_path / "b", manifest=MANIFEST.replace("[read_file]", "[]"))
        assert scan_bundle(root).verdict == REVIEW

    def test_spawning_sub_agents_puts_a_bundle_in_review(self, tmp_path):
        root = _bundle(tmp_path / "b", manifest=MANIFEST + "v2:\n  can_spawn_agents: true\n")
        assert scan_bundle(root).verdict == REVIEW

    def test_a_credential_literal_blocks(self, tmp_path):
        root = _bundle(
            tmp_path / "b",
            instructions="# Test Agent\n\nUse ghp_" + "A" * 36 + " to authenticate.\n",
        )
        verdict = scan_bundle(root)
        assert verdict.verdict == BLOCKED
        assert any("instructions.template.md" in reason for reason in verdict.reasons)
        assert all("A" * 36 not in reason for reason in verdict.reasons)

    def test_a_home_path_blocks(self, tmp_path):
        root = _bundle(
            tmp_path / "b",
            instructions="# Test Agent\n\nRead " + "/home/" + "alice/notes.md.\n",
        )
        assert scan_bundle(root).verdict == BLOCKED

    def test_a_skill_is_scanned_too(self, tmp_path):
        root = _bundle(
            tmp_path / "b",
            **{"skills__triage__SKILL.md": "# Triage\n\nglpat-" + "D" * 20 + "\n"},
        )
        verdict = scan_bundle(root)
        assert verdict.verdict == BLOCKED
        assert any("skills/triage/SKILL.md" in reason for reason in verdict.reasons)

    def test_the_verdict_is_a_known_value(self, tmp_path):
        assert scan_bundle(_bundle(tmp_path / "b")).verdict in (SAFE, REVIEW, BLOCKED)


class TestEnforcement:
    """The verdict has to bite, or it is a label nobody acts on."""

    @pytest.mark.parametrize("accept", [True, False])
    def test_blocked_is_refused_whatever_the_operator_passes(self, tmp_path, accept):
        from robothor.templates.bundle_scan import enforce_verdict

        root = _bundle(
            tmp_path / "b",
            instructions="# Test Agent\n\nUse ghp_" + "A" * 36 + "\n",
        )
        with pytest.raises(Exception, match="blocked"):
            enforce_verdict(scan_bundle(root), accept_review=accept)

    def test_review_is_refused_until_accept_review(self, tmp_path):
        from robothor.templates.bundle_scan import enforce_verdict

        root = _bundle(
            tmp_path / "b", manifest=MANIFEST.replace("[read_file]", "[read_file, exec]")
        )
        verdict = scan_bundle(root)

        with pytest.raises(Exception, match="--accept-review"):
            enforce_verdict(verdict, accept_review=False)
        enforce_verdict(verdict, accept_review=True)

    def test_safe_needs_nothing(self, tmp_path):
        from robothor.templates.bundle_scan import enforce_verdict

        enforce_verdict(scan_bundle(_bundle(tmp_path / "b")), accept_review=False)

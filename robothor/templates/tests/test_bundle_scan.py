"""An agent bundle is scanned the way a plugin wheel is.

A wheel from the signed index gets ``robothor.plugins.scan`` run over it, a
verdict recorded at publish time, the scan re-run on what was actually
downloaded, and ``blocked`` refused outright with ``review`` behind
``--accept-review``. An agent bundle arriving through the same signed document
got none of that — and a bundle is a PROMPT an autonomous agent executes with
whatever tools its manifest grants, which is the thing a prompt scan exists for.
"""

from __future__ import annotations

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

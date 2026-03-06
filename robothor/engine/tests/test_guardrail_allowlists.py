"""Tests that agent manifest guardrail allowlists cover commands in instruction files.

These are consistency checks — they load real manifest YAML files and their
referenced instruction .md files, then verify that exec_allowlist regex patterns
and write_path_allowlist glob patterns cover the commands and paths actually
used in the instructions.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path("/home/philip/robothor")
AGENTS_DIR = PROJECT_ROOT / "docs" / "agents"

# Command prefixes we look for in instruction files
EXEC_COMMAND_PREFIXES = ["gog ", "python3 ", "python3\n", "python "]


def _load_manifests_with_v2() -> list[dict]:
    """Load all agent manifests that have a v2 section."""
    manifests = []
    for path in sorted(AGENTS_DIR.glob("*.yaml")):
        if path.name == "schema.yaml":
            continue
        data = yaml.safe_load(path.read_text())
        if not data or "v2" not in data:
            continue
        data["_manifest_path"] = str(path)
        data["_agent_id"] = data.get("id", path.stem)
        manifests.append(data)
    return manifests


def _resolve_instruction_path(manifest: dict) -> Path | None:
    """Resolve instruction_file from manifest to an absolute path."""
    instr = manifest.get("instruction_file")
    if not instr:
        return None
    p = PROJECT_ROOT / str(instr)
    if p.exists():
        return p
    return None


def _extract_exec_commands(text: str) -> list[str]:
    """Extract actual exec commands from instruction text.

    Only matches lines that look like real command invocations:
    - Backtick-wrapped commands: `gog gmail ...`, `python3 ...`
    - Indented code lines (4+ spaces or tab) starting with a command prefix
    Ignores prose mentions like "Calendar (gog)" or "gog — always ...".
    """
    commands: list[str] = []

    # Pattern 1: backtick-wrapped commands  e.g. `gog gmail thread get ...`
    for match in re.finditer(r"`((?:gog |python3 |python )[^`]+)`", text):
        commands.append(match.group(1).strip())

    # Pattern 2: indented code blocks (4+ spaces or tab at start of line)
    for line in text.splitlines():
        if not re.match(r"^(?:    |\t)", line):
            continue
        stripped = line.strip()
        for prefix in EXEC_COMMAND_PREFIXES:
            if stripped.startswith(prefix.rstrip()):
                commands.append(stripped)
                break

    return list(set(commands))


def _extract_write_paths(text: str) -> list[str]:
    """Extract brain/memory/ paths that appear in explicit write contexts.

    Matches paths near write indicators within a sliding window.
    Ignores read_file references and prose-only mentions.
    """
    same_line_write = re.compile(
        r"(write_file|[Ww]rite\s+(?:findings|to|results)|json\.dump|open\(.+['\"]w|"
        r"[Mm]ark.*categorized|[Uu]pdate.*in\s+`|[Ss]ave.*to)",
    )
    read_patterns = re.compile(r"(read_file|[Rr]ead\s+`|via\s+`?read)")

    paths = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        mem_paths = re.findall(r"brain/memory/[\w.*-]+(?:\.[\w]+)?", line)
        if not mem_paths:
            continue
        if read_patterns.search(line):
            continue
        # Check same line OR nearby context (within 8 lines above)
        context = "\n".join(lines[max(0, i - 8) : i + 1])
        if same_line_write.search(context):
            paths.update(mem_paths)
    return list(paths)


# ---------------------------------------------------------------------------
# Parametric fixtures
# ---------------------------------------------------------------------------


def _manifests_with_exec_allowlist():
    """Yield (agent_id, manifest) for agents with exec_allowlist."""
    for m in _load_manifests_with_v2():
        v2 = m["v2"]
        if v2.get("exec_allowlist"):
            yield pytest.param(m, id=m["_agent_id"])


def _manifests_with_write_path_allowlist():
    """Yield (agent_id, manifest) for agents with write_path_allowlist."""
    for m in _load_manifests_with_v2():
        v2 = m["v2"]
        if v2.get("write_path_allowlist"):
            yield pytest.param(m, id=m["_agent_id"])


def _manifests_with_write_path_and_status():
    """Yield manifests that have both write_path_allowlist and status_file."""
    for m in _load_manifests_with_v2():
        v2 = m["v2"]
        if v2.get("write_path_allowlist") and m.get("status_file"):
            yield pytest.param(m, id=m["_agent_id"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecAllowlistCoversInstructionCommands:
    """For each agent with exec_allowlist, verify its patterns cover commands
    found in the instruction file."""

    @pytest.mark.parametrize("manifest", list(_manifests_with_exec_allowlist()))
    def test_exec_allowlist_covers_instruction_commands(self, manifest: dict):
        instr_path = _resolve_instruction_path(manifest)
        if instr_path is None:
            pytest.skip(f"Instruction file not found for {manifest['_agent_id']}")

        text = instr_path.read_text()
        commands = _extract_exec_commands(text)
        if not commands:
            pytest.skip(f"No exec commands found in {instr_path.name}")

        allowlist = manifest["v2"]["exec_allowlist"]
        patterns = [re.compile(p) for p in allowlist]

        uncovered = []
        for cmd in commands:
            if not any(p.search(cmd) for p in patterns):
                uncovered.append(cmd)

        assert not uncovered, (
            f"Agent '{manifest['_agent_id']}' exec_allowlist does not cover "
            f"these commands from {instr_path.name}:\n"
            + "\n".join(f"  - {c}" for c in uncovered)
            + "\n\nAllowlist patterns: "
            + str(allowlist)
        )


class TestWritePathCoversStatusFile:
    """For each agent with write_path_allowlist and a status_file, verify
    the status_file matches at least one allowlist pattern."""

    @pytest.mark.parametrize("manifest", list(_manifests_with_write_path_and_status()))
    def test_write_path_covers_status_file(self, manifest: dict):
        status_file = manifest["status_file"]
        allowlist = manifest["v2"]["write_path_allowlist"]

        covered = any(fnmatch.fnmatch(status_file, pat) for pat in allowlist)
        assert covered, (
            f"Agent '{manifest['_agent_id']}' status_file '{status_file}' "
            f"is not covered by write_path_allowlist: {allowlist}"
        )


class TestWritePathCoversInstructionWrites:
    """For each agent with write_path_allowlist, scan its instruction file
    for brain/memory/ path references and verify each is covered."""

    @pytest.mark.parametrize("manifest", list(_manifests_with_write_path_allowlist()))
    def test_write_path_covers_instruction_writes(self, manifest: dict):
        instr_path = _resolve_instruction_path(manifest)
        if instr_path is None:
            pytest.skip(f"Instruction file not found for {manifest['_agent_id']}")

        text = instr_path.read_text()
        write_paths = _extract_write_paths(text)
        if not write_paths:
            pytest.skip(f"No brain/memory/ paths found in {instr_path.name}")

        allowlist = manifest["v2"]["write_path_allowlist"]

        uncovered = []
        for wp in write_paths:
            if not any(fnmatch.fnmatch(wp, pat) for pat in allowlist):
                uncovered.append(wp)

        assert not uncovered, (
            f"Agent '{manifest['_agent_id']}' write_path_allowlist does not cover "
            f"these paths from {instr_path.name}:\n"
            + "\n".join(f"  - {p}" for p in uncovered)
            + "\n\nAllowlist patterns: "
            + str(allowlist)
        )

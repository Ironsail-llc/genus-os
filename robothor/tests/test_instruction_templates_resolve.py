"""Shipped instructions name only files and tools that exist.

The workspace guide told every agent to read ``SOUL.md``, ``USER.md``, daily
notes and ``MEMORY.md`` at the workspace root, where setup puts none of them
(it scaffolds ``brain/SOUL.md`` and ``brain/USER.md`` and never creates the
other two). The operator-facing agent's starter instructions told it to
``read_file`` a directory and to call a function that is not an agent tool.
Every one of those reads failed on every turn.
"""

from __future__ import annotations

import re
from pathlib import Path

from robothor.setup import BRAIN_SCAFFOLD_FILES

_REPO = Path(__file__).resolve().parents[2]
_AGENTS_MD = _REPO / "templates" / "AGENTS.md"
_MAIN = _REPO / "templates" / "agents" / "core" / "main" / "instructions.template.md"


def _every_session_steps() -> str:
    text = _AGENTS_MD.read_text()
    return text.split("## Every Session", 1)[1].split("\n## ", 1)[0]


def test_every_session_reads_files_where_setup_puts_them() -> None:
    steps = _every_session_steps()
    assert "`brain/SOUL.md`" in steps
    assert "`brain/USER.md`" in steps
    # The scaffold really does put them there.
    assert "SOUL.md" in BRAIN_SCAFFOLD_FILES and "USER.md" in BRAIN_SCAFFOLD_FILES
    # No bare root-relative read of a scaffolded file.
    assert not re.search(r"Read `(SOUL|USER)\.md`", steps)


def test_files_setup_never_creates_are_read_only_if_present() -> None:
    steps = _every_session_steps()
    for line in steps.splitlines():
        if "MEMORY.md" in line or "memory/" in line:
            assert "if" in line.lower() and "exist" in line.lower(), line


def test_main_template_uses_only_real_tools_and_files() -> None:
    text = _MAIN.read_text()
    assert 'read_file("docs/agents/")' not in text
    assert "get_fleet_health" not in text
    from robothor.engine.tools.handlers import crm, observability

    handlers = {**crm.HANDLERS, **observability.HANDLERS}
    for tool in ("list_agent_schedules", "list_agent_runs", "list_tasks"):
        assert f"{tool}(" in text, tool
        assert tool in handlers, f"{tool} is not an agent tool"

"""The journal's workspace guard did not contain.

All three journal entry points checked containment with a string prefix:

    if not str(full_path).startswith(str(workspace_resolved)):

`/tmp/x/ws-evil` starts with `/tmp/x/ws`, so a sibling directory whose name
merely begins with the workspace's name passes the check. A manifest whose
`journal_file` is `../ws-evil/current.json` reads a file the agent was never
given, and the loaded state becomes the agent's resume preamble — session
id, iteration, and `next_action` all sourced from outside the workspace.

Demonstrated before the fix:

    workspace: /tmp/tmp18ulmzse/ws
    read from: /tmp/tmp18ulmzse/ws-evil/current.json
    RESULT   : ESCAPED — session_id = attacker-session, next_action = APPLY_CHANGE

The module already documented "Path traversal guard" and had no test for it.
This repo has found six of its own controls inert; this one was worse than
inert, because it ran and returned the wrong answer.

Containment is now `Path.relative_to`, which is the rule the rest of the
codebase already uses — see `deliverables.missing_deliverables_note`, whose
comment explains why you rebuild from the trusted root rather than validate
the untrusted string.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robothor.engine.journal import JournalManager, JournalState

_GOOD = {"session_id": "s1", "agent_id": "victim", "next_action": "APPLY_CHANGE"}


@pytest.fixture
def sandbox(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sibling = tmp_path / "ws-evil"
    sibling.mkdir()
    (sibling / "current.json").write_text(json.dumps(_GOOD))
    return ws, sibling


class TestLoadIsContained:
    def test_a_prefix_sibling_cannot_be_read(self, sandbox):
        """The exact bypass: /tmp/x/ws-evil starts with /tmp/x/ws."""
        ws, _ = sandbox
        assert JournalManager.load("victim", "../ws-evil/current.json", ws) is None

    def test_plain_traversal_is_still_blocked(self, sandbox, tmp_path):
        ws, _ = sandbox
        (tmp_path / "outside.json").write_text(json.dumps(_GOOD))
        assert JournalManager.load("victim", "../outside.json", ws) is None

    def test_an_absolute_path_outside_is_blocked(self, sandbox, tmp_path):
        ws, sibling = sandbox
        assert JournalManager.load("victim", str(sibling / "current.json"), ws) is None

    def test_a_journal_inside_the_workspace_still_loads(self, sandbox):
        ws, _ = sandbox
        d = ws / "brain" / "journals" / "victim"
        d.mkdir(parents=True)
        (d / "current.json").write_text(json.dumps(_GOOD))
        state = JournalManager.load("victim", "brain/journals/victim/current.json", ws)
        assert state is not None and state.session_id == "s1"


class TestSaveIsContained:
    def test_a_prefix_sibling_cannot_be_written(self, sandbox):
        ws, sibling = sandbox
        target = sibling / "written.json"
        state = JournalState(session_id="s", agent_id="victim", next_action="IDLE")
        assert JournalManager.save(state, "../ws-evil/written.json", ws) is False
        assert not target.exists(), "the guard let a write escape the workspace"

    def test_a_journal_inside_the_workspace_still_saves(self, sandbox):
        ws, _ = sandbox
        state = JournalState(session_id="s", agent_id="victim", next_action="IDLE")
        assert JournalManager.save(state, "brain/journals/victim/current.json", ws) is True
        assert (ws / "brain/journals/victim/current.json").exists()


class TestClearIsContained:
    def test_a_prefix_sibling_cannot_be_cleared(self, sandbox):
        ws, sibling = sandbox
        victim_file = sibling / "current.json"
        JournalManager.clear("../ws-evil/current.json", ws)
        assert victim_file.read_text(), "the guard let a clear reach outside the workspace"

    def test_a_journal_inside_the_workspace_still_clears(self, sandbox):
        ws, _ = sandbox
        d = ws / "brain" / "journals" / "victim"
        d.mkdir(parents=True)
        p = d / "current.json"
        p.write_text(json.dumps(_GOOD))
        assert JournalManager.clear("brain/journals/victim/current.json", ws) is True
        assert p.read_text().strip() in ("{}", ""), "clear left the journal populated"


class TestTheOldShapeIsGone:
    def test_no_string_prefix_containment_remains(self):
        """A prefix check reads as containment and is not containment."""
        src = Path(__file__).resolve().parents[1] / "journal.py"
        body = src.read_text(encoding="utf-8")
        assert "startswith(str(" not in body, (
            "journal.py still checks containment with a string prefix"
        )

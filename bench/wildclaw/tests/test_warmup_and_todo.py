"""Two ways the harness measured a platform that was not the one under test.

**The warmup failed silently.** Six of ten Productivity tasks began their
container log with `sh: 1: npm: not found` — the `npm install -g agent-browser`
those tasks declare — and the harness logged it and carried on. So six tasks ran
without the skill the task handed every harness, and the score they produced was
not a score for this platform. The benchmark's own runner raises on a failing
warmup command; ours has to stop too, loudly enough that the failure cannot be
read as an agent result.

**We withheld a tool we ship.** `todo_write` exists in the engine and was not in
the bench manifest, so the agent's requirement list was prose — which compaction
summarises away, and which was itself truncated (`finish_reason=length`) five
times in one sweep. The competing scaffold's first action on its highest-scoring
task was to decompose that task's numbered output requirements into a tracked
list, and it closed the task by marking them complete.

Neither is bench-shaped: one is harness parity with the benchmark's own runner,
the other is giving the agent under test the tools the platform actually ships.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bench.wildclaw import harness

REPO = Path(__file__).resolve().parents[3]


class TestTheWarmupFailsLoudly:
    def test_a_declared_warmup_stops_the_container_when_it_fails(self):
        """`set -e` is the whole mechanism: without it a failing install is one
        line of stderr scrolling past, and the agent runs anyway."""
        prelude = harness._warmup_prelude({"warmup": "npm install -g agent-browser"})
        assert prelude.startswith("set -e")

    def test_it_says_which_command_failed(self):
        prelude = harness._warmup_prelude({"warmup": "npm install -g agent-browser"})
        assert "WARMUP FAILED" in prelude

    def test_the_declared_command_is_still_run(self):
        prelude = harness._warmup_prelude({"warmup": "pip install foo\npython -c 'pass'"})
        assert "pip install foo" in prelude
        assert "python -c 'pass'" in prelude

    def test_comments_and_blanks_are_still_dropped(self):
        prelude = harness._warmup_prelude({"warmup": "# a note\n\npip install foo\n"})
        assert "# a note" not in prelude
        assert "pip install foo" in prelude

    def test_a_task_with_no_warmup_gets_no_prelude(self):
        """An empty guard would turn every task into a two-line shell script
        whose only job is to be correct about nothing."""
        assert harness._warmup_prelude({}) == ""
        assert harness._warmup_prelude({"warmup": "\n# only a comment\n"}) == ""

    def test_the_failure_is_visible_outside_the_container(self):
        """A container that exits 3 with nothing on the host mount is
        indistinguishable from an agent crash. The marker is what tells the
        ledger which of the two happened."""
        prelude = harness._warmup_prelude({"warmup": "npm install -g agent-browser"})
        assert "/out/" in prelude


class TestTheImageHasWhatTheTasksDeclare:
    def test_npm_is_installed(self):
        """`npm install -g agent-browser` is declared by six of the ten
        Productivity tasks. An image without npm cannot run any of them."""
        dockerfile = (REPO / "bench" / "wildclaw" / "Dockerfile").read_text()
        assert "npm" in dockerfile


class TestTheAgentGetsItsTodoTool:
    @staticmethod
    def _manifest() -> dict:
        return yaml.safe_load((REPO / "bench" / "wildclaw" / "agent.yaml").read_text())

    def test_todo_write_is_allowed(self):
        assert "todo_write" in self._manifest()["tools_allowed"]

    def test_the_todo_list_is_enabled(self):
        """The allow-list is not enough: `registry` filters TODO_TOOLS out
        again unless `todo_list_enabled` is set, so a manifest with one and not
        the other advertises nothing and looks configured."""
        assert self._manifest()["v2"]["todo_list_enabled"] is True

    def test_both_halves_survive_the_registry_filter(self):
        from robothor.engine.tools.constants import TODO_TOOLS

        manifest = self._manifest()
        assert set(manifest["tools_allowed"]) >= TODO_TOOLS
        assert manifest["v2"]["todo_list_enabled"]

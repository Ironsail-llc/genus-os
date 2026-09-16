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

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from bench.wildclaw import harness

REPO = Path(__file__).resolve().parents[3]

#: The benchmark declares a warmup as a fenced block under a `## Warmup`
#: heading. Read straight from the spec rather than through the harness's own
#: loader, so the test reads what the benchmark wrote.
_WARMUP_RE = re.compile(
    r"^##\s+Warmup\s*$\n+```[a-z]*\n(.*?)^```", re.MULTILINE | re.DOTALL | re.IGNORECASE
)


def _declared_warmup(spec_text: str) -> str:
    match = _WARMUP_RE.search(spec_text)
    return match.group(1) if match else ""


class TestTheWarmupParses:
    """A prelude that does not parse is worse than one that does not check.

    The whole container script is one `sh -c`: a parse error means the agent
    never starts, the grader never runs, and `/out/warmup.failed` is never
    written — the operator gets a bare `Syntax error` with no marker, which is
    the indistinguishable-from-an-agent-crash case the marker exists to
    prevent. The first version rewrote each line as `<line> || _warmup_failed`,
    and a warmup that backgrounds a mock service (`cmd &`) then became
    `cmd & || …`, a dash SYNTAX ERROR for 7 of 60 tasks — the whole mock-service
    half of one category (hostile review 2026-09-16, C2).
    """

    @staticmethod
    def _parses(script: str) -> tuple[bool, str]:
        if not script:
            return True, ""
        for shell in ("/bin/dash", "/bin/sh"):
            if Path(shell).exists():
                proc = subprocess.run(  # noqa: S603 — fixed argv, our own text
                    [shell, "-n"], input=script, capture_output=True, text=True
                )
                return proc.returncode == 0, proc.stderr.strip()
        pytest.skip("no POSIX shell to parse with")

    @pytest.mark.parametrize(
        "warmup",
        [
            "npm install -g agent-browser",
            "python /root/mock_service.py &",
            "python /root/mock_service.py &\nsleep 2",
            "pip install foo && pip install bar",
            "cd /tmp && python -m http.server 8000 &",
            "echo 'single quoted' > /tmp/x",
            "a=1; export a",
            "for i in 1 2 3; do echo $i; done",
        ],
        ids=[
            "plain",
            "backgrounded",
            "backgrounded-then-sync",
            "chained",
            "cd-and-background",
            "single-quotes",
            "assignment",
            "loop",
        ],
    )
    def test_generated_preludes_parse(self, warmup):
        ok, err = self._parses(harness._warmup_prelude({"warmup": warmup}))
        assert ok, f"{warmup!r} -> {err}"

    def test_every_benchmark_task_s_prelude_parses(self):
        """The corpus, read-only, because a unit fixture is what missed this:
        the one command smoke-tested by hand was the one that already parsed."""
        tasks = Path("/home/philip/robothor-bench/WildClawBench/tasks")
        if not tasks.is_dir():
            pytest.skip("benchmark checkout not present")
        failures: list[str] = []
        seen = 0
        for spec in sorted(tasks.rglob("*.md")):
            warmup = _declared_warmup(spec.read_text(encoding="utf-8", errors="replace"))
            if not warmup:
                continue
            seen += 1
            ok, err = self._parses(harness._warmup_prelude({"warmup": warmup}))
            if not ok:
                failures.append(f"{spec.name}: {err}")
        assert seen >= 40, f"only {seen} specs declared a warmup — is the corpus right?"
        assert not failures, failures

    def test_a_backgrounded_service_survives_the_prelude(self):
        """Not just parseable — the point of backgrounding is that the process
        outlives the line, which is why the grader runs in this same container."""
        ok, err = self._parses(harness._warmup_prelude({"warmup": "sleep 30 &\necho started"}))
        assert ok, err

    def test_a_warmup_that_is_not_shell_becomes_the_marker_not_a_syntax_error(self):
        """No wrapper can rescue the parse — the lines share one shell on
        purpose, so a backgrounded service outlives its line and `cd` reaches
        the next one. What it CAN do is fail with the marker, which is the
        difference between "this task's setup is wrong" and an unexplained
        container exit. (The benchmark's own task template carries a prose
        block in its warmup field, which is how this case was found.)"""
        prelude = harness._warmup_prelude({"warmup": "`unterminated\n<!-- prose -->"})
        ok, err = self._parses(prelude)
        assert ok, f"the fallback prelude must itself parse: {err}"
        assert "WARMUP FAILED" in prelude
        assert "/out/warmup.failed" in prelude
        assert "exit 3" in prelude


class TestTheWarmupFailsLoudly:
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

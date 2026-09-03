"""The deliverable contract must fire on the task that actually failed.

2026-08-27. WildClawBench `01_Productivity_Flow_task_4_2022_conference_papers`
scored 0.0 with `output_exists: 0.0` — the agent spent 333 requests and 704
seconds, reported status "completed", and wrote no file at all. That single
task is most of this fleet's -10.4pt gap on the category; seven of the ten
tasks are within 0.02 of the comparison harness.

`deliverable_contract` exists precisely for that failure and has written ZERO
guardrail rows in its lifetime. Probing it against the real task text showed
why, and it was not the disabled flag: `required_deliverables()` returned []
on a task whose prompt names `/tmp_workspace/results/2022.tsv` in backticks.
Enabling the flag would have changed nothing — the control was inert twice
over, and the flag would have hidden that behind an "enforcing" dashboard.

The wording it missed is the ordinary one:

    ...and save them to:

    - `/tmp_workspace/results/2022.tsv`

The pattern required the preposition to be followed by whitespace on the SAME
line. Real prompts put a colon after it and the path on the next line as a
list item.

These tests read the real task file when present (it is instance data) and
otherwise assert on the same shape inline, so the platform keeps the guarantee
on a checkout that has no benchmark suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from robothor.engine.deliverable_contract import required_deliverables

#: The benchmark checkout is instance data, so its location is an env var and
#: never a path in platform code — `WILDCLAW_REPO` is the same variable the
#: bench rotation reads (bench/wildclaw/README.md). Unset is the normal state
#: of CI and of a fresh checkout, and skips only the tests that need the file.
_WILDCLAW = os.environ.get("WILDCLAW_REPO")
_REAL_TASK = Path(
    _WILDCLAW or "",
    "tasks/01_Productivity_Flow/01_Productivity_Flow_task_4_2022_conference_papers.md",
)


class TestTheShapeThatWasMissed:
    def test_a_colon_and_a_list_item_still_names_a_deliverable(self):
        text = "Please compile the papers and save them to:\n\n- `/tmp/results/2022.tsv`\n"
        assert "/tmp/results/2022.tsv" in required_deliverables(text)

    def test_a_colon_on_the_same_line_works(self):
        assert "out/report.md" in required_deliverables("Write the summary to: out/report.md")

    def test_the_plain_form_still_works(self):
        """The existing behaviour must not regress."""
        assert "out/x.tsv" in required_deliverables("save the table to out/x.tsv")
        assert "r.json" in required_deliverables("export results as r.json")


class TestItDoesNotOverReach:
    """Silence is the safe default. A false deliverable would fail a run that
    did exactly what it was asked."""

    def test_an_input_file_is_not_a_deliverable(self):
        assert required_deliverables("Read the records from data/input.csv and summarise") == []

    def test_a_url_is_not_a_local_path(self):
        assert required_deliverables("save them to https://example.com/results/2022.tsv") == []

    def test_prose_mentioning_a_file_is_not_a_contract(self):
        assert required_deliverables("The 2022.tsv format has one row per paper.") == []

    def test_no_task_text_is_no_contract(self):
        assert required_deliverables(None) == []
        assert required_deliverables("") == []


class TestAgainstTheRealFailingTask:
    # Class-scoped, not module-scoped: the inline-shape tests above are the
    # platform's guarantee on a checkout with no benchmark suite, and skipping
    # them alongside this one would leave the contract untested in CI.
    pytestmark = pytest.mark.skipif(
        not (_WILDCLAW and _REAL_TASK.is_file()),
        reason="benchmark suite is instance data; set WILDCLAW_REPO to run",
    )

    def test_the_task_that_scored_zero_declares_a_deliverable(self):
        found = required_deliverables(_REAL_TASK.read_text())
        assert "/tmp_workspace/results/2022.tsv" in found, (
            "the contract still cannot see the deliverable in the task it exists to catch"
        )


class TestTheFinalizerCanActuallySeeTheTask:
    """The second reason this control was inert, flagged in its own docstring.

    `task_text_for_run` reads the originating CRM task via `run.task_id`. A
    benchmark run — or any run triggered by a message rather than a delegated
    task — has no task_id, so it returned "" and the contract found nothing to
    require. Fixing the patterns alone would have left it blind on exactly the
    runs that motivated it.

    `agent_runs` stores only prompt CHAR COUNTS, not the text, so the wording
    has to be retained on the session. Recovering it from `session.messages`
    is not enough: the failing task ran 333 requests and 3.4M input tokens, so
    compaction would have dropped the first user message long before
    finalization.
    """

    def test_the_session_retains_the_originating_message(self):
        from robothor.engine.models import TriggerType
        from robothor.engine.session import AgentSession

        s = AgentSession("probe", TriggerType.EVENT, "d", "t")
        s.start("sys", "save them to:\n\n- `/tmp/x.tsv`\n", [])
        assert getattr(s, "originating_message", None), (
            "the task wording is gone by finalization, so no contract can be read"
        )

    def test_a_run_with_no_crm_task_still_yields_task_text(self):
        from robothor.engine.deliverable_contract import task_text_for_run
        from robothor.engine.models import TriggerType
        from robothor.engine.session import AgentSession

        s = AgentSession("probe", TriggerType.EVENT, "d", "t")
        s.start("sys", "Please save them to:\n\n- `/tmp/x.tsv`\n", [])

        class _Run:
            task_id = None
            tenant_id = "t"

        assert "/tmp/x.tsv" in task_text_for_run(_Run(), session=s)

    def test_the_crm_task_still_wins_when_present(self):
        """A delegated task is the more authoritative source; the message is
        the fallback, not a replacement."""
        from robothor.engine.deliverable_contract import task_text_for_run

        class _Run:
            task_id = None
            tenant_id = "t"

        assert task_text_for_run(_Run(), session=None) == ""

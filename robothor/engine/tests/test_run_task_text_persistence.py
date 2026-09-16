"""The wording a run was asked to satisfy has to outlive the session.

`agent_runs` stored `user_prompt_chars` — a COUNT of the prompt, not the
prompt. `run_finalizer` runs after the loop has ended, by which point
compaction may have dropped the first user turn from the live window (one
measured run made 333 requests and kept 62 of its messages), and a resumed run
is a different session object entirely. So the deliverable contract had no
source of task text at the moment it computes its verdict.

Migration 123 adds the column. This file holds the two things that make it
safe to write: it is redacted through the same door as chat history, and it is
bounded.
"""

from __future__ import annotations

from robothor.engine.deliverable_contract import (
    TASK_TEXT_MAX_CHARS,
    task_text_for_column,
    task_text_for_run,
)


class _Run:
    def __init__(self, task_text=None, task_id=None):
        self.task_text = task_text
        self.task_id = task_id
        self.tenant_id = "default"


class _Session:
    def __init__(self, message=""):
        self.originating_message = message


class TestTheColumnValue:
    def test_a_credential_in_a_task_never_reaches_the_column(self):
        """`agent_runs` outlives the session, is exported into support bundles
        and is read by the Helm. Same rule and same redactor as
        `chat_store.save_exchange`: redacted at the door, not at each caller."""
        stored = task_text_for_column(
            "Deploy it. The API_KEY=sk-liveexamplekeyvalue000 is in the vault."
        )
        assert stored is not None
        assert "sk-liveexamplekeyvalue000" not in stored

    def test_it_is_bounded(self):
        stored = task_text_for_column("x" * (TASK_TEXT_MAX_CHARS * 3))
        assert stored is not None
        assert len(stored) <= TASK_TEXT_MAX_CHARS

    def test_the_cap_eats_the_middle_not_the_contract(self):
        """A spec states its output requirements at the END as often as at the
        start. A cap that always dropped the tail would discard exactly the
        half this column exists to carry."""
        text = "Save it to results/a.tsv\n" + ("filler " * 40_000) + "\nuse this exact header"
        stored = task_text_for_column(text)
        assert stored is not None
        assert stored.startswith("Save it to results/a.tsv")
        assert stored.endswith("use this exact header")

    def test_nothing_in_means_nothing_stored(self):
        assert task_text_for_column("") is None
        assert task_text_for_column(None) is None

    def test_a_short_prompt_is_stored_whole(self):
        assert task_text_for_column("Write the report to out/r.md") == (
            "Write the report to out/r.md"
        )


class TestWhereTheCheckerLooks:
    def test_the_persisted_column_wins(self):
        """It is the only source that survives compaction, a resume, and the
        finalizer running after the loop."""
        run = _Run(task_text="save it to results/persisted.tsv")
        session = _Session("save it to results/live.tsv")
        assert task_text_for_run(run, session) == "save it to results/persisted.tsv"

    def test_a_live_session_still_works_without_the_column(self):
        assert task_text_for_run(_Run(), _Session("save it to results/live.tsv")) == (
            "save it to results/live.tsv"
        )

    def test_a_crm_task_no_longer_hides_the_prompt(self):
        """Reading the task row first meant a run WITH a crm_task could never
        see its own spec — and of 4,000 crm_tasks probed over 60 days, ZERO
        named an explicit output path. The row is the last resort, not the
        first."""
        run = _Run(task_text="save it to results/persisted.tsv", task_id="t-1")
        assert task_text_for_run(run, None) == "save it to results/persisted.tsv"

    def test_no_source_at_all_requires_nothing(self):
        assert task_text_for_run(_Run(), None) == ""


class TestTheRunCarriesIt:
    def test_the_session_writes_the_column_at_start(self):
        from robothor.engine.models import AgentRun
        from robothor.engine.session import AgentSession

        session = AgentSession(AgentRun(agent_id="a"), "a")
        session.start("system", "Save the table to results/x.tsv", [])
        assert session.run.task_text == "Save the table to results/x.tsv"

    def test_the_insert_carries_the_column(self):
        """A column the writer never fills is the same as no column at all —
        this instance has shipped that six times."""
        import inspect

        from robothor.engine import tracking

        source = inspect.getsource(tracking.create_run)
        assert "task_text" in source
        placeholders = source.count("%s")
        columns = source.split("INSERT INTO agent_runs (")[1].split(")")[0]
        names = [c.strip() for c in columns.replace("\n", " ").split(",") if c.strip()]
        assert placeholders == len(names), (names, placeholders)

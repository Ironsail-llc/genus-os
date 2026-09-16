"""A database one migration behind must not cost the whole step trail.

Migration 125 adds `agent_run_steps.batch_id` / `batch_position`. The writer
names them unconditionally, so against a database that has not taken 124 the
INSERT raises `UndefinedColumn` — a `ProgrammingError`, not in `retry_sync`'s
retryable set. `flush_new_steps_sync` catches it, falls back to per-step inserts
that each raise again, logs one warning per step, and advances
`persisted_step_count` regardless. Every run keeps running and its `agent_runs`
row updates normally, so the only symptom is a run viewer with nothing in it —
LLM steps included, not merely the batched ones.

Losing the whole trail to a deploy ordering is not a trade worth making. The
writer degrades to the pre-125 insert, says so once and loudly, and the startup
line names what is pending so the degradation is something an operator can act
on rather than something they discover.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine import tracking
from robothor.engine.models import RunStep, StepType


@pytest.fixture(autouse=True)
def _fresh_latch(monkeypatch):
    """The latch is module state; no test may inherit another's answer."""
    monkeypatch.setattr(tracking, "_batch_columns_present", None)


def _step() -> RunStep:
    return RunStep(
        run_id="run-1",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        tool_name="read_file",
        batch_id="6f1b0d7c-0000-4000-8000-000000000000",
        batch_position=3,
    )


class _MissingColumnError(Exception):
    """What psycopg2 raises, in the shape the detector reads."""


class TestTheDetectorIsNarrow:
    def test_it_recognises_the_missing_column(self):
        exc = _MissingColumnError('column "batch_id" of relation "agent_run_steps" does not exist')
        assert tracking._missing_batch_columns(exc) is True

    def test_it_recognises_the_other_one(self):
        exc = _MissingColumnError('column "batch_position" does not exist')
        assert tracking._missing_batch_columns(exc) is True

    def test_it_does_not_swallow_an_unrelated_schema_error(self):
        """A writer that degrades on every failure is a writer that cannot tell
        you it is broken."""
        exc = _MissingColumnError(
            'column "tool_output" of relation "agent_run_steps" does not exist'
        )
        assert tracking._missing_batch_columns(exc) is False

    def test_it_does_not_swallow_a_permission_error(self):
        exc = _MissingColumnError("permission denied for table agent_run_steps")
        assert tracking._missing_batch_columns(exc) is False


class TestTheInsertShape:
    def test_it_names_the_batch_columns_by_default(self):
        columns, placeholders = tracking._step_columns()
        assert "batch_id, batch_position" in columns
        assert placeholders.count("%s") == 18
        assert len(tracking._step_values(_step())) == 18

    def test_it_drops_them_once_the_database_has_said_it_lacks_them(self, monkeypatch):
        monkeypatch.setattr(tracking, "_batch_columns_present", False)
        columns, placeholders = tracking._step_columns()
        assert "batch_id" not in columns
        assert placeholders.count("%s") == 16
        assert len(tracking._step_values(_step())) == 16

    def test_the_columns_and_the_values_never_disagree(self):
        """Two lists that must be the same length, written in two places."""
        for present in (None, False):
            tracking._batch_columns_present = present
            columns, placeholders = tracking._step_columns()
            assert len(columns.split(",")) == placeholders.count("%s")
            assert len(tracking._step_values(_step())) == placeholders.count("%s")


class TestItSaysSoOnceAndLoudly:
    def test_the_line_names_the_migration_and_the_fix(self, caplog):
        with caplog.at_level(logging.ERROR, logger="robothor.engine.tracking"):
            tracking._note_missing_batch_columns()
        assert "125_agent_run_step_batch" in caplog.text
        assert "genus migrate" in caplog.text

    def test_it_does_not_repeat_for_every_step(self, caplog):
        """One line per process. A per-step line is how the credential pool
        logged one outage 452 times while paging zero."""
        with caplog.at_level(logging.ERROR, logger="robothor.engine.tracking"):
            for _ in range(5):
                tracking._note_missing_batch_columns()
        assert caplog.text.count("125_agent_run_step_batch") == 1

    def test_the_latch_sticks(self):
        tracking._note_missing_batch_columns()
        assert tracking._batch_columns_present is False

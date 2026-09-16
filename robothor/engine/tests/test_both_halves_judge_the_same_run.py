"""The loop and the finalizer must judge one run, not two.

Both halves of this control read a workspace and a task text, and until now
they read them from different places (hostile review 2026-09-16, I4/I5):

- the loop resolved `agent_config.workspace or config.workspace`; the finalizer
  only ever had `config.workspace`. A manifest with its own workspace made the
  two differ, so the finalizer could produce a verdict about a directory the
  agent never wrote to.
- the check-in comparison read `session.originating_message`; the verdict read
  `task_text_for_run`. On a RESUMED run the session carries no originating
  message, so the comparison stayed silent while the re-ask still fired — the
  agent was failed for a contract it was never shown mid-run.

Both are the same defect in two costumes: a run judged against something it was
not shown.
"""

from __future__ import annotations

import pytest

from robothor.engine.deliverable_verdict import WORKSPACE_ATTR, resolve_workspace
from robothor.engine.loop_guards import append_engine_note, reask_for_wrong_deliverable_shape
from robothor.engine.models import AgentRun, RunStatus

SPEC = (
    "Save the table to `/work/results/rows.tsv`.\n\n"
    "The TSV must use exactly the following header:\n\n"
    "```text\nTrack\tTitle\tSpeakers\n```\n"
)


class _Session:
    def __init__(self, run, message=""):
        self.run = run
        self.originating_message = message
        self.messages: list[dict] = []


def _run(task_text=None):
    run = AgentRun(agent_id="probe", status=RunStatus.COMPLETED)
    run.task_text = task_text
    return run


def _wrong_shape(root):
    (root / "results").mkdir(parents=True)
    (root / "results" / "rows.tsv").write_text("Title\tSpeakers\n", encoding="utf-8")


@pytest.fixture
def mode(monkeypatch):
    def _set(value):
        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: value
        )

    return _set


class TestOneWorkspace:
    def test_the_loop_records_the_root_it_judged(self, tmp_path, mode):
        mode("enforce")
        _wrong_shape(tmp_path)
        session = _Session(_run(), SPEC)
        reask_for_wrong_deliverable_shape(session, str(tmp_path))
        assert getattr(session, WORKSPACE_ATTR) == str(tmp_path)

    def test_the_finalizer_prefers_it_over_its_own_fallback(self, tmp_path):
        """The manifest's workspace, not the engine-wide one."""
        session = _Session(_run())
        setattr(session, WORKSPACE_ATTR, str(tmp_path / "manifest-root"))
        assert resolve_workspace(session, str(tmp_path / "engine-root")) == str(
            tmp_path / "manifest-root"
        )

    def test_the_fallback_still_covers_a_run_that_never_reached_the_loop(self, tmp_path):
        assert resolve_workspace(_Session(_run()), str(tmp_path)) == str(tmp_path)

    def test_neither_is_not_an_error(self):
        assert resolve_workspace(_Session(_run()), None) is None

    def test_a_manifest_workspace_is_the_one_judged_end_to_end(self, tmp_path, mode, monkeypatch):
        """The whole point: the finalizer must not read the engine-wide root
        and report a verdict about a directory the agent never touched."""
        from robothor.engine.deliverable_verdict import record_deliverable_verdicts

        mode("enforce")
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda **kw: None, raising=False
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert", lambda **kw: None
        )
        agent_root = tmp_path / "agent"
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        agent_root.mkdir()
        (agent_root / "results").mkdir()
        (agent_root / "results" / "rows.tsv").write_text(
            "Track\tTitle\tSpeakers\n", encoding="utf-8"
        )
        session = _Session(_run(), SPEC)
        reask_for_wrong_deliverable_shape(session, str(agent_root))
        record_deliverable_verdicts(session.run, session, str(engine_root))
        assert session.run.status == RunStatus.COMPLETED, (
            "the finalizer judged the engine root, where the agent never wrote"
        )


class TestOneTaskText:
    def test_a_resumed_run_still_gets_the_comparison(self, tmp_path, mode):
        """No `originating_message` — the shape of a resumed run. The persisted
        `run.task_text` is the source both halves share."""
        mode("enforce")
        _wrong_shape(tmp_path)
        session = _Session(_run(task_text=SPEC), message="")
        append_engine_note(session, "[SYSTEM] Progress check-in.", str(tmp_path))
        content = session.messages[0]["content"]
        assert "Track Title Speakers" in content, "the check-in was silent on a resumed run"

    def test_the_re_ask_and_the_check_in_agree_on_a_resumed_run(self, tmp_path, mode):
        """The failure was asymmetric: the re-ask fired from `task_text_for_run`
        while the check-in read an empty `originating_message`, so the agent was
        failed for a contract nothing had shown it."""
        mode("enforce")
        _wrong_shape(tmp_path)
        session = _Session(_run(task_text=SPEC), message="")
        append_engine_note(session, "[SYSTEM] Progress check-in.", str(tmp_path))
        saw_comparison = "Track Title Speakers" in session.messages[0]["content"]
        re_asked = reask_for_wrong_deliverable_shape(session, str(tmp_path))
        assert saw_comparison == re_asked

    def test_a_live_session_is_unaffected(self, tmp_path, mode):
        mode("enforce")
        _wrong_shape(tmp_path)
        session = _Session(_run(), message=SPEC)
        append_engine_note(session, "[SYSTEM] Progress check-in.", str(tmp_path))
        assert "Track Title Speakers" in session.messages[0]["content"]

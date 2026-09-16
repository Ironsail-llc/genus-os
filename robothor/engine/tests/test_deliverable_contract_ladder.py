"""Where the shape contract acts, and what each rung of the ladder does.

Two places, one ladder. The check-in turns a question the agent answers yes to
("are you making progress?") into a comparison it cannot ("your header is X and
the task requires Y"). The loop's end gate is the only point where a verdict
can still change the outcome — `run_finalizer` runs after the loop and can
record a wrong-shaped deliverable, never prevent one.

`off` computes nothing. `observe` records the verdict and lets the run end.
`enforce` re-asks once and, if the shape is still wrong, fails the run with the
report in its summary rather than letting it report success. That last part is
the point: three measured runs reported `completed` with the file present, the
header wrong, and every grader criterion at 0.
"""

from __future__ import annotations

import pytest

from robothor.engine.deliverable_contract import contract_checkin_note
from robothor.engine.deliverable_verdict import record_deliverable_verdicts
from robothor.engine.loop_guards import reask_for_wrong_deliverable_shape
from robothor.engine.models import AgentRun, RunStatus

SPEC = """\
Save the table to `/work/results/rows.tsv`.

The TSV must use exactly the following header:

```text
Track   Title   Speakers
```
"""


class _Session:
    def __init__(self, run, message=SPEC):
        self.run = run
        self.originating_message = message
        self.messages: list[dict] = []


def _run(**kw):
    return AgentRun(agent_id="probe", status=RunStatus.COMPLETED, **kw)


def _wrong_shape(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "rows.tsv").write_text("Title\tSpeakers\n", encoding="utf-8")


def _right_shape(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "rows.tsv").write_text("Track\tTitle\tSpeakers\n", encoding="utf-8")


@pytest.fixture
def mode(monkeypatch):
    def _set(value):
        monkeypatch.setattr(
            "robothor.engine.feature_flags.deliverable_contract_mode", lambda: value
        )

    return _set


# ─── The check-in ─────────────────────────────────────────────────────


class TestTheCheckinIsAComparison:
    def test_it_names_both_headers(self, tmp_path):
        _wrong_shape(tmp_path)
        note = contract_checkin_note(SPEC, tmp_path)
        assert note is not None
        assert "Title Speakers" in note
        assert "Track Title Speakers" in note

    def test_it_is_silent_when_the_shape_is_right(self, tmp_path):
        _right_shape(tmp_path)
        assert contract_checkin_note(SPEC, tmp_path) is None

    def test_it_is_silent_when_the_task_stated_no_shape(self, tmp_path):
        assert contract_checkin_note("Summarise the inbox.", tmp_path) is None

    def test_it_is_silent_without_a_workspace(self):
        """Nothing to compare against is not a breach; unconfined filesystem
        access on untrusted task text would be."""
        assert contract_checkin_note(SPEC, None) is None


# ─── The loop's end gate ──────────────────────────────────────────────


class TestTheReask:
    def test_off_computes_nothing(self, tmp_path, mode):
        mode("off")
        session = _Session(_run())
        _wrong_shape(tmp_path)
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is False
        assert session.messages == []
        assert not hasattr(session, "_deliverable_contract_report")

    def test_observe_records_and_lets_the_run_end(self, tmp_path, mode, caplog):
        mode("observe")
        session = _Session(_run())
        _wrong_shape(tmp_path)
        with caplog.at_level("WARNING"):
            assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is False
        assert session.messages == [], "observe must not change what the model sees"
        assert session._deliverable_contract_report is not None
        assert any("deliverable contract observe:" in r.getMessage() for r in caplog.records)

    def test_enforce_re_asks_once_with_the_report(self, tmp_path, mode):
        mode("enforce")
        session = _Session(_run())
        _wrong_shape(tmp_path)
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is True
        assert len(session.messages) == 1
        assert "Track Title Speakers" in session.messages[0]["content"]

    def test_the_re_ask_quotes_the_spec(self, tmp_path, mode):
        """An agent told its header is wrong argues; an agent shown the sentence
        it is wrong against fixes it."""
        mode("enforce")
        session = _Session(_run())
        _wrong_shape(tmp_path)
        reask_for_wrong_deliverable_shape(session, str(tmp_path))
        assert "the task said:" in session.messages[0]["content"]

    def test_the_re_ask_leaves_room_to_refuse(self, tmp_path, mode):
        """Some tasks should not be completed. A gate that can only say
        'produce it' turns a correct refusal into a fabricated artifact."""
        mode("enforce")
        session = _Session(_run())
        _wrong_shape(tmp_path)
        reask_for_wrong_deliverable_shape(session, str(tmp_path))
        content = session.messages[0]["content"]
        assert "should not complete" in content
        assert "do not invent content" in content

    def test_the_re_ask_is_spent_once(self, tmp_path, mode):
        """An unbounded 'you are not done' is a loop, and the agent may have a
        reason the shape is wrong that trying again will not fix."""
        mode("enforce")
        session = _Session(_run())
        _wrong_shape(tmp_path)
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is True
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is False
        assert len(session.messages) == 1

    def test_a_right_shape_ends_the_run(self, tmp_path, mode):
        mode("enforce")
        session = _Session(_run())
        _right_shape(tmp_path)
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is False

    def test_a_task_with_no_shape_ends_the_run(self, tmp_path, mode):
        mode("enforce")
        session = _Session(_run(), message="Summarise the inbox.")
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is False


# ─── The finalizer ────────────────────────────────────────────────────


class TestTheHonestFailure:
    def test_enforce_refuses_to_let_the_run_claim_completion(self, tmp_path, mode, monkeypatch):
        mode("enforce")
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda **kw: None, raising=False
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert", lambda **kw: None
        )
        _wrong_shape(tmp_path)
        run = _run()
        session = _Session(run)
        record_deliverable_verdicts(run, session, str(tmp_path))
        assert run.status == RunStatus.FAILED
        assert "Deliverable contract not satisfied" in (run.error_message or "")
        assert "Track Title Speakers" in (run.error_message or "")

    def test_observe_leaves_the_run_alone(self, tmp_path, mode, monkeypatch):
        mode("observe")
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda **kw: None, raising=False
        )
        _wrong_shape(tmp_path)
        run = _run()
        record_deliverable_verdicts(run, _Session(run), str(tmp_path))
        assert run.status == RunStatus.COMPLETED
        assert not run.error_message

    def test_a_run_that_already_failed_keeps_its_own_cause(self, tmp_path, mode, monkeypatch):
        """Burying the error a run actually hit under this verdict would lose
        the cause — and the cause is what an operator opens the row for."""
        mode("enforce")
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda **kw: None, raising=False
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert", lambda **kw: None
        )
        _wrong_shape(tmp_path)
        run = _run()
        run.status = RunStatus.TIMEOUT
        run.error_message = "watchdog: hard timeout at 1200s"
        record_deliverable_verdicts(run, _Session(run), str(tmp_path))
        assert run.status == RunStatus.TIMEOUT
        assert "watchdog: hard timeout" in (run.error_message or "")

    def test_a_satisfied_shape_writes_nothing(self, tmp_path, mode, monkeypatch):
        """A vacuous pass recorded on every run in the fleet buries the real
        verdicts — the defect the alert digest already has."""
        mode("enforce")
        calls: list[dict] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda **kw: calls.append(kw),
            raising=False,
        )
        _right_shape(tmp_path)
        run = _run()
        record_deliverable_verdicts(run, _Session(run), str(tmp_path))
        assert calls == []
        assert run.status == RunStatus.COMPLETED

    def test_it_re_reads_rather_than_trusting_the_loop(self, tmp_path, mode, monkeypatch):
        """The loop's verdict is a statement about the moment the agent stopped.

        The re-ask exists so the agent can FIX the file, so between the loop's
        read and this one the workspace may have changed — and it is the later
        answer that is true. Preferring the stash made every compliant run
        record as `failed` (hostile review C1)."""
        mode("enforce")
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event", lambda **kw: None, raising=False
        )
        _wrong_shape(tmp_path)
        run = _run()
        session = _Session(run)
        reask_for_wrong_deliverable_shape(session, str(tmp_path))
        (tmp_path / "results" / "rows.tsv").write_text("Track\tTitle\tSpeakers\n", encoding="utf-8")
        record_deliverable_verdicts(run, session, str(tmp_path))
        assert run.status == RunStatus.COMPLETED
        assert not run.error_message


@pytest.mark.usefixtures("mode")
class TestComplyingWithTheReAskEndsWell:
    """stop -> re-ask -> the agent fixes the file -> stop -> finalize.

    The one success path this feature exists to create, and the only one no
    test drove. It was unreachable: `reask_for_wrong_deliverable_shape`
    returned early on a satisfied re-check without clearing the stash the
    previous stop had set, and the finalizer preferred that stash — so a run
    that did exactly what it was told still ended `failed`, with a `blocked`
    guardrail row and an operator alert (hostile review 2026-09-16, C1).
    """

    @staticmethod
    def _events(monkeypatch) -> list[dict]:
        rows: list[dict] = []
        monkeypatch.setattr(
            "robothor.engine.tracking.log_guardrail_event",
            lambda **kw: rows.append(kw),
            raising=False,
        )
        monkeypatch.setattr(
            "robothor.engine.feature_flags.notify_guardrail_alert",
            lambda **kw: rows.append({"alert": kw}),
        )
        return rows

    def test_the_compliant_run_completes(self, tmp_path, mode, monkeypatch):
        mode("enforce")
        rows = self._events(monkeypatch)
        _wrong_shape(tmp_path)
        run = _run()
        session = _Session(run)

        # First stop: the shape is wrong, the loop re-asks once.
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is True
        assert len(session.messages) == 1

        # The agent complies.
        (tmp_path / "results" / "rows.tsv").write_text("Track\tTitle\tSpeakers\n", encoding="utf-8")

        # Second stop: nothing left to say, and the stash must not survive it.
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is False
        assert session._deliverable_contract_report is None, "a stale verdict outlived its facts"

        record_deliverable_verdicts(run, session, str(tmp_path))
        assert run.status == RunStatus.COMPLETED
        assert not run.error_message
        assert rows == [], "a compliant run must write no guardrail row and raise no alert"

    def test_a_run_that_ignores_the_re_ask_still_fails(self, tmp_path, mode, monkeypatch):
        """The other half of the same path — the fix must not make the gate
        toothless."""
        mode("enforce")
        rows = self._events(monkeypatch)
        _wrong_shape(tmp_path)
        run = _run()
        session = _Session(run)
        assert reask_for_wrong_deliverable_shape(session, str(tmp_path)) is True
        record_deliverable_verdicts(run, session, str(tmp_path))
        assert run.status == RunStatus.FAILED
        assert any(r.get("action") == "blocked" for r in rows)

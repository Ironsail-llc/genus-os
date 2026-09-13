"""A thin announce reply falls back to the note the run actually wrote.

The failure this pins: a scheduled announce agent does all its work, saves the
report as a CRM note via ``create_note`` (a 1,000+ char body), and then ends the
run with ``"Briefing delivered."``. ``deliver()`` announced that 19-char stub and
the operator received a header with nothing under it. The platform already
*detected* it — ``run_finalizer._assess_outcome`` writes "Thin announce output
(N chars) — likely meta-confirmation instead of full content" — and then did
nothing about it.

What must stay true, and is asserted here rather than assumed:

* the note body is delivered only when the final text is thin AND the run
  itself wrote a substantial note;
* the substitution is looked up in THIS run's steps, under the ``create_note``
  tool name — a note from another run, an unattributed step, or another tool's
  ``body`` argument is never used;
* a thin note body is not a rescue, it is another stub;
* a real report is never replaced by a note, however long the note is;
* with several notes the most recent one wins;
* the outcome note states what actually happened — whether the note saved, and
  whether the send was acknowledged — because it is the only record explaining
  why the delivered text differs from ``agent_runs.output_text``;
* the thin threshold is the one ``run_finalizer`` already flags on, not a
  second constant that can drift away from it.

Each guard is pinned by a test that reds when THAT guard is deleted: the review
that ordered this round found three guards surviving their own mutation.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.engine import delivery as delivery_mod
from robothor.engine import run_finalizer, thin_announce
from robothor.engine.channels import SendReceipt, register_channel, reset_channels
from robothor.engine.delivery import deliver, set_telegram_sender
from robothor.engine.models import (
    AgentConfig,
    AgentRun,
    DeliveryMode,
    RunStatus,
    RunStep,
    StepType,
)

#: The stub the morning-briefing agent actually ended its run with.
THIN_REPLY = "Briefing delivered."

#: A body long enough to be the report the agent was asked to broadcast.
BRIEFING_BODY = (
    "Morning Briefing — three items need you today. "
    "1) Two invoices are waiting on approval, both over the auto-approve cap. "
    "2) The staging deploy from last night rolled back on its own; the image "
    "tag never moved off the previous release. "
    "3) A reply came in on the vendor thread asking for a decision by Friday. "
    "Nothing else moved overnight and the fleet is clean."
)


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _RecordingChannel:
    """Records the exact body it was handed, acknowledging what it was told to."""

    name = "fake"
    inbound_router = None

    def __init__(self, acknowledged: int = 1, expected: int = 1) -> None:
        self.sends: list[tuple[str, str]] = []
        self._acknowledged = acknowledged
        self._expected = expected

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"channel": self.name}

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        self.sends.append((target, text))
        return SendReceipt(
            acknowledged=self._acknowledged,
            expected=self._expected,
            platform_ids=["1"] * self._acknowledged,
            target=target,
            body=text,
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_channels()
    yield
    reset_channels()


@pytest.fixture(autouse=True)
def _telegram_sender():
    sender = AsyncMock(return_value=[_FakeMessage(1)])
    set_telegram_sender(sender)
    yield sender
    set_telegram_sender(None)  # type: ignore[arg-type]


@pytest.fixture
def persisted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """(delivery_status, outcome_notes) handed to the ``agent_runs`` writer.

    Asserting the in-memory fields alone would pass with the DB write deleted,
    and the columns are what the operator's own review reads back. The statement
    that writer executes is pinned separately, in ``TestTheOutcomeNoteReachesTheRow``.
    """
    seen: list[tuple[str, str | None]] = []

    async def _record(run: AgentRun) -> None:
        seen.append((run.delivery_status or "", run.outcome_notes))

    monkeypatch.setattr("robothor.engine.delivery._persist_delivery_status", _record)
    return seen


def _note_step(
    body: str,
    *,
    run_id: str = "run-1",
    step_number: int = 1,
    tool: str = "create_note",
    error_message: str | None = None,
    tool_output: dict[str, Any] | None = None,
) -> RunStep:
    return RunStep(
        run_id=run_id,
        step_number=step_number,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_input={"title": "Morning Briefing", "body": body},
        tool_output={"id": "note-1"} if tool_output is None else tool_output,
        error_message=error_message,
    )


def _run(**kwargs: object) -> AgentRun:
    defaults: dict[str, object] = {
        "id": "run-1",
        "agent_id": "morning-briefing",
        "status": RunStatus.COMPLETED,
        "output_text": THIN_REPLY,
        "delivery_mode": DeliveryMode.ANNOUNCE,
    }
    defaults.update(kwargs)
    return AgentRun(**defaults)  # type: ignore[arg-type]


def _config(**kwargs: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "id": "morning-briefing",
        "name": "Morning Briefing",
        "delivery_mode": DeliveryMode.ANNOUNCE,
        "delivery_to": "42",
        "delivery_channel": "fake",
    }
    defaults.update(kwargs)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


class TestThinAnnounceFallsBackToTheNote:
    @pytest.mark.asyncio
    async def test_note_body_is_delivered_instead_of_the_stub(self, persisted):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        result = await deliver(_config(), run)

        assert result is True
        assert len(channel.sends) == 1
        _, body = channel.sends[0]
        assert body == BRIEFING_BODY
        assert THIN_REPLY not in body
        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None
        assert persisted and persisted[-1][0] == "delivered"

    @pytest.mark.asyncio
    async def test_the_channel_still_adds_its_own_header(self, _telegram_sender):
        """The body replaces the stub; the header is the channel's, as before."""
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(delivery_channel="telegram"), run)

        _telegram_sender.assert_called_once()
        _chat_id, sent = _telegram_sender.call_args.args
        assert sent == f"*Morning Briefing*\n\n{BRIEFING_BODY}"

    @pytest.mark.asyncio
    async def test_the_existing_outcome_note_is_kept(self, persisted):
        register_channel("fake", _RecordingChannel())
        run = _run(steps=[_note_step(BRIEFING_BODY)])
        run.outcome_notes = "Thin announce output (19 chars) — likely meta-confirmation"

        await deliver(_config(), run)

        notes = run.outcome_notes or ""
        assert "Thin announce output (19 chars)" in notes
        assert thin_announce.SUBSTITUTION_PREFIX in notes

    @pytest.mark.asyncio
    async def test_the_most_recent_substantial_note_wins(self):
        """An agent that files research and THEN the briefing broadcasts the briefing."""
        channel = _RecordingChannel()
        register_channel("fake", channel)
        research = "R" * 600
        briefing = "B" * 400
        run = _run(
            steps=[
                _note_step(research, step_number=1),
                _note_step(briefing, step_number=2),
            ]
        )

        await deliver(_config(), run)

        assert channel.sends[0][1] == briefing
        assert research not in channel.sends[0][1]

    def test_selection_ignores_list_order_and_reads_step_number(self):
        """Steps arriving out of order still resolve to the last one authored."""
        run = _run(
            steps=[
                _note_step("B" * 400, step_number=2),
                _note_step("R" * 600, step_number=1),
            ]
        )

        substitution = thin_announce.note_substitution(run, THIN_REPLY)

        assert substitution is not None
        assert substitution.body == "B" * 400


class TestTheOutcomeNoteSaysWhatHappened:
    """The note is the only record of why the delivered text differs from
    ``agent_runs.output_text``. It is written AFTER the receipt, and it states
    two facts it now actually checks: did the note save, and did the send land.
    """

    @pytest.mark.asyncio
    async def test_saved_note_and_acknowledged_send(self, persisted):
        register_channel("fake", _RecordingChannel())
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(), run)

        assert run.outcome_notes == "substituted note body (saved) — delivered"
        assert persisted[-1] == ("delivered", "substituted note body (saved) — delivered")

    @pytest.mark.asyncio
    async def test_a_note_that_failed_to_save_says_so(self, persisted):
        """The body is still recovered — losing it is the defect — but the row
        must not claim a note was filed when the write was refused."""
        register_channel("fake", _RecordingChannel())
        step = _note_step(
            BRIEFING_BODY,
            error_message="permission denied: crm write refused",
            tool_output={"error": "Failed to create note"},
        )
        run = _run(steps=[step])

        await deliver(_config(), run)

        assert run.outcome_notes == "substituted note body (note save failed) — delivered"
        assert persisted[-1][0] == "delivered"

    @pytest.mark.asyncio
    async def test_a_send_nobody_acknowledged_is_never_called_delivered(self, persisted):
        """0 of 1 chunks landed. The note said "delivered" beside a `failed:`
        status until this test existed."""
        channel = _RecordingChannel(acknowledged=0, expected=1)
        register_channel("fake", channel)
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        result = await deliver(_config(), run)

        assert result is False
        assert run.delivery_status == "failed:fake_send"
        assert run.delivered_at is None
        assert run.outcome_notes == "substituted note body — send failed: failed:fake_send"
        assert "delivered" not in (run.outcome_notes or "").replace("send failed", "")
        assert persisted[-1][1] == "substituted note body — send failed: failed:fake_send"

    @pytest.mark.asyncio
    async def test_a_truncated_send_is_not_delivered_either(self):
        channel = _RecordingChannel(acknowledged=1, expected=3)
        register_channel("fake", channel)
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(), run)

        assert run.delivery_status == "partial:1/3"
        assert run.outcome_notes == "substituted note body — send failed: partial:1/3"

    @pytest.mark.asyncio
    async def test_an_unregistered_channel_records_the_substitution_too(self):
        """Nothing was sent, and the row has to say the body was swapped anyway —
        otherwise a re-read of the run cannot explain the swap."""
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        result = await deliver(_config(delivery_channel="nowhere"), run)

        assert result is False
        assert run.delivery_status == "failed:no_channel:nowhere"
        assert (run.outcome_notes or "").startswith(thin_announce.SUBSTITUTION_PREFIX)
        assert "send failed: failed:no_channel:nowhere" in (run.outcome_notes or "")

    @pytest.mark.asyncio
    async def test_delivering_twice_leaves_one_note(self):
        register_channel("fake", _RecordingChannel())
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(), run)
        await deliver(_config(), run)

        notes = run.outcome_notes or ""
        assert notes.count(thin_announce.SUBSTITUTION_PREFIX) == 1

    @pytest.mark.asyncio
    async def test_a_retry_that_lands_replaces_the_failure_note(self):
        """A failed send followed by a good one must not leave the row saying
        the operator never got it."""
        register_channel("fake", _RecordingChannel(acknowledged=0, expected=1))
        run = _run(steps=[_note_step(BRIEFING_BODY)])
        await deliver(_config(), run)

        reset_channels()
        register_channel("fake", _RecordingChannel())
        await deliver(_config(), run)

        assert run.outcome_notes == "substituted note body (saved) — delivered"


class TestNoFallbackWhenItWouldBeWrong:
    @pytest.mark.asyncio
    async def test_thin_text_without_a_note_is_delivered_as_today(self, persisted):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run()

        result = await deliver(_config(), run)

        assert result is True
        assert channel.sends[0][1] == THIN_REPLY
        assert run.delivery_status == "delivered"
        assert thin_announce.SUBSTITUTION_PREFIX not in (run.outcome_notes or "")

    @pytest.mark.asyncio
    async def test_a_real_report_is_never_replaced_by_a_note(self):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        report = "A. " + ("A full report that stands on its own. " * 10)
        run = _run(output_text=report, steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(), run)

        assert channel.sends[0][1] == report.strip()
        assert BRIEFING_BODY not in channel.sends[0][1]
        assert thin_announce.SUBSTITUTION_PREFIX not in (run.outcome_notes or "")

    def test_a_non_thin_report_is_kept_even_when_the_note_is_far_longer(self):
        """Pins the thin guard ALONE: the length comparison cannot catch this."""
        report = "R" * 250
        run = _run(output_text=report, steps=[_note_step("N" * 2000)])

        assert thin_announce.note_substitution(run, report) is None

    def test_a_note_shorter_than_the_thin_threshold_never_wins(self):
        """Pins the substantiality guard against a second, lower threshold: a
        60-char note cannot rescue a 150-char stub."""
        stub = "S" * 150
        run = _run(output_text=stub, steps=[_note_step("N" * 60)])

        assert thin_announce.note_substitution(run, stub) is None

    @pytest.mark.asyncio
    async def test_a_note_from_another_run_is_never_borrowed(self):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run(steps=[_note_step(BRIEFING_BODY, run_id="some-other-run")])

        await deliver(_config(), run)

        assert channel.sends[0][1] == THIN_REPLY
        assert BRIEFING_BODY not in channel.sends[0][1]

    def test_an_unattributed_step_is_never_used(self):
        """``RunStep.run_id`` defaults to "". Unreachable in production — every
        constructor passes the session's run id — and refused rather than
        trusted, because "the note THIS run wrote" is the whole scope."""
        run = _run(steps=[_note_step(BRIEFING_BODY, run_id="")])

        assert thin_announce.note_substitution(run, THIN_REPLY) is None

    def test_a_run_without_an_id_matches_nothing(self):
        run = _run(id="", steps=[_note_step(BRIEFING_BODY, run_id="another-run")])

        assert thin_announce.note_substitution(run, THIN_REPLY) is None

    @pytest.mark.asyncio
    async def test_a_thin_note_body_is_another_stub_not_a_rescue(self):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run(steps=[_note_step("Briefing saved. Sent to the operator.")])

        await deliver(_config(), run)

        assert channel.sends[0][1] == THIN_REPLY

    def test_another_tools_body_argument_is_never_delivered(self):
        """``gws_gmail_send`` also takes a ``body``. Without the tool-name scope
        an email addressed to a third party becomes the operator's briefing."""
        run = _run(steps=[_note_step(BRIEFING_BODY, tool="gws_gmail_send")])

        assert thin_announce.note_substitution(run, THIN_REPLY) is None

    @pytest.mark.asyncio
    async def test_no_other_tool_output_is_ever_delivered(self):
        """Only the note body the agent authored — never a tool's own output."""
        channel = _RecordingChannel()
        register_channel("fake", channel)
        search = RunStep(
            run_id="run-1",
            step_number=1,
            step_type=StepType.TOOL_CALL,
            tool_name="search_memory",
            tool_input={"query": "briefing"},
            tool_output={"results": BRIEFING_BODY},
        )
        run = _run(steps=[search])

        await deliver(_config(), run)

        assert channel.sends[0][1] == THIN_REPLY
        assert BRIEFING_BODY not in channel.sends[0][1]

    @pytest.mark.asyncio
    async def test_non_announce_modes_never_fall_back(self, monkeypatch):
        """LOG mode publishes what the run said. The fallback is announce-only."""
        published: list[str] = []

        async def _publish(config: AgentConfig, text: str, run: AgentRun) -> bool:
            published.append(text)
            run.delivery_status = "published"
            return True

        monkeypatch.setattr(delivery_mod, "_deliver_event_bus", _publish)
        run = _run(steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(delivery_mode=DeliveryMode.LOG), run)

        assert published == [THIN_REPLY]
        assert thin_announce.SUBSTITUTION_PREFIX not in (run.outcome_notes or "")


class TestTheOutcomeNoteReachesTheRow:
    """The ``persisted`` fixture replaces the writer wholesale, so the statement
    it executes needs its own test — breaking the SQL left every other test green.
    """

    @staticmethod
    def _fake_connection(captured: list[tuple[str, tuple]]) -> Any:
        class _Cur:
            def execute(self, sql: str, params: tuple) -> None:
                captured.append((sql, params))

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        class _Conn:
            def cursor(self) -> Any:
                return _Cur()

            def commit(self) -> None:
                return None

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        module = types.ModuleType("robothor.db.connection")
        module.get_connection = lambda: _Conn()  # type: ignore[attr-defined]
        return module

    def test_the_update_carries_the_outcome_note(self, monkeypatch):
        captured: list[tuple[str, tuple]] = []
        monkeypatch.setitem(sys.modules, "robothor.db.connection", self._fake_connection(captured))
        run = _run()
        run.delivery_status = "delivered"
        run.outcome_notes = "substituted note body (saved) — delivered"

        asyncio.run(delivery_mod._persist_delivery_status(run))

        assert captured, "no SQL executed"
        sql, params = captured[0]
        assert "outcome_notes" in sql
        assert sql.count("%s") == len(params)
        assert run.outcome_notes in params
        assert params[-1] == run.id

    def test_a_run_without_a_note_cannot_blank_the_column(self, monkeypatch):
        """The interactive path calls this writer too, on runs the finalizer
        never assessed. A NULL must leave whatever is already there."""
        captured: list[tuple[str, tuple]] = []
        monkeypatch.setitem(sys.modules, "robothor.db.connection", self._fake_connection(captured))
        run = _run()
        run.delivery_status = "delivered"
        run.outcome_notes = None

        asyncio.run(delivery_mod._persist_delivery_status(run))

        sql, params = captured[0]
        assert "COALESCE" in sql.upper()
        assert None in params


class TestOneThresholdNotTwo:
    def test_the_fallback_uses_the_threshold_run_finalizer_flags_on(self):
        """One constant. A second copy here would drift off the detector."""
        assert (
            thin_announce.ANNOUNCE_MIN_OUTPUT_CHARS
            == run_finalizer.ANNOUNCE_MIN_OUTPUT_CHARS
            == 200
        )

    def test_the_predicate_agrees_with_the_assessment_at_the_boundary(self):
        """Whatever ``_assess_outcome`` calls thin, the fallback calls thin."""
        for length in (199, 200, 201):
            text = "x" * length
            run = AgentRun(
                id="run-1",
                agent_id="a",
                status=RunStatus.COMPLETED,
                output_text=text,
                delivery_mode=DeliveryMode.ANNOUNCE,
            )
            run_finalizer.RunFinalizationMixin._assess_outcome(run)
            flagged = "Thin announce output" in (run.outcome_notes or "")
            assert thin_announce.is_thin_announce_output(text) is flagged

    def test_only_one_module_defines_the_threshold(self):
        """The PR's headline claim, as a gate: four copies is what the runner
        decomposition left behind, and hand-maintained duplicates drift."""
        from pathlib import Path

        engine = Path(thin_announce.__file__).parent
        # Built, not written literally: this file would otherwise match itself.
        needle = "ANNOUNCE_MIN_OUTPUT_CHARS" + " = "
        definitions = [
            path.name for path in engine.rglob("*.py") if needle in path.read_text(encoding="utf-8")
        ]
        assert definitions == ["thin_announce.py"], definitions

    def test_a_thin_run_with_no_note_yields_no_substitution(self):
        run = AgentRun(id="run-1", agent_id="a", output_text=THIN_REPLY)
        assert thin_announce.note_substitution(run, THIN_REPLY) is None

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
* the note is looked up in THIS run's steps — a note from another run is never
  borrowed;
* a thin note body is not a rescue, it is another stub;
* a real report is never replaced by a note, however long the note is;
* the thin threshold is the one ``run_finalizer`` already flags on, not a
  second constant that can drift away from it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

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
    """Records the exact body it was handed and acknowledges all of it."""

    name = "fake"
    inbound_router = None

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"channel": self.name}

    async def send(self, target: str, text: str, **kw: Any) -> SendReceipt:
        self.sends.append((target, text))
        return SendReceipt(
            acknowledged=1,
            expected=1,
            platform_ids=["1"],
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
    and the columns are what the operator's own review reads back.
    """
    seen: list[tuple[str, str | None]] = []

    async def _record(run: AgentRun) -> None:
        seen.append((run.delivery_status or "", run.outcome_notes))

    monkeypatch.setattr("robothor.engine.delivery._persist_delivery_status", _record)
    return seen


def _note_step(body: str, *, run_id: str = "run-1", step_number: int = 1) -> RunStep:
    return RunStep(
        run_id=run_id,
        step_number=step_number,
        step_type=StepType.TOOL_CALL,
        tool_name="create_note",
        tool_input={"title": "Morning Briefing — Sun 9/13", "body": body},
        tool_output={"id": "note-1", "title": "Morning Briefing — Sun 9/13"},
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
        assert thin_announce.THIN_FALLBACK_NOTE in (run.outcome_notes or "")
        assert persisted and persisted[-1][0] == "delivered"
        assert thin_announce.THIN_FALLBACK_NOTE in (persisted[-1][1] or "")

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
        assert thin_announce.THIN_FALLBACK_NOTE in notes


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
        assert thin_announce.THIN_FALLBACK_NOTE not in (run.outcome_notes or "")

    @pytest.mark.asyncio
    async def test_a_real_report_is_never_replaced_by_a_note(self):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        report = "A. " + ("A full report that stands on its own. " * 10)
        run = _run(output_text=report, steps=[_note_step(BRIEFING_BODY)])

        await deliver(_config(), run)

        assert channel.sends[0][1] == report.strip()
        assert BRIEFING_BODY not in channel.sends[0][1]
        assert thin_announce.THIN_FALLBACK_NOTE not in (run.outcome_notes or "")

    @pytest.mark.asyncio
    async def test_a_note_from_another_run_is_never_borrowed(self):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run(steps=[_note_step(BRIEFING_BODY, run_id="some-other-run")])

        await deliver(_config(), run)

        assert channel.sends[0][1] == THIN_REPLY
        assert BRIEFING_BODY not in channel.sends[0][1]

    @pytest.mark.asyncio
    async def test_a_thin_note_body_is_another_stub_not_a_rescue(self):
        channel = _RecordingChannel()
        register_channel("fake", channel)
        run = _run(steps=[_note_step("Briefing saved. Sent to the operator.")])

        await deliver(_config(), run)

        assert channel.sends[0][1] == THIN_REPLY

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
    async def test_non_announce_modes_never_fall_back(self):
        """LOG mode publishes what the run said. The fallback is announce-only."""
        published: list[str] = []

        async def _publish(config: AgentConfig, text: str, run: AgentRun) -> bool:
            published.append(text)
            run.delivery_status = "published"
            return True

        run = _run(steps=[_note_step(BRIEFING_BODY)])
        from robothor.engine import delivery as delivery_mod

        original = delivery_mod._deliver_event_bus
        delivery_mod._deliver_event_bus = _publish  # type: ignore[assignment]
        try:
            await deliver(_config(delivery_mode=DeliveryMode.LOG), run)
        finally:
            delivery_mod._deliver_event_bus = original  # type: ignore[assignment]

        assert published == [THIN_REPLY]


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

    def test_a_thin_run_with_no_note_yields_no_fallback(self):
        run = AgentRun(id="run-1", agent_id="a", output_text=THIN_REPLY)
        assert thin_announce.note_body_fallback(run, THIN_REPLY) is None

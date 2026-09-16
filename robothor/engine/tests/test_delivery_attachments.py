"""A scheduled run can arrive with its file attached.

An interactive agent sends immediately: there is a person on the other end of
the run and ``send_file`` reaches them. A scheduled one has nobody watching
while it works — its output arrives later as an announcement — so a file it
made has to travel WITH that announcement or not at all. Sending it the moment
it was written would put a PDF in the operator's chat minutes before the report
explaining it, or hours before nothing at all if the run then failed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine import delivery
from robothor.engine.channels.base import SendReceipt
from robothor.engine.models import AgentConfig, AgentRun, DeliveryMode


@pytest.fixture(autouse=True)
def _empty_queue():
    delivery.clear_queued_attachments()
    yield
    delivery.clear_queued_attachments()


@pytest.fixture(autouse=True)
def _workspace_is_the_tmp_tree(tmp_path, monkeypatch):
    """Point the instance workspace at this test's tmp tree.

    ``run.attachments`` carries bare paths with no workspace of their own, so
    the delivery ladder falls back to the instance workspace to judge
    containment — which on a developer's box is the REAL one. Without this the
    tests either pass for the wrong reason or fail depending on whose machine
    they run on, and neither is a test.
    """
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))


@pytest.fixture
def channel(monkeypatch):
    sent: list[dict] = []

    class Fake:
        name = "telegram"

        async def send(self, target, text, **kw):
            sent.append({"kind": "text", "target": target, "text": text})
            return SendReceipt(acknowledged=1, expected=1, platform_ids=["1"], target=target)

        async def send_attachment(self, target, path, caption="", *, as_="auto", **kw):
            sent.append({"kind": "file", "target": target, "path": path, "caption": caption})
            return SendReceipt(acknowledged=1, expected=1, platform_ids=["2"], target=target)

    monkeypatch.setattr("robothor.engine.delivery.get_channel", lambda name: Fake(), raising=True)
    monkeypatch.setattr(
        "robothor.engine.delivery._persist_delivery_status", AsyncMock(), raising=True
    )
    return sent


def _config() -> AgentConfig:
    return AgentConfig(
        id="nightly-report",
        name="Nightly Report",
        delivery_mode=DeliveryMode.ANNOUNCE,
        delivery_to="100200300",
    )


def _run(**kw) -> AgentRun:
    run = AgentRun(agent_id="nightly-report")
    run.output_text = kw.pop("output_text", "Here is tonight's report.")
    for key, value in kw.items():
        setattr(run, key, value)
    return run


class TestTheRunCarriesThem:
    def test_a_run_has_an_attachments_field(self) -> None:
        assert AgentRun().attachments == []

    @pytest.mark.asyncio
    async def test_the_announcement_goes_first_then_the_file(self, tmp_path, channel) -> None:
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF")
        run = _run(attachments=[str(path)])
        assert await delivery.deliver(_config(), run) is True
        assert [event["kind"] for event in channel] == ["text", "file"]
        assert channel[1]["path"] == str(path)
        assert channel[1]["target"] == "100200300"

    @pytest.mark.asyncio
    async def test_a_queued_file_is_found_by_run_id(self, tmp_path, channel) -> None:
        path = tmp_path / "chart.png"
        path.write_bytes(b"\x89PNG")
        run = _run()
        delivery.queue_attachment(
            run.id, str(path), caption="tonight's numbers", workspace=str(tmp_path)
        )
        await delivery.deliver(_config(), run)
        assert channel[1]["path"] == str(path)
        assert channel[1]["caption"] == "tonight's numbers"

    @pytest.mark.asyncio
    async def test_the_queue_is_emptied_so_a_later_run_never_inherits_it(
        self, tmp_path, channel
    ) -> None:
        path = tmp_path / "a.txt"
        path.write_text("x")
        run = _run()
        delivery.queue_attachment(run.id, str(path), workspace=str(tmp_path))
        await delivery.deliver(_config(), run)
        channel.clear()
        await delivery.deliver(_config(), _run())
        assert [event["kind"] for event in channel] == ["text"]

    @pytest.mark.asyncio
    async def test_a_file_that_vanished_does_not_fail_the_delivery(self, tmp_path, channel) -> None:
        run = _run(attachments=[str(tmp_path / "gone.pdf")])
        assert await delivery.deliver(_config(), run) is True
        assert [event["kind"] for event in channel] == ["text"]

    @pytest.mark.asyncio
    async def test_a_failed_announcement_still_tries_the_file(self, tmp_path, monkeypatch) -> None:
        """The report and the file are two sends. One failing is not evidence
        about the other, and an operator with the PDF and no covering note is
        better off than one with neither."""
        events: list[str] = []

        class HalfBroken:
            name = "telegram"

            async def send(self, target, text, **kw):
                events.append("text")
                return SendReceipt(acknowledged=0, expected=1, target=target)

            async def send_attachment(self, target, path, caption="", *, as_="auto", **kw):
                events.append("file")
                return SendReceipt(acknowledged=1, expected=1, target=target)

        monkeypatch.setattr(
            "robothor.engine.delivery.get_channel", lambda name: HalfBroken(), raising=True
        )
        monkeypatch.setattr(
            "robothor.engine.delivery._persist_delivery_status", AsyncMock(), raising=True
        )
        path = tmp_path / "a.pdf"
        path.write_bytes(b"%PDF")
        await delivery.deliver(_config(), _run(attachments=[str(path)]))
        assert events == ["text", "file"]

    @pytest.mark.asyncio
    async def test_a_channel_that_cannot_attach_is_logged_not_raised(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        class NoFiles:
            name = "slack"

            async def send(self, target, text, **kw):
                return SendReceipt(acknowledged=1, expected=1, target=target)

            async def send_attachment(self, target, path, caption="", *, as_="auto", **kw):
                raise NotImplementedError("the slack channel cannot send files yet")

        monkeypatch.setattr(
            "robothor.engine.delivery.get_channel", lambda name: NoFiles(), raising=True
        )
        monkeypatch.setattr(
            "robothor.engine.delivery._persist_delivery_status", AsyncMock(), raising=True
        )
        path = tmp_path / "a.pdf"
        path.write_bytes(b"%PDF")
        config = _config()
        config.delivery_channel = "slack"
        assert await delivery.deliver(config, _run(attachments=[str(path)])) is True


class TestSendFileDuringAScheduledRun:
    @pytest.mark.asyncio
    async def test_it_queues_rather_than_sending_into_a_chat_nobody_is_in(
        self, tmp_path, monkeypatch
    ) -> None:
        from robothor.engine.tools.handlers import attachments as tool

        monkeypatch.setattr(
            "robothor.engine.tracking.get_run",
            lambda run_id: {"trigger_type": "schedule", "trigger_detail": ""},
            raising=True,
        )
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF")

        ctx = MagicMock()
        ctx.workspace = str(tmp_path)
        ctx.run_id = "run-sched"
        ctx.user_role = "owner"
        out = await tool.send_file({"path": str(path), "caption": "tonight"}, ctx)

        assert out.get("queued") is True
        assert out.get("sent") is not True
        queued = delivery.take_queued_attachments("run-sched")
        assert [item.path for item in queued] == [str(path)]

    @pytest.mark.asyncio
    async def test_the_refusals_still_apply_before_anything_is_queued(
        self, tmp_path, monkeypatch
    ) -> None:
        from robothor.engine.tools.handlers import attachments as tool

        monkeypatch.setattr(
            "robothor.engine.tracking.get_run",
            lambda run_id: {"trigger_type": "schedule", "trigger_detail": ""},
            raising=True,
        )
        secret = tmp_path / ".env"
        secret.write_text("TOKEN=abc")
        out = await tool.send_file({"path": str(secret)}, MagicMock(workspace=str(tmp_path)))
        assert "error" in out
        assert not delivery.take_queued_attachments("run-sched")


class TestTheLadderRunsAgainAtDelivery:
    """Hostile review C1. The queue holds a PATH; the channel reads the bytes at
    send time. Everything `send_file` checked ran against different bytes."""

    @pytest.mark.asyncio
    async def test_a_file_rewritten_after_queueing_is_not_delivered(
        self, tmp_path, channel
    ) -> None:
        from robothor.engine import attachment_gate as gate

        path = tmp_path / "report.csv"
        path.write_bytes(b"month,total\n2026-09,12\n")
        run = _run()
        delivery.queue_attachment(
            run.id, str(path), caption="the monthly numbers", digest=gate.digest_of(path)
        )
        path.write_bytes(b"AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n")

        await delivery.deliver(_config(), run)

        assert [event["kind"] for event in channel] == ["text"], (
            "the rewritten bytes must never reach the channel"
        )

    @pytest.mark.asyncio
    async def test_an_unchanged_queued_file_still_goes(self, tmp_path, channel) -> None:
        from robothor.engine import attachment_gate as gate

        path = tmp_path / "report.csv"
        path.write_bytes(b"month,total\n2026-09,12\n")
        run = _run()
        delivery.queue_attachment(
            run.id, str(path), digest=gate.digest_of(path), workspace=str(tmp_path)
        )
        await delivery.deliver(_config(), run)
        assert [event["kind"] for event in channel] == ["text", "file"]

    @pytest.mark.asyncio
    async def test_run_attachments_go_through_the_ladder_too(self, tmp_path, channel) -> None:
        """`run.attachments` was never checked by anything, ever."""
        secret = tmp_path / ".env"
        secret.write_bytes(b"TOKEN=abc")
        await delivery.deliver(_config(), _run(attachments=[str(secret)]))
        assert [event["kind"] for event in channel] == ["text"]

    @pytest.mark.asyncio
    async def test_a_queued_file_that_left_the_workspace_is_refused(
        self, tmp_path, channel
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"not yours")
        run = _run()
        delivery.queue_attachment(run.id, str(outside), workspace=str(workspace))
        await delivery.deliver(_config(), run)
        assert [event["kind"] for event in channel] == ["text"]

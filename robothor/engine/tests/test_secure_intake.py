"""Exercise real Telegram entry points with fake network, storage and agent sinks."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.autonomy.models import Scope
from robothor.engine.tests.test_telegram_attachments import (  # noqa: F401
    arm_download,
    document,
    enqueued,
    message,
    photo,
)
from robothor.engine.tests.test_telegram_attachments import (
    bot as telegram_bot_fixture,
)

bot = telegram_bot_fixture


@pytest.fixture
def secure():
    with (
        patch(
            "robothor.engine.secure_intake.scope_for_actor",
            return_value=Scope(tenant_id="t-alpha", owner_id="person:alice"),
        ),
        patch("robothor.engine.secure_intake.AutonomyStore") as store,
    ):
        store.return_value.put_resource.return_value = {"id": "reference", "kind": "credential"}
        yield store.return_value


@pytest.mark.asyncio
async def test_credential_goes_to_vault_before_log_history_and_agent(bot, secure, caplog):
    text = '/secure credential https://shop.example {"username":"alice","password":"private-intake-93!"}'
    msg = message(text=text)
    with caplog.at_level("INFO"):
        await bot.handle_text(msg)
    saved = secure.put_resource.call_args.args[1]
    assert json.loads(saved.payload.get_secret_value())["password"] == "private-intake-93!"
    bot._enqueue_message.assert_awaited_once()
    assert "private-intake" not in str(bot._enqueue_message.call_args)
    assert "private-intake" not in str(msg.answer.call_args_list)
    assert "private-intake" not in caplog.text


@pytest.mark.asyncio
async def test_document_bypasses_plaintext_inbox_and_enrichment(bot, secure, caplog):
    arm_download(bot, b"private-document-canary")
    msg = message(caption="/secure document", document=document(name="private-name.pdf"))
    with patch.object(bot, "_keep_attachment", new_callable=AsyncMock) as ordinary:
        await bot.handle_file(msg)
    ordinary.assert_not_called()
    payload = json.loads(secure.put_resource.call_args.args[1].payload.get_secret_value())
    assert payload["name"] == "private-name.pdf"
    assert "private-name" not in str(bot._enqueue_message.call_args)
    assert "private-document" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bad_input", "no_identity", "storage", "group"])
async def test_sensitive_errors_do_not_fall_back_to_normal_processing(bot, secure, caplog, failure):
    msg = message(text='/secure profile {"email":"private-canary@example.com"}')
    if failure == "bad_input":
        msg.text = "/secure credential private-intake-canary"
    elif failure == "no_identity":
        bot._resolve_user.return_value = {"tenant_id": "t-alpha", "role": "owner"}
    elif failure == "storage":
        secure.put_resource.side_effect = RuntimeError("private-intake-canary")
    elif failure == "group":
        msg.chat.type = "supergroup"
    await bot.handle_text(msg)
    bot._enqueue_message.assert_not_called()
    assert "private-" not in caplog.text
    assert "private-" not in str(msg.answer.call_args_list)


@pytest.mark.asyncio
async def test_card_details_are_never_enrolled_through_chat(bot, secure):
    await bot.handle_text(message(text='/secure payment_card {"number":"4242424242424242"}'))
    secure.put_resource.assert_not_called()
    bot._enqueue_message.assert_not_called()


@pytest.mark.asyncio
async def test_secure_album_never_downloads_marked_item(bot, secure):
    msg = message(caption="/secure document", document=document(), media_group_id="album")
    bot._download_media = AsyncMock()
    await bot.handle_file(msg)
    bot._download_media.assert_not_called()
    secure.put_resource.assert_not_called()


@pytest.mark.asyncio
async def test_caption_on_later_album_member_protects_the_entire_batch(bot, secure, monkeypatch):
    import asyncio

    from robothor.engine import telegram_attachments

    monkeypatch.setattr(telegram_attachments, "ALBUM_WINDOW_SECONDS", 0.01)
    bot._download_media = AsyncMock()
    first = message(caption="", document=document(), media_group_id="batch")
    second = message(caption="/secure document", document=document(), media_group_id="batch")
    await bot.handle_file(first)
    await bot.handle_file(second)
    await asyncio.sleep(0.06)
    bot._download_media.assert_not_called()
    bot._enqueue_message.assert_not_called()


class TestAnOrdinaryAlbumSurvivesOneBadMember:
    """Every media group now detours through ``secure_intake.collect_album``.

    Before it existed, each album member was its own aiogram update task, so a
    member that blew up cost exactly that member and ``handle_file`` still
    answered "I couldn't download ...". The intake flush re-dispatches the
    members in ONE ``try``/``except Exception: return``, so the first member to
    raise ends the loop: a four-photo album delivered one photo and said
    nothing at all. This instance shipped Telegram attachments in September
    precisely because the operator reported pictures and files did not work.
    """

    @staticmethod
    def _album(bot, monkeypatch, *, failing_uid: str, members: int = 4):
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a photo"),
        )
        monkeypatch.setattr(
            "robothor.engine.telegram_attachments.ALBUM_WINDOW_SECONDS", 0.05, raising=True
        )
        arm_download(bot, b"\xff\xd8\xff")
        keep = bot._keep_attachment

        async def failing_keep(chat_id, media, caption):
            if media.file_unique_id == failing_uid:
                # Whatever escapes `_keep_attachment` — the unguarded
                # `_enrich_attachment` call is a real one.
                raise RuntimeError("private-canary: the disk filled up mid-write")
            return await keep(chat_id, media, caption)

        monkeypatch.setattr(bot, "_keep_attachment", failing_keep)
        return [
            message(
                caption="four shots" if index == 0 else "",
                photo=photo(uid=f"AgACalb{index}"),
                media_group_id="MGALB",
            )
            for index in range(members)
        ]

    @pytest.mark.asyncio
    async def test_the_members_after_the_failure_are_still_delivered(
        self, bot, secure, monkeypatch
    ) -> None:
        messages = self._album(bot, monkeypatch, failing_uid="AgACalb1")
        for msg in messages:
            await bot.handle_file(msg)
        await asyncio.sleep(0.4)

        _, rows = enqueued(bot)
        assert len(rows) == 3, f"one bad member ate the rest of the album: {rows}"

    @pytest.mark.asyncio
    async def test_the_operator_is_told_the_one_photo_that_was_lost(
        self, bot, secure, monkeypatch, caplog
    ) -> None:
        messages = self._album(bot, monkeypatch, failing_uid="AgACalb1")
        with caplog.at_level("WARNING"):
            for msg in messages:
                await bot.handle_file(msg)
            await asyncio.sleep(0.4)

        answers = " ".join(str(msg.answer.call_args_list) for msg in messages)
        assert "couldn't" in answers or "could not" in answers, (
            f"the operator heard nothing about the lost photo: {answers!r}"
        )
        # The intake boundary's own promise still holds: no raw exception text.
        assert "private-canary" not in answers
        assert "private-canary" not in caplog.text


class TestSecureNeverAnswersSomeoneItDoesNotKnow:
    """`intercept` was the FIRST statement of `handle_text`, before identity.

    So a stranger and a group chat both got "Private input was not saved. Open
    Account → Personal automation … /secure credential https://example.com.
    Card details belong on that page." — a reply that fingerprints the instance
    and names its dashboard path — while
    `_notify_operator_of_unregistered_sender` never fired, so the operator
    never heard that a stranger had reached the bot at all. The ordinary text
    path alerts; this one silently did not.
    """

    PAYLOAD = '/secure credential https://shop.example {"password":"private-canary-77"}'
    LEAKS = ("Personal automation", "/secure credential", "Card details")

    @pytest.mark.asyncio
    async def test_an_unregistered_sender_gets_the_ordinary_refusal_and_alerts_the_operator(
        self, bot, secure, monkeypatch
    ) -> None:
        bot._resolve_user = MagicMock(return_value=None)
        notified = AsyncMock()
        monkeypatch.setattr(bot, "_notify_operator_of_unregistered_sender", notified)
        msg = message(text=self.PAYLOAD)

        await bot.handle_text(msg)

        answers = str(msg.answer.call_args_list)
        for leak in self.LEAKS:
            assert leak not in answers, f"a stranger was told {leak!r}: {answers!r}"
        notified.assert_awaited_once()
        bot._enqueue_message.assert_not_called()
        secure.put_resource.assert_not_called()
        assert "private-canary" not in answers

    @pytest.mark.asyncio
    async def test_a_group_chat_is_never_told_where_the_dashboard_is(
        self, bot, secure, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            bot, "_notify_operator_of_unregistered_sender", AsyncMock(), raising=False
        )
        msg = message(text=self.PAYLOAD)
        msg.chat.type = "supergroup"

        await bot.handle_text(msg)

        answers = str(msg.answer.call_args_list)
        for leak in self.LEAKS:
            assert leak not in answers, f"a group room was told {leak!r}: {answers!r}"
        bot._enqueue_message.assert_not_called()
        secure.put_resource.assert_not_called()
        assert "private-canary" not in answers

    @pytest.mark.asyncio
    async def test_the_operator_alert_never_quotes_the_private_payload(
        self, bot, secure, monkeypatch
    ) -> None:
        """`_notify_operator_of_unregistered_sender` quotes the sender's raw
        text back to the operator's chat. Routing a `/secure` line through it
        would put the payload in a Telegram message — which is the one place
        this whole boundary exists to keep it out of."""
        bot._resolve_user = MagicMock(return_value=None)
        sent = AsyncMock()
        monkeypatch.setattr(bot, "send_message", sent)

        await bot.handle_text(message(text=self.PAYLOAD))

        assert "private-canary" not in str(sent.call_args_list), (
            f"the payload reached the operator's chat: {sent.call_args_list!r}"
        )


@pytest.mark.asyncio
async def test_a_docked_multi_line_message_says_so(bot, secure):
    """`intercept` consumes only a message that STARTS with /secure; the
    backstop's marker matches any LINE start. An ordinary message whose second
    line began "/secure the loading bay doors" reached the model with its tail
    removed and its sender none the wiser."""
    msg = message(text="Draft the checklist:\n/secure the loading bay doors\nthen email the team")

    await bot.handle_text(msg)

    answers = str(msg.answer.call_args_list)
    assert "loading bay" not in str(bot._enqueue_message.call_args), "the backstop stopped working"
    assert answers.strip("[]"), "the operator was told nothing about the removed tail"
    assert "/secure" in answers or "private" in answers.lower()


@pytest.mark.asyncio
async def test_an_oversized_ordinary_batch_degrades_with_a_count(bot, secure, monkeypatch):
    """A 26-file batch was answered "Private albums were not saved" and none of
    it was kept. Nothing about it was private; the ordinary album path has
    always degraded with a count instead. The marked-batch property does not
    depend on the 26th member's `Message` being buffered — every caption is
    checked as it arrives, before the cap."""
    monkeypatch.setattr(
        "robothor.engine.tools.handlers.images.describe_image_bytes",
        AsyncMock(return_value="a photo"),
    )
    monkeypatch.setattr(
        "robothor.engine.telegram_attachments.ALBUM_WINDOW_SECONDS", 0.05, raising=True
    )
    arm_download(bot, b"\xff\xd8\xff")
    messages = [message(photo=photo(uid=f"AgACbig{i}"), media_group_id="MGBIG") for i in range(26)]
    for msg in messages:
        await bot.handle_file(msg)
    await asyncio.sleep(0.6)

    answers = " ".join(str(msg.answer.call_args_list) for msg in messages)
    assert "Private albums were not saved" not in answers, "an ordinary batch called private"
    assert "1 more file" in answers, f"the operator was not given the count: {answers!r}"
    _, rows = enqueued(bot)
    assert len(rows) == 25, f"the 25 that fit were thrown away too: {len(rows)}"

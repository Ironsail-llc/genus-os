"""Exercise real Telegram entry points with fake network, storage and agent sinks."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from robothor.autonomy.models import Scope
from robothor.engine.tests.test_telegram_attachments import (  # noqa: F401
    arm_download,
    document,
    message,
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

"""What happens to a picture or a file the operator sends over Telegram.

The operator, 2026-09-15: "In Telegram we should be able to send and receive
pictures and files to one another, and that doesn't work well, but it should."

What it did: downloaded the bytes, turned them into text (or into the string
``[Binary file: name, N bytes]``) and threw them away, with a 5 MB ceiling of
our own invention where Telegram allows 20 MB — and, for a photo, consumed the
operator's caption as the vision model's prompt so the agent never saw the
question that was actually asked.

No test here reaches Telegram. The bot is a fake with ``get_file`` and
``download_file`` recorders, which is the whole API surface the intake uses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import attachments
from robothor.engine.chat import _sessions
from robothor.engine.telegram import TelegramBot

ALICE = {
    "tenant_id": "t-alpha",
    "display_name": "Alice",
    "role": "owner",
    "user_id": "tu-1",
    "telegram_user_id": "1001",
}


@pytest.fixture
def bot(engine_config):
    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        fake = MagicMock()
        fake.send_message = AsyncMock()
        mock_bot_cls.return_value = fake
        instance = TelegramBot(engine_config, MagicMock())
        instance.bot = fake
        instance._resolve_user = MagicMock(return_value=dict(ALICE))
        instance._enqueue_message = AsyncMock()
        yield instance
    _sessions.clear()


def arm_download(bot, payload: bytes, *, file_path: str = "documents/file_0.bin") -> None:
    """Teach the fake bot to hand back ``payload`` for any file id."""
    bot.bot.get_file = AsyncMock(return_value=MagicMock(file_path=file_path))

    async def _download(path, destination):
        destination.write(payload)
        return destination

    bot.bot.download_file = AsyncMock(side_effect=_download)


def message(
    *,
    chat_id: int = 100200300,
    caption: str = "",
    document=None,
    photo=None,
    media_group_id=None,
    **kw,
):
    msg = MagicMock()
    msg.from_user.id = 1001
    msg.from_user.first_name = "Alice"
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.caption = caption
    msg.media_group_id = media_group_id
    msg.message_id = 7
    msg.document = document
    msg.photo = photo
    msg.audio = None
    msg.video = None
    msg.voice = None
    msg.video_note = None
    msg.sticker = None
    msg.answer = AsyncMock()
    for key, value in kw.items():
        setattr(msg, key, value)
    return msg


def document(name="report.pdf", size=1024, mime="application/pdf", uid="AgACdoc"):
    doc = MagicMock()
    doc.file_name = name
    doc.file_size = size
    doc.mime_type = mime
    doc.file_id = "BQAC" + uid
    doc.file_unique_id = uid
    return doc


def photo(width=1280, height=720, size=2048, uid="AgACpic"):
    size_obj = MagicMock()
    size_obj.width = width
    size_obj.height = height
    size_obj.file_size = size
    size_obj.file_id = "AgAD" + uid
    size_obj.file_unique_id = uid
    # aiogram's PhotoSize has NO file_name. A bare MagicMock invents one, and
    # the intake would then name the saved file after a mock repr instead of
    # taking its `photo.jpg` fallback — a test fixture that does not behave
    # like the object it stands in for.
    size_obj.file_name = None
    return [size_obj]


def enqueued(bot) -> tuple[str, list[dict]]:
    """``(user_text, attachment rows)`` from the one enqueued turn."""
    assert bot._enqueue_message.await_count == 1, bot._enqueue_message.await_args_list
    call = bot._enqueue_message.await_args
    return call.args[3], list(call.kwargs.get("attachments") or [])


class TestDocuments:
    @pytest.mark.asyncio
    async def test_a_pdf_gives_the_agent_the_text_and_the_path(self, bot, monkeypatch) -> None:
        async def fake_pdf(raw):
            return "Q3 revenue was up"

        monkeypatch.setattr(
            "robothor.engine.telegram_handlers._extract_pdf_text", fake_pdf, raising=True
        )
        arm_download(bot, b"%PDF-1.7 fake")
        await bot.handle_file(message(caption="summarise this", document=document()))

        text, rows = enqueued(bot)
        assert "summarise this" in text
        assert "Q3 revenue was up" in text
        assert len(rows) == 1
        assert Path(rows[0]["path"]).read_bytes() == b"%PDF-1.7 fake"
        assert rows[0]["path"] in text

    @pytest.mark.asyncio
    async def test_a_zip_is_kept_and_the_agent_is_told_where(self, bot) -> None:
        arm_download(bot, b"PK\x03\x04binary")
        await bot.handle_file(
            message(document=document(name="bundle.zip", mime="application/zip", uid="AgACzip"))
        )
        text, rows = enqueued(bot)
        assert Path(rows[0]["path"]).exists()
        assert rows[0]["path"] in text
        assert "Binary file" not in text, "the old shape threw the bytes away"

    @pytest.mark.asyncio
    async def test_the_extract_says_how_much_was_left_behind(self, bot) -> None:
        arm_download(bot, b"x" * (attachments.TEXT_EXTRACT_CHARS + 500))
        await bot.handle_file(
            message(document=document(name="big.txt", mime="text/plain", uid="AgACbig"))
        )
        text, _ = enqueued(bot)
        assert str(attachments.TEXT_EXTRACT_CHARS + 500) in text
        assert "read_file" in text

    @pytest.mark.asyncio
    async def test_a_traversal_filename_lands_inside_the_inbox(self, bot) -> None:
        arm_download(bot, b"not a key")
        await bot.handle_file(
            message(
                document=document(name="../../../../.ssh/id_rsa", mime="text/plain", uid="AgACevil")
            )
        )
        _, rows = enqueued(bot)
        inbox = (Path(bot.config.workspace) / "inbox").resolve()
        assert inbox in Path(rows[0]["path"]).resolve().parents

    @pytest.mark.asyncio
    async def test_the_same_file_twice_is_stored_once(self, bot) -> None:
        arm_download(bot, b"same bytes")
        await bot.handle_file(message(document=document(name="a.txt", mime="text/plain")))
        first = enqueued(bot)[1][0]["path"]
        bot._enqueue_message.reset_mock()
        await bot.handle_file(message(document=document(name="a.txt", mime="text/plain")))
        second = enqueued(bot)[1][0]["path"]
        assert first == second


class TestASecretsFileTheOperatorSends:
    @pytest.mark.asyncio
    async def test_it_is_kept_but_never_quoted_into_the_turn(self, bot) -> None:
        """`.json` is an extractable suffix, so `credentials.json` would have
        been decoded and pasted whole into the prompt."""
        arm_download(bot, b'{"private_key": "super-secret-value"}')
        await bot.handle_file(
            message(
                document=document(name="credentials.json", mime="application/json", uid="AgACcred")
            )
        )
        text, rows = enqueued(bot)
        assert rows[0]["secret"] is True
        assert Path(rows[0]["path"]).exists(), "the operator sent it; it is kept"
        assert "super-secret-value" not in text
        assert "kept, but not read" in text
        assert rows[0]["path"] in text, "they can still be told where it went"

    @pytest.mark.asyncio
    async def test_a_dotenv_is_marked_despite_the_sanitised_name(self, bot) -> None:
        arm_download(bot, b"TOKEN=abc")
        await bot.handle_file(
            message(document=document(name=".env", mime="text/plain", uid="AgACenv"))
        )
        _, rows = enqueued(bot)
        assert rows[0]["secret"] is True
        assert rows[0]["name"] == "env", "sanitising is exactly what loses the evidence"


class TestSizeCeiling:
    @pytest.mark.asyncio
    async def test_a_25mb_file_is_refused_with_the_real_limit(self, bot) -> None:
        msg = message(document=document(name="clip.mp4", size=25 * 1024 * 1024, mime="video/mp4"))
        await bot.handle_file(msg)
        msg.answer.assert_awaited_once()
        said = msg.answer.await_args.args[0]
        assert "20 MB" in said
        assert "clip.mp4" in said
        bot._enqueue_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_19_9mb_file_is_accepted(self, bot) -> None:
        arm_download(bot, b"nearly too big")
        size = int(19.9 * 1024 * 1024)
        await bot.handle_file(
            message(document=document(name="big.bin", size=size, mime="application/octet-stream"))
        )
        _, rows = enqueued(bot)
        assert rows

    @pytest.mark.asyncio
    async def test_a_file_reporting_no_size_is_still_capped(self, bot) -> None:
        """Hostile review I5. `file_size` is absent for some media types and
        for forwarded content; `media.size` is then 0, `size and size > MAX`
        is False, and the whole thing was buffered into memory with no cap. A
        25 MB payload went through with no refusal and no answer at all."""
        payload = b"x" * (attachments.MAX_DOWNLOAD_BYTES + 4096)
        arm_download(bot, payload)
        msg = message(document=document(name="forwarded.bin", size=0, mime=""))
        await bot.handle_file(msg)

        msg.answer.assert_awaited_once()
        said = msg.answer.await_args.args[0]
        assert "20 MB" in said
        assert "forwarded.bin" in said
        bot._enqueue_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_sentence_never_states_a_size_it_only_half_measured(self, bot) -> None:
        """Re-review R2. The bound fires the moment the write crosses the line,
        so the only number it has is how far the transfer got. A huge upload was
        reported as "is 21 MB" — true of the transfer, false of the file."""
        arm_download(bot, b"x" * (attachments.MAX_DOWNLOAD_BYTES * 4))
        msg = message(document=document(name="huge.bin", size=0, mime=""))
        await bot.handle_file(msg)
        said = msg.answer.await_args.args[0]
        assert "is larger than 20 MB" in said
        assert "21 MB" not in said
        assert "80 MB" not in said

    @pytest.mark.asyncio
    async def test_nothing_is_written_for_an_oversized_download(self, bot) -> None:
        arm_download(bot, b"x" * (attachments.MAX_DOWNLOAD_BYTES + 4096))
        await bot.handle_file(message(document=document(name="forwarded.bin", size=0, mime="")))
        inbox = Path(bot.config.workspace) / "inbox"
        assert not any(p.is_file() for p in inbox.rglob("*")) if inbox.exists() else True

    @pytest.mark.asyncio
    async def test_a_lying_declared_size_does_not_get_past_the_download_bound(self, bot) -> None:
        """Telegram says 1 KB, 25 MB arrives. The declared number is a hint."""
        arm_download(bot, b"x" * (attachments.MAX_DOWNLOAD_BYTES + 4096))
        msg = message(document=document(name="liar.bin", size=1024, mime=""))
        await bot.handle_file(msg)
        assert "20 MB" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_voice_note_with_no_declared_size_is_capped_too(self, bot) -> None:
        arm_download(bot, b"x" * (attachments.MAX_DOWNLOAD_BYTES + 4096))
        voice = MagicMock()
        voice.file_id = "AwAC1"
        voice.file_unique_id = "AgACvoice"
        voice.file_size = None
        voice.mime_type = "audio/ogg"
        voice.file_name = None
        msg = message(voice=voice)
        with patch.dict("os.environ", {"ROBOTHOR_VOICE_NOTES_ENABLED": "1"}):
            await bot.handle_voice(msg)
        assert "20 MB" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_file_just_under_the_ceiling_still_arrives(self, bot) -> None:
        payload = b"x" * (attachments.MAX_DOWNLOAD_BYTES - 1)
        arm_download(bot, payload)
        await bot.handle_file(message(document=document(name="big.bin", size=0, mime="")))
        _, rows = enqueued(bot)
        assert rows[0]["size"] == len(payload)

    @pytest.mark.asyncio
    async def test_the_old_five_megabyte_ceiling_is_gone(self) -> None:
        from robothor.engine import telegram_handlers

        assert telegram_handlers.MAX_FILE_SIZE == attachments.MAX_DOWNLOAD_BYTES


class TestPhotos:
    @pytest.mark.asyncio
    async def test_the_caption_is_the_instruction_not_the_vision_prompt(
        self, bot, monkeypatch
    ) -> None:
        seen: dict[str, str] = {}

        async def fake_vlm(data, prompt="", **kw):
            seen["prompt"] = prompt
            return "a whiteboard covered in boxes"

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", fake_vlm)
        arm_download(bot, b"\xff\xd8\xffJPEGBYTES")
        await bot.handle_file(message(caption="what's this?", photo=photo()))

        text, rows = enqueued(bot)
        assert text.startswith("what's this?")
        assert seen["prompt"] != "what's this?", (
            "the caption is the operator's question, never the VLM's prompt"
        )
        assert rows[0]["kind"] == "image"
        assert rows[0]["width"] == 1280

    @pytest.mark.asyncio
    async def test_the_image_is_kept_and_view_image_is_offered(self, bot, monkeypatch) -> None:
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a cat"),
        )
        arm_download(bot, b"\xff\xd8\xffJPEGBYTES")
        await bot.handle_file(message(photo=photo()))
        text, rows = enqueued(bot)
        assert Path(rows[0]["path"]).read_bytes() == b"\xff\xd8\xffJPEGBYTES"
        assert "view_image" in text

    @pytest.mark.asyncio
    async def test_no_caption_asks_what_to_do(self, bot, monkeypatch) -> None:
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a cat"),
        )
        arm_download(bot, b"\xff\xd8\xff")
        await bot.handle_file(message(photo=photo()))
        text, _ = enqueued(bot)
        assert "ask what they want done" in text

    @pytest.mark.asyncio
    async def test_a_failing_vision_model_never_stops_the_file_being_kept(
        self, bot, monkeypatch
    ) -> None:
        async def broken(data, prompt="", **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("robothor.engine.tools.handlers.images.describe_image_bytes", broken)
        arm_download(bot, b"\xff\xd8\xff")
        await bot.handle_file(message(caption="look", photo=photo()))
        text, rows = enqueued(bot)
        assert Path(rows[0]["path"]).exists()
        assert "view_image" in text

    @pytest.mark.asyncio
    async def test_a_four_thousand_character_caption_survives_intact(
        self, bot, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a cat"),
        )
        arm_download(bot, b"\xff\xd8\xff")
        caption = "please " * 570
        await bot.handle_file(message(caption=caption.strip(), photo=photo()))
        text, _ = enqueued(bot)
        assert caption.strip() in text


class TestAlbums:
    @pytest.mark.asyncio
    async def test_three_photos_arrive_as_one_turn(self, bot, monkeypatch) -> None:
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a photo"),
        )
        arm_download(bot, b"\xff\xd8\xff")
        monkeypatch.setattr(
            "robothor.engine.telegram_attachments.ALBUM_WINDOW_SECONDS", 0.05, raising=True
        )
        for index in range(3):
            await bot.handle_file(
                message(
                    caption="three shots" if index == 0 else "",
                    photo=photo(uid=f"AgACpic{index}"),
                    media_group_id="MG1",
                )
            )
        await asyncio.sleep(0.25)
        text, rows = enqueued(bot)
        assert len(rows) == 3
        assert text.startswith("three shots")
        for row in rows:
            assert row["path"] in text

    @pytest.mark.asyncio
    async def test_an_album_of_ten_keeps_all_ten(self, bot, monkeypatch) -> None:
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a photo"),
        )
        arm_download(bot, b"\xff\xd8\xff")
        monkeypatch.setattr(
            "robothor.engine.telegram_attachments.ALBUM_WINDOW_SECONDS", 0.05, raising=True
        )
        for index in range(10):
            await bot.handle_file(message(photo=photo(uid=f"AgACten{index}"), media_group_id="MG2"))
        await asyncio.sleep(0.3)
        _, rows = enqueued(bot)
        assert len(rows) == 10


class TestALateAlbumMember:
    """Hostile review I4. The flush popped its buffer and then awaited the
    route. A member arriving in that window found no buffer, created a fresh
    one and a fresh timer — and the OLD flush's unconditional `finally` then
    deleted both. The new timer fired into nothing: the photo was on disk and
    the agent was never told it existed, and because `_album_tasks[key]` went
    too, `stop()` could no longer cancel the orphan timer.
    """

    @pytest.mark.asyncio
    async def test_it_gets_its_own_turn_instead_of_vanishing(
        self, bot, monkeypatch, tmp_path
    ) -> None:
        import robothor.engine.telegram_attachments as ta

        monkeypatch.setattr(ta, "ALBUM_WINDOW_SECONDS", 0.05, raising=True)
        gate = asyncio.Event()
        routed: list[list[str]] = []

        async def slow_route(chat_id, note, rows, user_info, message):
            # Hold the flush open exactly as a real enqueue or DB write would.
            await gate.wait()
            routed.append([Path(row["path"]).name for row in rows])

        bot._route_attachment_turn = slow_route
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a photo"),
        )
        arm_download(bot, b"\xff\xd8\xff")

        for uid in ("D01", "D02"):
            await bot.handle_file(message(photo=photo(uid=uid), media_group_id="GX"))
        await asyncio.sleep(0.15)
        key = ("100200300", "GX")
        assert key not in bot._album_buffers, "the flush should hold its own copy by now"

        # The late member arrives while the first flush is still routing.
        await bot.handle_file(message(photo=photo(uid="D99"), media_group_id="GX"))
        late_task = bot._album_tasks[key]

        gate.set()
        await asyncio.sleep(0.05)
        assert bot._album_buffers.get(key) is not None, "the old flush destroyed it"
        assert bot._album_tasks.get(key) is late_task, "stop() could no longer cancel it"

        await asyncio.sleep(0.25)
        assert [name for turn in routed for name in turn] == [
            "D01-photo.jpg",
            "D02-photo.jpg",
            "D99-photo.jpg",
        ]
        assert late_task.done()

    @pytest.mark.asyncio
    async def test_every_saved_file_is_named_in_some_turn(self, bot, monkeypatch, tmp_path) -> None:
        """The property that actually matters: nothing is kept in silence."""
        import robothor.engine.telegram_attachments as ta

        monkeypatch.setattr(ta, "ALBUM_WINDOW_SECONDS", 0.05, raising=True)
        gate = asyncio.Event()
        told: set[str] = set()

        async def slow_route(chat_id, note, rows, user_info, message):
            await gate.wait()
            told.update(Path(row["path"]).name for row in rows)

        bot._route_attachment_turn = slow_route
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a photo"),
        )
        arm_download(bot, b"\xff\xd8\xff")

        for uid in ("E01", "E02"):
            await bot.handle_file(message(photo=photo(uid=uid), media_group_id="GY"))
        await asyncio.sleep(0.15)
        await bot.handle_file(message(photo=photo(uid="E99"), media_group_id="GY"))
        gate.set()
        await asyncio.sleep(0.35)

        on_disk = {
            path.name
            for path in (Path(bot.config.workspace) / "inbox" / "telegram").rglob("E*")
            if path.is_file()
        }
        assert on_disk, "the probe wrote nothing"
        assert on_disk - told == set(), "saved but never mentioned to the agent"


class TestOtherKinds:
    @pytest.mark.asyncio
    async def test_a_sticker_is_kept_as_an_image(self, bot, monkeypatch) -> None:
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.images.describe_image_bytes",
            AsyncMock(return_value="a sticker of a duck"),
        )
        arm_download(bot, b"RIFFwebp")
        sticker = MagicMock()
        sticker.file_id = "CAAC1"
        sticker.file_unique_id = "AgACstick"
        sticker.file_size = 20
        sticker.width = 512
        sticker.height = 512
        sticker.is_animated = False
        sticker.is_video = False
        sticker.emoji = "🦆"
        sticker.file_name = None  # aiogram Sticker has none either
        await bot.handle_file(message(sticker=sticker))
        _, rows = enqueued(bot)
        assert rows[0]["kind"] == "image"

    @pytest.mark.asyncio
    async def test_a_video_is_kept(self, bot) -> None:
        arm_download(bot, b"\x00\x00\x00 ftypmp42")
        video = MagicMock()
        video.file_id = "BAAC1"
        video.file_unique_id = "AgACvid"
        video.file_size = 4096
        video.file_name = "clip.mp4"
        video.mime_type = "video/mp4"
        video.width = 1920
        video.height = 1080
        await bot.handle_file(message(video=video))
        _, rows = enqueued(bot)
        assert rows[0]["kind"] == "video"
        assert Path(rows[0]["path"]).exists()

    @pytest.mark.asyncio
    async def test_a_message_with_nothing_attached_says_so(self, bot) -> None:
        msg = message()
        await bot.handle_file(msg)
        msg.answer.assert_awaited_once()
        bot._enqueue_message.assert_not_awaited()


class TestPersistence:
    """What the stored user turn carries. The Helm chat UI reads this next."""

    def test_the_rows_go_on_the_turn_beside_the_text(self) -> None:
        from robothor.engine.telegram import build_user_extras

        rows = [{"path": "/w/inbox/telegram/100200300/2026-09-15/u-a.txt", "kind": "document"}]
        extras = build_user_extras(user_message_id="7", reply_ctx=None, attachments=rows)
        assert extras is not None
        assert extras["attachments"] == rows
        assert extras["telegram_message_id"] == "7"

    def test_a_plain_text_turn_is_unchanged(self) -> None:
        """No files, no reply, no message id — the payload must stay exactly
        what it was before any of this existed."""
        from robothor.engine.telegram import build_user_extras

        assert build_user_extras(user_message_id=None, reply_ctx=None, attachments=None) is None

    def test_attachments_do_not_displace_a_reply_linkage(self) -> None:
        from robothor.engine.telegram import build_user_extras

        extras = build_user_extras(
            user_message_id="7",
            reply_ctx={"platform_message_id": "42", "author_agent_id": "devops"},
            attachments=[{"path": "/w/a.png", "kind": "image"}],
        )
        assert extras is not None
        assert extras["replies_to"]["author_agent_id"] == "devops"
        assert extras["attachments"]

    @pytest.mark.asyncio
    async def test_the_drain_threads_them_from_the_handler_to_the_turn(self, bot) -> None:
        """The production path: the handler buffers rows, the drain pops them
        in the same synchronous stretch as the text and hands them on."""
        real_enqueue = TelegramBot._enqueue_message
        bot._enqueue_message = real_enqueue.__get__(bot, TelegramBot)
        captured: dict = {}

        async def fake_run_interactive(*a, **kw):
            captured.update(kw)

        bot._run_interactive = fake_run_interactive
        arm_download(bot, b"hello")
        await bot.handle_file(message(document=document(name="a.txt", mime="text/plain")))
        await asyncio.sleep(0.5)

        rows = captured.get("attachments") or []
        assert len(rows) == 1
        assert rows[0]["name"] == "a.txt"
        assert bot._attachment_buffers.get("100200300") in (None, [])

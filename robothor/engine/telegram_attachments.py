"""The Telegram attachment intake: what arrives, where it is kept, what the
agent is told about it.

Split out of ``telegram_handlers.py`` rather than added to it. That module is
already the engine's largest handler file and the decomposition ratchet asks
for a cohesive cluster in a module of its own instead of one more feature on a
god-object — and this IS one cluster: recognising an attachment, downloading
it, saving it (``robothor.engine.attachments``), describing it, grouping an
album, and routing the composed turn.

The rule the whole cluster enforces is that **the bytes survive**. What used to
happen: the file was downloaded into memory, turned into text or into the
string ``"[Binary file: name, N bytes]"``, and dropped. The agent was handed a
description of something it could never open, forward, convert, attach to a
task or re-read. A photo lost more still — the operator's caption was spent as
the vision model's prompt and then cleared, so the question they actually asked
never reached the agent at all.

Now: save first, describe second. Text and PDF extraction still happen, but as
a convenience beside the path rather than in place of it, and the extract says
how much of the file it is.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiogram.types import Message

from robothor.engine import attachments
from robothor.engine.chat import _plan_is_expired, get_shared_session

logger = logging.getLogger(__name__)

__all__ = [
    "ALBUM_WINDOW_SECONDS",
    "MAX_ALBUM_ITEMS",
    "MAX_FILE_SIZE",
    "MediaRef",
    "TelegramAttachmentsMixin",
    "media_ref",
]

# The ceiling is Telegram's own (20 MB for `getFile`), not one of ours: the
# 5 MB that used to live in the handler refused files the Bot API would happily
# have handed over, and the operator had no way to tell the difference between
# "too big for Telegram" and "too big for us".
MAX_FILE_SIZE = attachments.MAX_DOWNLOAD_BYTES

#: How long an album's members are collected before the turn is composed.
#: Telegram sends each photo of an album as its own update, typically inside a
#: few hundred milliseconds; 1.5 s covers a slow batch without making the
#: operator wait on an ordinary single file (which never enters this path).
ALBUM_WINDOW_SECONDS = 1.5

#: How many members of one album get their own entry in the composed turn.
#: Telegram's own album limit is 10; the headroom covers a client that batches
#: differently. Beyond it the files are still SAVED and the note says how many
#: were left out — a media_group_id is chosen by the sender, so an unbounded
#: one is a way to push an arbitrarily long note into the model's context.
MAX_ALBUM_ITEMS = 25


def _voice_notes_enabled() -> bool:
    """Is inbound voice transcription armed on this instance?

    Through the settings registry, which is where ``ROBOTHOR_VOICE_NOTES_ENABLED``
    has been declared all along — the handler read ``os.environ`` directly, so an
    operator who set it in ``config.yaml`` was ignored. Defaults to off if
    settings cannot be resolved: an unreadable config must not silently turn on
    a capability that has no provider behind it.
    """
    try:
        from robothor.settings import get_settings

        return bool(get_settings().channels.voice_notes_enabled)
    except Exception:  # noqa: BLE001 - a config failure never arms a feature
        logger.debug("settings unavailable while reading voice_notes_enabled")
        return False


class AttachmentTooLargeError(Exception):
    """A download crossed the ceiling. Carries how far it got, for the sentence.

    Its own exception rather than a return value because it is raised from
    inside ``aiogram``'s writer callback, where there is nowhere to return to.
    """

    def __init__(self, written: int) -> None:
        super().__init__(f"attachment exceeded {attachments.MAX_DOWNLOAD_BYTES} bytes")
        self.written = written


class _BoundedBuffer(io.BytesIO):
    """A ``BytesIO`` that refuses to grow past *limit*.

    aiogram writes into whatever file-like object it is handed, so the cheapest
    place to enforce a download ceiling is the object itself. Overriding
    ``write`` means the check happens per chunk and the process never holds
    more than the limit plus one chunk — which is the point: the old code
    trusted the declared size and buffered whatever arrived.
    """

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit
        self._written = 0

    def write(self, data: Any, /) -> int:
        self._written += len(data)
        if self._written > self._limit:
            raise AttachmentTooLargeError(self._written)
        return super().write(data)


@dataclass(frozen=True)
class MediaRef:
    """What one inbound attachment is, before anything has been downloaded.

    Built from the aiogram message so the intake has ONE shape to work with
    rather than a branch per Telegram field. ``size`` is what Telegram claims,
    which is what the ceiling is checked against — checking after the download
    would mean downloading the file we are about to refuse.
    """

    kind: str
    file_id: str
    file_unique_id: str
    name: str
    mime: str = ""
    size: int = 0
    width: int | None = None
    height: int | None = None


def _ref(
    media: Any,
    *,
    kind: str,
    fallback_name: str,
    mime: str = "",
) -> MediaRef | None:
    """One :class:`MediaRef` from an aiogram media object, or None if unusable."""
    file_id = str(getattr(media, "file_id", "") or "")
    unique = str(getattr(media, "file_unique_id", "") or "")
    if not file_id or not unique:
        return None
    resolved_mime = str(getattr(media, "mime_type", "") or mime or "")
    name = str(getattr(media, "file_name", "") or "") or fallback_name
    return MediaRef(
        kind=kind or attachments.kind_for(resolved_mime, name),
        file_id=file_id,
        file_unique_id=unique,
        name=name,
        mime=resolved_mime,
        size=int(getattr(media, "file_size", 0) or 0),
        width=getattr(media, "width", None),
        height=getattr(media, "height", None),
    )


def media_ref(message: Any) -> MediaRef | None:
    """The one attachment on *message*, whatever field Telegram put it in.

    Order matters only in that a message carries exactly one of these. A
    sticker is treated as an image when it is a still one and as a video when
    it is animated, because that is what it is on disk — and because an agent
    asking to look at an animated sticker with ``view_image`` should be told it
    is a video file rather than handed a frame.
    """
    if getattr(message, "document", None):
        doc = message.document
        name = str(getattr(doc, "file_name", "") or "") or "document"
        mime = str(getattr(doc, "mime_type", "") or "")
        return _ref(doc, kind=attachments.kind_for(mime, name), fallback_name="document")
    if getattr(message, "photo", None):
        # The last entry is the largest rendition Telegram kept.
        return _ref(message.photo[-1], kind="image", fallback_name="photo.jpg", mime="image/jpeg")
    if getattr(message, "video", None):
        return _ref(message.video, kind="video", fallback_name="video.mp4", mime="video/mp4")
    if getattr(message, "audio", None):
        return _ref(message.audio, kind="audio", fallback_name="audio.mp3", mime="audio/mpeg")
    if getattr(message, "voice", None):
        return _ref(message.voice, kind="voice", fallback_name="voice.ogg", mime="audio/ogg")
    if getattr(message, "video_note", None):
        return _ref(
            message.video_note, kind="video", fallback_name="video_note.mp4", mime="video/mp4"
        )
    if getattr(message, "sticker", None):
        sticker = message.sticker
        animated = bool(
            getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False)
        )
        if animated:
            return _ref(sticker, kind="video", fallback_name="sticker.webm", mime="video/webm")
        return _ref(sticker, kind="image", fallback_name="sticker.webp", mime="image/webp")
    return None


def _kind_from_the_bytes(media: MediaRef, raw: bytes) -> MediaRef:
    """Correct ``kind`` against what actually arrived. Round-1 M5.

    ``kind`` comes from the MIME type Telegram reported and the name the sender
    chose, and neither is evidence. A text file called ``notes.png`` with
    ``mime: image/png`` was filed as an image, so the agent was told to
    ``view_image`` something that answers "could not read as an image"; a real
    PNG called ``.dat`` was filed as a document and never offered to be looked
    at. Both are cosmetic until an agent acts on the note, which is the whole
    point of the note.

    Only the image/document boundary is corrected — that is the one the note
    changes its advice on. A video or audio claim is left alone: there is no
    cheap prefix test for the long tail of container formats, and getting it
    wrong there costs nothing an agent acts on.
    """
    actually_an_image = attachments.looks_like_an_image(raw)
    if media.kind == "image" and not actually_an_image:
        return replace(media, kind="document")
    if media.kind == "document" and actually_an_image:
        return replace(media, kind="image")
    return media


class TelegramAttachmentsMixin:
    """The intake, as ``TelegramBot`` methods. See the module docstring.

    Same contract as ``TelegramHandlersMixin``: these ARE ``TelegramBot``
    methods at runtime and may use its composed surface.
    """

    if TYPE_CHECKING:  # pragma: no cover - typing only

        def __getattr__(self, name: str) -> Any: ...

    def _attachment_workspace(self) -> Any:
        """Where this instance keeps its inbox. Never a hardcoded home path."""
        return getattr(getattr(self, "config", None), "workspace", None)

    async def _download_media(self, media: MediaRef) -> bytes:
        """Fetch one attachment's bytes through the Bot API.

        Raises on anything that goes wrong; the caller turns that into a
        sentence for the operator rather than a traceback.

        The ceiling is enforced on what ARRIVES, not on what Telegram claimed.
        ``file_size`` is absent for some media types and for forwarded content,
        and ``media.size`` is then 0 — which made ``size and size > MAX`` False
        and buffered the whole thing into memory unbounded. A 25 MB payload
        reporting no size went through with no refusal and no answer to the
        operator.

        :class:`_BoundedBuffer` raises :class:`AttachmentTooLargeError` the moment the write
        crosses the line, so the memory cost is bounded by the limit plus one
        chunk rather than by whatever the sender felt like uploading.
        """
        handle = await self.bot.get_file(media.file_id)
        file_path = getattr(handle, "file_path", None)
        if not file_path:
            raise RuntimeError("Telegram returned no download path for the file")
        buffer = _BoundedBuffer(attachments.MAX_DOWNLOAD_BYTES)
        await self.bot.download_file(file_path, buffer)
        return buffer.getvalue()

    async def _keep_attachment(
        self, chat_id: str, media: MediaRef, caption: str
    ) -> attachments.NotedAttachment | None:
        """Save one attachment and gather what can cheaply be said about it.

        ``None`` means the download failed — the caller tells the operator, and
        nothing is enqueued, because an agent told "a file arrived" with no file
        behind it is exactly the state this replaces.
        """
        try:
            raw = await self._download_media(media)
        except AttachmentTooLargeError:
            # Re-raised: the caller owes the operator the sentence naming the
            # limit, which is a different answer from "the download failed".
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            logger.warning("Telegram attachment download failed (%s): %s", media.name, exc)
            return None

        media = _kind_from_the_bytes(media, raw)

        try:
            row = attachments.save_attachment(
                chat_id=chat_id,
                file_id=media.file_id,
                file_unique_id=media.file_unique_id,
                name=media.name,
                data=raw,
                kind=media.kind,
                mime=media.mime,
                width=media.width,
                height=media.height,
                caption=caption,
                workspace=self._attachment_workspace(),
            )
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            # Round-1 M3: only the DOWNLOAD was guarded, so a ValueError from
            # the containment assertion or an OSError from a full disk escaped
            # into aiogram's dispatcher and the operator got no reply at all.
            # The one thing worse than losing the file is losing it silently.
            logger.warning("Could not save the attachment %s: %s", media.name, exc)
            return None
        noted = attachments.NotedAttachment(row=row)
        await self._enrich_attachment(noted, raw, media, chat_id)
        return noted

    async def _enrich_attachment(
        self,
        noted: attachments.NotedAttachment,
        raw: bytes,
        media: MediaRef,
        chat_id: str,
    ) -> None:
        """Extracted text for a document, a local description for a picture.

        Both are best-effort and neither can fail the intake: the file is
        already on disk and the path is already in the note, so the worst case
        is an agent that has to open it itself.
        """
        from pathlib import PurePath

        from robothor.engine.telegram_handlers import _extract_pdf_text

        if noted.row.get("secret"):
            # Named like a credentials file. It is on disk and the operator can
            # ask for it to be moved or renamed, but its CONTENTS never reach a
            # prompt — `credentials.json` would otherwise have been decoded and
            # quoted whole, because `.json` is an extractable suffix.
            return

        suffix = PurePath(media.name).suffix.lower()
        extract = attachments.extractable(suffix, media.mime)
        if extract is not None and media.kind != "image":
            whole = (
                await _extract_pdf_text(raw)
                if extract == "pdf"
                else raw.decode("utf-8", errors="replace")
            )
            noted.text_total_chars = len(whole)
            noted.text = whole[: attachments.TEXT_EXTRACT_CHARS]
            return

        if media.kind != "image":
            return
        if self._model_sees_images(chat_id):
            # The agent's own model can look at the file itself, and its eyes
            # beat an 11B local model's paragraph. The note still names the
            # path and tells it to call view_image.
            return
        from robothor.engine.tools.handlers.images import describe_image_bytes

        try:
            noted.vision = await describe_image_bytes(raw)
        except Exception as exc:  # noqa: BLE001 - a description is never invented
            logger.info("local vision model unavailable for an inbound photo: %s", exc)
            noted.error = (
                "no description available — the local vision model did not answer; "
                "call view_image on the path to look at it yourself"
            )

    def _model_sees_images(self, chat_id: str) -> bool:
        """Can the model this chat runs on be shown a picture?

        Only a model the registry states can — ``unknown`` counts as no, which
        is the safe direction HERE and the opposite of the direction
        ``view_image`` takes. The asymmetry is deliberate: describing an image
        nobody needed described costs one local call, while withholding a
        description from an agent that turns out to be blind costs the turn.
        """
        from robothor.engine.model_registry import image_capability

        return image_capability(self._model_override.get(chat_id, "")) == "accepts"

    # ── Voice and video notes ──
    #
    # Here rather than in `telegram_handlers` because this IS the intake: it
    # calls `media_ref`, `_keep_attachment` and the same ceiling. Transcription
    # is still gated on ROBOTHOR_VOICE_NOTES_ENABLED (no STT provider is wired)
    # but the RECORDING is kept either way — an audio file the operator sent
    # once and cannot resend is the same loss this whole change is about, and
    # whatever STT lands next needs a file to work from.

    async def handle_voice(self, message: Message) -> None:
        """Keep a voice or video note; transcription is still pending a provider.

        The flag is read through ``get_settings().channels.voice_notes_enabled``
        rather than from ``os.environ``. It was a raw read that MOVED here with
        the handler, which is the cheap moment to route it through the
        declaration that already existed (re-review R5) — and the declaration is
        what makes ``genus config`` able to show it and an operator able to set
        it in ``config.yaml`` rather than only in a unit file.
        """
        if not message.from_user:
            return
        if not _voice_notes_enabled():
            await message.answer(
                "🎤 I can't process voice notes yet — please send text. "
                "(Voice transcription will arrive once an STT provider is configured.)"
            )
            return

        media = media_ref(message)
        if media is None:
            await message.answer("🎤 I couldn't read that voice note.")
            return
        if media.size and media.size > attachments.MAX_DOWNLOAD_BYTES:
            await message.answer(attachments.too_large_sentence(media.size, name=media.name))
            return
        try:
            noted = await self._keep_attachment(str(message.chat.id), media, "")
        except AttachmentTooLargeError:
            # Voice notes are one of the kinds Telegram most often reports no
            # size for, so the declared check above sees 0 and the bound on the
            # DOWNLOAD is what actually holds here. No size is passed: all this
            # path knows is that the file is bigger than the limit (R2).
            await message.answer(attachments.too_large_sentence(name=media.name))
            return
        if noted is None:
            await message.answer("🎤 Couldn't fetch the voice note from Telegram.")
            return
        await message.answer(
            "🎤 Voice received and saved — transcription isn't wired up yet, so tell me in "
            "text what you need and I can work from the recording's file."
        )

    async def handle_file(self, message: Message) -> None:
        """Keep whatever arrived, then hand the agent the caption and the paths."""
        if not message.from_user:
            return

        chat_id = str(message.chat.id)

        user_info = self._resolve_user(chat_id, message)
        if user_info is None:
            reply = await self._handle_unregistered_sender(message, str(message.from_user.id))
            await message.answer(reply)
            return

        caption = (message.caption or "").strip()
        media = media_ref(message)
        if media is None:
            await message.answer(
                "I couldn't tell what that attachment was. Send it as a file and I'll keep it."
            )
            return

        # What Telegram CLAIMS, when it claims anything: refusing here saves the
        # download. It is not the only check — `_download_media` bounds what
        # actually arrives — because `file_size` is absent for some media types
        # and for forwarded content, and 0 used to mean "no ceiling at all".
        if media.size and media.size > attachments.MAX_DOWNLOAD_BYTES:
            await message.answer(attachments.too_large_sentence(media.size, name=media.name))
            return

        try:
            noted = await self._keep_attachment(chat_id, media, caption)
        except AttachmentTooLargeError:
            # The bound fired mid-transfer, so the only thing known here is that
            # the file is bigger than the limit — not how big. Saying "is 21 MB"
            # for a 500 MB upload was true of the transfer and false of the file
            # (re-review R2).
            await message.answer(attachments.too_large_sentence(name=media.name))
            return
        if noted is None:
            await message.answer(
                f"I couldn't download {media.name} from Telegram, so I have nothing to work "
                "with. Try sending it again."
            )
            return

        logger.info(
            "Telegram attachment in chat %s: %s (%s, %s bytes), caption=%s",
            chat_id,
            noted.row.get("name"),
            noted.row.get("kind"),
            noted.row.get("size"),
            "yes" if caption else "none",
        )

        group_id = getattr(message, "media_group_id", None)
        if group_id:
            self._collect_album(chat_id, str(group_id), caption, noted, user_info, message)
            return

        await self._route_attachment_turn(
            chat_id,
            attachments.format_attachment_note(caption, [noted]),
            [noted.row],
            user_info,
            message,
        )

    # ── Albums ──
    #
    # Telegram sends an album as N separate updates sharing a media_group_id,
    # and the caption rides on exactly one of them. Handled one at a time, the
    # operator's three photos became three runs, two of them captionless. They
    # are collected for a short window and delivered as ONE turn with N paths.

    def _collect_album(
        self,
        chat_id: str,
        group_id: str,
        caption: str,
        noted: attachments.NotedAttachment,
        user_info: dict[str, Any],
        message: Message,
    ) -> None:
        """Add one album member to the pending group, starting its timer once."""
        key = (chat_id, group_id)
        pending = self._album_buffers.get(key)
        if pending is None:
            pending = {"items": [], "caption": "", "user_info": user_info, "message": message}
            self._album_buffers[key] = pending
            # ADD, never replace. A late member starting a second window over a
            # flush that is still routing used to overwrite the slot, and the
            # first task then survived `stop()` and delivered its turn into a
            # closed session (re-review R4).
            self._album_tasks.setdefault(key, set()).add(
                asyncio.create_task(self._flush_album(chat_id, group_id))
            )
        if len(pending["items"]) < MAX_ALBUM_ITEMS:
            pending["items"].append(noted)
        else:
            # The FILE is already saved — only its entry in the composed turn is
            # dropped, and the prompt says how many.
            pending["overflow"] = int(pending.get("overflow", 0)) + 1
        # First caption wins: Telegram puts it on one member, and a later empty
        # one must not erase it.
        if caption and not pending["caption"]:
            pending["caption"] = caption

    async def _flush_album(self, chat_id: str, group_id: str) -> None:
        """Wait out the window, then enqueue the whole album as one turn.

        The cleanup removes only what THIS flush owns. It used to pop the key
        unconditionally in a ``finally``, and the window between popping the
        buffer and finishing the route is long enough for a late member to
        arrive, find no buffer, create a fresh one and start a fresh timer — at
        which point the old flush's ``finally`` deleted both. The new timer
        then fired into nothing, so the late photo was saved to disk and the
        agent was never told it existed; worse, dropping ``_album_tasks[key]``
        also meant ``stop()`` could no longer cancel the orphan timer.
        """
        key = (chat_id, group_id)
        mine = asyncio.current_task()
        try:
            await asyncio.sleep(ALBUM_WINDOW_SECONDS)
            pending = self._album_buffers.pop(key, None)
            if not pending or not pending["items"]:
                return
            items = list(pending["items"])
            note = attachments.format_attachment_note(pending["caption"], items)
            overflow = int(pending.get("overflow", 0))
            if overflow:
                note += (
                    f"\n\n[{overflow} further file(s) arrived in the same batch and are in "
                    f"the same inbox folder as the paths above — list the directory to find "
                    f"them.]"
                )
            await self._route_attachment_turn(
                chat_id,
                note,
                [item.row for item in items],
                pending["user_info"],
                pending["message"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram album %s could not be delivered", group_id)
        finally:
            # Remove only THIS task. The buffer is not touched at all — this
            # flush took its own copy at the top, so anything under the key now
            # belongs to a later album — and the set keeps every other in-flight
            # task so `stop()` can still cancel them.
            live = self._album_tasks.get(key)
            if live is not None:
                live.discard(mine)
                if not live:
                    self._album_tasks.pop(key, None)

    async def _route_attachment_turn(
        self,
        chat_id: str,
        user_text: str,
        rows: list[dict[str, Any]],
        user_info: dict[str, Any],
        message: Message,
    ) -> None:
        """Send the composed turn down the same path a text message takes."""
        session_key = self._session_key(chat_id)
        session = get_shared_session(session_key)

        if session.active_plan and session.active_plan.status == "pending":
            if not _plan_is_expired(session.active_plan):
                session.active_plan.rejection_feedback = user_text
                session.active_plan.status = "superseded"
                session.active_plan = None
                await self._run_plan_mode(
                    chat_id, session_key, session, user_text, message, sender_info=user_info
                )
                return
            session.active_plan.status = "expired"
            session.active_plan = None

        if session.plan_mode:
            session.plan_mode = False
            await self._run_plan_mode(
                chat_id, session_key, session, user_text, message, sender_info=user_info
            )
            return

        if getattr(message, "message_id", None):
            self._user_message_id_buffers[chat_id] = str(message.message_id)

        await self._enqueue_message(
            chat_id,
            session_key,
            session,
            user_text,
            sender_info=user_info,
            attachments=rows,
        )

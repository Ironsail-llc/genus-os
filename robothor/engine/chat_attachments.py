"""Files attached to a webchat turn: checked, kept, and described to the agent.

The dashboard's composer posts ``attachments: [{name, mime, size, data}]``
(base64) beside the message. This is the webchat end of the pipeline Telegram
already uses (:mod:`robothor.engine.attachments`): each file is saved under
``<workspace>/inbox/webchat/<session>/<date>/``, a file named like a credential
goes one level deeper into ``secret/`` and is never quoted, and the agent gets
the operator's words followed by the same structured note Telegram writes —
paths, kinds, sizes, ``view_image`` for a picture, the extracted text of a
document.

The limits are the composer's, enforced again here because the browser is not
the boundary: :data:`MAX_FILES` a turn, :data:`MAX_FILE_BYTES` a file, the
types in :data:`ACCEPTED_MIMES`. Too many or too large answers 413; a type off
the list, a body that is not base64, or a file that claims to be a picture and
is not, answers 422. Nothing is written until every file has passed.

A picture also reaches the model as an image block, but only when the registry
says the model ``accepts`` images. ``unknown`` counts as no, the same rule
Telegram applies before skipping its local description: the note always names
the path, so a model that cannot take the block can still ``view_image`` it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from robothor.engine import attachments

__all__ = [
    "ACCEPTED_MIMES",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "AttachmentRefusal",
    "WebchatTurn",
    "parse_attachments",
    "prepare_turn",
]

#: Files one turn may carry. The composer states it; the route and this check it.
MAX_FILES = 3

#: Bytes one file may be — five megabytes, well under the inbox's own ceiling
#: (``attachments.MAX_DOWNLOAD_BYTES``) because the whole turn travels as JSON.
MAX_FILE_BYTES = 5 * 1024 * 1024

#: What the composer offers: four picture formats and the plain documents the
#: intake can read into the turn.
ACCEPTED_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/pdf",
    }
)

CHANNEL = "webchat"

logger = logging.getLogger(__name__)


class AttachmentRefusal(Exception):  # noqa: N818 - a refusal, not a failure
    """One attachment the turn cannot carry, with the HTTP status that says why."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    def body(self) -> dict[str, Any]:
        """The JSON answer: the reason, and that no turn was started."""
        return {"error": self.message, "request_admitted": False}


@dataclass
class _Incoming:
    name: str
    mime: str
    data: bytes


@dataclass
class WebchatTurn:
    """What a turn with files becomes: the text, the image blocks, the record."""

    text: str
    images: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def extras(self) -> dict[str, Any] | None:
        from robothor.engine.chat_store import build_user_extras

        return build_user_extras(attachments=self.rows)


def _limit_words() -> str:
    return f"{MAX_FILE_BYTES // (1024 * 1024)} MB"


def parse_attachments(raw: Any) -> list[_Incoming]:
    """Every attachment decoded and checked, or :class:`AttachmentRefusal`."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttachmentRefusal(422, "attachments must be a list")
    if len(raw) > MAX_FILES:
        raise AttachmentRefusal(413, f"at most {MAX_FILES} files per message")
    out: list[_Incoming] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AttachmentRefusal(422, "each attachment must be an object")
        name = item.get("name")
        mime = item.get("mime")
        data = item.get("data")
        if not isinstance(name, str) or not name.strip():
            raise AttachmentRefusal(422, "an attachment has no name")
        if not isinstance(mime, str) or mime.strip().lower() not in ACCEPTED_MIMES:
            raise AttachmentRefusal(422, f"{name} is not a supported type")
        if not isinstance(data, str):
            raise AttachmentRefusal(422, f"{name} has no data")
        # A cheap bound before decoding: base64 is 4 characters per 3 bytes.
        if len(data) > (MAX_FILE_BYTES // 3 + 1) * 4:
            raise AttachmentRefusal(413, f"{name} is larger than {_limit_words()}")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise AttachmentRefusal(422, f"{name} is not valid base64") from None
        if len(decoded) > MAX_FILE_BYTES:
            raise AttachmentRefusal(413, f"{name} is larger than {_limit_words()}")
        declared = item.get("size")
        if declared is not None and declared != len(decoded):
            raise AttachmentRefusal(422, f"{name}'s size does not match its data")
        mime = mime.strip().lower()
        if mime.startswith("image/") and not attachments.looks_like_an_image(decoded):
            raise AttachmentRefusal(422, f"{name} is not the picture it claims to be")
        out.append(_Incoming(name=name, mime=mime, data=decoded))
    return out


async def _enrich(noted: attachments.NotedAttachment, item: _Incoming) -> None:
    """The extracted text of a document, as Telegram's intake gathers it.

    Best-effort, like Telegram's: the file is already on disk and its path is
    in the note, so a failure here leaves an un-enriched entry with a note
    saying why, never a failed turn.
    """
    try:
        await _extract(noted, item)
    except Exception as exc:  # noqa: BLE001 - the file is already kept
        logger.warning("Could not read the text of webchat attachment %s: %s", item.name, exc)
        noted.text = ""
        noted.text_total_chars = 0
        noted.error = "the text could not be extracted; read_file the path to open it"


async def _extract(noted: attachments.NotedAttachment, item: _Incoming) -> None:
    from robothor.engine import file_extract

    if noted.row.get("secret") or noted.row.get("kind") == "image":
        return
    suffix = PurePath(item.name).suffix.lower()
    extract = attachments.extractable(suffix, item.mime)
    if extract == "text" and file_extract.looks_binary(item.data[:8192]):
        # The mail path's guard: a NUL byte means these are not text, and
        # decoding them would quote a page of replacement characters.
        noted.error = "not quoted: the bytes are not text; read_file the path to inspect it"
        return
    if extract == "text":
        whole = item.data.decode("utf-8", errors="replace")
        noted.text_total_chars = len(whole)
        noted.text = whole[: attachments.TEXT_EXTRACT_CHARS]
    elif extract is not None:
        whole = await asyncio.to_thread(
            file_extract.describe_bytes,
            item.data,
            extract,
            max_chars=attachments.TEXT_EXTRACT_CHARS,
        )
        noted.text_total_chars = len(whole)
        noted.text = whole


def _forget(rows: list[dict[str, Any]]) -> None:
    """Remove what this turn wrote, so a refused turn leaves no files behind.

    A deduplicated row's file was already there before this turn, from an
    earlier one, and is left alone.
    """
    for row in rows:
        if row.get("deduplicated"):
            continue
        with contextlib.suppress(OSError):
            Path(str(row.get("path", ""))).unlink()


def _model_for(config: Any, session: Any, session_key: str) -> str:
    """The model this turn will run on: the session's override, else the agent's."""
    if getattr(session, "model_override", None):
        return str(session.model_override)
    parts = session_key.split(":")
    agent_id = parts[1] if len(parts) >= 2 else getattr(config, "default_chat_agent", "main")
    try:
        from robothor.engine.config import load_agent_config

        agent = load_agent_config(agent_id, config.manifest_dir, config.workspace)
    except Exception:  # noqa: BLE001 - an unreadable manifest means "unknown"
        return ""
    return agent.model_primary if agent else ""


async def prepare_turn(
    body: dict[str, Any], message: str, *, session: Any, session_key: str, config: Any
) -> WebchatTurn:
    """Check, keep and describe the request's files. Raises :class:`AttachmentRefusal`."""
    incoming = parse_attachments(body.get("attachments"))
    if incoming and body.get("join_running"):
        # A follow-up joins a run whose prompt is already built: there is no
        # turn left for the files' note or their image blocks to ride on.
        raise AttachmentRefusal(422, "files cannot be added to a running task; send them next")
    if not incoming:
        return WebchatTurn(text=message)
    from robothor.engine.model_registry import image_capability

    sees_images = image_capability(_model_for(config, session, session_key)) == "accepts"
    request_id = str(body.get("request_id") or uuid.uuid4())
    turn = WebchatTurn(text=message)
    noted_all: list[attachments.NotedAttachment] = []
    for index, item in enumerate(incoming):
        kind = attachments.kind_for(item.mime, item.name)
        try:
            row = await asyncio.to_thread(
                attachments.save_attachment,
                chat_id=session_key or "main",
                file_id=f"{request_id}:{index}",
                file_unique_id=hashlib.sha256(item.data).hexdigest(),
                name=item.name,
                data=item.data,
                kind=kind,
                mime=item.mime,
                workspace=getattr(config, "workspace", None),
                channel=CHANNEL,
            )
        except Exception as exc:  # noqa: BLE001 - answered, and the turn's files removed
            # Telegram's rule: a file that cannot be kept is an answer to the
            # operator, never a traceback. No turn starts, so nothing the
            # turn already wrote is worth keeping.
            logger.warning("Could not keep webchat attachment %s: %s", item.name, exc)
            _forget(turn.rows)
            raise AttachmentRefusal(
                500, f"{attachments.safe_name(item.name)} could not be kept; try sending it again"
            ) from None
        if attachments.safe_name(item.name) != item.name:
            # The chip shows what the operator sent; the stored name is the
            # sanitised one (`Résumé.txt` becomes `R-sum.txt`).
            row.setdefault("original_name", item.name)
        noted = attachments.NotedAttachment(row=row)
        await _enrich(noted, item)
        noted_all.append(noted)
        turn.rows.append(row)
        if kind == "image" and sees_images and not row.get("secret"):
            encoded = base64.b64encode(item.data).decode("ascii")
            turn.images.append(
                {"type": "image_url", "image_url": {"url": f"data:{item.mime};base64,{encoded}"}}
            )
    turn.text = attachments.format_attachment_note(message, noted_all)
    return turn

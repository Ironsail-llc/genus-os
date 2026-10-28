"""Opening a file attached to an email: download it, keep it, read it.

The mail tools listed attachments by name, type and size and stopped there, so
the assistant could see that a contract was attached and could not read it.
This is the step after the listing, for whichever provider serves the mailbox
(the provider fetches the bytes; see ``MailProvider.get_attachment``).

Nothing here is new machinery. The bytes go through the SAME intake a Telegram
file does (:mod:`robothor.engine.attachments`): the 20 MB ceiling, the inbox
tree (``<workspace>/inbox/email/<provider>/<day>/``), owner-only files,
deduplication, the credentials quarantine (a ``.env`` is kept under
``secret/`` and never quoted) and the retention prune. The text comes from
:mod:`robothor.engine.file_extract`, which ``read_file`` uses too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.engine import attachments, file_extract

if TYPE_CHECKING:
    from robothor.workspace.protocols import BlockingMail

__all__ = ["INBOX_CHANNEL", "fetch_attachment"]

#: The inbox subtree email attachments are kept under.
INBOX_CHANNEL = "email"


def _invalid(message: str) -> dict[str, Any]:
    return {"error": message, "hint": "invalid_params: take both ids from gws_gmail_get"}


def _too_large(name: str, size: int) -> dict[str, Any]:
    from robothor.workspace.bridge import error_result
    from robothor.workspace.errors import attachment_too_large

    return error_result(attachment_too_large(name, size, attachments.MAX_DOWNLOAD_BYTES))


def fetch_attachment(
    mail: BlockingMail,
    args: dict[str, Any],
    *,
    provider: str,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Download one attachment, save it into the inbox and return what it says."""
    message_id = str(args.get("message_id") or "").strip()
    attachment_id = str(args.get("attachment_id") or "").strip()
    if not message_id or not attachment_id:
        return _invalid("message_id and attachment_id are both required")

    limit = attachments.MAX_DOWNLOAD_BYTES
    got = mail.get_attachment(message_id, attachment_id, max_bytes=limit)
    if not isinstance(got, dict):
        return {"error": "the mail provider returned an unexpected shape for an attachment"}
    if "error" in got:
        return got
    data = got.get("data")
    if not isinstance(data, bytes | bytearray):
        return {"error": "the mail provider returned no bytes for this attachment"}
    data = bytes(data)
    name = str(got.get("filename") or "attachment")
    mime = str(got.get("mime_type") or "")
    # The provider checked the size it was TOLD; this checks the bytes it got.
    if len(data) > limit:
        return _too_large(name, len(data))

    kind = attachments.kind_for(mime, name)
    if kind == "image" and not attachments.looks_like_an_image(data):
        kind = "document"  # a claim is not evidence; see attachments._IMAGE_MAGIC
    # The same bytes from the same message land on the same path.
    uid = hashlib.sha256(f"{message_id}\0{attachment_id}".encode()).hexdigest()[:16]
    row = attachments.save_attachment(
        chat_id=provider or "mail",
        file_id="",
        file_unique_id=uid,
        name=name,
        data=data,
        kind=kind,
        mime=mime,
        workspace=workspace or None,
        channel=INBOX_CHANNEL,
    )
    out: dict[str, Any] = {
        "path": row["path"],
        "filename": name,
        "mime_type": mime,
        "size_bytes": len(data),
    }
    if row.get("secret"):
        out["format"] = "secret"
        out["secret"] = True
        out["message"] = attachments.SECRET_FILE_NOTE
        return out
    if kind == "image":
        out["format"] = "image"
        out["message"] = "This is an image. Call view_image with this path to look at it."
        return out

    cap = attachments.TEXT_EXTRACT_CHARS
    extract = attachments.extractable(Path(name).suffix, mime)
    if extract == "text" or (extract is None and not file_extract.looks_binary(data[:8192])):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None:
            out["format"] = "text"
            out["text"] = text[:cap]
            out["truncated"] = len(text) > cap
            return out
    elif extract is not None:
        out["format"] = extract
        try:
            text = file_extract.extract_bytes(data, extract, max_chars=cap)
        except file_extract.ExtractionError as exc:
            out["extract_error"] = str(exc)
            return out
        stopped = file_extract.STOPPED_EARLY in text
        out["text"] = text if stopped else text[:cap]
        out["truncated"] = stopped or len(text) > cap
        return out

    out["format"] = "binary"
    out["message"] = (
        f"Saved ({attachments.human_size(len(data))}, {mime or 'unknown type'}); there is "
        "no text reader for this format, so its contents were not returned."
    )
    return out

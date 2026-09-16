"""Inbound files are KEPT, at a path the agent can actually use.

The complaint this module answers, from the operator on 2026-09-15: "In
Telegram we should be able to send and receive pictures and files to one
another, and that doesn't work well, but it should."

What it did instead
-------------------
``telegram_handlers.handle_file`` downloaded a document into memory, extracted
text if the extension was on a list, reduced everything else to the string
``"[Binary file: name, N bytes]"`` — and then **dropped the bytes**. A photo was
downloaded, described by a local vision model, and the image dropped the same
way; worse, the operator's caption was consumed as the vision PROMPT, so the
agent never saw the words the operator typed as an instruction. The agent could
not open, forward, convert, re-read or attach anything it had been sent, and a
5 MB ceiling refused files Telegram itself would have handed over (the Bot API's
``getFile`` allows 20 MB).

The rule here
-------------
**A file that arrived is on disk before the agent is told about it, and the
agent is told the path.** Everything else — the extracted text, the vision
description — is a convenience layered on top of that path, never a substitute
for it. Text extraction is still capped, and the cap now ships with the path so
the agent can read the rest rather than believing the file ended.

Containment is by construction, not by inspection: every component of the
destination is sanitised into a single path segment before it is joined, and
the result is verified to be under the inbox root before a byte is written. A
document named ``../../.ssh/id_rsa`` therefore lands as ``<inbox>/…/id_rsa``
and nothing outside the tree is reachable, which is the first probe any hostile
review of this code will run.

Kept for :data:`DEFAULT_RETENTION_DAYS` days by a daily prune that walks the
inbox and nothing else — a file the agent moved somewhere useful has left the
tree and is no longer this module's business.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "INBOX_DIRNAME",
    "MAX_DOWNLOAD_BYTES",
    "MAX_NAME_CHARS",
    "TEXT_EXTRACT_CHARS",
    "NotedAttachment",
    "configured_retention_days",
    "extractable",
    "format_attachment_note",
    "human_size",
    "is_inbox_secret",
    "inbox_path",
    "inbox_root",
    "kind_for",
    "prune_inbox",
    "safe_name",
    "save_attachment",
    "too_large_sentence",
]

#: Telegram's own ceiling for ``getFile`` — 20 MB. The previous 5 MB was ours,
#: not the platform's, and it refused files the Bot API would have handed over.
#: Anything above this is refused with a sentence that NAMES the limit, because
#: "file too large" with no number leaves the operator guessing.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

#: Directory under the workspace that holds everything received over a channel.
INBOX_DIRNAME = "inbox"

#: Where a file whose ORIGINAL name said "credentials" is kept, under the day's
#: directory. The directory IS the flag: a filename cannot carry it (sanitising
#: destroys the evidence — ``.env`` becomes ``env``) and a name that has to be
#: parsed to be understood will eventually be parsed wrongly, which is exactly
#: what the hostile review found. Everything in here is kept and nothing in
#: here is read into a prompt, sent, or opened by ``read_file``.
SECRET_SUBDIR = "secret"

#: Longest a sanitised filename may be. Long enough for a real document name,
#: short enough that ``<file_unique_id>-<name>`` stays inside the 255-byte
#: limit every filesystem this runs on enforces.
MAX_NAME_CHARS = 120

#: The text-extraction cap, unchanged from the handler it came from. It rides
#: with the total and the path now, which is the actual fix: the agent used to
#: be handed a truncated file with no way of knowing it was truncated.
TEXT_EXTRACT_CHARS = 50_000

#: How long a kept file survives with nobody touching it.
DEFAULT_RETENTION_DAYS = 30

#: Extensions whose bytes are worth decoding as text for the agent's first
#: turn. Moved here from ``telegram_handlers`` so the tool layer and the
#: channel layer share one answer. ``.env`` is deliberately ABSENT: a file
#: named like a credentials file is saved but never quoted into a prompt —
#: see ``robothor.engine.secret_paths`` for the rule this follows.
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".py",
        ".js",
        ".ts",
        ".sh",
        ".toml",
        ".ini",
        ".cfg",
        ".log",
        ".eml",
        ".tex",
        ".rst",
        ".sql",
    }
)

#: Extensions that mean "picture" when the platform told us no MIME type.
_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
)

#: Everything a sanitised path segment may contain. Anything else collapses to
#: a single dash, which is what makes traversal impossible rather than merely
#: unlikely: ``/`` and ``\`` and ``..`` cannot survive this.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

#: Leading dots and dashes, stripped so a sanitised name can never be a dotfile
#: or read as an option by something later in the chain.
_LEADING_JUNK = re.compile(r"^[.\-]+")


def safe_name(name: str, *, default: str = "file") -> str:
    """One filesystem-safe path SEGMENT derived from *name*.

    Directory separators, parent references, control characters and spaces all
    collapse; what survives is a name with at most one meaningful suffix. Pure,
    so the answer does not depend on what happens to exist on disk.
    """
    raw = str(name or "")
    # Both separators, whatever platform typed the name. `PurePosixPath` alone
    # leaves `..\..\file` intact on Linux, which is a traversal on the box that
    # eventually reads it.
    tail = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_CHARS.sub("-", tail).strip("-")
    cleaned = _LEADING_JUNK.sub("", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    # A dash that only exists because punctuation was stripped next to a dot
    # ("report (final).pdf") is noise in the name, not part of it.
    cleaned = re.sub(r"-*\.-*", ".", cleaned).strip("-.")
    if not cleaned or set(cleaned) <= {".", "-"}:
        return default
    if len(cleaned) > MAX_NAME_CHARS:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and 0 < len(suffix) <= 12:
            keep = MAX_NAME_CHARS - len(suffix) - 1
            cleaned = f"{stem[:keep]}.{suffix}"
        else:
            cleaned = cleaned[:MAX_NAME_CHARS]
    return cleaned or default


def _workspace_root(workspace: str | Path | None = None) -> Path:
    """The instance workspace this inbox hangs off.

    An explicit argument wins (the bot passes its own ``EngineConfig``
    workspace, a tool passes ``ctx.workspace``); otherwise settings answer.
    Never a hardcoded home directory — see the platform/instance rule in
    the root ``CLAUDE.md``.
    """
    if workspace:
        return Path(str(workspace)).expanduser()
    try:
        from robothor.settings import get_settings

        configured = str(get_settings().paths.workspace or "")
        if configured:
            return Path(configured).expanduser()
    except Exception:  # noqa: BLE001 - settings must never break an inbound file
        logger.debug("settings unavailable while resolving the inbox workspace")
    from robothor.settings.sources import workspace_path

    resolved = workspace_path()
    if resolved is None:
        # No workspace and no home: keep the file beside the process rather
        # than dropping it, which is the failure this module exists to end.
        return Path.cwd()
    return resolved


def inbox_root(workspace: str | Path | None = None, *, channel: str = "telegram") -> Path:
    """``<workspace>/inbox/<channel>`` — the only tree this module writes to."""
    return _workspace_root(workspace) / INBOX_DIRNAME / safe_name(channel, default="channel")


def inbox_path(
    *,
    chat_id: str,
    file_unique_id: str,
    name: str,
    workspace: str | Path | None = None,
    channel: str = "telegram",
    when: datetime | None = None,
) -> Path:
    """Where one attachment belongs: ``<inbox>/<chat>/<date>[/secret]/<uid>-<name>``.

    The date is the day it ARRIVED, which is what makes retention a directory
    walk rather than a database query, and the ``file_unique_id`` prefix is what
    makes two files with the same name from the same day distinct.

    A file whose ORIGINAL name said "credentials" goes one level deeper, into
    :data:`SECRET_SUBDIR`. That directory is how every later reader knows — the
    sanitised filename cannot say it, and a name that has to be parsed to be
    understood gets parsed wrongly: Telegram's ``file_unique_id`` is URL-safe
    base64 and the first version of that parse split on ``-``.

    Every component is sanitised into a single segment before it is joined, so
    a hostile ``chat_id`` or ``name`` cannot reach outside the root. The result
    is asserted to be inside the root as well: two independent checks, because
    a containment bug here writes an attacker's bytes wherever they asked.
    """
    root = inbox_root(workspace, channel=channel)
    day = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
    # A group chat id is negative; the sign is part of the id, not punctuation.
    chat_segment = safe_name(str(chat_id).replace("-", "neg-", 1), default="unknown")
    chat_segment = chat_segment.replace("neg-", "-", 1)
    if chat_segment in ("", "-"):
        chat_segment = "unknown"
    uid = safe_name(file_unique_id, default="file")
    folder = root / chat_segment / day
    if holds_credentials(name):
        folder = folder / SECRET_SUBDIR
    candidate = folder / f"{uid}-{safe_name(name)}"
    resolved_root = root.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if resolved_root not in resolved.parents:
        # Unreachable through `safe_name`, and kept anyway: the day it becomes
        # reachable is the day this refuses rather than the day it writes.
        raise ValueError("attachment path escaped the inbox root")
    return candidate


def kind_for(mime: str, name: str = "") -> str:
    """The coarse family a file belongs to: what the agent needs to decide.

    MIME first because Telegram usually supplies one, extension second because
    it sometimes does not. Anything unrecognised is a ``document``: a file the
    agent can still open, which is the honest answer, rather than a guess.
    """
    lowered = (mime or "").strip().lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("video/"):
        return "video"
    if lowered.startswith("audio/"):
        return "audio"
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    return "document"


def extractable(suffix: str, mime: str = "") -> str | None:
    """``"text"``, ``"pdf"`` or None — what can be quoted into the first turn.

    Never decides on MIME alone: Telegram reports ``application/octet-stream``
    for plenty of real text files, and reports ``text/plain`` for some that are
    not. The suffix is the operator's own label for the file.
    """
    lowered = (suffix or "").lower()
    if lowered == ".pdf" or (mime or "").strip().lower() == "application/pdf":
        return "pdf"
    if lowered in TEXT_EXTENSIONS:
        return "text"
    return None


def holds_credentials(name: str) -> bool:
    """Does the name Telegram supplied say this file holds credentials?

    Asked on the ORIGINAL name, because sanitising destroys the evidence:
    ``.env`` becomes ``env`` and ``.ssh/id_rsa`` becomes ``id_rsa``, and
    :func:`robothor.engine.secret_paths.is_secret_path` recognises neither. A
    file saved under a sanitised name would otherwise be readable, quotable and
    sendable where the original was refused.
    """
    from robothor.engine.secret_paths import is_secret_path

    raw = str(name or "").replace("\\", "/")
    return is_secret_path(raw) or is_secret_path(raw.rsplit("/", 1)[-1])


def is_inbox_secret(path: str | os.PathLike[str]) -> bool:
    """Is *path* the inbox copy of a file that holds credentials?

    Answered from the DIRECTORY, not the filename. The first version of this
    stripped the ``<file_unique_id>-`` prefix by splitting on the first ``-``
    and asked ``secret_paths`` about what was left — and Telegram's
    ``file_unique_id`` is URL-safe base64, whose alphabet contains ``-``. When
    the uid held one, the split cut in the wrong place: ``BQAD-77-credentials
    .json`` was read as ``77-credentials.json``, which matches nothing, and the
    hostile review sent an OAuth client-secret JSON in full. Roughly one uid in
    four for a 20-character id.

    So the verdict is recorded once, at save time, by putting the file in
    :data:`SECRET_SUBDIR`. A directory cannot be mis-parsed, survives a
    restart, needs no sidecar to stay in sync with the bytes, and is visible to
    an operator listing the tree.
    """
    parts = PurePath(str(path)).parts
    if INBOX_DIRNAME not in parts:
        return False
    return SECRET_SUBDIR in parts[parts.index(INBOX_DIRNAME) :]


#: What the agent is told in place of a secrets file's contents. The file IS
#: kept — the operator sent it deliberately and may want it moved, renamed or
#: handed to a tool — but nothing quotes it into a prompt.
SECRET_FILE_NOTE = (
    "kept, but not read: this is named like a credentials file, and credentials "
    "reach tools through the platform rather than through the transcript. If a "
    "value is missing, say so instead of opening it."
)


def human_size(size: int) -> str:
    """``5 B`` / ``245.3 KB`` / ``12.0 MB`` — a size an operator can read."""
    value = float(max(0, int(size)))
    if value < 1024:
        return f"{int(value)} B"
    unit = "KB"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            break
    # A whole number keeps no decimal: the refusal sentence says "20 MB",
    # which is the number Telegram's own documentation states, and "20.0 MB"
    # reads like a measurement rather than a limit.
    rendered = f"{value:.1f}".removesuffix(".0")
    return f"{rendered} {unit}"


def too_large_sentence(size: int | None = None, *, name: str = "") -> str:
    """What the operator is told about a file the Bot API will not hand over.

    Names the real limit and offers the way round it. A refusal that says only
    "too large" makes the operator guess at a number the platform already knows.

    ``size`` is the file's ACTUAL size, and ``None`` means nobody knows it. That
    distinction is the whole point of re-review R2: the download bound fires the
    moment the write crosses the limit, so the only number available there is
    how far the transfer got — and passing it in made a 500 MB upload come back
    as "is 21 MB, and Telegram only lets me download files up to 20 MB". Honest
    about the limit, wrong about the file, on a platform whose whole theme is
    not saying things it cannot show. When only the bound is known the sentence
    says "is larger than 20 MB" and claims nothing more.
    """
    which = f"“{name}”" if name else "That file"
    if size is None:
        measured = f"{which} is larger than {human_size(MAX_DOWNLOAD_BYTES)}"
    else:
        measured = f"{which} is {human_size(size)}"
    return (
        f"{measured}, and Telegram only lets me download files up to "
        f"{human_size(MAX_DOWNLOAD_BYTES)}. Put it somewhere I can fetch it and send me the "
        "link, or split it up."
    )


def save_attachment(
    *,
    chat_id: str,
    file_id: str,
    file_unique_id: str,
    name: str,
    data: bytes,
    kind: str,
    mime: str = "",
    width: int | None = None,
    height: int | None = None,
    caption: str = "",
    workspace: str | Path | None = None,
    channel: str = "telegram",
    when: datetime | None = None,
) -> dict[str, Any]:
    """Write one attachment into the inbox and return the row that records it.

    Owner-only (0600) because an inbound file is the operator's, not the box's.
    Deduplicated by ``file_unique_id``: Telegram's own identity for the bytes,
    so the same picture forwarded twice occupies one path and the second row
    says ``deduplicated``.

    Returns the dict that is stored on the message row and read by every
    consumer (the Helm chat UI next). Keys with nothing to say are ABSENT
    rather than null, so a reader never has to tell "unknown" from "zero".
    """
    path = inbox_path(
        chat_id=chat_id,
        file_unique_id=file_unique_id,
        name=name,
        workspace=workspace,
        channel=channel,
        when=when,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    deduplicated = path.exists() and path.stat().st_size == len(data)
    if not deduplicated:
        # Open with the mode set rather than chmod-ing afterwards: between the
        # write and the chmod the bytes are world-readable, and this tree holds
        # whatever the operator happened to send.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            # A half-written attachment is worse than none: the agent would be
            # handed a path to truncated bytes and no way to tell.
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        path.chmod(0o600)
        if when is not None:
            stamp = when.timestamp()
            os.utime(path, (stamp, stamp))

    row: dict[str, Any] = {
        "path": str(path),
        "name": safe_name(name),
        # Judged on the name Telegram gave it, NOT on the sanitised one: `.env`
        # sanitises to `env` and `.ssh/id_rsa` to `id_rsa`, so by the time the
        # file is on disk `secret_paths` no longer recognises either. Recorded
        # here, once, so every reader downstream can ask — the intake uses it to
        # refuse quoting the file into a prompt, and `send_file` reads it before
        # it reads the bytes.
        #
        # The file is still SAVED. The operator sent it on purpose; refusing to
        # keep it would be a different kind of unhelpful. What is refused is
        # putting its contents in front of a model.
        "kind": kind,
        "mime": mime or "",
        "size": len(data),
        "telegram_file_id": str(file_id or ""),
        "telegram_file_unique_id": str(file_unique_id or ""),
        "caption": caption or "",
    }
    if holds_credentials(name):
        row["secret"] = True
        # The name Telegram gave it, carried on the row because the stored one
        # cannot say it: `.env` sanitises to `env`. The Helm chat UI shows the
        # operator what they actually sent; nothing derives a decision from it.
        row["original_name"] = str(name or "")
    if width:
        row["width"] = int(width)
    if height:
        row["height"] = int(height)
    if deduplicated:
        row["deduplicated"] = True
    return row


@dataclass
class NotedAttachment:
    """One saved attachment plus whatever could be said about it cheaply.

    ``text`` is the extract that fits; ``text_total_chars`` is how long the
    whole thing was, so the note can say what was left behind rather than
    handing the agent a truncated file it believes is complete. ``vision`` is
    the local VLM's paragraph, present only when the agent's own model cannot
    accept images — it is a FALLBACK, and the note labels it as one.
    """

    row: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    text_total_chars: int = 0
    vision: str = ""
    error: str = ""


def _describe(item: NotedAttachment, index: int, total: int) -> list[str]:
    row = item.row
    name = str(row.get("name") or "file")
    kind = str(row.get("kind") or "document")
    bits = [kind]
    if row.get("mime"):
        bits.append(str(row["mime"]))
    if row.get("width") and row.get("height"):
        bits.append(f"{row['width']}x{row['height']}")
    bits.append(human_size(int(row.get("size") or 0)))
    prefix = f"{index}. " if total > 1 else "- "
    lines = [f"{prefix}{name} — {', '.join(bits)}", f"   path: {row.get('path', '')}"]
    if row.get("secret"):
        lines.append(f"   {SECRET_FILE_NOTE}")
        return lines
    if item.error:
        lines.append(f"   note: {item.error}")
    if kind == "image":
        lines.append(f"   call view_image on {row.get('path', '')} to look at it")
    if item.vision:
        lines.append(f"   local vision model (fallback, not your own eyes): {item.vision}")
    if item.text:
        total_chars = item.text_total_chars or len(item.text)
        if total_chars > len(item.text):
            lines.append(
                f"   extracted text — {len(item.text)} of {total_chars} characters; "
                f"read_file the path above for the rest:"
            )
        else:
            lines.append("   extracted text:")
        lines.append(f"   --- begin {name} ---")
        lines.append(item.text)
        lines.append(f"   --- end {name} ---")
    return lines


def format_attachment_note(caption: str, items: Iterable[NotedAttachment]) -> str:
    """The user turn for a message that carried files.

    **The caption comes first and verbatim.** It is the operator's instruction,
    and before this it was consumed as the vision model's prompt and then
    deleted — the operator asked "what's this?" and the agent was handed a
    description with no question attached to it.

    Everything after it is a structured inventory: one entry per attachment,
    each naming its path, kind and size, with the cheap extras (extracted text,
    the fallback description) indented underneath. Structured rather than prose
    because the agent has to be able to find the PATH.
    """
    noted = list(items)
    parts: list[str] = []
    text = (caption or "").strip()
    if text:
        parts.append(text)
    if not noted:
        return "\n\n".join(parts)

    count = len(noted)
    header = "1 file attached" if count == 1 else f"{count} files attached"
    lines = [f"[{header} — kept on disk; use the paths below]"]
    for index, item in enumerate(noted, start=1):
        lines.extend(_describe(item, index, count))
    parts.append("\n".join(lines))
    if not text:
        parts.append(
            "The operator sent this with no message. Look at what arrived, say what it is, "
            "and ask what they want done with it."
        )
    return "\n\n".join(parts)


def configured_retention_days() -> int:
    """How long the inbox keeps a file, from settings, defaulting sanely."""
    try:
        from robothor.settings import get_settings

        return int(get_settings().channels.inbox_retention_days)
    except Exception:  # noqa: BLE001 - a prune must never break on config
        return DEFAULT_RETENTION_DAYS


def prune_inbox(
    *,
    retention_days: int | None = None,
    workspace: str | Path | None = None,
    channel: str = "telegram",
    now: datetime | None = None,
) -> int:
    """Delete inbox files older than the retention window. Returns the count.

    Walks the inbox tree and nothing else: a file an agent MOVED somewhere
    useful has left this tree and is not the prune's business — which is the
    difference between retention and deleting the operator's work.

    ``retention_days <= 0`` disables the prune entirely rather than deleting
    everything, because "keep for zero days" is far more likely to be a
    misconfiguration than an instruction.
    """
    days = retention_days if retention_days is not None else configured_retention_days()
    if days is None or int(days) <= 0:
        return 0
    root = inbox_root(workspace, channel=channel)
    if not root.is_dir():
        return 0
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=int(days))).timestamp()
    removed = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the sweep
            logger.warning("inbox prune could not remove a file: %s", exc)
    # Empty day/chat directories left behind are noise; the tree, never above it.
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            # Non-empty is the normal case and not an error.
            with contextlib.suppress(OSError):
                path.rmdir()
    return removed

"""Whether a file may leave the box — one ladder, asked at every send.

Hostile review 2026-09-15, finding C1. The ladder used to live inside the
``send_file`` tool, which meant it ran once, against the bytes that were on disk
at the moment the agent called the tool. For a scheduled run that is not the
moment the file is sent: ``send_file`` queues a PATH,
``delivery_attachments.send_attachments`` takes it back out, and the channel
reads the bytes at send time. The reviewer's probe wrote a benign CSV, queued
it, overwrote it with an AWS secret key, and the operator received the key.
``AgentRun.attachments`` was worse: those paths had never been checked at all.

So the ladder is here, and both callers run it **immediately before the bytes
are read**. The same function, asked twice — which is the argument this change
already makes for reusing ``scan_secret_literals`` rather than keeping a second
opinion about what a credential looks like.

The order matters and each rung earns its place:

1. **Containment** — the RESOLVED path (symlinks followed) must be inside the
   workspace.
2. **Hard links** — a second name for a file outside the workspace is a name the
   resolver cannot see. ``ln ~/.ssh/id_rsa ~/robothor/notes.bin`` was a working
   exfiltration of the operator's private key (review I3).
3. **Secret paths** — :mod:`robothor.engine.secret_paths`, the same rule
   ``read_file`` and ``exec`` enforce.
4. **The inbox secret flag** — a credentials file the operator SENT is kept in a
   dedicated subdirectory precisely so this question has an answer that no
   filename parsing can get wrong (review I1, I6).
5. **Shape and size** — exists, is a file, is not empty, is under the ceiling.
6. **Credential shapes** — every file small enough, whatever its name: the
   decode is the "is this text?" test, and it is better than a suffix list
   (review I2).
7. **The pin** — for a queued file, the digest it was approved with.

A refusal is a sentence for the agent. It names the file and the reason and
**never the value**, because an error that helpfully echoes the credential
publishes it to the transcript, which is the exposure being refused.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "CHANGED_MARKER",
    "MAX_DOCUMENT_BYTES",
    "MAX_SCAN_BYTES",
    "digest_of",
    "refuse_to_send",
    "resolve_for_send",
]

#: Telegram's ceiling for an outbound document. Checked before the read, so a
#: 2 GB file is refused rather than buffered into memory first.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

#: Largest file scanned in full for credential shapes. Above this a file is
#: refused rather than sent unscanned: "too big to check" must not quietly mean
#: "sent anyway".
MAX_SCAN_BYTES = 4 * 1024 * 1024

#: How many bytes of a file are decoded to decide whether it is text at all.
#: A real binary raises ``UnicodeDecodeError`` inside the first few hundred
#: bytes; a text file with a misleading suffix does not. There is no suffix list
#: any more — renaming ``tok.txt`` to ``tok.png`` was the whole attack.
SCAN_HEAD_BYTES = 256 * 1024

#: The token a refusal carries when the file is not the one that was approved.
#: Matched by callers and by tests, so the wording above it can change.
CHANGED_MARKER = "changed_since_queued"

#: Read size for hashing. Large enough that a 50 MB file is a handful of reads.
_CHUNK = 1024 * 1024


def digest_of(path: Path | str) -> str:
    """A content digest for *path*, or ``""`` if it cannot be read.

    Streamed, because the ceiling is 50 MB and a queue of ten of them should not
    be held in memory to be hashed. ``""`` on failure rather than a raise: the
    caller's next step is the ladder, which will refuse an unreadable file with
    a sentence the agent can act on.
    """
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def resolve_for_send(raw_path: str, root: Path) -> tuple[Path | None, str | None]:
    """``(resolved path, refusal)`` — exactly one of the two is None.

    Relative paths resolve against the workspace, which is also what makes
    ``../../../../etc/passwd`` land outside it and be refused. ``strict=False``
    so a missing file is reported as missing rather than as an ``OSError``, and
    symlinks are followed BEFORE containment is judged.
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return None, "path is required"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if root != resolved and root not in resolved.parents:
        return None, (
            f"refused: {Path(raw).name} resolves outside the workspace. Only files "
            "inside the workspace (including the channel inbox) can be sent."
        )
    return resolved, None


#: Longest UTF-8 sequence, so a read that stops mid-character stops at most
#: this far into one.
_MAX_UTF8_SEQUENCE = 4


def _decode_head(head: bytes) -> str | None:
    """*head* as text, or None when the bytes are genuinely not text.

    The subtlety is that a TRUNCATING read can cut a multi-byte character in
    half, and a strict decode then raises exactly as it would for a binary
    file. Treating that as "binary" would silently stop scanning a real text
    file whose 256 KB boundary happened to land inside a ``€`` — which is the
    same class of bug as the suffix gate this replaced: a rule that answers
    "not text" for the wrong reason.

    So a failure within the last few bytes of a full-length read is retried
    without them. A failure anywhere earlier is a real binary.
    """
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if len(head) == SCAN_HEAD_BYTES and exc.start >= len(head) - _MAX_UTF8_SEQUENCE:
            try:
                return head[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


def _credential_refusal(path: Path, size: int) -> str | None:
    """Why this file's CONTENTS may not leave the box, or None.

    Every file is offered to the decoder, whatever it is called. The
    ``UnicodeDecodeError`` catch is the "is this actually text?" test and it is
    strictly better than a suffix list: ``tok.txt`` was refused and the same
    bytes as ``tok.png`` were sent (review I2). Only the head is decoded, so the
    cost is bounded for a large file.
    """
    if size > MAX_SCAN_BYTES:
        return (
            f"refused: {path.name} is {size // 1024 // 1024} MB, too large to check for "
            "credentials before sending. Send a smaller extract of it."
        )
    try:
        with path.open("rb") as handle:
            head = handle.read(SCAN_HEAD_BYTES)
    except OSError:
        # Caught by the shape rungs above; nothing to scan either way.
        return None
    text = _decode_head(head)
    if text is None:
        # Genuinely binary: no lines for the scanner to judge.
        return None

    from robothor.templates.bundle import scan_secret_literals

    findings = scan_secret_literals(text, path.name)
    if not findings:
        return None
    reasons = sorted({finding.reason for finding in findings})
    lines = sorted({finding.line for finding in findings})[:5]
    return (
        f"refused: {path.name} contains {reasons[0]} (line "
        f"{', '.join(str(line) for line in lines)}). Credentials must not leave the box in a "
        "file. Remove the value or send a redacted copy."
    )


def refuse_to_send(
    path: Path | str,
    root: Path | str,
    *,
    expect_digest: str | None = None,
) -> str | None:
    """Why *path* may not be sent, or None if every rung passes.

    ``expect_digest`` pins a queued file to the bytes it was approved with. A
    mismatch is refused with :data:`CHANGED_MARKER` in the sentence rather than
    re-judged on its merits: the file the agent asked to send is not the file on
    disk, and silently sending the new one is the C1 defect.

    Never raises. A file that cannot be stat'd is a refusal, not an exception,
    because one caller is a delivery loop that must not turn a failed attachment
    into a failed run.
    """
    resolved = Path(path)
    root_path = Path(root).resolve(strict=False)
    try:
        resolved = resolved.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - resolve barely ever raises
        return f"refused: {resolved.name} could not be resolved ({type(exc).__name__})."

    if root_path != resolved and root_path not in resolved.parents:
        return (
            f"refused: {resolved.name} resolves outside the workspace. Only files inside "
            "the workspace (including the channel inbox) can be sent."
        )

    from robothor.engine.secret_paths import is_secret_path, refusal_for

    if is_secret_path(resolved):
        return refusal_for(resolved)

    from robothor.engine.attachments import is_inbox_secret

    if is_inbox_secret(resolved):
        return (
            f"refused: {resolved.name} was received over a channel and is named like a "
            "credentials file. It is kept on disk, but its contents do not leave the box."
        )

    try:
        if not resolved.exists():
            return f"no such file: {resolved.name}"
        if not resolved.is_file():
            return f"refused: {resolved.name} is not a file. Send one file at a time."
        stat = resolved.stat()
    except OSError as exc:
        return f"refused: {resolved.name} could not be read ({type(exc).__name__})."

    # A hard link is a second NAME for the same inode, and the resolver cannot
    # see the other one. `ln ~/.ssh/id_rsa ~/robothor/notes.bin` puts the
    # operator's private key inside the workspace under a name nothing objects
    # to. Refusing every multiply-linked file is blunt — a legitimately
    # hard-linked file inside the workspace is refused too — and it is the only
    # check that does not require walking the whole tree to answer.
    if stat.st_nlink > 1:
        return (
            f"refused: {resolved.name} has more than one name on this filesystem, so it "
            "may be the same file as something outside the workspace. Copy it to a new "
            "path and send the copy."
        )

    size = stat.st_size
    if size == 0:
        return f"refused: {resolved.name} is empty — there is nothing to send."
    if size > MAX_DOCUMENT_BYTES:
        from robothor.engine.attachments import human_size

        return (
            f"refused: {resolved.name} is {human_size(size)}, over the "
            f"{human_size(MAX_DOCUMENT_BYTES)} a chat attachment may be. Put it somewhere "
            "the operator can fetch it and send the link instead."
        )

    # BEFORE the content scan, deliberately. If the bytes are not the bytes that
    # were approved, judging the new ones on their merits answers the wrong
    # question — the file the agent asked to send no longer exists, and
    # "changed_since_queued" is what the operator needs to see in the log. It is
    # also the cheaper check, and it catches a rewrite that happens to be benign.
    if expect_digest and digest_of(resolved) != expect_digest:
        return (
            f"refused ({CHANGED_MARKER}): {resolved.name} is not the file that was "
            "approved — its contents changed after it was queued. Queue it again if the "
            "new version is the one you meant to send."
        )

    return _credential_refusal(resolved, size)

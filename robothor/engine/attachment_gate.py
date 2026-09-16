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
2. **Secret paths** — :mod:`robothor.engine.secret_paths`, the same rule
   ``read_file`` and ``exec`` enforce. Before touching the filesystem, so the
   refusal does not reveal whether the file exists.
3. **The inbox secret flag** — a credentials file the operator SENT is kept in a
   dedicated subdirectory precisely so this question has an answer that no
   filename parsing can get wrong (review I1, I6).
4. **Shape** — exists, is a file. This is the first rung that needs a ``stat``,
   which is why the two name rules come before it.
5. **Hard links** — a second name for a file outside the workspace is a name the
   resolver cannot see. ``ln ~/.ssh/id_rsa ~/robothor/notes.bin`` was a working
   exfiltration of the operator's private key (review I3).
6. **Size** — not empty, under the ceiling.
7. **The pin** — for a queued file, the digest it was approved with. Before the
   content scan: if the bytes are not the approved bytes, judging the new ones
   on their merits answers the wrong question.
8. **Credential shapes** — every file, whatever its name and whatever its size:
   the decode of the first 256 KB is the "is this text?" test, and it is better
   than a suffix list (review I2). There is no size rung on the scan — one was
   left behind when the suffix list went and it refused every binary over 4 MB
   (re-review R1).

A refusal is a sentence for the agent. It names the file and the reason and
**never the value**, because an error that helpfully echoes the credential
publishes it to the transcript, which is the exposure being refused.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "CHANGED_MARKER",
    "MAX_DOCUMENT_BYTES",
    "SCAN_HEAD_BYTES",
    "digest_of",
    "refuse_to_send",
    "resolve_for_send",
]

#: Telegram's ceiling for an outbound document. Checked before the read, so a
#: 2 GB file is refused rather than buffered into memory first.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

#: How many bytes of a file are decoded to decide whether it is text at all.
#: A real binary raises ``UnicodeDecodeError`` inside the first few hundred
#: bytes; a text file with a misleading suffix does not. There is no suffix list
#: any more — renaming ``tok.txt`` to ``tok.png`` was the whole attack.
SCAN_HEAD_BYTES = 256 * 1024

#: Key names that mean "the value beside me is a credential", for the structured
#: shapes a credentials FILE takes. ``scan_secret_literals`` already has a rule
#: like this for YAML mappings, but only for keys it recognises in a bundle
#: manifest; a Google OAuth ``client_secret`` is an opaque short string with no
#: literal shape at all, so the reviewer copied ``credentials.json`` out of the
#: inbox's ``secret/`` directory — which is where the flag lives — and sent it.
#:
#: Whole words only, and the value must be a non-empty string. That is what
#: keeps ``max_tokens``, ``token_path`` and ``password_changed_at`` out: this
#: file's own gate exists to be kept switched on, and a rule that refuses every
#: config scaffold is a rule somebody turns off.
_CREDENTIAL_KEYS = (
    "client_secret",
    "client-secret",
    "clientsecret",
    "private_key",
    "private-key",
    "privatekey",
    "api_key",
    "api-key",
    "apikey",
    "access_token",
    "access-token",
    "accesstoken",
    "refresh_token",
    "refresh-token",
    "refreshtoken",
    "secret_key",
    "secret-key",
    "secretkey",
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
)

#: One ``key: value`` / ``key = value`` assignment whose key means "credential".
#: The key must sit at the START of the line or right after a structural ``{``,
#: ``[`` or ``,`` — never merely after a word, because "Set the token: paste it
#: into the field" is a sentence in a README, not an assignment, and
#: documentation is exactly the kind of file an agent hands to the operator
#: (re-review R7). The value is captured so :func:`_is_placeholder` can judge it.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?:^|[{\[,])[\s\-]*
    ["']?(?:"""
    + "|".join(_CREDENTIAL_KEYS)
    + r""")["']?
    \s*[:=]\s*
    (?P<value>"[^"]*"|'[^']*'|\$\{[^}]*\}|[^,}\]]*)
    """
)

#: Trailing characters that belong to the surrounding structure, not the value.
#: Stripped one at a time rather than as a class, because ``${PASSWORD}`` ends
#: in a brace of its own — eating it turned the reference into ``${PASSWORD``
#: and the exclusion stopped matching.
_STRUCTURAL_TAIL = ",}] \t"

#: A comment line carries no configuration. ``# password: hunter2`` in an
#: example file documents a setting; it is not the setting.
#:
#: SQL's ``--`` is deliberately ABSENT. It was here for one draft, and it made
#: ``-----BEGIN RSA PRIVATE KEY-----`` look like a comment — a false NEGATIVE
#: that would have let a private key through, which is the opposite of the
#: mistake this whole exclusion list is guarding against. A SQL comment holding
#: a credential-shaped value keeps being refused, and that is the safe direction.
_COMMENT_LINE = re.compile(r"^\s*(?:#|//|;)")

#: Values that NAME a credential rather than being one. Every entry is a real
#: file an operator sends: a reference, a blank to fill in, a disabled setting,
#: a count, a redaction, or a YAML tag pointing at a vault.
_PLACEHOLDER_VALUE = re.compile(
    r"""(?ix)^(?:
        |\$\{[^}]*\}|\$[a-z_][a-z0-9_]*|%[a-z0-9_]+%|\{\{[^}]*\}\}
        |<[^>]*>|\[[^\]]*\]
        |null|nil|none|nan|~|true|false|yes|no|on|off
        |disabled|enabled|auto|default|required|optional|unset|todo|fixme|tbd
        |redacted|changeme|change[_-]me|placeholder|example|sample|dummy|secret
        |x{3,}|\.{2,}|\*+|-+|_+
        |your[_\- ][a-z0-9_\- ]*
        |[\d.]+
        |!.*
    )$"""
)

#: ``YOUR_API_KEY_HERE``, ``CHANGE_ME``, ``<REPLACE_ME>`` — a placeholder shouts.
#: Case-SENSITIVE and deliberately narrow: a lowercase value ending in "-here"
#: is an ordinary string, and treating it as a placeholder would excuse a real
#: secret. (It excused one of this module's own test fixtures, which is how the
#: breadth was noticed.)
_SHOUTED_PLACEHOLDER = re.compile(
    r"^(?:YOUR[_\- ][A-Z0-9_\- ]*|[A-Z0-9_\- ]*[_\- ]HERE|REPLACE[_\- ]?ME|CHANGE[_\- ]?ME)$"
)


def _is_placeholder(value: str) -> bool:
    """Is this value a NAME for a credential rather than one?

    The value arrives with whatever followed it on the line still attached — a
    closing brace, a comma, an inline comment — so this peels those off one at a
    time and asks again after each. One character at a time rather than as a
    character class, because ``${PASSWORD}`` ends in a brace of its own and
    stripping it wholesale turned the reference into ``${PASSWORD``.

    Quotes come off too: ``"${DB_PASSWORD}"`` in TOML is the same reference as a
    bare ``${DB_PASSWORD}`` in YAML, and the shared scanner sees the quoted form
    as a literal.
    """
    candidate = value.strip()
    # An inline comment is not part of the value. Only when the `#` is preceded
    # by whitespace — `abc#123` is a value that contains a hash.
    head = re.split(r"\s+#", candidate, maxsplit=1)[0]
    if head != candidate:
        candidate = head.strip()

    for _ in range(4):
        probe = candidate.strip().strip("\"'").strip()
        if _PLACEHOLDER_VALUE.fullmatch(probe) or _SHOUTED_PLACEHOLDER.fullmatch(probe):
            return True
        if candidate and candidate[-1] in _STRUCTURAL_TAIL:
            candidate = candidate[:-1]
        else:
            return False
    return False


def _line_is_excluded(line: str) -> bool:
    """Is this line a credential assignment that carries no credential?

    A comment, or an assignment whose value is a placeholder. Used BOTH to
    filter the shared scanner's findings and to gate this module's own rule,
    which is the correction re-review R7 asked for: the exclusions used to live
    only on the second rule, ``scan_secret_literals``'s own "credential-named
    field" rule fired first and had none of them, so none of them ever ran.
    """
    if _COMMENT_LINE.match(line):
        return True
    # EVERY assignment on the line, not the first: `{"password": "", "token": ""}`
    # is one line with two of them, and judging only the first left the file
    # refused. Excluded only when all of them are placeholders — one real
    # credential beside three blanks is still a credential.
    values = [match.group("value") for match in _CREDENTIAL_ASSIGNMENT.finditer(line)]
    if not values:
        # Not an assignment this module recognises, so it has no opinion and
        # the shared scanner's finding stands on its own merits.
        return False
    return all(_is_placeholder(value) for value in values)


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


def _credential_key_line(text: str) -> int | None:
    """The 1-based line where a credential-named key is set to a literal, or None.

    The line number only — never the key, never the value. A refusal that quotes
    what it found publishes it to the transcript, which is the exposure being
    refused.
    """
    for number, line in enumerate(text.splitlines(), start=1):
        if _line_is_excluded(line):
            continue
        if any(m.group("value").strip() for m in _CREDENTIAL_ASSIGNMENT.finditer(line)):
            return number
    return None


def _credential_refusal(path: Path) -> str | None:
    """Why this file's CONTENTS may not leave the box, or None.

    Every file is offered to the decoder, whatever it is called and **whatever
    it weighs**. The ``UnicodeDecodeError`` catch is the "is this actually
    text?" test and it is strictly better than a suffix list: ``tok.txt`` was
    refused and the same bytes as ``tok.png`` were sent (review I2).

    There is no size rung here. There used to be — ``size > MAX_SCAN_BYTES``
    refused anything too big to read in full — and it belonged to the text-only
    scanner the suffix list fed. With the list gone it applied to everything,
    and re-review R1 caught the result: a 12 MB PNG, a 6 MB PDF and a 20 MB MP4
    were all refused as "too large to check for credentials", which broke the
    brief's own acceptance case and the operator's actual request ("send me
    that as a PDF").

    Deleting it costs nothing, because the read was already bounded to
    :data:`SCAN_HEAD_BYTES`. Refusing at 4 MB never bought coverage either: a
    credential past 256 KB is unseen in a 3 MB file that passes, so the rung was
    enforcing a limit the scanner had already stopped honouring. The size rules
    that remain are the ones about what a chat can carry — the photo and
    document ceilings in :func:`refuse_to_send`.
    """
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

    # The shared scanner's findings, MINUS the lines this module knows carry no
    # credential. `templates/bundle.py` is not touched: its rules are right for
    # an export bundle, where a placeholder in a shipped manifest is still worth
    # querying. Here the file is one the operator asked for, and refusing every
    # scaffold, README and commented-out example is how a gate gets switched off
    # (re-review R7 — the exclusions existed and this filter is what makes them
    # reachable, because the scanner's own credential-named-field rule fires
    # first and has none of them).
    source_lines = text.splitlines()

    def _carries_a_credential(finding: Any) -> bool:
        index = int(getattr(finding, "line", 0)) - 1
        if not 0 <= index < len(source_lines):
            return True
        return not _line_is_excluded(source_lines[index])

    findings = [f for f in scan_secret_literals(text, path.name) if _carries_a_credential(f)]
    if not findings:
        # The export gate's scanner looks for credential-shaped VALUES. A
        # credentials file often has none — an OAuth `client_secret` is a short
        # opaque string — so the KEY is the evidence there.
        keyed = _credential_key_line(text)
        if keyed is None:
            return None
        return (
            f"refused: {path.name} line {keyed} sets a credential-named field to a literal "
            "value. Credentials must not leave the box in a file. Remove the value, use a "
            "${VAR} reference, or send a redacted copy."
        )
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

    # The workspace is already resolved here, so the predicate anchors on THIS
    # instance's inbox rather than on any path that happens to contain an
    # `inbox` component and a `secret` one (re-review R3).
    if is_inbox_secret(resolved, workspace=root_path):
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

        limit = human_size(MAX_DOCUMENT_BYTES)
        # Round-1 M7: a file one byte over renders at the same scale as the
        # limit, and "big.bin is 50 MB, over the 50 MB a chat attachment may be"
        # invites the operator to check the arithmetic rather than the file.
        # When the two render alike, say so in words instead of repeating a
        # number that appears to contradict itself.
        measured = human_size(size)
        over = f"just over the {limit}" if measured == limit else f"{measured}, over the {limit}"
        return (
            f"refused: {resolved.name} is {over} a chat attachment may be. Put it somewhere "
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

    return _credential_refusal(resolved)

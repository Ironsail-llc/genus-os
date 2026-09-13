"""Take credential-shaped text out of a line before anything prints it.

:mod:`robothor.secrets` states the rule this implements: *no value reaches a log
record*. That rule is easy to keep where the platform builds the message itself
and impossible to keep where it does not, and the two places it has already been
broken were both of the second kind:

* **An exception somebody else raised.** A Slack bot token that survived the
  vault with a trailing newline makes ``slack_sdk``'s header encoder raise
  ``ValueError: Invalid header value b'Bearer xoxb-…'``. The channel's error
  formatter passed a non-Slack exception's message through verbatim, so the
  token went to the journal.
* **argparse.** ``genus channel add slack --bot-tokn xoxb-…`` — one letter
  wrong — never reaches the code that refuses a token on a command line.
  argparse fails first and prints ``unrecognized arguments: --bot-tokn xoxb-…``
  to stderr, publishing the credential from the very command whose job is to
  keep it off a command line. ``genus vault set`` takes a secret as a
  *positional*, ``genus federation connect`` an invite token, ``genus init
  --telegram-token`` a bot token: all of them reach the same ``error()``.

So this is deliberately pattern-based rather than value-based. A value-based
redactor only works on credentials the process is already holding, and in both
cases above the process is holding nothing — the string arrived from outside.

**Shapes, not entropy.** Guessing at "this looks secret" would redact a UUID, a
git sha and half of every error message; the cost of a false positive here is an
operator who cannot read their own error. So the set is closed and each entry is
a credential shape this platform actually handles. Adding one is cheap; nothing
downstream needs to know.
"""

from __future__ import annotations

import re

__all__ = ["PLACEHOLDER", "redact", "redact_unrecognized_arguments"]

#: What a redacted run is replaced by. Visibly a redaction rather than a
#: mangled value, so an operator reading an error knows something was removed
#: and does not go hunting for a token that "changed".
PLACEHOLDER = "<redacted>"

#: Credential shapes this platform issues, accepts or forwards.
#:
#: ``xox[...]-``/``xapp-`` — every Slack token kind (bot, user, app-level,
#: refresh, configuration). ``Bearer <x>`` — the Authorization header, which is
#: how a token reaches an HTTP client's own exception text. ``<digits>:<secret>``
#: — a Telegram bot token, which ``genus init --telegram-token`` takes on a
#: command line. The digit run is anchored at 5+ (the shortest bot id Telegram
#: has issued) and the secret at 30+ so an ordinary ``12:34`` in prose, a
#: timestamp or a ``partial:2/3`` status is left alone.
#:
#: ``AUTH PLAIN|LOGIN|XOAUTH2 <blob>`` — an SMTP authentication line. An SMTP
#: password has no shape of its own (it is whatever the mail provider issued),
#: so there is nothing to match on it directly; the AUTH line that carries it
#: does have one, and that is the line ``smtplib`` quotes back inside
#: ``SMTPAuthenticationError`` when a server rejects the credential. The
#: base64 after ``AUTH PLAIN`` decodes straight to the password.
#:
#: ``AUTH LOGIN`` is the reason this entry spans lines. That mechanism sends
#: nothing on the AUTH line itself: the server prompts, and the client answers
#: with the username and then the password as two SEPARATE base64 lines. A
#: single-line pattern matched ``AUTH LOGIN`` and stopped, leaving the
#: credential on the line after it — the shape was advertised as covering
#: ``AUTH LOGIN`` and covered only its announcement. So the run continues
#: through any following base64-only lines, which is exactly what a captured
#: conversation looks like and is not a shape ordinary prose takes.
#:
#: What follows the keyword has to look like a BLOB, not like a word: the first
#: cut took ``\S+`` and ate the sentence "the AUTH LOGIN mechanism is not
#: offered", which is the kind of false positive that costs an operator their
#: own error message. A real credential blob is base64 — long, or short and
#: padded — and an announcement with nothing after it has nothing to hide.
_AUTH_KEYWORD = r"AUTH\s+(?:PLAIN|LOGIN|XOAUTH2)"
_AUTH_BLOB = r"(?:[A-Za-z0-9+/_-]{16,}={0,2}|[A-Za-z0-9+/_-]{4,}={1,2})"

_SHAPES = (
    r"xox[abceprs]-[\w-]+",
    r"xapp-[\w-]+",
    r"Bearer\s+\S+",
    r"\b\d{5,}:[A-Za-z0-9_-]{30,}",
    rf"{_AUTH_KEYWORD}(?:[ \t]+{_AUTH_BLOB})?(?:[ \t]*\r?\n{_AUTH_BLOB})+",
    rf"{_AUTH_KEYWORD}[ \t]+{_AUTH_BLOB}",
)

_CREDENTIAL_SHAPED = re.compile("|".join(_SHAPES), re.IGNORECASE)


def redact(text: str) -> str:
    """``text`` with every credential-shaped run replaced by :data:`PLACEHOLDER`.

    Never raises and never returns ``None``: every caller is on a path that is
    already reporting a failure, and a redactor that could fail there would be
    the second bug in one line.
    """
    if not text:
        return text
    try:
        return _CREDENTIAL_SHAPED.sub(PLACEHOLDER, text)
    except Exception:  # noqa: BLE001 - pragma: no cover - a regex that cannot fail
        # If this ever somehow raises, printing nothing beats printing a token.
        return PLACEHOLDER


#: argparse's own wording, and the only message whose tail is raw ``argv``.
_UNRECOGNIZED_RE = re.compile(r"(unrecognized arguments:\s*)(.+)\Z", re.IGNORECASE | re.DOTALL)


def _scrub_argument(token: str) -> str:
    """One ``argv`` token with any value in it replaced.

    A token that does not start with ``-`` is a value outright. One that does
    may still carry a value after ``=`` (``--smtp-passwrd=hunter2``), and the
    half before the ``=`` is the flag name the operator needs to see.
    """
    if not token.startswith("-"):
        return PLACEHOLDER
    name, sep, value = token.partition("=")
    return f"{name}={PLACEHOLDER}" if sep and value else token


def redact_unrecognized_arguments(message: str) -> str:
    """Take the VALUES out of argparse's "unrecognized arguments" message.

    :func:`redact` handles credentials with a shape. An SMTP password has none
    — it is whatever the mail provider issued — so ``genus channel add email
    --smtp-passwrd hunter2``, one letter wrong, printed it verbatim to stderr
    from the very command whose job is to keep a credential off a command line.
    ``--bot-tokn xoxb-…`` was only ever caught because Slack tokens have a
    prefix.

    So this scrubs by POSITION rather than by shape, and only inside the one
    message whose tail is raw ``argv``: every token there that is not a flag
    name is replaced. The flag names survive, because the typo is what the
    operator has to see, and the fix is to retype the flag — not the value,
    which was already published to ``ps`` and the shell history the moment the
    command ran. This closes the hole for every verb at once rather than per
    credential.

    Any other message is returned untouched: a redactor that ate an ordinary
    error would trade one unusable outcome for another.
    """
    if not message:
        return message
    try:
        match = _UNRECOGNIZED_RE.search(message)
        if match is None:
            return message
        scrubbed = " ".join(_scrub_argument(token) for token in match.group(2).split())
        return message[: match.start(2)] + scrubbed
    except Exception:  # noqa: BLE001 - pragma: no cover - printing nothing beats a token
        return PLACEHOLDER

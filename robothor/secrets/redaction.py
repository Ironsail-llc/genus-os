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

#: ``sk-``-prefixed API keys — OpenAI, OpenRouter and Anthropic all issue them,
#: and the fleet's whole LLM spend rides on one. 16+ trailing characters,
#: because the prefix alone is three letters that also begin ordinary words;
#: the word boundary in front is what keeps ``risk-weighted-average`` and
#: ``task-management-service`` out of the match.
_API_KEY = r"\bsk-[A-Za-z0-9_-]{16,}"

#: Words that mean "credential" on their own, wherever a name ends in one.
#: A password has no shape of its own — it is whatever the provider issued — so
#: for a whole class of secrets the name on the left of the ``=`` is the only
#: thing there is to match on. ``SECRET`` and ``PASSWORD`` are unambiguous in
#: English; a name ending in either is not describing anything else.
_UNAMBIGUOUS_WORD = r"(?:SECRET|SECRETS|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|CREDENTIALS)"

#: The compounds in which ``KEY`` is an ordinary English noun rather than a
#: credential. A DENY-list, and the direction matters — it was an allow-list of
#: "authority qualifiers" (``API``, ``AUTH``, ``SIGNING``, …) for exactly one
#: round, and that made ``GITHUB_TOKEN``, ``VAULT_TOKEN``, ``DEPLOY_KEY``,
#: ``SSH_KEY`` and a dozen more invisible: a real credential name usually has no
#: qualifier at all. Tuning against false positives alone produces that every
#: time, so the rule is now "a key is a credential unless it is one of these",
#: and the positive list in ``tests/test_secret_redaction.py`` is what keeps the
#: other direction honest.
#:
#: Every entry is a false positive observed in a real log line. ``public_key``
#: is the one that mattered most: a public key is not a secret, and a rule that
#: ate only its first token left half of it visible — redaction that neither
#: protects nor informs.
_KEY_IS_A_NOUN = (
    r"(?:SORT|PRIMARY|FOREIGN|PARTITION|SHARD|ROW|GROUP|CACHE|IDEMPOTENCY|PUBLIC|COMPOSITE|NATURAL)"
)

#: Trailing words that turn a credential name into a name ABOUT credentials.
#: ``ROBOTHOR_KEY_POOL_SIZE`` is a count, not a key; ``*_TOKEN_PATH`` is a
#: filename. Without this the rotation suffix below would swallow them.
_ABOUT_NOT_THE_THING = (
    r"(?:POOL|SIZE|COUNT|LEN|LENGTH|PREFIX|SUFFIX|PATH|FILE|DIR|NAME|ID|TYPE|ALG|ALGO"
    r"|FORMAT|ENABLED|DISABLED|MODE|TTL|COLUMN|FIELD|ORDER|SOURCE|BACKEND|PROVIDER)"
)

#: At most eight segments, at most forty characters each. NOT a taste: an
#: unbounded ``(?:[A-Za-z0-9]+[-_])*`` backtracks over every split of a long
#: hyphenated run, and ``redact("ab-" * 3000 + "=")`` took 0.54s. Bounded, the
#: work per start position is constant and the whole pass is linear — which it
#: has to be, because ``/api/logs`` runs it over up to 1000 journal lines per
#: request.
_NAME_PREFIX = r"(?:[A-Za-z0-9]{1,40}[-_]){0,8}"
_KEY_PREFIX = r"(?:[A-Za-z0-9]{1,40}[-_]){0,7}"

#: ``PGPASSWORD`` is libpq's own spelling and has no separator at all, so the
#: word may be glued to a short prefix. Bounded to keep the pass linear.
_GLUED = r"[A-Za-z]{0,12}"

#: A rotated or superseded slot: ``OPENROUTER_API_KEY_2``, ``GITHUB_TOKEN_OLD``.
#: The trailing segment used to defeat the word boundary and the whole name
#: missed. Guarded by :data:`_ABOUT_NOT_THE_THING` so ``…_KEY_POOL_SIZE`` does
#: not become a key by acquiring a suffix. The inner lookahead is
#: ``(?![A-Za-z0-9])`` rather than ``\b`` because ``\b`` does not fire between a
#: letter and an underscore — which is precisely the ``_POOL_SIZE`` case.
_ROTATION_SUFFIX = rf"(?:[-_](?!{_ABOUT_NOT_THE_THING}(?![A-Za-z0-9]))[A-Za-z0-9]{{1,8}})?"

#: ``TOKEN`` takes no qualifier and no deny-list. Round 0 produced eleven
#: ``*_KEY`` false positives and not one ``*_TOKEN`` — "token" in isolation is
#: not an English noun the way "key" is, and every ``X_TOKEN`` this platform or
#: its CI has ever set is a credential.
_TOKEN_NAME = rf"{_NAME_PREFIX}TOKEN"

#: ``KEY`` needs at least one leading segment, so a bare ``key=value`` is prose,
#: and that segment must not be one of the ordinary nouns above.
_KEY_NAME = rf"{_KEY_PREFIX}(?!{_KEY_IS_A_NOUN}[-_]KEY)[A-Za-z0-9]{{1,40}}[-_]KEY"

#: ``SECRET``/``PASSWORD`` stay unqualified, and may be glued (``PGPASSWORD``).
_UNAMBIGUOUS_NAME = rf"{_NAME_PREFIX}{_GLUED}{_UNAMBIGUOUS_WORD}"

#: Where a VALUE ends. Whitespace, a comma or a semicolon separate one
#: assignment from the next; the quote/bracket/angle characters are structural,
#: and eating them is how a redacted assignment inside a JSON string took the
#: closing quote and brace with it and left the audit CSV's ``details`` cell
#: unparseable. An export that mangles its own details is an integrity problem.
_VALUE_END = r"\s,;\"'}\]>)"

#: ``NAME=value``. The separator is ``=`` and only ``=``: ``TOKEN: expected`` in
#: prose is a sentence, and a ``:`` rule would eat the word after it.
#:
#: The name is captured so the replacement can keep it — "which variable" is
#: what the operator needs from the line, and "what it was set to" is what they
#: must not get — and the QUOTE is captured so a quoted value is replaced
#: quotes and all, leaving the surrounding JSON or shell line structurally
#: intact.
_ASSIGNMENT = re.compile(
    rf"(?P<name>\b(?:{_KEY_NAME}|{_TOKEN_NAME}|{_UNAMBIGUOUS_NAME}){_ROTATION_SUFFIX}\b)"
    r"\s*=\s*"
    rf"(?:(?P<quote>[\"'])(?P<quoted>[^\"'\n]{{1,4096}})(?P=quote)"
    rf"|(?P<value>[^{_VALUE_END}]{{1,4096}}))",
    re.IGNORECASE,
)


def _redact_assignment(match: re.Match[str]) -> str:
    name = match.group("name")
    quote = match.group("quote")
    if quote:
        return f"{name}={quote}{PLACEHOLDER}{quote}"
    return f"{name}={PLACEHOLDER}"


_SHAPES = (
    r"xox[abceprs]-[\w-]+",
    r"xapp-[\w-]+",
    r"Bearer\s+\S+",
    r"\b\d{5,}:[A-Za-z0-9_-]{30,}",
    _API_KEY,
    rf"{_AUTH_KEYWORD}(?:[ \t]+{_AUTH_BLOB})?(?:[ \t]*\r?\n{_AUTH_BLOB})+",
    rf"{_AUTH_KEYWORD}[ \t]+{_AUTH_BLOB}",
)

_CREDENTIAL_SHAPED = re.compile("|".join(_SHAPES), re.IGNORECASE)


def redact(text: str) -> str:
    """``text`` with every credential-shaped run replaced by :data:`PLACEHOLDER`.

    Two passes, in this order. The named-assignment pass runs FIRST so that
    ``OPENROUTER_API_KEY=sk-…`` keeps its variable name: the shape pass would
    otherwise reach the ``sk-`` value on its own and produce the same safety
    with less information. The shape pass then catches everything with a
    credential shape of its own, assignment or not.

    Never raises and never returns ``None``: every caller is on a path that is
    already reporting a failure, and a redactor that could fail there would be
    the second bug in one line.
    """
    if not text:
        return text
    try:
        named = _ASSIGNMENT.sub(_redact_assignment, text)
        return _CREDENTIAL_SHAPED.sub(PLACEHOLDER, named)
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

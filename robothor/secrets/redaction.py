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

__all__ = ["PLACEHOLDER", "redact"]

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
_SHAPES = (
    r"xox[abceprs]-[\w-]+",
    r"xapp-[\w-]+",
    r"Bearer\s+\S+",
    r"\b\d{5,}:[A-Za-z0-9_-]{30,}",
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

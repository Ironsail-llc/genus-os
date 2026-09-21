"""Intercept payment-shaped text before normal chat/model processing.

This is the CHAT-INTAKE boundary, and the only place the payment backstop
belongs. It used to run inside ``robothor.secrets.redaction.redact`` as well,
which meant a feature nobody had enabled rewrote every log line, audit field
and scrubbed page on every instance — and appended a ``[...]``-shaped advisory
sentence that any long number could get into the model's context. ``redact``
is a redaction primitive again; the product wording is here.
"""

import re

from robothor.secrets.redaction import SECURE_MARKER, cut_private_input

#: A plausible PAN: 12-19 digits, single space or dash between them.
#:
#: It ENDS ON A DIGIT. The first form, ``(?:\d[ -]?){12,19}``, let the final
#: repetition consume the separator after the last digit, so
#: ``card 4242424242424242 charged`` came back as ``…withheld]charged`` with
#: the surrounding words run together. ``{11,18}`` groups plus a trailing
#: ``\d`` is the same 12-19 digits with the separator left where it was.
_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){11,18}\d(?!\d)")
_CODE = re.compile(
    r"(?i)(\b(?:cvv|cvc|cid|security code|verification code)\s*[:=]\s*)[\"']?\d{3,4}[\"']?"
)


def _luhn(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 12 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    numbers = [int(char) * (2 if i % 2 else 1) for i, char in enumerate(reversed(digits))]
    return sum(number - 9 if number > 9 else number for number in numbers) % 10 == 0


def marker_truncated(text: str) -> bool:
    """Would :func:`protect_payment_text` discard this message's tail?

    ``SECURE_MARKER`` is ``(?im)^\\s*/secure…`` — any LINE start. ``intercept``
    only consumes a message that starts with ``/secure``, so an ordinary
    multi-line message whose second line happens to begin ``/secure the
    loading bay doors`` falls through to this backstop and loses everything
    from that line on. The cut is deliberate (a mid-message ``/secure card
    4242…`` must not reach the model) but it was also silent: the advisory
    sentence it leaves behind is addressed to the MODEL, and the person who
    typed the message was never told a tail had been removed.

    The caller uses this to say so. It is not itself a guard.
    """
    if not text:
        return False
    marker = SECURE_MARKER.search(text)
    if marker is None:
        return False
    return text[marker.start() :] != text.lstrip()


def protect_payment_text(text: str) -> str:
    """Keep incidental IDs readable; remove plausible PANs and named CVCs.

    This is a backstop for accidental pastes, not card enrollment. Secure
    enrollment goes directly to the typed vault endpoint and never passes
    through a model.

    Call it on text a PERSON just typed into chat. Do not call it on log
    lines, page content or anything else arriving from outside: the advisory
    sentence it appends is addressed to the operator, and Luhn alone cannot
    tell a card from an IMEI or a consignment number.
    """
    if not text:
        return text
    if SECURE_MARKER.search(text):
        return cut_private_input(
            text,
            "[Private input withheld from chat; use /account/autonomy for secure enrollment.]",
        )
    protected = _PAN.sub(
        lambda match: "[payment number withheld]" if _luhn(match[0]) else match[0], text
    )
    protected = _CODE.sub(r"\1[verification code withheld]", protected)
    if protected != text:
        protected += (
            "\n[Payment details were withheld from chat. Enroll securely at /account/autonomy.]"
        )
    return protected

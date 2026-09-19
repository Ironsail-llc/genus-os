"""Intercept payment-shaped text before normal chat/model processing."""

import re

_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")
_CODE = re.compile(
    r"(?i)(\b(?:cvv|cvc|cid|security code|verification code)\s*[:=]\s*)[\"']?\d{3,4}[\"']?"
)


def _luhn(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 12 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    numbers = [int(char) * (2 if i % 2 else 1) for i, char in enumerate(reversed(digits))]
    return sum(number - 9 if number > 9 else number for number in numbers) % 10 == 0


def protect_payment_text(text: str) -> str:
    """Keep incidental IDs readable; remove plausible PANs and named CVCs.

    This is a backstop for accidental pastes, not card enrollment. Secure enrollment
    goes directly to the typed vault endpoint and never passes through a model.
    """
    if not text:
        return text
    protected = _PAN.sub(
        lambda match: "[payment number withheld]" if _luhn(match[0]) else match[0], text
    )
    protected = _CODE.sub(r"\1[verification code withheld]", protected)
    if protected != text:
        protected += (
            "\n[Payment details were withheld from chat. Enroll securely at /account/autonomy.]"
        )
    return protected

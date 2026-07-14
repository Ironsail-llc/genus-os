"""Payment-safe audit serialization for the Entity Kernel.

The treasury boundary accepts opaque provider references, but those references
are still credentials and must not appear in logs or ledger payloads.  This
module deliberately produces a small JSON-compatible value set and errs on the
side of redaction for payment-like data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import JsonValue as AuditValue
from pydantic import SecretStr

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "account_number",
        "card_number",
        "cardnumber",
        "cryptogram",
        "cvc",
        "cvc2",
        "cvv",
        "cvv2",
        "pan",
        "password",
        "payment_method_token",
        "provider_authorization_token",
        "provider_payment_method_token",
        "provider_reference",
        "provider_token",
        "provider_virtual_card_token",
        "security_code",
        "secret",
    }
)
_PAYMENT_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)")


def _normalise_key(key: object) -> str:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")


def _contains_payment_digits(value: str) -> bool:
    """Return True when a string contains a plausible 12--19 digit PAN.

    We intentionally do not require a Luhn match.  Audit safety should not
    depend on a supplied card number being valid.
    """

    for candidate in _PAYMENT_DIGITS.findall(value):
        digit_count = len(re.sub(r"\D", "", candidate))
        if 12 <= digit_count <= 19:
            return True
    return False


def redact_for_audit(value: object, *, field_name: object | None = None) -> AuditValue:
    """Return a recursively redacted, JSON-compatible audit representation.

    Unknown object types are represented only by their type name.  Calling
    ``str`` or ``repr`` on an unknown provider object could itself disclose a
    credential.
    """

    if field_name is not None and _normalise_key(field_name) in _SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, SecretStr):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return REDACTED if _contains_payment_digits(value) else value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return redact_for_audit(value.value, field_name=field_name)
    if isinstance(value, Mapping):
        return {str(key): redact_for_audit(item, field_name=key) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_for_audit(item) for item in value]
    return f"<{type(value).__name__}>"


__all__ = ["AuditValue", "REDACTED", "redact_for_audit"]

"""The one place vault key names are spelled.

Four components touch a provider credential: the first-run wizard writes it,
the Settings page rewrites it, the engine's secrets reload reads it back, and
``export_secrets`` derives an environment variable name from it by
upper-casing and swapping ``/`` for ``_``. Nothing in that chain fails loudly
when two of them disagree by one character — the write lands under a key
nobody reads, and the operator is left with a provider the UI calls configured
and the engine calls missing. That is the failure this module exists to make
impossible: every name is built here, by a function, from validated parts.

Two rules, and they are the whole vocabulary:

    providers/<provider-id>/api_key[_<N>]   one credential, N >= 2 for spares
    channels/<channel>/<field>              one channel setting
"""

from __future__ import annotations

import re

#: A component may not be empty, carry whitespace, or contain the ``/`` the
#: key path itself uses as a separator. Anything else would either split the
#: path somewhere unintended or produce an ``export_env`` variable name that
#: no shell can express.
_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

PROVIDER_PREFIX = "providers"
CHANNEL_PREFIX = "channels"
API_KEY_FIELD = "api_key"


def validate_component(value: str, *, what: str) -> str:
    """Normalise one path component, or raise ``ValueError`` saying which.

    Normalisation is case folding, and it is not cosmetic: ``export_secrets``
    upper-cases the whole key, so ``providers/OpenRouter/api_key`` and
    ``providers/openrouter/api_key`` are two distinct vault rows that export
    to one environment variable — the second write silently shadowing the
    first depending on row order.
    """
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, got {type(value).__name__}")
    candidate = value.strip().lower()
    if not candidate:
        raise ValueError(f"{what} must not be empty")
    if not _COMPONENT.fullmatch(candidate):
        raise ValueError(
            f"{what} {value!r} is not a valid vault key component: use lower-case "
            "letters, digits, '_', '.' or '-' with no '/' and no whitespace"
        )
    return candidate


def provider_key(provider_id: str, position: int = 1) -> str:
    """The vault key holding one provider credential.

    Slot 1 is unsuffixed so the common single-key install reads naturally and
    matches the environment convention (``OPENROUTER_API_KEY``); spares are
    numbered from 2 for the same reason ``key_pool`` numbers env siblings
    from ``_2``.
    """
    provider = validate_component(provider_id, what="provider id")
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        raise ValueError(f"provider key position must be an integer >= 1, got {position!r}")
    field = API_KEY_FIELD if position == 1 else f"{API_KEY_FIELD}_{position}"
    return f"{PROVIDER_PREFIX}/{provider}/{field}"


def channel_field(channel: str, field: str) -> str:
    """The vault key holding one channel setting (a bot token, a signing secret)."""
    name = validate_component(channel, what="channel name")
    attribute = validate_component(field, what="channel field")
    return f"{CHANNEL_PREFIX}/{name}/{attribute}"


def env_name(vault_key: str) -> str:
    """The environment variable ``export_secrets`` derives from a vault key.

    Mirrors ``robothor.vault.dal.export_secrets`` so callers can look a key up
    in an exported mapping without re-deriving the transform (and drifting
    from it).
    """
    return vault_key.upper().replace("/", "_")


__all__ = [
    "API_KEY_FIELD",
    "CHANNEL_PREFIX",
    "PROVIDER_PREFIX",
    "channel_field",
    "env_name",
    "provider_key",
    "validate_component",
]

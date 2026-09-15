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

import contextlib
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


#: ``<PROVIDER>_API_KEY`` and its numbered spares, which is the one environment
#: name shape that already has a richer vault spelling: ``providers/<id>/api_key``
#: is what the first-run wizard, the Helm provider page and ``key_pool`` all
#: write and read. Matched mechanically rather than against a list of provider
#: ids, so a provider nobody has heard of gets the same treatment.
_PROVIDER_ENV = re.compile(
    r"^(?P<provider>[A-Z0-9][A-Z0-9_]*?)_API_KEY(?:_(?P<slot>[2-9]|1[0-6]))?$"
)


def vault_keys_for_env_name(name: str) -> tuple[str, ...]:
    """Every vault key that may hold the value of environment variable ``name``.

    Canonical first. This is the INVERSE of :func:`env_name`, and it has to be a
    tuple because that transform is not injective and because one shape already
    has two spellings in the wild: ``OPENROUTER_API_KEY`` is written as
    ``providers/openrouter/api_key`` by the wizard, the Helm provider page and
    ``key_pool``, and would be written as ``openrouter_api_key`` by anything
    that only knew how to invert ``env_name``.

    That mismatch is not hypothetical: before this function,
    ``resolve_secret("OPENROUTER_API_KEY")`` looked the name up in
    ``export_env()`` and therefore could not see the row the wizard had
    written, so a vault-only instance read as having no key at all. One
    function, used by the accessor to search and by ``genus secrets migrate``
    to choose where to write, is what keeps the two directions from drifting --
    which is the whole argument of this module.
    """
    candidates: list[str] = []
    match = _PROVIDER_ENV.match(name.strip().upper())
    if match:
        # A provider component the key path cannot carry is not an error here:
        # the literal spelling below still works, so the caller still gets a
        # usable candidate.
        with contextlib.suppress(ValueError):
            candidates.append(provider_key(match.group("provider"), int(match.group("slot") or 1)))
    literal = name.strip().lower().replace("__", "_")
    if literal and literal not in candidates:
        candidates.append(literal)
    return tuple(candidates)


__all__ = [
    "API_KEY_FIELD",
    "CHANNEL_PREFIX",
    "PROVIDER_PREFIX",
    "channel_field",
    "env_name",
    "provider_key",
    "validate_component",
    "vault_keys_for_env_name",
]

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


def normalise_key(key: str) -> str:
    """One spelling per row: stripped and lower-cased, path separators kept.

    ``export_secrets`` upper-cases the whole key, so ``Providers/GitHub/api_key``
    and ``providers/github/api_key`` are two rows exporting to ONE variable, and
    which one a reader gets depends on row order. Whitespace is the same defect
    with a worse consequence: ``'ROBOTHOR_DB_PASSWORD '`` slipped past the
    bootstrap refusal (which compared the padded string) and was stored under a
    key nothing could find.

    Applied at ``vault.get``/``set``/``delete``, so the normalisation cannot be
    skipped by a caller that did not know about it.
    """
    return "/".join(part.strip().lower() for part in str(key).strip().split("/"))


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


# ── the two-way mapping ──────────────────────────────────────────────────────
#
# ``env_name()`` upper-cases and swaps ``/`` for ``_``, so it has no inverse. It
# does not need one: it needs a SEARCH, in both directions, and both directions
# have to be derived from the same rules or they drift — which is the whole
# argument of this module and, on 2026-09-15, a real outage twice over.
#
# The failure the second direction exists for: an assistant stores a GitHub
# token at ``providers/github/api_key`` (the spelling the wizard, the Helm
# provider page and the vault handler's own docstring all use). ``vault_get``
# says configured. ``vault_test`` dials it and says ok. And every READER keeps
# serving the dead environment value, because nothing mapped ``GITHUB_TOKEN``
# to that row. Three tools attesting a rotation that did not happen is worse
# than the bug they were added to fix, so ``vault_set`` now asks
# :func:`env_names_for_vault_key` which readers its key will reach and says so.

#: ``<PROVIDER>_API_KEY[_n]`` — the slot convention ``key_pool``, the first-run
#: wizard and the Helm provider page share.
_PROVIDER_API_KEY_ENV = re.compile(
    r"^(?P<provider>[A-Z0-9][A-Z0-9_]*?)_API_KEY(?:_(?P<slot>[2-9]|1[0-6]))?$"
)

#: ``<VENDOR>_TOKEN`` and ``<VENDOR>_API_TOKEN``. The shape the incident was
#: about: nothing calls a GitHub PAT an "API key", so nothing mapped
#: ``GITHUB_TOKEN`` to a provider row and the token was stored where no reader
#: looked. A provider row is the right home for it — it IS a per-vendor
#: credential — so the same rule applies.
_PROVIDER_TOKEN_ENV = re.compile(r"^(?P<provider>[A-Z0-9][A-Z0-9_]*?)(?:_API)?_TOKEN$")

#: ``ROBOTHOR_<CHANNEL>_<FIELD>`` — the channel convention. Bounded to the
#: channels the platform ships rather than matched mechanically, because
#: ``ROBOTHOR_INTENT_HMAC_SECRET`` would otherwise become
#: ``channels/intent/hmac_secret``: the prefix alone cannot tell a channel from
#: any other ``ROBOTHOR_*`` setting, and inventing a channel is how a row lands
#: somewhere nothing reads.
CHANNELS: tuple[str, ...] = ("telegram", "slack", "email", "sms", "twilio", "whatsapp", "webchat")

#: Vendors the platform (and the vendor) spell more than one way. Two entries,
#: both published by the vendor itself: GitHub documents ``GH_TOKEN`` and
#: ``GITHUB_TOKEN`` as interchangeable, and ``gh`` reads both. An alias table is
#: a list beside the thing it describes, so it stays this small and every entry
#: has to name a vendor that publishes both spellings.
_VENDOR_ALIASES: dict[str, str] = {"gh": "github"}


def _vendor(raw: str) -> str:
    return _VENDOR_ALIASES.get(raw.lower(), raw.lower())


def vault_keys_for_env_name(name: str) -> tuple[str, ...]:
    """Every vault key that may hold the value of environment variable ``name``.

    Canonical first — ``candidates[0]`` is where ``genus secrets migrate`` and
    ``vault_set`` write, so the order decides where a credential lands, and the
    richer spelling wins because that is the one ``key_pool`` and the Helm
    provider page already read.

    The literal lower-cased name is ALWAYS a candidate, last. It is what an
    earlier release wrote and what ``env_name`` inverts to, so rows written
    before this function existed stay readable.
    """
    cleaned = name.strip()
    if not cleaned:
        return ()
    upper = cleaned.upper()
    candidates: list[str] = []

    def _add(key: str) -> None:
        if key not in candidates:
            candidates.append(key)

    api_key = _PROVIDER_API_KEY_ENV.match(upper)
    if api_key:
        # A component the key path cannot carry is not an error: the literal
        # spelling below still works, so the caller still gets a usable
        # candidate.
        with contextlib.suppress(ValueError):
            _add(provider_key(_vendor(api_key.group("provider")), int(api_key.group("slot") or 1)))

    token = _PROVIDER_TOKEN_ENV.match(upper)
    if token:
        with contextlib.suppress(ValueError):
            _add(provider_key(_vendor(token.group("provider"))))

    for channel in CHANNELS:
        prefix = f"ROBOTHOR_{channel.upper()}_"
        if upper.startswith(prefix):
            with contextlib.suppress(ValueError):
                _add(channel_field(channel, upper[len(prefix) :]))
            break

    _add(cleaned.lower())
    return tuple(candidates)


def env_names_for_vault_key(key: str) -> tuple[str, ...]:
    """Every environment variable whose reader would find the row at ``key``.

    The dual of :func:`vault_keys_for_env_name`, and derived from it rather
    than from a second reading of the rules: a key is claimed by exactly the
    names that list it as a candidate. Two names can claim one row
    (``GH_TOKEN`` and ``GITHUB_TOKEN`` both resolve to
    ``providers/github/api_key``), which is correct — they are the same
    credential.

    ``()`` means no reader in the platform will ever look at this row. That is
    a legitimate thing to store (the vault is a general store) and a thing
    ``vault_set`` must SAY, because a credential written where nothing reads it
    is the incident with the tools reporting success.
    """
    cleaned = str(key).strip().lower()
    if not cleaned:
        return ()

    found: list[str] = []

    def _claim(candidate_env: str) -> None:
        if cleaned in vault_keys_for_env_name(candidate_env) and candidate_env not in found:
            found.append(candidate_env)

    # The literal inverse of env_name, always.
    _claim(env_name(cleaned))

    parts = cleaned.split("/")
    if len(parts) == 3 and parts[0] == PROVIDER_PREFIX:
        vendor, field = parts[1], parts[2]
        slot = field[len(API_KEY_FIELD) + 1 :] if field.startswith(f"{API_KEY_FIELD}_") else ""
        suffix = f"_{slot}" if slot else ""
        spellings = [f"{vendor.upper()}_API_KEY{suffix}"]
        if not slot:
            spellings += [f"{vendor.upper()}_TOKEN", f"{vendor.upper()}_API_TOKEN"]
            spellings += [
                f"{alias.upper()}_TOKEN"
                for alias, real in _VENDOR_ALIASES.items()
                if real == vendor
            ]
        for spelling in spellings:
            _claim(spelling)
    elif len(parts) == 3 and parts[0] == CHANNEL_PREFIX:
        _claim(f"ROBOTHOR_{parts[1].upper()}_{parts[2].upper()}")

    return tuple(found)


__all__ = [
    "API_KEY_FIELD",
    "CHANNELS",
    "CHANNEL_PREFIX",
    "PROVIDER_PREFIX",
    "channel_field",
    "env_name",
    "env_names_for_vault_key",
    "normalise_key",
    "provider_key",
    "validate_component",
    "vault_keys_for_env_name",
]

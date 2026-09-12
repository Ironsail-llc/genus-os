"""The one place the platform asks "what is this credential, and where from?".

Before this package, a secret was read four ways — the process environment, the
AES vault, SOPS, a ``.env`` file — with each reader implementing its own
subset: ``auth/tokens.py`` did env then vault, ``auth/mfa_secrets.py`` inherited
whatever that resolved, ``engine/key_pool.py`` did vault then env slot by slot,
and the rest was bare ``os.environ``. So "where does this instance keep that
value?" had no single answer, and neither did the question an operator actually
asks: why does the bridge think it is unset?

The chain, in order:

1. **the process environment** — which on systemd is
   ``/run/robothor/secrets.env`` loaded by ``EnvironmentFile=`` (written by
   whichever backend ``scripts/load-secrets.sh`` dispatched to), and in a
   container is simply the container environment. Either way the platform reads
   it the same way, which is why there is no "sops" source here: SOPS is how
   the file got there, not a place this code looks.
2. **the AES vault** (``robothor.vault``) — where the first-run wizard and the
   Settings page write credentials.
3. **nothing**, reported as ``"missing"`` rather than guessed at.

Two rules hold this module together.

**A vault that cannot be read is "unset", never an exception.** Most instances
have no vault: no master key file, no database, or both. ``key_pool`` learned
this the expensive way — raising here turns every optional credential into a
startup crash. The failure is logged once per cooldown and the lookup returns
``None``.

**No value reaches a log record.** Names and sources are logged; values are
not, and neither is the text of an exception that may carry a connection
string. A journal outlives the tmpfs file the credential came from.

There is deliberately no cache of the vault's contents. ``vault.export_env()``
returns EVERY secret the instance owns, so keeping it would hold every channel
token and SMTP password in memory for the sake of one lookup —
``engine/key_pool.py`` filters its snapshot down to provider slots for exactly
that reason, and a general accessor has no such filter to apply. Callers that
resolve a credential on a hot path cache the resolved value themselves
(``tokens.signing_key()`` does).
"""

from __future__ import annotations

import logging
import time
from typing import Literal, NamedTuple

from robothor.constants import DEFAULT_TENANT
from robothor.settings.env import process_env_get

logger = logging.getLogger(__name__)

__all__ = [
    "VAULT_RETRY_SECONDS",
    "ResolvedSecret",
    "SecretSource",
    "get_secret",
    "reset_vault_availability",
    "resolve_secret",
    "secret_source",
]

SecretSource = Literal["env", "vault", "missing"]

#: How long an unreadable vault sits out. A per-call retry would put a
#: synchronous psycopg2 connect on whatever path asked for the credential; five
#: minutes is short enough that a vault which comes back is picked up without a
#: restart and long enough that a box with no vault is not paying for one.
VAULT_RETRY_SECONDS = 300.0

#: Seam for the suite: monkeypatched to move time past the cooldown.
_clock = time.monotonic

#: Monotonic deadline before which the vault will not be probed again, or None
#: when it is believed usable.
_vault_retry_after: float | None = None


class ResolvedSecret(NamedTuple):
    """A credential and where it came from.

    Returned as a pair because asking ``get_secret`` and then ``secret_source``
    is two vault round trips for one question — and, on a vault that failed
    between them, two different answers.
    """

    value: str | None
    source: SecretSource


def reset_vault_availability() -> None:
    """Re-arm the vault probe. For tests, and after a deliberate reload."""
    global _vault_retry_after  # noqa: PLW0603
    _vault_retry_after = None


def _clean(value: str | None) -> str | None:
    """Normalise a raw lookup: whitespace-only and empty both mean unset.

    ``KEY=`` left behind in ``robothor.env`` after an operator commented a value
    out has not configured anything, and a caller handed ``""`` would sign
    tokens with the empty string instead of generating a key.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _vault_read(name: str, vault_key: str | None, tenant_id: str) -> str | None:
    """The vault's answer, or None — including when the vault is unusable."""
    global _vault_retry_after  # noqa: PLW0603

    if _vault_retry_after is not None and _clock() < _vault_retry_after:
        return None

    try:
        # Lazy: importing the vault pulls in the crypto and DAL layers, and a
        # process whose credentials all come from the environment must not pay
        # for them (nor fail when there is no master key to import against).
        from robothor import vault

        if vault_key is not None:
            found = vault.get(vault_key, tenant_id=tenant_id)
        else:
            # ``vault.naming.env_name()`` is the one env<->vault mapping, and it
            # upper-cases and replaces "/" with "_", so it has no inverse: both
            # ``providers/x/api_key`` and ``providers/x_api/key`` export to the
            # same name. Looking the ENVIRONMENT name up in the vault's own
            # export therefore uses that mapping rather than inventing a second
            # one to drift from it — the same thing ``key_pool._vault_lookup``
            # does.
            found = vault.export_env(tenant_id=tenant_id).get(name)
    except Exception as exc:  # noqa: BLE001 - the vault is optional, by design
        _vault_retry_after = _clock() + VAULT_RETRY_SECONDS
        # The exception TYPE, never its text: a psycopg2 error carries the
        # connection string, and a connection string carries a password.
        logger.info(
            "secrets: vault unreadable while resolving %s (%s); treating it as unset "
            "and not retrying for %.0fs",
            name,
            type(exc).__name__,
            VAULT_RETRY_SECONDS,
        )
        return None

    _vault_retry_after = None
    return _clean(found)


def resolve_secret(
    name: str,
    *,
    vault_key: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> ResolvedSecret:
    """Resolve one credential and report which layer answered.

    Args:
        name: the environment variable name, e.g. ``GENUS_AUTH_SIGNING_KEY``.
        vault_key: the vault row to read instead of deriving one from ``name``.
            Pass it when the row predates the environment convention:
            ``auth/tokens.py`` keeps its signing key at
            ``auth/jwt_signing_key``, which exports as
            ``AUTH_JWT_SIGNING_KEY`` and would otherwise be invisible to a
            lookup for ``GENUS_AUTH_SIGNING_KEY`` — so the accessor would
            generate a second signing key on a box that already had one and
            invalidate every session and every MFA secret derived from it.
        tenant_id: whose vault to read.
    """
    from_env = _clean(process_env_get(name, None))
    if from_env is not None:
        logger.debug("secrets: %s resolved from the process environment", name)
        return ResolvedSecret(from_env, "env")

    from_vault = _vault_read(name, vault_key, tenant_id)
    if from_vault is not None:
        logger.debug("secrets: %s resolved from the vault", name)
        return ResolvedSecret(from_vault, "vault")

    logger.debug("secrets: %s is not configured in the environment or the vault", name)
    return ResolvedSecret(None, "missing")


def get_secret(
    name: str,
    *,
    vault_key: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """The credential ``name`` holds, or None if this instance has none."""
    return resolve_secret(name, vault_key=vault_key, tenant_id=tenant_id).value


def secret_source(
    name: str,
    *,
    vault_key: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> SecretSource:
    """Where ``name`` would be resolved from: ``env``, ``vault`` or ``missing``.

    Safe to print: it is the answer to "is this configured, and where?" with
    the value left out, which is what the doctor and ``genus config explain``
    need.
    """
    return resolve_secret(name, vault_key=vault_key, tenant_id=tenant_id).source

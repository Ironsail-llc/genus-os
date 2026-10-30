"""The one place the platform asks "what is this credential, and where from?".

Before this package, a secret was read four ways — the process environment, the
AES vault, SOPS, a ``.env`` file — with each reader implementing its own
subset: ``auth/tokens.py`` did env then vault, ``auth/mfa_secrets.py`` inherited
whatever that resolved, ``engine/key_pool.py`` did vault then env slot by slot,
and the rest was bare ``os.environ``. So "where does this instance keep that
value?" had no single answer, and neither did the question an operator actually
asks: why does the bridge think it is unset?

There is no longer ONE chain. Which store is asked first depends on what the
credential is, and :mod:`robothor.secrets.classification` is where that is
decided:

* **bootstrap** credentials -- the ones that bring the instance up, including
  the database password the vault's own rows live behind -- are resolved
  environment first, vault second.
* **application** credentials -- every third-party token an assistant is
  handed, uses, proves and rotates -- are resolved VAULT first, environment
  second. On 2026-09-15 an expired ``GH_TOKEN`` in the process environment
  shadowed a fresh vault row the assistant had just written, and nothing short
  of root editing the SOPS file and restarting the unit could clear it. A dead
  environment value must never silently shadow a live vault row.

The two stores, in either order:

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

**A vault that cannot be read is "unavailable", never an exception — and never
"missing".** Most instances have no vault: no master key file, no database, or
both. ``key_pool`` learned this the expensive way — raising here turns every
optional credential into a startup crash. So ``get_secret`` returns ``None``
either way and the failure is logged once per cooldown. But the SOURCE keeps
the two apart: ``"missing"`` means the vault answered and holds no row,
``"unavailable"`` means nobody knows. The difference is load-bearing for one
caller: ``tokens.signing_key()`` generates-and-stores a key when nothing holds
one, and the store is an UPSERT. A vault whose read fails while its write works
would otherwise overwrite the live signing key, invalidating every session and
making every stored MFA secret undecryptable. A caller that must not act on a
stale verdict passes ``live=True`` to skip the cooldown and probe the vault now.

**No value reaches a log record.** Names and sources are logged; values are
not, and neither is the text of an exception that may carry a connection
string. A journal outlives the tmpfs file the credential came from.

**What is cached, and what is not.** Rows the vault answered for, by key,
invalidated on every write — see :data:`_vault_cache`. Nothing calls
``export_env()`` here: it returns EVERY secret the instance owns, so using it to
answer a question about one is a full decrypt per lookup and holds every channel
token and SMTP password in memory for the sake of it. That matters because the
callers are hot — ``github_api._get_token`` per request, ``build_exec_env`` once
per grant per ``exec`` — and because before the vault went first, the
environment short-circuited and none of this ran at all. The environment half is
NOT cached: it is a dict lookup, and caching it would make a test that sets a
variable not take effect.
"""

from __future__ import annotations

import time
from typing import Literal, NamedTuple

from robothor.constants import DEFAULT_TENANT
from robothor.secrets.classification import is_bootstrap
from robothor.secrets.trace import SecretLabel
from robothor.settings.env import process_env_get

# No logger here on purpose. Every line this module used to write now goes
# through `robothor.secrets.trace.SecretLabel`, which holds a name and nothing
# else — so "no value reaches a log record" is a property of what that module
# can see, rather than of every future edit to this one remembering it.

__all__ = [
    "SECRET_CACHE_MISS_TTL_SECONDS",
    "SECRET_CACHE_TTL_SECONDS",
    "VAULT_RETRY_SECONDS",
    "ResolvedSecret",
    "SecretSource",
    "get_secret",
    "is_bootstrap",
    "reset_secret_cache",
    "reset_vault_availability",
    "resolve_secret",
    "secret_source",
]

SecretSource = Literal["env", "vault", "missing", "unavailable"]

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


#: What the vault answered for a key, so a hot caller does not ask twice.
#:
#: Keyed by ``(tenant, vault_key or name)`` and holding ``(value, available)``
#: — the MISS is cached too, because ``build_exec_env`` resolves an unset grant
#: on every ``exec`` and ``_remote_enabled`` asks about a provider that may not
#: be configured, so remembering only hits would leave the common case paying
#: full price.
#:
#: Only the vault half. The environment is a dict lookup and caching it would
#: make a test that sets a variable not take effect — a debugging afternoon for
#: whoever hits it, bought for nothing.
#:
#: Two ways out, and both are needed. :func:`reset_secret_cache` is the explicit
#: one, which ``vault_set`` and the bridge's ``POST /api/admin/secrets/reload``
#: both reach through ``key_pool.reload_provider_keys`` — for writers that can
#: reach this process. And a short TTL, for the writers that cannot: the CLI and
#: the setup wizard are separate processes, and a cache with no TTL made every
#: one of their writes invisible to a running engine until a restart.
_vault_cache: dict[tuple[str, str], tuple[str | None, bool, float]] = {}

#: How long a cached HIT is served. Short, because this process is not the only
#: writer: ``genus vault set``, ``genus secrets migrate``, ``genus channel add``
#: and the setup wizard are all separate processes, and on ``main`` a CLI write
#: took effect on the engine's next read. A performance fix that costs that is
#: a regression. Ten seconds is long enough to collapse a burst of grants on one
#: ``exec`` and short enough that nobody debugs it.
SECRET_CACHE_TTL_SECONDS = 10.0

#: How long a cached MISS is served — shorter, deliberately. A stale hit is a
#: correct answer briefly out of date; a stale miss is a FEATURE THAT STAYS
#: DEAD. The operator adds ``BRAVE_API_KEY`` from the CLI, web search keeps
#: failing, and "slow" is indistinguishable from "broken".
SECRET_CACHE_MISS_TTL_SECONDS = 3.0


def reset_secret_cache() -> None:
    """Forget what the vault said. Called on every write, and by the suite."""
    _vault_cache.clear()


def reset_vault_availability() -> None:
    """Re-arm the vault probe. For tests, and after a deliberate reload."""
    global _vault_retry_after  # noqa: PLW0603
    _vault_retry_after = None
    reset_secret_cache()


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


def _vault_read(
    name: str, vault_key: str | None, tenant_id: str, *, live: bool = False
) -> tuple[str | None, bool]:
    """``(value, available)``: the vault's answer and whether it answered at all.

    ``(None, False)`` is an unusable vault (or one inside its cooldown when
    ``live`` is False); ``(None, True)`` is a vault that was read and holds no
    such row. Callers that generate on absence must only do so on the second.
    """
    global _vault_retry_after  # noqa: PLW0603

    if not live and _vault_retry_after is not None and _clock() < _vault_retry_after:
        return None, False

    cache_key = (tenant_id, vault_key or name)
    if not live:
        cached = _vault_cache.get(cache_key)
        if cached is not None:
            value, available, stored_at = cached
            ttl = SECRET_CACHE_TTL_SECONDS if value is not None else SECRET_CACHE_MISS_TTL_SECONDS
            if _clock() - stored_at < ttl:
                return value, available
            del _vault_cache[cache_key]

    try:
        # Lazy: importing the vault pulls in the crypto and DAL layers, and a
        # process whose credentials all come from the environment must not pay
        # for them (nor fail when there is no master key to import against).
        from robothor import vault

        if vault_key is not None:
            found = vault.get(vault_key, tenant_id=tenant_id)
        else:
            # One ROW at a time, by key. Never ``export_env()``.
            #
            # ``export_env`` decrypts every secret the instance owns, and this
            # is a question about one. The callers are hot —
            # ``github_api._get_token`` per request, ``build_exec_env`` once per
            # grant per ``exec`` — so an export here is a psycopg2 connection
            # and a full decrypt on each, and a much larger window in which
            # every credential is in memory at once. Before the vault went
            # first, the environment short-circuited and none of this ran.
            #
            # ``vault_keys_for_env_name`` is the one place the env-name→key
            # mapping is spelled, and it ends with the literal lower-cased name
            # — which is exactly what ``export_env`` would have matched — so
            # searching the candidates covers everything the export did without
            # decrypting the rest.
            from robothor.vault.naming import vault_keys_for_env_name

            found = None
            for candidate in vault_keys_for_env_name(name):
                found = vault.get(candidate, tenant_id=tenant_id)
                if found is not None:
                    break
    except Exception as exc:  # noqa: BLE001 - the vault is optional, by design
        _vault_retry_after = _clock() + VAULT_RETRY_SECONDS
        # The exception TYPE, never its text: a psycopg2 error carries the
        # connection string, and a connection string carries a password.
        SecretLabel(name).vault_unreadable(
            error_class=type(exc).__name__, retry_seconds=VAULT_RETRY_SECONDS
        )
        return None, False

    _vault_retry_after = None
    answer = (_clean(found), True)
    _vault_cache[cache_key] = (*answer, _clock())
    return answer


def _absent_or(label: SecretLabel, from_vault: str | None, available: bool) -> ResolvedSecret:
    """The tail of both chains: a vault row, or why there is no value.

    ``missing`` and ``unavailable`` stay apart here for the reason the module
    docstring gives — ``tokens.signing_key()`` GENERATES a key on ``missing``
    and its store is an upsert, so acting on a stale "nobody knows" would
    overwrite the live signing key.
    """
    if from_vault is not None:
        label.resolved_from_vault()
        return ResolvedSecret(from_vault, "vault")
    if not available:
        label.unavailable()
        return ResolvedSecret(None, "unavailable")
    label.missing()
    return ResolvedSecret(None, "missing")


def resolve_secret(
    name: str,
    *,
    vault_key: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
    live: bool = False,
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
        live: probe the vault even inside its failure cooldown. For a caller
            that will act on "missing" (generate a key, for instance) and must
            not act on a five-minute-old verdict about a vault that may have
            recovered.
    """
    # Built from the NAME, before anything is fetched. Every log line below
    # goes through it, so the only expression that reaches a logger is a field
    # of an object that has never seen a value — see robothor/secrets/trace.py.
    label = SecretLabel(name)

    from_env = _clean(process_env_get(name, None))

    if is_bootstrap(name):
        # Environment first, and the vault only as a fallback. These are the
        # credentials the instance needs in order to HAVE a vault (the rows
        # live in the database ``ROBOTHOR_DB_PASSWORD`` opens) or that
        # rotating would lock everybody out of a running instance.
        if from_env is not None:
            label.resolved_from_env(bootstrap=True)
            return ResolvedSecret(from_env, "env")
        from_vault, available = _vault_read(name, vault_key, tenant_id, live=live)
        return _absent_or(label, from_vault, available)

    # Application credential: the vault is the store the operator and the
    # assistant manage, so a row there beats whatever the box booted with.
    from_vault, available = _vault_read(name, vault_key, tenant_id, live=live)
    if from_vault is not None:
        label.resolved_from_vault(ahead_of_env=True)
        return ResolvedSecret(from_vault, "vault")

    if from_env is not None:
        # Either the vault holds no such row, or it could not be read at all.
        # Both fall through to the environment: failing closed on an
        # unreadable vault would take every channel, provider and integration
        # down with it, for credentials a root-owned file is still holding
        # good copies of.
        label.fell_through_to_env(vault_answered=available)
        return ResolvedSecret(from_env, "env")

    return _absent_or(label, None, available)


def get_secret(
    name: str,
    *,
    vault_key: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
    live: bool = False,
) -> str | None:
    """The credential ``name`` holds, or None if this instance has none.

    None also when the vault is unavailable; use :func:`resolve_secret` when
    that distinction changes what you do next.
    """
    return resolve_secret(name, vault_key=vault_key, tenant_id=tenant_id, live=live).value


def secret_source(
    name: str,
    *,
    vault_key: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
    live: bool = False,
) -> SecretSource:
    """Where ``name`` would be resolved from: ``env``, ``vault``, ``missing`` or
    ``unavailable`` (the vault could not be read, so nobody knows).

    Safe to print: it is the answer to "is this configured, and where?" with
    the value left out, which is what the doctor and ``genus config explain``
    need.
    """
    return resolve_secret(name, vault_key=vault_key, tenant_id=tenant_id, live=live).source

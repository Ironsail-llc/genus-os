"""Which store owns a credential — bootstrap, or the vault the assistant manages.

The 2026-09-15 incident: the operator handed the assistant a GitHub token and
expected it to be kept, used and rotated by the assistant. The assistant could
write the vault, but the accessor read the process environment first, so the
expired ``GH_TOKEN`` the box had booted with shadowed the fresh vault row until
somebody with root edited the SOPS file and restarted the unit. The assistant
can do neither, and must never need to.

The fix is not "vault always wins" — some credentials cannot come from the
vault, because the vault needs them to exist. The vault's rows live in the
Postgres that ``ROBOTHOR_DB_PASSWORD`` opens; a vault-first lookup for that
password asks the vault for the key to the vault. So there are two classes:

**Bootstrap** — environment first. The credentials that bring the instance up:
database and test DSNs, the Redis password, the session signing keys (rotating
one signs every session out and makes every stored MFA secret undecryptable),
the substrate transport, and the variables that say where the vault's own
master key and the SOPS age key live. An operator locked out of their box by a
vault row is a worse failure than a stale token.

**Application** — vault first, environment second. Every other credential: the
third-party tokens an assistant is handed, uses, proves and rotates. A vault
row that EXISTS beats the environment. A vault row that does not exist falls
through to the environment, and an unreadable vault falls through too — losing
every channel and provider to a vault outage, for credentials the environment
still holds good copies of, would be a self-inflicted one.

Nothing here is a list of names. The bootstrap class is a marker on the
settings declaration itself (``declare(..., secret=True, bootstrap=True)``), so
adding a setting is the only way into the set and a list cannot drift from the
model beside it — the defect ``hardcoded-names-drift`` cost three PRs to learn
once. The single exception is structural rather than enumerated, and is
documented on :data:`BOOTSTRAP_PREFIXES`.

The safe default for a name nobody declared is APPLICATION. An assistant handed
a credential for an integration the platform has never heard of must still be
able to keep it in the vault and have the vault win; "unknown means bootstrap"
would silently restore env-first precedence for exactly the credentials this
module exists for.
"""

from __future__ import annotations

from functools import lru_cache

__all__ = [
    "BOOTSTRAP_PREFIXES",
    "bootstrap_names",
    "declared_secret_names",
    "is_bootstrap",
    "is_declared_secret",
    "is_platform_credential",
    "non_secret_env_names",
]

#: The two families of variable that no settings field can carry, because they
#: are read before the settings model exists and they are how the vault is
#: OPENED: ``ROBOTHOR_VAULT_*`` says where the AES master key lives, and
#: ``SOPS_*`` (``SOPS_AGE_KEY_FILE``) is the age key that decrypted the file the
#: environment itself came from. Resolving either of them vault-first is
#: circular, so the rule is a prefix rather than a name — it holds for the next
#: variable in the family without anyone remembering to add it.
#:
#: ``AUTH_*`` is the third family, for the same structural reason: it is what
#: the vault's own ``auth/*`` rows export as. ``auth/jwt_signing_key`` holds the
#: session signing key; deleting or rotating it from a tool or a dashboard
#: signs everybody out and strands every stored MFA secret. Declared names are
#: still decided by their declaration; the prefix covers the rows nobody
#: declares under their exported name.
BOOTSTRAP_PREFIXES: tuple[str, ...] = ("ROBOTHOR_VAULT_", "SOPS_", "AUTH_")


def _index() -> dict[str, dict[str, object]]:
    from robothor.settings.registry import field_index

    return field_index()


@lru_cache(maxsize=1)
def bootstrap_names() -> frozenset[str]:
    """Every DECLARED bootstrap credential, primary names and aliases alike.

    The prefix rule is deliberately not folded in: this set is what the status
    table and ``genus secrets migrate`` enumerate, and a set cannot enumerate a
    prefix. Ask :func:`is_bootstrap` when the question is about one name.
    """
    return frozenset(name for name, record in _index().items() if record.get("bootstrap"))


@lru_cache(maxsize=1)
def declared_secret_names() -> frozenset[str]:
    """Every declared credential, bootstrap and application together."""
    return frozenset(name for name, record in _index().items() if record.get("secret"))


@lru_cache(maxsize=1)
def non_secret_env_names() -> frozenset[str]:
    """Every declared name the model does NOT mark as a credential.

    The exec allowlist is built from this, so the derivation matters more than
    it looks: a secret that leaks into this set is a credential in every
    agent's shell. It is computed by exclusion from the same index rather than
    by listing what is safe, because a new field is safe-by-omission only if
    omission means "not in the allowlist".
    """
    return frozenset(name for name, record in _index().items() if not record.get("secret"))


def is_bootstrap(name: str) -> bool:
    """True when ``name`` keeps environment-first precedence."""
    record = _index().get(name)
    if record is not None:
        return bool(record.get("bootstrap"))
    return name.startswith(BOOTSTRAP_PREFIXES)


#: Connection strings and libpq's own variables. No settings field declares
#: them, and each one opens the same database or cache as a declared bootstrap
#: credential. The platform composes ``DATABASE_URL`` and ``REDIS_URL`` itself.
#: libpq reads ``PGPASSWORD`` and ``PGPASSFILE`` from the C environment, which
#: the startup drop empties, so an engine that authenticates through one of
#: them alone refuses to start: see
#: :func:`robothor.engine.process_hardening.drop_inherited_credentials`.
_CONNECTION_NAMES: frozenset[str] = frozenset(
    {"DATABASE_URL", "REDIS_URL", "PGPASSWORD", "PGPASSFILE"}
)

#: Connection settings declared non-secret, because a host or a port is not a
#: credential. Their VALUE can be one: ``postgresql://alice:hunter2@db/genus``
#: is an ordinary value for ``ROBOTHOR_DB_HOST``, and the pool will connect
#: with it.
_CONNECTION_SETTING_PREFIXES: tuple[str, ...] = ("ROBOTHOR_DB_", "ROBOTHOR_REDIS_")


def is_platform_credential(name: str, value: str | None = None) -> bool:
    """True when ``name`` opens the platform itself rather than one integration.

    The bootstrap class plus the connection strings, and, given ``value``, a
    connection setting whose value holds a credential. These reach every
    tenant at once, so no child process is ever given one: see
    :mod:`robothor.engine.exec_env`. Pass ``value`` wherever there is one; a
    name-only answer is for a name with no value yet, such as a grant.
    """
    if is_bootstrap(name) or name in _CONNECTION_NAMES:
        return True
    if value is None or not name.startswith(_CONNECTION_SETTING_PREFIXES):
        return False
    from robothor.secrets.redaction import redact

    candidate = value.strip()
    return bool(candidate) and redact(candidate) != candidate


def is_declared_secret(name: str) -> bool:
    """True when the settings model marks ``name`` as a credential.

    False is NOT "safe": most application credentials (``GITHUB_TOKEN``,
    ``OPENROUTER_API_KEY``) are not settings at all. Callers deciding what to
    hide use this together with the redactor's credential-name shape.
    """
    record = _index().get(name)
    return bool(record is not None and record.get("secret"))

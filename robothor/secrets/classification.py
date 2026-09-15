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
    "non_secret_env_names",
]

#: The two families of variable that no settings field can carry, because they
#: are read before the settings model exists and they are how the vault is
#: OPENED: ``ROBOTHOR_VAULT_*`` says where the AES master key lives, and
#: ``SOPS_*`` (``SOPS_AGE_KEY_FILE``) is the age key that decrypted the file the
#: environment itself came from. Resolving either of them vault-first is
#: circular, so the rule is a prefix rather than a name — it holds for the next
#: variable in the family without anyone remembering to add it.
BOOTSTRAP_PREFIXES: tuple[str, ...] = ("ROBOTHOR_VAULT_", "SOPS_")


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


def is_declared_secret(name: str) -> bool:
    """True when the settings model marks ``name`` as a credential.

    False is NOT "safe": most application credentials (``GITHUB_TOKEN``,
    ``OPENROUTER_API_KEY``) are not settings at all. Callers deciding what to
    hide use this together with the redactor's credential-name shape.
    """
    record = _index().get(name)
    return bool(record is not None and record.get("secret"))

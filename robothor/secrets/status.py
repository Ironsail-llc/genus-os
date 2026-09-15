"""One answer to "is this credential configured, where, and which one?".

Four surfaces ask it: the doctor's shadow check, ``genus secrets status``, the
Helm Secrets page, and the assistant's own ``vault_get``. Before this they
would have been four implementations, and four implementations of one question
is how "the UI calls it configured and the engine calls it missing" happened
the first time (``robothor/vault/naming.py`` exists because of that).

Nothing here returns a value. Every function returns some combination of a
name, a boolean, a fingerprint, a source and a timestamp — which is the whole
answer an operator or an assistant needs, and which discloses nothing. The one
exception is :func:`resolve_for_key`, which exists so ``vault_test`` can DIAL
with a credential it never returns; it is named to be conspicuous.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from robothor.constants import DEFAULT_TENANT
from robothor.secrets import ResolvedSecret, SecretSource, resolve_secret
from robothor.secrets.classification import is_bootstrap
from robothor.secrets.fingerprint import fingerprint

logger = logging.getLogger(__name__)

__all__ = [
    "SecretStatus",
    "environment_credential_names",
    "resolve_for_key",
    "status_for_key",
    "status_for_name",
    "status_table",
]


@dataclass(frozen=True)
class SecretStatus:
    """Everything that may be said about a credential in public.

    Matches the shape the Helm Secrets page and the provider API already use
    (``configured``, ``fingerprint``, ``updated_at``, ``source``) so the agent
    surface and the operator surface agree word for word, with the fields the
    two stores make newly answerable added: which store holds it, and whether
    they disagree.
    """

    name: str
    configured: bool
    fingerprint: str | None
    source: SecretSource
    in_env: bool
    in_vault: bool
    bootstrap: bool
    updated_at: str | None = None
    #: Both stores hold a value and the values DIFFER. The name of the 2026-09-15
    #: incident: an expired environment value sitting in front of a live vault
    #: row, with nothing anywhere saying so.
    shadowed: bool = False
    #: Which store the accessor is NOT serving, when shadowed.
    shadowed_by: str | None = None
    env_fingerprint: str | None = None
    vault_fingerprint: str | None = None


def _env_value(name: str) -> str | None:
    from robothor.settings.env import process_env_get

    raw = process_env_get(name, None)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _vault_value(name: str, tenant_id: str, vault_key: str | None) -> str | None:
    """The vault's own answer, read WITHOUT the precedence chain.

    :func:`resolve_secret` answers "what would a reader get"; this answers
    "what does the vault hold", and the shadow check needs both or it cannot
    tell a shadow from an agreement.
    """
    try:
        from robothor import vault

        if vault_key is not None:
            found = vault.get(vault_key, tenant_id=tenant_id)
        else:
            found = vault.export_env(tenant_id=tenant_id).get(name)
    except Exception:  # noqa: BLE001 - an unreadable vault is not an exception here
        return None
    if found is None:
        return None
    stripped = found.strip()
    return stripped or None


def _updated_at(vault_key: str | None, tenant_id: str) -> str | None:
    if vault_key is None:
        return None
    try:
        from robothor.vault.dal import get_secrets_updated_at

        stamps = get_secrets_updated_at([vault_key], tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - a timestamp is never worth a failure
        return None
    found = stamps.get(vault_key)
    return found.isoformat() if found is not None else None


def status_for_name(
    name: str,
    *,
    tenant_id: str = DEFAULT_TENANT,
    vault_key: str | None = None,
) -> SecretStatus:
    """The status of the credential an ENVIRONMENT name refers to."""
    tenant = tenant_id or DEFAULT_TENANT
    from_env = _env_value(name)
    from_vault = _vault_value(name, tenant, vault_key)
    resolved: ResolvedSecret = resolve_secret(name, vault_key=vault_key, tenant_id=tenant)

    shadowed = bool(from_env and from_vault and from_env != from_vault)
    return SecretStatus(
        name=name,
        configured=resolved.value is not None,
        fingerprint=fingerprint(resolved.value) if resolved.value is not None else None,
        source=resolved.source,
        in_env=from_env is not None,
        in_vault=from_vault is not None,
        bootstrap=is_bootstrap(name),
        updated_at=_updated_at(vault_key, tenant),
        shadowed=shadowed,
        shadowed_by=(
            None
            if not shadowed
            else ("the environment" if resolved.source == "vault" else "the vault")
        ),
        env_fingerprint=fingerprint(from_env) if from_env else None,
        vault_fingerprint=fingerprint(from_vault) if from_vault else None,
    )


def status_for_key(key: str, *, tenant_id: str = DEFAULT_TENANT) -> SecretStatus:
    """The status of the credential a VAULT KEY refers to.

    The key is translated to its exported environment name through
    ``vault.naming.env_name`` — the one mapping — so that a question asked
    about ``providers/github/api_key`` and the same question asked about
    ``PROVIDERS_GITHUB_API_KEY`` cannot give different answers.
    """
    from robothor.vault.naming import env_name

    return status_for_name(env_name(key), tenant_id=tenant_id, vault_key=key)


def resolve_for_key(key: str, *, tenant_id: str = DEFAULT_TENANT) -> str | None:
    """The VALUE behind a vault key, for a caller that must dial with it.

    Named to be conspicuous: this is the one function in this module that
    hands back a credential, and its only caller is the ``vault_test`` probe,
    which returns ``{ok, identity_hint, error_class}`` and never the value.
    """
    from robothor.vault.naming import env_name

    return resolve_secret(env_name(key), vault_key=key, tenant_id=tenant_id).value


def environment_credential_names() -> set[str]:
    """Every name in THIS process's environment that holds a credential.

    Two sources, unioned, because neither alone is the answer. The settings
    model knows which DECLARED names are credentials. The redactor knows what a
    credential NAME looks like, which is what catches ``GITHUB_TOKEN``,
    ``AWS_SECRET_ACCESS_KEY`` and every other third-party token the platform
    never declared -- and those are precisely the credentials an assistant is
    handed, so a status table that could not see them would be blind to the
    ones this change is about.

    A third, hand-written list would be the drift defect. There isn't one.
    """
    from robothor.engine.exec_env import looks_like_a_credential_name
    from robothor.secrets.classification import declared_secret_names
    from robothor.settings.env import process_env_get  # noqa: F401 - import for symmetry

    declared = declared_secret_names()
    return {name for name in os.environ if name in declared or looks_like_a_credential_name(name)}


def status_table(*, tenant_id: str = DEFAULT_TENANT) -> list[SecretStatus]:
    """Every credential this instance declares or holds, in one list.

    The union of three sources, because none of them alone is the answer: the
    settings model knows what the platform DECLARES, the vault knows what this
    instance was actually given, and the process environment knows what the box
    booted with. A credential the operator handed the assistant for a vendor
    nobody declared appears because the vault holds it; a declared credential
    nobody has configured appears as ``configured: False``, which is the answer
    to "why does this integration not work".
    """
    from robothor.secrets.classification import declared_secret_names
    from robothor.settings.registry import field_index
    from robothor.vault.naming import env_name

    index = field_index()
    names: set[str] = {
        name for name in declared_secret_names() if index.get(name, {}).get("env") == name
    }
    # ...and whatever the environment is actually carrying, declared or not. A
    # token the operator handed the assistant for a vendor nobody declared is
    # the case this table exists for.
    names |= environment_credential_names()

    # Whatever the vault holds, whether or not anybody declared it.
    vault_keys: dict[str, str] = {}
    try:
        from robothor import vault

        for key in vault.list(tenant_id=tenant_id):
            exported = env_name(key)
            vault_keys[exported] = key
            names.add(exported)
    except Exception:  # noqa: BLE001 - an unreadable vault narrows the table, not fails it
        # The declared names still make a usable table, and every row's `source`
        # will say `unavailable`, which is the honest answer.
        logger.debug("secrets: the vault could not be listed for the status table")

    return [
        status_for_name(name, tenant_id=tenant_id, vault_key=vault_keys.get(name))
        for name in sorted(names)
    ]

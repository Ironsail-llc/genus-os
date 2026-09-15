"""``genus secrets`` — what this instance holds, where, and how to move it.

Two commands, for the two questions an operator has once the vault is the store
that wins.

``genus secrets status`` prints every credential the platform declares or the
vault holds: name, which store has it, which store a reader is served, a
fingerprint per store, and whether the two disagree. It reads the same
:func:`robothor.secrets.status.status_table` the doctor's ``secrets.shadowed``
check and the Helm Secrets page read, so the three cannot disagree about
whether something is configured — the failure ``robothor/vault/naming.py``
exists because of.

``genus secrets migrate --from-env`` moves credentials out of the process
environment and into the vault. That is the shape of the fix for the 2026-09-15
incident: the environment is a snapshot of a root-owned SOPS file taken at
boot, and everything in it that the operator wants to be able to rotate — every
third-party token — belongs in the store the operator and the assistant can
both write. What stays behind is the bootstrap set, which is the file's real
job: the credentials that bring the instance up.

Neither command prints a value, including under ``--dry-run``, including for a
name the operator named explicitly. ``genus vault get`` is where a value comes
from, and it says so in its own name.
"""

from __future__ import annotations

import argparse  # noqa: TC003
import os

from robothor.constants import DEFAULT_TENANT

__all__ = ["cmd_secrets"]

#: How a migrated credential is filed when the settings model has no opinion.
#: ``category`` is only a filter for ``vault list``; getting it wrong costs an
#: operator a tidy listing, not a credential.
_DEFAULT_CATEGORY = "credential"


def _tenant(args: argparse.Namespace) -> str:
    return getattr(args, "tenant", None) or os.environ.get("ROBOTHOR_TENANT_ID") or DEFAULT_TENANT


def cmd_secrets(args: argparse.Namespace) -> int:
    sub = getattr(args, "secrets_command", None)
    if sub == "status":
        return _status(args)
    if sub == "migrate":
        return _migrate(args)
    print("Usage: genus secrets {status|migrate}")
    return 0


def _status(args: argparse.Namespace) -> int:
    """The table. Names, sources, fingerprints — never a value."""
    from robothor.secrets.status import status_table

    rows = status_table(tenant_id=_tenant(args))
    if not rows:
        print("This instance declares no credentials and the vault holds none.")
        return 0

    width = max(len(row.name) for row in rows)
    print(f"{'NAME'.ljust(width)}  ENV    VAULT  SERVED      FINGERPRINT       NOTE")
    shadows = 0
    for row in rows:
        note = ""
        if row.shadowed:
            shadows += 1
            note = f"SHADOW — the {row.shadowed_by} holds a different value"
        elif row.bootstrap:
            note = "bootstrap (environment-first by design)"
        print(
            f"{row.name.ljust(width)}  "
            f"{'yes' if row.in_env else '-  '}    "
            f"{'yes' if row.in_vault else '-  '}    "
            f"{row.source.ljust(11)} "
            f"{(row.fingerprint or '-').ljust(17)} "
            f"{note}".rstrip()
        )

    configured = sum(1 for row in rows if row.configured)
    print(f"\n{configured} of {len(rows)} configured; {shadows} shadowed.")
    if shadows:
        print(
            "A shadow is the same credential set to DIFFERENT values in the two stores. "
            "The fingerprints say which is which without printing either; "
            "`genus doctor` reports the same thing."
        )
    return 0


def _vault_key_for(name: str) -> str:
    """Where in the vault this environment name's value belongs.

    The FIRST candidate ``vault.naming.vault_keys_for_env_name`` offers, which
    is the canonical spelling for that shape: ``providers/<id>/api_key`` for a
    provider slot (what the wizard, the Helm provider page and ``key_pool``
    already use), the literal lower-cased name otherwise. Deliberately not a
    spelling invented here: the accessor searches with the same function, so a
    row this writes is a row a reader finds. Inventing a prettier key would
    produce a row nothing reads -- the failure that module exists to prevent.
    """
    from robothor.vault.naming import vault_keys_for_env_name

    candidates = vault_keys_for_env_name(name)
    if not candidates:
        raise ValueError(f"{name} cannot be expressed as a vault key")
    return candidates[0]


def _migrate(args: argparse.Namespace) -> int:
    """Copy application credentials from the environment into the vault."""
    if not getattr(args, "from_env", False):
        print("Usage: genus secrets migrate --from-env [--dry-run] [--only NAME ...]")
        return 2

    import robothor.vault as vault
    from robothor.secrets.classification import is_bootstrap
    from robothor.secrets.fingerprint import fingerprint
    from robothor.secrets.status import environment_credential_names, status_for_name
    from robothor.settings.registry import field_index

    tenant = _tenant(args)
    dry_run = bool(getattr(args, "dry_run", False))
    only = [name.strip() for name in (getattr(args, "only", None) or []) if name.strip()]

    index = field_index()
    candidates = sorted(only) if only else sorted(environment_credential_names())

    planned: list[tuple[str, str, str]] = []  # (name, vault key, fingerprint)
    refused: list[str] = []
    unchanged: list[str] = []

    for name in candidates:
        value = value_for(name)
        if not value:
            # Nothing to move. Silent for a scan; the explicit `--only` case is
            # caught below, where saying nothing would read as success.
            continue
        if is_bootstrap(name):
            refused.append(name)
            continue
        status = status_for_name(name, tenant_id=tenant)
        if status.in_vault and status.vault_fingerprint == fingerprint(value):
            unchanged.append(name)
            continue
        planned.append((name, _vault_key_for(name), fingerprint(value)))

    for name in refused:
        print(
            f"refused  {name}  — a bootstrap credential. It is what brings this "
            "instance up (the vault's own rows live behind it), so it stays in the "
            "secrets file."
        )
    for name in unchanged:
        print(f"already  {name}  — the vault holds the same value; nothing to do.")

    verb = "would store" if dry_run else "stored"
    for name, key, digest in planned:
        if not dry_run:
            category = index.get(name, {}).get("group") or _DEFAULT_CATEGORY
            vault.set(key, value_for(name), category=str(category), tenant_id=tenant)
        print(f"{verb}  {name}  -> {key}  {digest}")

    for name in only:
        if name not in {row[0] for row in planned} and name not in refused + unchanged:
            print(f"skipped  {name}  — not set in this environment.")

    if not planned and not refused and not unchanged:
        print("Nothing to migrate: no application credential is set in this environment.")

    if planned and not dry_run:
        _reload_cached_readers()
        print(
            f"\n{len(planned)} credential(s) are now in the vault, which wins over the "
            "environment for application credentials. Remove them from the instance's "
            "secrets file when you are satisfied — `genus doctor` will report them as "
            "shadows until you do."
        )
    return 1 if (refused and only) else 0


def value_for(name: str) -> str:
    """The environment's value for ``name``.

    A named function rather than an inline read so the migration's one point of
    contact with a credential is greppable — everything else in this module
    handles names and digests.
    """
    return (os.environ.get(name) or "").strip()


def _reload_cached_readers() -> None:
    """Best effort: let a running engine in this process see the new rows."""
    try:
        from robothor.engine import key_pool
        from robothor.secrets import reset_vault_availability

        reset_vault_availability()
        key_pool.reload_provider_keys()
    except Exception:  # noqa: BLE001 - the CLI usually runs outside the engine
        # Nothing to warn about: a CLI invocation normally has no engine to
        # refresh, and the rows are written either way.
        return

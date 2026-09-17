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
import logging
from dataclasses import dataclass
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine_control import control_request

logger = logging.getLogger(__name__)

__all__ = ["cmd_secrets"]

#: How a migrated credential is filed when the settings model has no opinion.
#: ``category`` is only a filter for ``vault list``; getting it wrong costs an
#: operator a tidy listing, not a credential.
_DEFAULT_CATEGORY = "credential"


def _tenant(args: argparse.Namespace) -> str:
    """Whose vault to act on: the flag, then the instance's configured tenant."""
    from robothor.settings import get_settings

    explicit = getattr(args, "tenant", None)
    if explicit:
        return str(explicit)
    return get_settings().database.tenant_id or DEFAULT_TENANT


@dataclass(frozen=True)
class PlannedMigration:
    """One credential this run will move, described without its value.

    A dataclass rather than a tuple so the line that reports the move is built
    from FIELDS — ``entry.name``, ``entry.vault_key``, ``entry.fingerprint`` —
    none of which can hold a value and none of which is derived from the call
    that fetches one. The tuple form put the printed names in the same
    unpacking as the loop that read the credential, which is a path CodeQL has
    to assume carries it, and a false alert that stays open is
    indistinguishable from one nobody has read.
    """

    name: str
    vault_key: str
    fingerprint: str


def _store(entry: PlannedMigration, *, category: str, tenant_id: str) -> None:
    """Write one planned credential. Returns nothing, deliberately.

    The value is read and handed to the vault inside this function and never
    leaves it, so no expression at the call site is derived from it.
    """
    import robothor.vault as vault

    vault.set(
        entry.vault_key,
        value_for(entry.name),
        category=category or _DEFAULT_CATEGORY,
        tenant_id=tenant_id,
    )


def cmd_secrets(args: argparse.Namespace) -> int:
    sub = getattr(args, "secrets_command", None)
    if sub == "status":
        return _status(args)
    if sub == "migrate":
        return _migrate(args)
    if sub == "reload":
        return _reload(args)
    print("Usage: genus secrets {status|migrate|reload}")
    return 0


def _hours(seconds: float) -> str:
    """A duration an operator reads at a glance, never a bare float."""
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{int(seconds // 60)}m"


def live_rotation() -> dict[str, str]:
    """``{fingerprint: note}`` from the RUNNING engine, or empty if it is down.

    Rotation state lives in the engine's memory and nowhere else. A CLI that
    answered from its own process would report "active" for a key the fleet has
    not been able to use for six hours — so this asks, and says nothing at all
    when there is nobody to ask.
    """
    try:
        return rotation_notes(control_request("GET", "/api/admin/providers"))
    except Exception as exc:  # noqa: BLE001 — a down engine is not a CLI error
        logger.debug("rotation state unavailable: %s", exc)
        return {}


def rotation_notes(payload: dict[str, Any]) -> dict[str, str]:
    """Turn the engine's provider payload into one line per credential."""
    notes: dict[str, str] = {}
    for provider in payload.get("providers") or []:
        for slot in provider.get("slots") or []:
            fingerprint = str(slot.get("fingerprint") or "")
            if not fingerprint:
                continue
            if slot.get("state") not in ("capped", "revoked"):
                notes[fingerprint] = "in rotation"
                continue
            parts = [f"retired ({slot.get('reason') or slot.get('state')})"]
            elapsed = slot.get("retired_for_s")
            if isinstance(elapsed, (int, float)):
                parts.append(f"{_hours(float(elapsed))} ago")
            returns = slot.get("returns_in_s")
            parts.append(
                f"returns in {_hours(float(returns))}"
                if isinstance(returns, (int, float))
                else "not retried for the life of this process"
            )
            notes[fingerprint] = ", ".join(parts)
    return notes


def _reload(args: argparse.Namespace) -> int:
    """Make the running engine re-read its credentials, and un-retire its keys.

    The command an operator runs after topping up or raising a cap. Without it
    the only cure for a key the pool retired is a daemon restart or the wait —
    six hours for a calendar quota — and on 2026-09-16 that is exactly where
    the operator was left, with a raised limit and a fleet that would not use
    it. The token is minted per call and never printed.
    """
    try:
        body = control_request("POST", "/api/admin/secrets/reload")
    except Exception as exc:  # noqa: BLE001 — the message IS the output
        print(f"Could not reload: {exc}")
        return 1
    reloaded = body.get("reloaded") or []
    restored = body.get("restored") or []
    print(f"reloaded: {list(reloaded)} ({body.get('slots', 0)} slot(s) from the vault)")
    if restored:
        print(f"back in rotation: {', '.join(str(item) for item in restored)}")
    else:
        print("no credential was out of rotation")
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
    # Asked once for the whole table: the engine holds the only copy of
    # rotation state, and a credential the pool has retired is configured,
    # unshadowed, and completely unusable — three columns of green.
    rotation = live_rotation()
    shadows = 0
    for row in rows:
        note = ""
        if row.shadowed:
            shadows += 1
            note = f"SHADOW — the {row.shadowed_by} holds a different value"
        elif row.bootstrap:
            note = "bootstrap (environment-first by design)"
        pool_note = rotation.get(row.fingerprint or "")
        if pool_note and pool_note != "in rotation":
            note = f"{note}; {pool_note}" if note else pool_note
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

    from robothor.secrets.classification import is_bootstrap
    from robothor.secrets.fingerprint import fingerprint
    from robothor.secrets.status import environment_credential_names, status_for_name
    from robothor.settings.registry import field_index

    tenant = _tenant(args)
    dry_run = bool(getattr(args, "dry_run", False))
    only = [name.strip() for name in (getattr(args, "only", None) or []) if name.strip()]

    index = field_index()
    if only:
        # An explicit `--only` still has to name a CREDENTIAL. `--only HOME PATH`
        # wrote `home` and `path` rows into the vault, and a scan picked up
        # GOOGLE_APPLICATION_CREDENTIALS — a file PATH, not a secret. A vault
        # full of non-secrets is a status table nobody reads.
        from robothor.engine.exec_env import looks_like_a_credential_name
        from robothor.secrets.classification import declared_secret_names

        declared = declared_secret_names()
        candidates = sorted(n for n in only if n in declared or looks_like_a_credential_name(n))
        for name in sorted(set(only) - set(candidates)):
            print(
                f"skipped  {name}  — not a credential name. `migrate` moves credentials; "
                "use `genus vault set` for anything else."
            )
    else:
        candidates = sorted(environment_credential_names())

    overwrite = {n.strip() for n in (getattr(args, "overwrite", None) or []) if n.strip()}

    planned: list[PlannedMigration] = []
    refused: list[str] = []
    unchanged: list[str] = []
    # The vault holds a DIFFERENT value for these, and it is KEPT. Overwriting
    # inverted the platform's own precedence rule while printing "stored":
    # once the assistant has rotated anything the vault holds the new
    # credential and the environment holds the dead one the box booted with, so
    # a migration that wrote would revert every rotation -- and the SOPS
    # runbook tells the operator to run exactly this. The incident, executed by
    # the remediation for the incident.
    conflicts: list[tuple[str, Any]] = []

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
        if status.in_vault and name not in overwrite:
            conflicts.append((name, status))
            continue
        planned.append(PlannedMigration(name, _vault_key_for(name), fingerprint(value)))

    # Said FIRST, before any per-name line. An operator running this against a
    # box whose assistant has been rotating credentials needs to know, before
    # they read anything else, that the existing rows are safe — the previous
    # behaviour silently reverted every one of them.
    existing = len(conflicts) + len(unchanged)
    if existing:
        print(
            f"{existing} vault row(s) already exist and will NOT be touched "
            f"({len(unchanged)} identical, {len(conflicts)} holding a different value). "
            "The vault wins for application credentials; a migration never reverts a "
            "rotation.\n"
        )

    for name in refused:
        print(
            f"refused  {name}  — a bootstrap credential. It is what brings this "
            "instance up (the vault's own rows live behind it), so it stays in the "
            "secrets file."
        )
    for name in unchanged:
        print(f"already  {name}  — the vault holds the same value; nothing to do.")
    for name, status in conflicts:
        written = f", written {status.updated_at}" if status.updated_at else ""
        print(
            f"CONFLICT {name}  — the vault already holds a DIFFERENT value and it was "
            f"kept. environment {status.env_fingerprint}, vault "
            f"{status.vault_fingerprint} (at {status.vault_key}{written}). The vault "
            "wins for application credentials, so readers are already served its copy; "
            "`vault_test` says whether that copy is alive. Delete the environment copy "
            f"from the secrets file, or pass `--overwrite {name}` to replace the vault "
            "row with it."
        )

    verb = "would store" if dry_run else "stored"
    for entry in planned:
        if not dry_run:
            _store(
                entry, category=str(index.get(entry.name, {}).get("group") or ""), tenant_id=tenant
            )
        # Only fields of `entry`, which has no value field and cannot be built
        # from one. The write above is a separate statement returning None, so
        # nothing printed here is derived from the call that fetches a value.
        print(f"{verb}  {entry.name}  -> {entry.vault_key}  {entry.fingerprint}")

    accounted = (
        {entry.name for entry in planned}
        | set(refused)
        | set(unchanged)
        | {n for n, _ in conflicts}
    )
    for name in only:
        if name not in accounted:
            print(f"skipped  {name}  — not set in this environment.")

    if not planned and not refused and not unchanged and not conflicts:
        print("Nothing to migrate: no application credential is set in this environment.")

    if planned and not dry_run:
        _reload_cached_readers()
        print(
            f"\n{len(planned)} credential(s) are now in the vault, which wins over the "
            "environment for application credentials. Remove them from the instance's "
            "secrets file when you are satisfied — `genus doctor` will report them as "
            "shadows until you do."
        )
    if conflicts:
        print(
            f"\n{len(conflicts)} credential(s) were NOT migrated: the vault already "
            "holds a different value. That is the vault winning, which is the intended "
            "behaviour — a migration must never revert a rotation."
        )
    return 1 if (refused and only) else 0


def value_for(name: str) -> str:
    """The environment's value for ``name``.

    A named function rather than an inline read so the migration's one point of
    contact with a credential is greppable — everything else in this module
    handles names and digests.
    """
    from robothor.settings.env import process_env_get

    return (process_env_get(name, None) or "").strip()


def _reload_cached_readers() -> None:
    """Make the write visible: in this process, and in the running engine.

    Two halves, because the CLI is a DIFFERENT PROCESS from the engine. The
    local reset covers an invocation that shares the interpreter; the POST
    covers the ordinary case, where an operator runs ``genus`` in a terminal
    while the engine is up and would otherwise wait out the cache TTL wondering
    whether the command worked.
    """
    import contextlib

    # A CLI invocation normally has no engine to refresh in-process, and the
    # rows are written either way — so this half is allowed to find nothing.
    with contextlib.suppress(Exception):
        from robothor.engine import key_pool
        from robothor.secrets import reset_vault_availability

        reset_vault_availability()
        key_pool.reload_provider_keys()

    from robothor.secrets.reload import notify_engine

    notify_engine()

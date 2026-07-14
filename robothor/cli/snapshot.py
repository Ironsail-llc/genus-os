"""Operator-facing snapshot and disaster-recovery commands."""

from __future__ import annotations

import argparse  # noqa: TC003
import os
from pathlib import Path

from robothor.config import get_config
from robothor.snapshot import (
    SnapshotError,
    create_snapshot,
    list_snapshots,
    prune_snapshots,
    restore_snapshot,
    verify_snapshot,
)


def _repository(args: argparse.Namespace) -> Path:
    configured = getattr(args, "repository", None) or os.environ.get("GENUS_SNAPSHOT_REPOSITORY")
    if configured:
        return Path(configured).expanduser()
    return get_config().workspace / ".robothor" / "snapshots"


def _workspace(args: argparse.Namespace) -> Path:
    configured = getattr(args, "workspace", None)
    return Path(configured).expanduser() if configured else get_config().workspace


def _passphrase(args: argparse.Namespace, *, required: bool) -> str | None:
    environment_name = getattr(args, "passphrase_env", "GENUS_SNAPSHOT_PASSPHRASE")
    value = os.environ.get(environment_name)
    if required and not value:
        raise SnapshotError(
            f"Set {environment_name} to a strong snapshot passphrase; "
            "passphrases are intentionally not accepted as command-line arguments"
        )
    return value


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _cmd_create(args: argparse.Namespace) -> int:
    encrypted = not bool(args.plaintext)
    path = create_snapshot(
        workspace=_workspace(args),
        database=get_config().db,
        repository=_repository(args),
        output=Path(args.output).expanduser() if args.output else None,
        include_database=not bool(args.skip_database),
        include_workspace=not bool(args.skip_workspace),
        include_secrets=bool(args.include_secrets),
        encrypt=encrypted,
        passphrase=_passphrase(args, required=encrypted),
        force=bool(args.force),
    )
    print(f"Snapshot created: {path}")
    print(f"Protection: {'AES-256-GCM encrypted' if encrypted else 'PLAINTEXT (explicit)'}")
    print(f"Size: {_format_size(path.stat().st_size)}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    entries = list_snapshots(_repository(args))
    if not entries:
        print(f"No snapshots found in {_repository(args)}")
        return 0
    print(f"{'CREATED (UTC)':<22} {'SIZE':>12}  {'PROTECTION':<10}  PATH")
    for entry in entries:
        created = entry.created_at.strftime("%Y-%m-%d %H:%M:%S")
        protection = "encrypted" if entry.encrypted else "plaintext"
        print(f"{created:<22} {_format_size(entry.size):>12}  {protection:<10}  {entry.path}")
    print(f"\n{len(entries)} snapshot(s)")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.snapshot).expanduser()
    passphrase = _passphrase(args, required=False)
    result = verify_snapshot(path, passphrase=passphrase)
    manifest = result.manifest
    instance = manifest["instance"]
    print(f"Snapshot verified: {result.path}")
    print(f"Created: {manifest['created_at']}")
    print(f"Instance: {instance['name']} ({instance['id']})")
    print(f"Protection: {'encrypted' if result.encrypted else 'plaintext'}")
    print(f"Compatibility: {'compatible' if result.compatible else 'NOT COMPATIBLE'}")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    return 0 if result.compatible else 1


def _restore_selection(args: argparse.Namespace) -> tuple[bool, bool]:
    if args.database_only:
        return True, False
    if args.workspace_only:
        return False, True
    return True, True


def _cmd_restore(args: argparse.Namespace) -> int:
    database_selected, workspace_selected = _restore_selection(args)
    target_workspace = _workspace(args)
    target_database = get_config().db
    result = restore_snapshot(
        Path(args.snapshot).expanduser(),
        workspace=target_workspace,
        database=target_database,
        passphrase=_passphrase(args, required=False),
        restore_database=database_selected,
        restore_workspace=workspace_selected,
        confirm=bool(args.confirm),
        force=bool(args.force),
    )
    if result.dry_run:
        print("DRY RUN — snapshot verified; no data was changed.")
        print(
            f"Would restore: database={'yes' if database_selected else 'no'}, "
            f"workspace={'yes' if workspace_selected else 'no'}"
        )
        if database_selected:
            print(f"Database target: {target_database.name}")
        if workspace_selected:
            print(f"Workspace target: {target_workspace}")
        if result.conflicts:
            print(f"Workspace conflicts: {len(result.conflicts)} (actual restore needs --force)")
        if not result.verification.compatible:
            print("Compatibility: NOT COMPATIBLE (actual restore needs --force)")
        print("Run again with --confirm and, for replacement, --force.")
        return 0
    print(f"Restore complete: {result.verification.path}")
    print(f"Database restored: {'yes' if result.database_restored else 'no'}")
    print(f"Workspace files restored: {result.workspace_files_restored}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    result = prune_snapshots(
        _repository(args),
        keep=args.keep,
        older_than_days=args.older_than_days,
        confirm=bool(args.confirm),
    )
    if not result.candidates:
        print("Retention policy selected no snapshots.")
        return 0
    action = "Deleted" if not result.dry_run else "Would delete"
    for entry in result.candidates:
        print(f"{action}: {entry.path} ({_format_size(entry.size)})")
    if result.dry_run:
        print("DRY RUN — run again with --confirm to delete these snapshots.")
    else:
        print(f"Deleted {len(result.deleted)} snapshot(s).")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Dispatch a snapshot subcommand with concise, non-secret errors."""

    command = getattr(args, "snapshot_command", None)
    try:
        if command == "create":
            return _cmd_create(args)
        if command == "list":
            return _cmd_list(args)
        if command == "verify":
            return _cmd_verify(args)
        if command == "restore":
            return _cmd_restore(args)
        if command == "prune":
            return _cmd_prune(args)
    except (SnapshotError, OSError) as error:
        print(f"Snapshot error: {error}")
        return 1
    print("Usage: robothor snapshot {create|list|verify|restore|prune}")
    return 1

"""robothor upgrade — pull platform updates and run new migrations.

Instance configuration (brain/, docs/agents/*.yaml, .env) is never touched.

Schema changes go through the canonical migrator in ``robothor.db.migrate``
(manifest, ``schema_migrations_v2`` ledger, advisory lock, SHA-256 checksums).
This command owns no migration mechanism of its own.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

import yaml

logger = logging.getLogger(__name__)


def _workspace() -> Path:
    return Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))


def _state_file() -> Path:
    return _workspace() / ".robothor" / "migrations_applied.yaml"


def _pull_latest(dry_run: bool) -> bool:
    """Pull latest from remote. Returns True if successful."""
    workspace = _workspace()
    if not (workspace / ".git").is_dir():
        print("  Not a git repository — skipping pull.")
        return True

    if dry_run:
        subprocess.run(
            ["git", "fetch", "--dry-run"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        behind = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{u}"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        count = behind.stdout.strip() if behind.returncode == 0 else "?"
        print(f"  {count} commit(s) behind remote.")
        return True

    result = subprocess.run(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Pull failed: {result.stderr.strip()}")
        print("  Try: git pull --rebase origin main")
        return False

    print(f"  {result.stdout.strip()}")
    return True


def _run_migrations(dry_run: bool) -> int:
    """Bring the schema current through the canonical migrator.

    Mirrors the error handling of ``robothor migrate`` (``cli/admin.py``) so
    both entry points fail the same way.
    """

    try:
        import psycopg2

        from robothor.config import get_config
        from robothor.db.migrate import MigrationError, apply, status

        if dry_run:
            rows = status()
            pending = [row for row in rows if row["status"] != "applied"]
            print(f"  {len(pending)} of {len(rows)} canonical migration(s) not applied.")
            for row in pending:
                print(f"    - {row['migration_id']}: {row['status']}")
                message = row.get("message")
                if message:
                    print(f"      {message}")
            return 0

        cfg = get_config().db
        print(f"  Connecting to {cfg.host}:{cfg.port}/{cfg.name}...")
        conn = psycopg2.connect(**cfg.dict, connect_timeout=5)
        try:
            rows = status(connection=conn)
            pending = [row for row in rows if row["status"] != "applied"]
            print(f"  {len(pending)} of {len(rows)} canonical migration(s) not applied.")
            applied = apply(connection=conn)
            print(f"  Migration completed successfully ({len(applied)} applied).")
            return 0
        finally:
            conn.close()

    except ImportError:
        print("  Error: psycopg2 is required. Install with: pip install genusos")
        return 1
    except MigrationError as e:
        print(f"  Error: Migration safety check failed: {e}")
        return 1
    except Exception as e:
        print(f"  Error: Migration failed: {e}")
        print("  Check ROBOTHOR_DB_* environment variables and ensure PostgreSQL is running.")
        return 1


# Template source name → instance destination (relative to brain/)
TEMPLATE_CHECKS = {
    "brain-CLAUDE.md": "CLAUDE.md",
    "SOUL.md": "SOUL.md",
    "IDENTITY.md": "IDENTITY.md",
    "USER.md": "USER.md",
}


def _hash_file(path: Path) -> str:
    """SHA-256 hex digest of a file's content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_state() -> dict[str, Any]:
    """Load full upgrade state (template hashes)."""
    state = _state_file()
    if not state.exists():
        return {}
    return yaml.safe_load(state.read_text()) or {}


def _save_state(data: dict[str, Any]) -> None:
    """Write full upgrade state, dropping the retired ``migrations`` ledger.

    The YAML side-ledger was a second source of truth for applied migrations.
    ``schema_migrations_v2`` is the only one now, so any surviving key is
    stripped on the next write rather than left to mislead a future reader.
    """
    data.pop("migrations", None)
    state = _state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(yaml.dump(data, default_flow_style=False))


def _snapshot_template_hashes() -> dict[str, str]:
    """Compute current SHA-256 hashes for all known templates."""
    from robothor.setup import _find_template_dir

    template_dir = _find_template_dir()
    if not template_dir:
        return {}
    hashes = {}
    for src_name in TEMPLATE_CHECKS:
        src_path = template_dir / src_name
        if src_path.exists():
            hashes[src_name] = _hash_file(src_path)
    return hashes


def _check_template_updates() -> list[tuple[str, str]]:
    """Check if templates have been updated since last upgrade/init."""
    workspace = _workspace()
    state = _load_state()
    stored_hashes = state.get("template_hashes", {})
    current_hashes = _snapshot_template_hashes()
    updates = []

    for src_name, dst_name in TEMPLATE_CHECKS.items():
        dst_path = workspace / "brain" / dst_name
        if not dst_path.exists():
            continue  # Instance file doesn't exist — nothing to update
        current = current_hashes.get(src_name)
        stored = stored_hashes.get(src_name)
        if current and current != stored:
            updates.append((src_name, str(dst_path)))

    return updates


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Run the upgrade process."""
    dry_run = getattr(args, "dry_run", False)
    pull = getattr(args, "pull", False)
    skip_migrations = getattr(args, "skip_migrations", False)

    import robothor

    print(f"Genus OS v{robothor.__version__}")
    print()

    # 1. Pull latest (opt-in: a pip install has no checkout to pull)
    if pull:
        print("Pulling latest platform code...")
        if not _pull_latest(dry_run):
            return 1
    else:
        print("Skipping git pull (pass --pull for a git checkout).")
        print("  Installed from a wheel? Update with: pip install -U genusos")
    print()

    # 2. Migrations — canonical migrator only
    if not skip_migrations:
        print("Checking migrations...")
        if _run_migrations(dry_run) != 0:
            return 1
    else:
        print("Skipping migrations (--skip-migrations)")
    print()

    # 3. Template updates
    print("Checking template updates...")
    updates = _check_template_updates()
    if updates:
        print("  Templates have been updated since your instance files were created:")
        for src_name, dst_path in updates:
            print(f"    {src_name} → {dst_path}")
        print("  Review with: diff templates/<name>.md brain/<name>.md")
    else:
        print("  No template updates.")
    print()

    # 4. Save current template hashes for next upgrade comparison
    if not dry_run:
        state_data = _load_state()
        state_data["template_hashes"] = _snapshot_template_hashes()
        _save_state(state_data)

    # 5. Summary
    if dry_run:
        print("Dry run complete — no changes made.")
    else:
        print("Upgrade complete.")

    return 0

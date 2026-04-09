"""robothor upgrade — pull platform updates and run new migrations.

Instance configuration (brain/, docs/agents/*.yaml, .env) is never touched.
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


def _load_applied() -> list[str]:
    """Load list of already-applied migration filenames."""
    state = _state_file()
    if not state.exists():
        return []
    data = yaml.safe_load(state.read_text()) or {}
    return [entry["file"] for entry in data.get("migrations", [])]


def _save_applied(migrations: list[dict[str, Any]]) -> None:
    """Write migration tracking state (legacy helper for seed)."""
    state_data = _load_state()
    state_data["migrations"] = migrations
    _save_state(state_data)


def _discover_migrations() -> list[Path]:
    """Find all migration SQL files, sorted by number."""
    workspace = _workspace()
    migration_dirs = [
        workspace / "infra" / "migrations",
        workspace / "crm" / "migrations",
    ]
    files = []
    for d in migration_dirs:
        if d.is_dir():
            files.extend(d.glob("*.sql"))
    return sorted(files, key=lambda p: p.name)


def _seed_tracking_if_needed(applied: list[str], all_migrations: list[Path]) -> list[str]:
    """On first run, detect existing tables and seed all migrations as applied."""
    if applied:
        return applied  # Already tracking

    # Check if this is an existing instance (has a database with tables)
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'agent_runs'"
            )
            row = cur.fetchone()
            if row and row[0] > 0:
                # Existing instance — seed all current migrations as applied
                from datetime import UTC, datetime

                entries = [
                    {"file": m.name, "applied_at": datetime.now(UTC).isoformat()}
                    for m in all_migrations
                ]
                _save_applied(entries)
                return [m.name for m in all_migrations]
    except Exception:
        logger.debug("Could not check database state for migration seeding", exc_info=True)

    return applied


def _pull_latest(dry_run: bool) -> bool:
    """Pull latest from remote. Returns True if successful."""
    workspace = _workspace()
    if not (workspace / ".git").is_dir():
        print("  Not a git repository — skipping pull.")
        return True

    if dry_run:
        result = subprocess.run(
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


def _apply_migration(path: Path, dry_run: bool) -> bool:
    """Apply a single SQL migration. Returns True on success."""
    if dry_run:
        print(f"  Would apply: {path.name}")
        return True

    try:
        from robothor.db.connection import get_connection

        sql = path.read_text()
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            conn.commit()
        print(f"  Applied: {path.name}")
        return True
    except Exception as e:
        logger.error("Migration %s failed: %s", path.name, e)
        print(f"  FAILED: {path.name} — {e}")
        return False


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
    """Load full upgrade state (migrations + template hashes)."""
    state = _state_file()
    if not state.exists():
        return {}
    return yaml.safe_load(state.read_text()) or {}


def _save_state(data: dict[str, Any]) -> None:
    """Write full upgrade state."""
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
    skip_pull = getattr(args, "skip_pull", False)
    skip_migrations = getattr(args, "skip_migrations", False)

    import robothor

    print(f"Genus OS v{robothor.__version__}")
    print()

    # 1. Pull latest
    if not skip_pull:
        print("Pulling latest platform code...")
        if not _pull_latest(dry_run):
            return 1
    else:
        print("Skipping pull (--skip-pull)")
    print()

    # 2. Migrations
    if not skip_migrations:
        print("Checking migrations...")
        all_migrations = _discover_migrations()
        applied = _load_applied()
        applied = _seed_tracking_if_needed(applied, all_migrations)

        new_migrations = [m for m in all_migrations if m.name not in applied]
        if new_migrations:
            print(f"  {len(new_migrations)} new migration(s):")
            from datetime import UTC, datetime

            state_data = _load_state()
            entries = state_data.get("migrations", [])
            for m in new_migrations:
                if not _apply_migration(m, dry_run):
                    return 1
                if not dry_run:
                    entries.append({"file": m.name, "applied_at": datetime.now(UTC).isoformat()})
                    state_data["migrations"] = entries
                    _save_state(state_data)
        else:
            print("  All migrations already applied.")
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

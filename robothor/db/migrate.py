"""
Lightweight migration runner for ``crm/migrations/*.sql``.

Usage:
    python -m robothor.db.migrate status        # show applied vs pending
    python -m robothor.db.migrate apply         # apply all pending
    python -m robothor.db.migrate apply 018     # apply specific version
    python -m robothor.db.migrate apply --dry-run

No framework dependency — just SQL files, SHA-256 checksums, and transactions.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection

# Default migration directory (can be overridden for tests)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"

# Pattern: 001_name.sql, 015b_name.sql, etc.
_MIGRATION_RE = re.compile(r"^(\d+[a-z]?)_.+\.sql$")


def _discover(migrations_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return sorted list of (version, path) for all .sql files."""
    d = migrations_dir or MIGRATIONS_DIR
    results: list[tuple[str, Path]] = []
    for f in sorted(d.glob("*.sql")):
        m = _MIGRATION_RE.match(f.name)
        if m:
            results.append((m.group(1), f))
    return results


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_table(conn: Any) -> None:
    """Create schema_migrations table if it doesn't exist."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checksum    TEXT
        )
    """)
    conn.commit()


def _applied(conn: Any) -> dict[str, dict[str, Any]]:
    """Return {version: {filename, applied_at, checksum}} for all applied migrations."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT version, filename, applied_at, checksum FROM schema_migrations ORDER BY version"
    )
    return {r["version"]: dict(r) for r in cur.fetchall()}


def status(migrations_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return list of dicts with version, filename, status, checksum info."""
    all_files = _discover(migrations_dir)
    with get_connection() as conn:
        _ensure_table(conn)
        applied = _applied(conn)

    rows: list[dict[str, Any]] = []
    for version, path in all_files:
        file_checksum = _sha256(path)
        if version in applied:
            db_checksum = applied[version].get("checksum")
            drift = db_checksum and db_checksum != file_checksum
            rows.append(
                {
                    "version": version,
                    "filename": path.name,
                    "status": "DRIFT" if drift else "applied",
                    "applied_at": applied[version]["applied_at"],
                }
            )
        else:
            rows.append(
                {
                    "version": version,
                    "filename": path.name,
                    "status": "pending",
                    "applied_at": None,
                }
            )
    return rows


def apply(
    version: str | None = None,
    dry_run: bool = False,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply pending migrations. Returns list of applied version strings."""
    all_files = _discover(migrations_dir)

    with get_connection() as conn:
        _ensure_table(conn)
        applied = _applied(conn)

        to_apply: list[tuple[str, Path]] = []
        for v, path in all_files:
            if v in applied:
                continue
            if version and v != version:
                continue
            to_apply.append((v, path))

        if not to_apply:
            print("Nothing to apply.")
            return []

        applied_versions: list[str] = []
        for v, path in to_apply:
            checksum = _sha256(path)
            if dry_run:
                print(f"[dry-run] Would apply {path.name} (version {v})")
                applied_versions.append(v)
                continue

            sql = path.read_text()
            cur = conn.cursor()
            try:
                # Execute migration SQL
                cur.execute(sql)
                # Record it
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) VALUES (%s, %s, %s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (v, path.name, checksum),
                )
                conn.commit()
                print(f"Applied {path.name} (version {v})")
                applied_versions.append(v)
            except Exception as e:
                conn.rollback()
                print(f"FAILED {path.name}: {e}", file=sys.stderr)
                raise

        return applied_versions


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]
    if not args or args[0] == "status":
        rows = status()
        if not rows:
            print("No migration files found.")
            return
        # Column widths
        print(f"{'Version':<10} {'Filename':<45} {'Status':<10} {'Applied At'}")
        print("-" * 90)
        for r in rows:
            at = str(r["applied_at"])[:19] if r["applied_at"] else ""
            print(f"{r['version']:<10} {r['filename']:<45} {r['status']:<10} {at}")

    elif args[0] == "apply":
        version = None
        dry_run = "--dry-run" in args
        # Check for a version argument
        for a in args[1:]:
            if a != "--dry-run":
                version = a
                break
        apply(version=version, dry_run=dry_run)

    else:
        print(f"Unknown command: {args[0]}", file=sys.stderr)
        print("Usage: python -m robothor.db.migrate [status|apply [VERSION] [--dry-run]]")
        sys.exit(1)


if __name__ == "__main__":
    main()

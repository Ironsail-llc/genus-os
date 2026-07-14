"""Canonical PostgreSQL schema migration runner.

The repository historically had two incompatible migration paths: the public
CLI applied only ``infra/migrations/001_init.sql`` while this module applied
only ``crm/migrations`` and keyed history by a non-unique numeric prefix.  This
runner is the single source of truth for both paths.

Migration identity is the complete filename stem (for example,
``071_memory_vault``), never the numeric display version.  The full stem is
immutable and globally unique across the canonical manifest; the SHA-256
checksum makes an edited, already-applied migration a hard failure.

Usage::

    python -m robothor.db.migrate status
    python -m robothor.db.migrate apply
    python -m robothor.db.migrate apply 071_memory_vault
    python -m robothor.db.migrate apply --dry-run
"""

from __future__ import annotations

import hashlib
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from robothor.db.connection import get_connection

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_MIGRATIONS_DIR = _PACKAGE_ROOT / "migrations"
_MIGRATION_MANIFEST = _PACKAGE_MIGRATIONS_DIR / "manifest.txt"

# Backward-compatible constant used by callers that explicitly override the
# old CRM-only directory.  Default discovery does *not* use this constant; it
# reads the canonical manifest and includes both schema roots.
MIGRATIONS_DIR = (
    _PACKAGE_MIGRATIONS_DIR / "crm"
    if (_PACKAGE_MIGRATIONS_DIR / "crm").is_dir()
    else _REPO_ROOT / "crm" / "migrations"
)

_HISTORY_TABLE = "schema_migrations_v2"
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"genusos:canonical-schema-migrations:v2").digest()[:8],
    byteorder="big",
    signed=True,
)

# Pattern: 001_name.sql, 015b_name.sql, etc.  The prefix remains useful for
# display and ordering but is deliberately not an identity key.
_MIGRATION_RE = re.compile(r"^(\d+)([a-z]?)_(.+)\.sql$")


class MigrationError(RuntimeError):
    """Base class for migration safety failures."""


class MigrationDiscoveryError(MigrationError):
    """The canonical manifest or a migration file is invalid."""


class MigrationSelectionError(MigrationError):
    """A requested migration selector is missing or ambiguous."""


class MigrationDriftError(MigrationError):
    """An applied migration no longer matches its immutable source file."""


class MigrationHistoryError(MigrationError):
    """Stored migration history cannot be reconciled safely."""


@dataclass(frozen=True)
class Migration:
    """A discovered immutable SQL migration."""

    migration_id: str
    version: str
    filename: str
    source: str
    path: Path
    checksum: str
    order: tuple[int, int, int, str]


@dataclass(frozen=True)
class AppliedMigration:
    """A row from the canonical migration ledger."""

    migration_id: str
    version: str
    filename: str
    source: str
    applied_at: Any
    checksum: str
    reconciled_from_legacy: bool


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_from_path(path: Path, source: str) -> Migration:
    match = _MIGRATION_RE.fullmatch(path.name)
    if match is None:
        raise MigrationDiscoveryError(
            f"Invalid migration filename {path.name!r}; expected NNN[_suffix]_name.sql"
        )

    number, suffix, _name = match.groups()
    version = f"{number}{suffix}"
    # Empty suffix sorts before a letter suffix (015 before 015b).  The initial
    # snapshot must precede the historical CRM 001 migration on a fresh DB.
    suffix_order = 0 if not suffix else ord(suffix) - ord("a") + 1
    baseline_order = 0 if path.name == "001_init.sql" else 1
    return Migration(
        migration_id=path.stem,
        version=version,
        filename=path.name,
        source=source,
        path=path,
        checksum=_sha256(path),
        order=(int(number), suffix_order, baseline_order, path.name),
    )


def _manifest_paths() -> list[tuple[str, Path]]:
    if not _MIGRATION_MANIFEST.is_file():
        raise MigrationDiscoveryError(
            f"Canonical migration manifest is missing: {_MIGRATION_MANIFEST}"
        )

    paths: list[tuple[str, Path]] = []
    for line_number, raw_line in enumerate(
        _MIGRATION_MANIFEST.read_text(encoding="utf-8").splitlines(), start=1
    ):
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = entry.split("/", 1)
        if len(parts) != 2 or parts[0] not in {"infra", "crm"}:
            raise MigrationDiscoveryError(
                f"Invalid manifest entry on line {line_number}: {entry!r}"
            )
        source, filename = parts
        if Path(filename).name != filename:
            raise MigrationDiscoveryError(f"Migration manifest entry must be a filename: {entry!r}")

        bundled = _PACKAGE_MIGRATIONS_DIR / source / filename
        development = _REPO_ROOT / source / "migrations" / filename
        path = bundled if bundled.is_file() else development
        if not path.is_file():
            raise MigrationDiscoveryError(f"Migration listed in manifest is missing: {entry}")
        paths.append((source, path))
    return paths


def _discover(migrations_dir: Path | None = None) -> list[Migration]:
    """Discover the ordered migration chain and reject identity collisions.

    Passing ``migrations_dir`` is intended for tests and controlled tooling;
    default production discovery is manifest-based so ignored, local, or
    instance-specific SQL files can never be applied accidentally.
    """

    if migrations_dir is None:
        candidates = _manifest_paths()
    else:
        directory = Path(migrations_dir)
        if not directory.is_dir():
            raise MigrationDiscoveryError(f"Migration directory does not exist: {directory}")
        candidates = [(directory.name, path) for path in directory.glob("*.sql")]

    migrations = [_migration_from_path(path, source) for source, path in candidates]
    migrations.sort(key=lambda migration: migration.order)

    seen_ids: dict[str, Path] = {}
    seen_filenames: dict[str, Path] = {}
    for migration in migrations:
        prior_id = seen_ids.get(migration.migration_id)
        if prior_id is not None:
            raise MigrationDiscoveryError(
                f"Duplicate immutable migration id {migration.migration_id!r}: "
                f"{prior_id} and {migration.path}"
            )
        prior_filename = seen_filenames.get(migration.filename)
        if prior_filename is not None:
            raise MigrationDiscoveryError(
                f"Duplicate migration filename {migration.filename!r}: "
                f"{prior_filename} and {migration.path}"
            )
        seen_ids[migration.migration_id] = migration.path
        seen_filenames[migration.filename] = migration.path

    if not migrations:
        raise MigrationDiscoveryError("No migration files found")
    return migrations


def _strip_outer_transaction(sql: str) -> str:
    """Remove a file's outer BEGIN/COMMIT so the runner owns atomicity.

    Historical files commonly contain their own wrapper.  Leaving it in place
    would commit the SQL before the history row is written.  Only standalone
    first/last statements are removed; PL/pgSQL ``BEGIN`` blocks are untouched.
    """

    lines = sql.splitlines(keepends=True)
    meaningful = [
        index
        for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("--")
    ]
    if (
        meaningful
        and lines[meaningful[0]].strip().upper() == "BEGIN;"
        and lines[meaningful[-1]].strip().upper() == "COMMIT;"
    ):
        del lines[meaningful[-1]]
        del lines[meaningful[0]]
    return "".join(lines)


@contextmanager
def _connection(connection: Any | None) -> Iterator[Any]:
    if connection is not None:
        yield connection
        return
    with get_connection() as pooled_connection:
        yield pooled_connection


@contextmanager
def _advisory_lock(conn: Any) -> Iterator[None]:
    """Hold a session-level PostgreSQL advisory lock for the whole run."""

    cursor = conn.cursor()
    cursor.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    # Session locks survive commits.  End the lock-acquisition transaction so
    # each migration can own a clean transaction.
    conn.commit()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            # Clear an aborted transaction before issuing the unlock query.
            conn.rollback()
            unlock_cursor = conn.cursor()
            unlock_cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            result = unlock_cursor.fetchone()
            conn.commit()
            if result is not None and not result[0]:
                raise MigrationError("PostgreSQL migration advisory lock was not held")
        except Exception:
            if not failed:
                raise


def _ensure_history_table(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {_HISTORY_TABLE} (
            migration_id            TEXT PRIMARY KEY,
            version                 TEXT NOT NULL,
            filename                TEXT NOT NULL UNIQUE,
            source                  TEXT NOT NULL,
            applied_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checksum                TEXT NOT NULL CHECK (length(checksum) = 64),
            reconciled_from_legacy  BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)


def _insert_history(
    conn: Any,
    migration: Migration,
    *,
    applied_at: Any = None,
    reconciled_from_legacy: bool = False,
) -> None:
    cursor = conn.cursor()
    cursor.execute(
        f"""
        INSERT INTO {_HISTORY_TABLE} (
            migration_id, version, filename, source, applied_at,
            checksum, reconciled_from_legacy
        )
        VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s)
        ON CONFLICT (migration_id) DO NOTHING
        """,
        (
            migration.migration_id,
            migration.version,
            migration.filename,
            migration.source,
            applied_at,
            migration.checksum,
            reconciled_from_legacy,
        ),
    )


def _reconcile_legacy_history(conn: Any, migrations: list[Migration]) -> None:
    """Adopt the old numeric-key ledger without conflating duplicate IDs.

    Exact historical filenames take precedence.  Thus an old ``071`` row for
    ``071_memory_vault.sql`` adopts only that migration and leaves
    ``071_user_accounts.sql`` pending.  A filename-less duplicate numeric key
    is rejected unless its checksum identifies exactly one file.
    """

    cursor = conn.cursor()
    cursor.execute("SELECT to_regclass(%s)", ("public.schema_migrations",))
    relation = cursor.fetchone()
    if not relation or relation[0] is None:
        return

    cursor.execute(
        "SELECT version, filename, applied_at, checksum "
        "FROM schema_migrations ORDER BY applied_at, version"
    )
    legacy_rows = cursor.fetchall()
    by_filename = {migration.filename: migration for migration in migrations}
    by_version: dict[str, list[Migration]] = {}
    by_checksum: dict[str, list[Migration]] = {}
    for discovered_migration in migrations:
        by_version.setdefault(discovered_migration.version, []).append(discovered_migration)
        by_checksum.setdefault(discovered_migration.checksum, []).append(discovered_migration)

    for legacy_version, legacy_filename, applied_at, legacy_checksum in legacy_rows:
        filename = Path(str(legacy_filename)).name if legacy_filename else ""
        migration: Migration | None = by_filename.get(filename)

        if migration is None and legacy_checksum:
            checksum_matches = by_checksum.get(str(legacy_checksum), [])
            if len(checksum_matches) == 1:
                migration = checksum_matches[0]

        if migration is None:
            version_matches = by_version.get(str(legacy_version), [])
            if len(version_matches) == 1:
                migration = version_matches[0]
            elif len(version_matches) > 1:
                choices = ", ".join(item.filename for item in version_matches)
                raise MigrationHistoryError(
                    f"Legacy migration version {legacy_version!r} is ambiguous "
                    f"without an exact filename/checksum; candidates: {choices}"
                )
            else:
                raise MigrationHistoryError(
                    f"Legacy migration {legacy_version!r}/{filename!r} is not present "
                    "in the canonical manifest"
                )

        if legacy_checksum and str(legacy_checksum) != migration.checksum:
            raise MigrationDriftError(
                f"Legacy migration drift for {migration.migration_id}: "
                f"database={legacy_checksum}, file={migration.checksum}"
            )

        _insert_history(
            conn,
            migration,
            applied_at=applied_at,
            reconciled_from_legacy=True,
        )


def _prepare_history(conn: Any, migrations: list[Migration]) -> None:
    _ensure_history_table(conn)
    _reconcile_legacy_history(conn, migrations)
    conn.commit()


def _applied(conn: Any) -> dict[str, AppliedMigration]:
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT migration_id, version, filename, source, applied_at, checksum,
               reconciled_from_legacy
        FROM {_HISTORY_TABLE}
        ORDER BY applied_at, migration_id
        """
    )
    return {
        row[0]: AppliedMigration(
            migration_id=row[0],
            version=row[1],
            filename=row[2],
            source=row[3],
            applied_at=row[4],
            checksum=row[5],
            reconciled_from_legacy=bool(row[6]),
        )
        for row in cursor.fetchall()
    }


def _validate_history(migrations: list[Migration], applied: dict[str, AppliedMigration]) -> None:
    discovered = {migration.migration_id: migration for migration in migrations}
    for migration_id, record in applied.items():
        migration = discovered.get(migration_id)
        if migration is None:
            raise MigrationHistoryError(
                f"Applied migration {migration_id!r} is missing from the canonical manifest"
            )
        mismatches: list[str] = []
        if record.filename != migration.filename:
            mismatches.append(f"filename database={record.filename!r} file={migration.filename!r}")
        if record.version != migration.version:
            mismatches.append(f"version database={record.version!r} file={migration.version!r}")
        if record.source != migration.source:
            mismatches.append(f"source database={record.source!r} file={migration.source!r}")
        if record.checksum != migration.checksum:
            mismatches.append(f"checksum database={record.checksum!r} file={migration.checksum!r}")
        if mismatches:
            raise MigrationDriftError(
                f"Applied migration {migration_id!r} has drifted: " + "; ".join(mismatches)
            )


def _select_migrations(migrations: list[Migration], selector: str | None) -> list[Migration]:
    if selector is None:
        return migrations

    exact = [
        migration
        for migration in migrations
        if selector in {migration.migration_id, migration.filename}
    ]
    if exact:
        return exact

    version_matches = [migration for migration in migrations if migration.version == selector]
    if len(version_matches) == 1:
        return version_matches
    if len(version_matches) > 1:
        choices = ", ".join(migration.migration_id for migration in version_matches)
        raise MigrationSelectionError(
            f"Migration version {selector!r} is ambiguous; use one of: {choices}"
        )
    raise MigrationSelectionError(f"Unknown migration selector: {selector!r}")


def status(
    migrations_dir: Path | None = None,
    *,
    connection: Any | None = None,
) -> list[dict[str, Any]]:
    """Return canonical migration status, including drift/missing history."""

    migrations = _discover(migrations_dir)
    with _connection(connection) as conn, _advisory_lock(conn):
        _prepare_history(conn, migrations)
        applied = _applied(conn)

    rows: list[dict[str, Any]] = []
    discovered_ids = {migration.migration_id for migration in migrations}
    for migration in migrations:
        record = applied.get(migration.migration_id)
        row_status = "pending"
        applied_at = None
        if record is not None:
            applied_at = record.applied_at
            row_status = "applied"
            if (
                record.filename != migration.filename
                or record.version != migration.version
                or record.source != migration.source
                or record.checksum != migration.checksum
            ):
                row_status = "DRIFT"
        rows.append(
            {
                "migration_id": migration.migration_id,
                "version": migration.version,
                "filename": migration.filename,
                "source": migration.source,
                "status": row_status,
                "applied_at": applied_at,
            }
        )

    for migration_id, record in applied.items():
        if migration_id not in discovered_ids:
            rows.append(
                {
                    "migration_id": migration_id,
                    "version": record.version,
                    "filename": record.filename,
                    "source": record.source,
                    "status": "MISSING",
                    "applied_at": record.applied_at,
                }
            )
    return rows


def apply(
    version: str | None = None,
    dry_run: bool = False,
    migrations_dir: Path | None = None,
    *,
    connection: Any | None = None,
) -> list[str]:
    """Apply selected pending migrations and return their immutable IDs.

    ``version`` is retained for API compatibility, but accepts the preferred
    full migration ID or filename.  A numeric prefix works only when unique.
    """

    migrations = _discover(migrations_dir)
    selected = _select_migrations(migrations, version)

    if dry_run:
        for migration in selected:
            print(
                f"-- {migration.migration_id} "
                f"({migration.source}/{migration.filename}, sha256={migration.checksum})"
            )
            print(_strip_outer_transaction(migration.path.read_text(encoding="utf-8")))
        return [migration.migration_id for migration in selected]

    with _connection(connection) as conn, _advisory_lock(conn):
        _prepare_history(conn, migrations)
        applied = _applied(conn)
        _validate_history(migrations, applied)
        to_apply = [migration for migration in selected if migration.migration_id not in applied]
        if not to_apply:
            print("Nothing to apply.")
            return []

        applied_ids: list[str] = []
        for migration in to_apply:
            sql = _strip_outer_transaction(migration.path.read_text(encoding="utf-8"))
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                _insert_history(conn, migration)
                conn.commit()
            except Exception as error:
                conn.rollback()
                print(f"FAILED {migration.filename}: {error}", file=sys.stderr)
                raise
            print(f"Applied {migration.filename} ({migration.migration_id})")
            applied_ids.append(migration.migration_id)
        return applied_ids


def main() -> None:
    """Module CLI entry point."""

    args = sys.argv[1:]
    try:
        if not args or args[0] == "status":
            rows = status()
            print(f"{'Migration ID':<38} {'Source':<8} {'Status':<10} {'Applied At'}")
            print("-" * 90)
            for row in rows:
                applied_at = str(row["applied_at"])[:19] if row["applied_at"] else ""
                print(
                    f"{row['migration_id']:<38} {row['source']:<8} {row['status']:<10} {applied_at}"
                )
        elif args[0] == "apply":
            selector = next((arg for arg in args[1:] if arg != "--dry-run"), None)
            apply(version=selector, dry_run="--dry-run" in args)
        else:
            print(f"Unknown command: {args[0]}", file=sys.stderr)
            print("Usage: python -m robothor.db.migrate [status|apply [ID] [--dry-run]]")
            raise SystemExit(1)
    except MigrationError as error:
        print(f"Migration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

"""Versioned, encrypted snapshots for Genus OS disaster recovery.

The snapshot format is intentionally small and inspectable:

* ``manifest.json`` records the instance, application, schema, and artifacts.
* ``database.dump`` is a PostgreSQL custom-format dump.
* ``workspace.tar.gz`` contains only the configured, instance-owned state.

The outer archive may be protected with streaming AES-256-GCM encryption.  A
vault master key is never included unless the caller explicitly requests it,
and a secret-bearing snapshot can never be written without encryption.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO

from robothor import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable

    from robothor.config import DatabaseConfig

SNAPSHOT_FORMAT = "genusos-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
SNAPSHOT_PREFIX = "genusos-snapshot-"
ENCRYPTED_SUFFIX = ".gss"
PLAINTEXT_SUFFIX = ".tar.gz"

_MAGIC = b"GENUSSNAP\x01\n"
_HEADER_LENGTH_BYTES = 4
_RECORD_LENGTH_BYTES = 4
_GCM_TAG_BYTES = 16
_CHUNK_SIZE = 1024 * 1024
_NONCE_PREFIX_BYTES = 8
_KDF_N = 2**15
_KDF_R = 8
_KDF_P = 1
_MIN_PASSPHRASE_LENGTH = 12
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_NAME_RE = re.compile(
    rf"^{SNAPSHOT_PREFIX}.+-(?P<timestamp>\d{{8}}T\d{{6}}Z)"
    rf"(?P<suffix>{re.escape(ENCRYPTED_SUFFIX)}|{re.escape(PLAINTEXT_SUFFIX)})$"
)

_WORKSPACE_PATHS = (
    ".robothor/owner.yaml",
    ".robothor/installed.yaml",
    ".robothor/federation.yaml",
    ".robothor/config.yaml",
    ".robothor/init_state.yaml",
    ".robothor/migrations_applied.yaml",
    ".robothor/autodream_last_run",
    ".robothor/overrides",
    ".robothor/archive",
    "docs/agents",
    "docs/workflows",
    "docs/hooks",
    "docs/webhooks.yaml",
    "agents/skills",
    "brain",
)
_WORKSPACE_DIRECTORY_PATHS = {
    "docs/agents",
    "docs/workflows",
    "docs/hooks",
    "agents/skills",
    "brain",
    ".robothor/overrides",
    ".robothor/archive",
}
_CONFIGURED_WORKSPACE_DIRECTORIES = {
    "ROBOTHOR_MANIFEST_DIR": "docs/agents",
    "ROBOTHOR_WORKFLOW_DIR": "docs/workflows",
}
_VAULT_KEY_PATH = ".vault-key"
_FEDERATION_IDENTITY_PATH = ".robothor/identity.json"
_FEDERATION_KEY_PATH = ".robothor/identity.key"
_SECRET_WORKSPACE_PATHS = (_VAULT_KEY_PATH, _FEDERATION_KEY_PATH)
_SECRET_GATED_WORKSPACE_PATHS = (
    _VAULT_KEY_PATH,
    _FEDERATION_IDENTITY_PATH,
    _FEDERATION_KEY_PATH,
)


class SnapshotError(RuntimeError):
    """Base class for snapshot failures safe to show to an operator."""


class SnapshotIntegrityError(SnapshotError):
    """A snapshot is malformed, unauthenticated, or fails a checksum."""


class SnapshotCompatibilityError(SnapshotError):
    """A valid snapshot cannot safely be restored by this installation."""


class SnapshotConflictError(SnapshotError):
    """A create or restore operation would overwrite existing data."""


@dataclass(frozen=True)
class VerificationResult:
    """Integrity and compatibility result for a snapshot."""

    path: Path
    encrypted: bool
    manifest: dict[str, Any]
    compatible: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RestoreResult:
    """Result of a restore plan or execution."""

    verification: VerificationResult
    dry_run: bool
    database_restored: bool
    workspace_files_restored: int
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotEntry:
    """Filesystem metadata for an automatically managed snapshot."""

    path: Path
    size: int
    created_at: datetime
    encrypted: bool


@dataclass(frozen=True)
class PruneResult:
    """Snapshots selected and optionally deleted by retention policy."""

    candidates: tuple[SnapshotEntry, ...]
    deleted: tuple[Path, ...]
    dry_run: bool


class _HashingReader:
    """Minimal file-like wrapper that hashes exactly what tarfile reads."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self._digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        self._digest.update(data)
        return data

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if not raw_path or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise SnapshotIntegrityError(f"Unsafe snapshot path: {raw_path!r}")
    if path.as_posix() != raw_path or "\\" in raw_path:
        raise SnapshotIntegrityError(f"Non-canonical snapshot path: {raw_path!r}")
    return path


def _secure_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise SnapshotError(f"Snapshot directory is not a real directory: {path}")
    # Do not silently change an operator-managed parent such as /var/backups.
    # Directories created by this process are private from their first use.
    if not existed:
        path.chmod(stat.S_IRWXU)


def _staging_parent() -> Path | None:
    configured = os.environ.get("GENUS_SNAPSHOT_STAGING_DIR")
    if not configured:
        return None
    path = Path(configured).expanduser()
    if path.is_symlink() or not path.is_dir():
        raise SnapshotError(f"GENUS_SNAPSHOT_STAGING_DIR is not a real directory: {path}")
    return path


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_atomic(source: Path, destination: Path, *, force: bool) -> None:
    """Publish ``source`` without a check-then-overwrite race."""

    source.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _fsync_file(source)
    if force:
        source.replace(destination)
    else:
        try:
            # A hard link is an atomic, same-filesystem no-clobber publish.
            os.link(source, destination)
        except FileExistsError as error:
            raise SnapshotConflictError(f"Snapshot already exists: {destination}") from error
        source.unlink()
    _fsync_directory(destination.parent)


def _password_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < _MIN_PASSPHRASE_LENGTH:
        raise SnapshotError(
            f"Snapshot passphrase must be at least {_MIN_PASSPHRASE_LENGTH} characters"
        )
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=_KDF_N, r=_KDF_R, p=_KDF_P).derive(
        passphrase.encode("utf-8")
    )


def _encrypt_archive(source: Path, destination: Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(16)
    nonce_prefix = os.urandom(_NONCE_PREFIX_BYTES)
    plaintext_size = source.stat().st_size
    header = json.dumps(
        {
            "cipher": "AES-256-GCM-CHUNKED",
            "kdf": "scrypt",
            "n": _KDF_N,
            "r": _KDF_R,
            "p": _KDF_P,
            "chunk_size": _CHUNK_SIZE,
            "plaintext_size": plaintext_size,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce_prefix": base64.b64encode(nonce_prefix).decode("ascii"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header) > 65535:
        raise SnapshotError("Encryption header is unexpectedly large")

    key = _password_key(passphrase, salt)
    aesgcm = AESGCM(key)
    total = 0
    counter = 0
    try:
        with source.open("rb") as plain, destination.open("xb") as encrypted:
            encrypted.write(_MAGIC)
            encrypted.write(len(header).to_bytes(_HEADER_LENGTH_BYTES, "big"))
            encrypted.write(header)
            while chunk := plain.read(_CHUNK_SIZE):
                if counter >= 2**32:
                    raise SnapshotError("Snapshot is too large for the encryption format")
                total += len(chunk)
                if total > plaintext_size:
                    raise SnapshotError("Snapshot archive changed while being encrypted")
                counter_bytes = counter.to_bytes(4, "big")
                nonce = nonce_prefix + counter_bytes
                associated_data = _MAGIC + header + counter_bytes
                ciphertext = aesgcm.encrypt(nonce, chunk, associated_data)
                encrypted.write(len(ciphertext).to_bytes(_RECORD_LENGTH_BYTES, "big"))
                encrypted.write(ciphertext)
                counter += 1
            if total != plaintext_size:
                raise SnapshotError("Snapshot archive changed while being encrypted")
            encrypted.flush()
            os.fsync(encrypted.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _read_encryption_header(source: BinaryIO) -> tuple[dict[str, Any], bytes]:
    magic = source.read(len(_MAGIC))
    if magic != _MAGIC:
        raise SnapshotIntegrityError("Not an encrypted Genus OS snapshot")
    raw_length = source.read(_HEADER_LENGTH_BYTES)
    if len(raw_length) != _HEADER_LENGTH_BYTES:
        raise SnapshotIntegrityError("Truncated snapshot encryption header")
    header_length = int.from_bytes(raw_length, "big")
    if not 1 <= header_length <= 65535:
        raise SnapshotIntegrityError("Invalid snapshot encryption header length")
    raw_header = source.read(header_length)
    if len(raw_header) != header_length:
        raise SnapshotIntegrityError("Truncated snapshot encryption header")
    try:
        header = json.loads(raw_header)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotIntegrityError("Invalid snapshot encryption header") from error
    if not isinstance(header, dict):
        raise SnapshotIntegrityError("Invalid snapshot encryption header")
    return header, raw_header


def _header_bytes(header: dict[str, Any], name: str, expected_length: int) -> bytes:
    value = header.get(name)
    if not isinstance(value, str):
        raise SnapshotIntegrityError(f"Missing encryption header field: {name}")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise SnapshotIntegrityError(f"Invalid encryption header field: {name}") from error
    if len(decoded) != expected_length:
        raise SnapshotIntegrityError(f"Invalid encryption header field: {name}")
    return decoded


def _decrypt_archive(source_path: Path, destination: Path, passphrase: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    with source_path.open("rb") as encrypted:
        header, raw_header = _read_encryption_header(encrypted)
        if (
            header.get("cipher") != "AES-256-GCM-CHUNKED"
            or header.get("kdf") != "scrypt"
            or header.get("n") != _KDF_N
            or header.get("r") != _KDF_R
            or header.get("p") != _KDF_P
            or header.get("chunk_size") != _CHUNK_SIZE
        ):
            raise SnapshotIntegrityError("Unsupported snapshot encryption parameters")
        salt = _header_bytes(header, "salt", 16)
        nonce_prefix = _header_bytes(header, "nonce_prefix", _NONCE_PREFIX_BYTES)
        plaintext_size = header.get("plaintext_size")
        if type(plaintext_size) is not int or plaintext_size <= 0:
            raise SnapshotIntegrityError("Invalid encrypted snapshot plaintext size")
        if plaintext_size > _CHUNK_SIZE * 2**32:
            raise SnapshotIntegrityError("Encrypted snapshot exceeds the format size limit")

        key = _password_key(passphrase, salt)
        aesgcm = AESGCM(key)
        remaining = plaintext_size
        counter = 0
        try:
            with destination.open("xb") as plain:
                while remaining:
                    raw_length = encrypted.read(_RECORD_LENGTH_BYTES)
                    if len(raw_length) != _RECORD_LENGTH_BYTES:
                        raise SnapshotIntegrityError("Encrypted snapshot payload is truncated")
                    ciphertext_length = int.from_bytes(raw_length, "big")
                    expected_plaintext_length = min(_CHUNK_SIZE, remaining)
                    if ciphertext_length != expected_plaintext_length + _GCM_TAG_BYTES:
                        raise SnapshotIntegrityError("Invalid encrypted snapshot chunk length")
                    ciphertext = encrypted.read(ciphertext_length)
                    if len(ciphertext) != ciphertext_length:
                        raise SnapshotIntegrityError("Encrypted snapshot payload is truncated")
                    counter_bytes = counter.to_bytes(4, "big")
                    nonce = nonce_prefix + counter_bytes
                    associated_data = _MAGIC + raw_header + counter_bytes
                    chunk = aesgcm.decrypt(nonce, ciphertext, associated_data)
                    if len(chunk) != expected_plaintext_length:
                        raise SnapshotIntegrityError("Invalid encrypted snapshot plaintext length")
                    plain.write(chunk)
                    remaining -= len(chunk)
                    counter += 1
                if encrypted.read(1):
                    raise SnapshotIntegrityError("Encrypted snapshot has trailing data")
                plain.flush()
                os.fsync(plain.fileno())
        except InvalidTag as error:
            destination.unlink(missing_ok=True)
            raise SnapshotIntegrityError(
                "Snapshot authentication failed (wrong passphrase or corrupted data)"
            ) from error
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def _is_encrypted(path: Path) -> bool:
    with path.open("rb") as source:
        return source.read(len(_MAGIC)) == _MAGIC


def _connection_args(db: DatabaseConfig) -> list[str]:
    args: list[str] = []
    if db.host:
        args.extend(["--host", db.host])
    args.extend(["--port", str(db.port)])
    if db.user:
        args.extend(["--username", db.user])
    args.extend(["--dbname", db.name])
    return args


def _postgres_env(db: DatabaseConfig) -> dict[str, str]:
    # Do not expose unrelated provider/API credentials to PostgreSQL child
    # processes. Preserve only execution/locale settings and libpq/Kerberos
    # configuration needed for authenticated database connections.
    allowed = {
        "HOME",
        "LANG",
        "PATH",
        "SYSTEMROOT",
        "TMPDIR",
        "TZ",
        "KRB5CCNAME",
        "KRB5_CONFIG",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_") or key.startswith("PG")
    }
    if db.password:
        environment["PGPASSWORD"] = db.password
    if db.ssl_mode:
        environment["PGSSLMODE"] = db.ssl_mode
    return environment


def _run_checked(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except FileNotFoundError as error:
        raise SnapshotError(f"Required PostgreSQL tool is not installed: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "command failed").strip()
        raise SnapshotError(f"{command[0]} failed: {detail}") from error


def _dump_database(db: DatabaseConfig, destination: Path) -> None:
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(destination),
        *_connection_args(db),
    ]
    _run_checked(command, environment=_postgres_env(db))
    if not destination.is_file() or destination.stat().st_size == 0:
        raise SnapshotError("pg_dump completed without producing a database dump")
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _local_migrations() -> list[dict[str, str]]:
    # The migration runner owns canonical discovery and drift rules.  Importing
    # lazily keeps the snapshot module usable for listing encrypted files even
    # in a minimal operator environment.
    from robothor.db.migrate import MigrationError, _discover

    try:
        return [
            {
                "id": migration.migration_id,
                "checksum": migration.checksum,
                "source": migration.source,
                "filename": migration.filename,
            }
            for migration in _discover()
        ]
    except MigrationError as error:
        raise SnapshotCompatibilityError(
            f"Canonical migration metadata is unavailable: {error}"
        ) from error


def _database_schema_metadata(db: DatabaseConfig) -> dict[str, Any]:
    """Read the canonical migration ledger without changing it."""

    import psycopg2

    connection = None
    try:
        connection = psycopg2.connect(**db.dict, connect_timeout=10)
        connection.set_session(readonly=True, autocommit=True)
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass(%s)", ("public.schema_migrations_v2",))
        relation = cursor.fetchone()
        if not relation or relation[0] is None:
            applied: list[dict[str, str]] = []
            ledger = "missing"
        else:
            cursor.execute(
                "SELECT migration_id, checksum FROM schema_migrations_v2 "
                "ORDER BY applied_at, migration_id"
            )
            applied = [{"id": str(row[0]), "checksum": str(row[1])} for row in cursor.fetchall()]
            ledger = "schema_migrations_v2"
    except Exception as error:
        raise SnapshotError(f"Could not read database schema metadata: {error}") from error
    finally:
        if connection is not None:
            connection.close()

    available = _local_migrations()
    available_by_id = {entry["id"]: entry["checksum"] for entry in available}
    state = "current" if len(applied) == len(available) else "behind"
    if ledger == "missing":
        state = "untracked"
    elif any(available_by_id.get(entry["id"]) != entry["checksum"] for entry in applied):
        state = "drift"
    return {
        "ledger": ledger,
        "state": state,
        "applied": applied,
        "available": available,
    }


def _instance_metadata(workspace: Path) -> dict[str, str]:
    instance_id = os.environ.get("ROBOTHOR_INSTANCE_ID", "").strip()
    instance_name = os.environ.get("ROBOTHOR_INSTANCE_NAME", "").strip()
    config_path = workspace / ".robothor" / "federation.yaml"
    if config_path.is_file() and not config_path.is_symlink():
        try:
            import yaml

            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if isinstance(payload, dict):
                instance_id = instance_id or str(payload.get("instance_id", "")).strip()
                instance_name = instance_name or str(payload.get("instance_name", "")).strip()
        except (OSError, yaml.YAMLError):
            pass
    return {
        "id": instance_id or platform.node() or "unknown",
        "name": instance_name or "Genus OS",
    }


def _workspace_state_paths(workspace: Path) -> tuple[list[str], set[str]]:
    roots = [*_WORKSPACE_PATHS]
    directory_roots = set(_WORKSPACE_DIRECTORY_PATHS)
    workspace_resolved = workspace.resolve(strict=True)
    for environment_name in _CONFIGURED_WORKSPACE_DIRECTORIES:
        configured = os.environ.get(environment_name)
        if not configured:
            continue
        configured_path = Path(configured).expanduser()
        if configured_path.is_symlink() or not configured_path.is_dir():
            raise SnapshotError(
                f"Configured state directory {environment_name} is missing or is a symlink: "
                f"{configured_path}"
            )
        try:
            relative = configured_path.resolve(strict=True).relative_to(workspace_resolved)
        except ValueError as error:
            raise SnapshotError(
                f"Configured state directory {environment_name} must be inside the workspace"
            ) from error
        if not relative.parts:
            raise SnapshotError(
                f"Configured state directory {environment_name} cannot be the workspace root"
            )
        relative_name = PurePosixPath(*relative.parts).as_posix()
        if relative_name not in roots:
            roots.append(relative_name)
        directory_roots.add(relative_name)
    return roots, directory_roots


def _workspace_files(workspace: Path, include_secrets: bool) -> list[tuple[Path, PurePosixPath]]:
    roots, _directory_roots = _workspace_state_paths(workspace)
    if include_secrets:
        key_path = workspace / _VAULT_KEY_PATH
        if not key_path.is_file() or key_path.is_symlink():
            raise SnapshotError(
                f"Vault key requested but {_VAULT_KEY_PATH} is missing or is a symlink"
            )
        key_mode = stat.S_IMODE(key_path.stat().st_mode)
        if key_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SnapshotError(f"Vault key permissions must be 0600 or stricter: {key_path}")
        if key_path.stat().st_size != 32:
            raise SnapshotError(f"Vault key must be exactly 32 bytes: {key_path}")
        roots.append(_VAULT_KEY_PATH)
        identity_metadata = workspace / _FEDERATION_IDENTITY_PATH
        identity_key = workspace / _FEDERATION_KEY_PATH
        if identity_key.exists() or identity_metadata.exists():
            if not identity_key.is_file() or identity_key.is_symlink():
                raise SnapshotError(
                    f"Federation identity key is not a regular file: {identity_key}"
                )
            if not identity_metadata.is_file() or identity_metadata.is_symlink():
                raise SnapshotError(
                    f"Federation identity metadata is not a regular file: {identity_metadata}"
                )
            identity_mode = stat.S_IMODE(identity_key.stat().st_mode)
            if identity_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise SnapshotError(
                    f"Federation identity key permissions must be 0600 or stricter: {identity_key}"
                )
            if identity_key.stat().st_size == 0:
                raise SnapshotError(f"Federation identity key is empty: {identity_key}")
            roots.extend([_FEDERATION_IDENTITY_PATH, _FEDERATION_KEY_PATH])

    selected: dict[str, tuple[Path, PurePosixPath]] = {}
    workspace_resolved = workspace.resolve(strict=True)
    for root_name in roots:
        root = workspace / root_name
        if not root.exists():
            continue
        candidates: Iterable[Path]
        if root.is_symlink():
            raise SnapshotError(f"Refusing to snapshot symlinked workspace state: {root}")
        if root.is_file():
            candidates = (root,)
        elif root.is_dir():
            candidates = sorted(root.rglob("*"))
        else:
            raise SnapshotError(f"Unsupported workspace state entry: {root}")
        for candidate in candidates:
            if candidate.is_symlink():
                raise SnapshotError(f"Refusing to snapshot symlinked workspace state: {candidate}")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise SnapshotError(f"Unsupported workspace state entry: {candidate}")
            try:
                relative = candidate.resolve(strict=True).relative_to(workspace_resolved)
            except ValueError as error:
                raise SnapshotError(
                    f"Workspace file escapes configured root: {candidate}"
                ) from error
            archive_path = PurePosixPath(*relative.parts)
            selected[archive_path.as_posix()] = (candidate, archive_path)
    return [selected[name] for name in sorted(selected)]


def _build_workspace_archive(
    workspace: Path,
    destination: Path,
    *,
    include_secrets: bool,
) -> list[dict[str, Any]]:
    files = _workspace_files(workspace, include_secrets)
    metadata: list[dict[str, Any]] = []
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for source_path, archive_path in files:
            before = source_path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise SnapshotError(f"Workspace state is not a regular file: {source_path}")
            info = archive.gettarinfo(str(source_path), arcname=archive_path.as_posix())
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = stat.S_IMODE(before.st_mode) & 0o777
            with source_path.open("rb") as source:
                hashing_source = _HashingReader(source)
                archive.addfile(info, hashing_source)
            after = source_path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SnapshotError(
                    f"Workspace file changed while being snapshotted: {source_path}"
                )
            metadata.append(
                {
                    "path": archive_path.as_posix(),
                    "size": before.st_size,
                    "sha256": hashing_source.hexdigest,
                    "mode": info.mode,
                }
            )
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return metadata


def _artifact(path: Path, kind: str) -> dict[str, Any]:
    return {
        "path": path.name,
        "kind": kind,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _write_outer_archive(
    destination: Path,
    manifest: dict[str, Any],
    artifacts: list[Path],
) -> None:
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        manifest_info.mode = stat.S_IRUSR | stat.S_IWUSR
        manifest_info.mtime = int(datetime.now(UTC).timestamp())
        import io

        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for artifact_path in artifacts:
            info = archive.gettarinfo(str(artifact_path), arcname=artifact_path.name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = stat.S_IRUSR | stat.S_IWUSR
            with artifact_path.open("rb") as source:
                archive.addfile(info, source)
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _default_output(repository: Path, instance_id: str, encrypted: bool) -> Path:
    safe_instance = re.sub(r"[^A-Za-z0-9_.-]+", "-", instance_id).strip("-.")[:64] or "instance"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = ENCRYPTED_SUFFIX if encrypted else PLAINTEXT_SUFFIX
    return repository / f"{SNAPSHOT_PREFIX}{safe_instance}-{timestamp}{suffix}"


def _destination_overlaps_workspace_state(workspace: Path, destination: Path) -> bool:
    destination_resolved = destination.resolve(strict=False)
    roots, directory_roots = _workspace_state_paths(workspace)
    for relative in (*roots, *_SECRET_GATED_WORKSPACE_PATHS):
        state_path = (workspace / relative).resolve(strict=False)
        if destination_resolved == state_path:
            return True
        if relative in directory_roots and state_path in destination_resolved.parents:
            return True
    return False


def create_snapshot(
    *,
    workspace: Path,
    database: DatabaseConfig,
    repository: Path,
    output: Path | None = None,
    include_database: bool = True,
    include_workspace: bool = True,
    include_secrets: bool = False,
    encrypt: bool = True,
    passphrase: str | None = None,
    force: bool = False,
) -> Path:
    """Create and atomically publish a versioned snapshot."""

    workspace = workspace.expanduser()
    if not workspace.is_dir() or workspace.is_symlink():
        raise SnapshotError(f"Workspace is not a real directory: {workspace}")
    if not include_database and not include_workspace:
        raise SnapshotError("A snapshot must include the database, workspace, or both")
    if include_secrets and not include_workspace:
        raise SnapshotError("Vault key inclusion requires workspace state")
    if include_secrets and not encrypt:
        raise SnapshotError("Secret-bearing snapshots must be encrypted")
    if encrypt and not passphrase:
        raise SnapshotError("Encrypted snapshots require a passphrase")
    if encrypt and passphrase and len(passphrase) < _MIN_PASSPHRASE_LENGTH:
        raise SnapshotError(
            f"Snapshot passphrase must be at least {_MIN_PASSPHRASE_LENGTH} characters"
        )
    if force and output is None:
        raise SnapshotError("--force requires an exact output path")

    repository = repository.expanduser()
    instance = _instance_metadata(workspace)
    if output is None:
        destination = _default_output(repository, instance["id"], encrypt).absolute()
        if destination.parent != repository.absolute():
            raise SnapshotError("Generated snapshot path escaped its repository")
    else:
        destination = output.expanduser().absolute()
    if include_workspace and _destination_overlaps_workspace_state(workspace, destination):
        raise SnapshotError(
            "Snapshot output cannot be placed inside workspace state selected for backup"
        )
    _secure_directory(destination.parent)
    if destination.exists() and not force:
        raise SnapshotConflictError(f"Snapshot already exists: {destination}")

    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    plaintext_parent = _staging_parent() if encrypt else destination.parent
    if (
        include_workspace
        and plaintext_parent is not None
        and _destination_overlaps_workspace_state(
            workspace, plaintext_parent / ".genus-snapshot-staging"
        )
    ):
        raise SnapshotError("Snapshot staging directory cannot be inside selected workspace state")
    with tempfile.TemporaryDirectory(prefix=".genus-snapshot-", dir=plaintext_parent) as temp_name:
        staging = Path(temp_name)
        staging.chmod(stat.S_IRWXU)
        artifact_files: list[Path] = []
        artifact_entries: list[dict[str, Any]] = []
        schema: dict[str, Any]
        if include_database:
            database_dump = staging / "database.dump"
            _dump_database(database, database_dump)
            schema = _database_schema_metadata(database)
            artifact_files.append(database_dump)
            artifact_entries.append(_artifact(database_dump, "postgresql-custom"))
        else:
            schema = {"ledger": "not-captured", "state": "not-captured", "applied": []}

        workspace_files: list[dict[str, Any]] = []
        if include_workspace:
            workspace_archive = staging / "workspace.tar.gz"
            workspace_files = _build_workspace_archive(
                workspace, workspace_archive, include_secrets=include_secrets
            )
            artifact_files.append(workspace_archive)
            artifact_entries.append(_artifact(workspace_archive, "workspace-tar-gzip"))

        manifest: dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "created_at": created_at,
            "application": {"name": "Genus OS", "version": __version__},
            "instance": instance,
            "database": {
                "included": include_database,
                "engine": "postgresql",
                "name": database.name if include_database else None,
                "dump_format": "custom" if include_database else None,
                "schema": schema,
            },
            "workspace": {
                "included": include_workspace,
                "files": workspace_files,
                "contains_vault_key": include_secrets,
            },
            "protection": {
                "encrypted": encrypt,
                "algorithm": "AES-256-GCM" if encrypt else None,
                "secret_bearing": include_secrets,
            },
            "artifacts": artifact_entries,
        }
        outer_archive = staging / "snapshot.tar.gz"
        _write_outer_archive(outer_archive, manifest, artifact_files)

        if encrypt:
            with tempfile.TemporaryDirectory(
                prefix=".genus-snapshot-publish-", dir=destination.parent
            ) as publish_name:
                publish_staging = Path(publish_name)
                publish_staging.chmod(stat.S_IRWXU)
                publishable = publish_staging / "snapshot.gss"
                _encrypt_archive(outer_archive, publishable, passphrase or "")
                _publish_atomic(publishable, destination, force=force)
        else:
            publishable = outer_archive
            try:
                _publish_atomic(publishable, destination, force=force)
            finally:
                publishable.unlink(missing_ok=True)
    return destination


def _materialize_snapshot(
    path: Path, destination: Path, passphrase: str | None
) -> tuple[Path, bool]:
    if not path.is_file() or path.is_symlink():
        raise SnapshotError(f"Snapshot is not a regular file: {path}")
    encrypted = _is_encrypted(path)
    if encrypted:
        if not passphrase:
            raise SnapshotError("Encrypted snapshot requires a passphrase")
        archive_path = destination / "snapshot.tar.gz"
        _decrypt_archive(path, archive_path, passphrase)
        return archive_path, True
    return path, False


def _extract_outer_archive(archive_path: Path, destination: Path) -> dict[str, Any]:
    allowed_names = {"manifest.json", "database.dump", "workspace.tar.gz"}
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.name not in allowed_names or not member.isfile() or member.name in seen:
                    raise SnapshotIntegrityError(
                        f"Unexpected or unsafe outer archive member: {member.name!r}"
                    )
                seen.add(member.name)
                source = archive.extractfile(member)
                if source is None:
                    raise SnapshotIntegrityError(f"Could not read archive member: {member.name}")
                target = destination / member.name
                with target.open("xb") as output:
                    while chunk := source.read(_CHUNK_SIZE):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except (tarfile.TarError, OSError) as error:
        raise SnapshotIntegrityError(f"Invalid snapshot archive: {error}") from error
    if "manifest.json" not in seen:
        raise SnapshotIntegrityError("Snapshot manifest is missing")
    try:
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotIntegrityError("Snapshot manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise SnapshotIntegrityError("Snapshot manifest must be a JSON object")
    return manifest


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotIntegrityError(f"Manifest field {name!r} must be an object")
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotIntegrityError(f"Manifest field {name!r} must be an array")
    return value


def _validate_manifest(manifest: dict[str, Any], *, encrypted: bool) -> None:
    if manifest.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotIntegrityError("Unknown snapshot format")
    if (
        type(manifest.get("format_version")) is not int
        or manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
    ):
        raise SnapshotCompatibilityError(
            f"Unsupported snapshot format version: {manifest.get('format_version')!r}"
        )
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise SnapshotIntegrityError("Snapshot timestamp is missing")
    try:
        parsed_timestamp = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise SnapshotIntegrityError("Snapshot timestamp is invalid") from error
    if parsed_timestamp.tzinfo is None:
        raise SnapshotIntegrityError("Snapshot timestamp must include a timezone")

    application = _require_mapping(manifest.get("application"), "application")
    if not isinstance(application.get("version"), str):
        raise SnapshotIntegrityError("Snapshot application version is missing")
    instance = _require_mapping(manifest.get("instance"), "instance")
    if not isinstance(instance.get("id"), str) or not instance["id"]:
        raise SnapshotIntegrityError("Snapshot instance ID is missing")
    database = _require_mapping(manifest.get("database"), "database")
    workspace = _require_mapping(manifest.get("workspace"), "workspace")
    protection = _require_mapping(manifest.get("protection"), "protection")
    if protection.get("encrypted") is not encrypted:
        raise SnapshotIntegrityError("Manifest encryption state does not match the snapshot")
    expected_algorithm = "AES-256-GCM" if encrypted else None
    if protection.get("algorithm") != expected_algorithm:
        raise SnapshotIntegrityError("Manifest encryption algorithm is inconsistent")
    secret_bearing = protection.get("secret_bearing") is True
    contains_vault_key = workspace.get("contains_vault_key") is True
    if secret_bearing != contains_vault_key:
        raise SnapshotIntegrityError("Manifest secret-bearing flags are inconsistent")
    if secret_bearing and not encrypted:
        raise SnapshotIntegrityError("Secret-bearing snapshot is not encrypted")

    artifacts = _require_list(manifest.get("artifacts"), "artifacts")
    expected_names: set[str] = set()
    expected_kinds = {
        "database.dump": "postgresql-custom",
        "workspace.tar.gz": "workspace-tar-gzip",
    }
    for artifact_value in artifacts:
        artifact = _require_mapping(artifact_value, "artifacts[]")
        path = artifact.get("path")
        if not isinstance(path, str):
            raise SnapshotIntegrityError("Artifact path is missing")
        _safe_relative_path(path)
        if path not in {"database.dump", "workspace.tar.gz"} or path in expected_names:
            raise SnapshotIntegrityError(f"Unexpected or duplicate artifact: {path!r}")
        if artifact.get("kind") != expected_kinds[path]:
            raise SnapshotIntegrityError(f"Invalid artifact kind: {path}")
        expected_names.add(path)
        if type(artifact.get("size")) is not int or artifact["size"] < 0:
            raise SnapshotIntegrityError(f"Invalid artifact size: {path}")
        checksum = artifact.get("sha256")
        if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
            raise SnapshotIntegrityError(f"Invalid artifact checksum: {path}")
    if (database.get("included") is True) != ("database.dump" in expected_names):
        raise SnapshotIntegrityError("Database inclusion flag does not match artifacts")
    if (workspace.get("included") is True) != ("workspace.tar.gz" in expected_names):
        raise SnapshotIntegrityError("Workspace inclusion flag does not match artifacts")
    if database.get("included") is True:
        if database.get("engine") != "postgresql" or database.get("dump_format") != "custom":
            raise SnapshotIntegrityError("Invalid database dump metadata")
        if not isinstance(database.get("name"), str) or not database["name"]:
            raise SnapshotIntegrityError("Snapshot database name is missing")
        _require_mapping(database.get("schema"), "database.schema")
    if workspace.get("included") is not True:
        if _require_list(workspace.get("files"), "workspace.files"):
            raise SnapshotIntegrityError("Excluded workspace cannot declare files")
        if contains_vault_key:
            raise SnapshotIntegrityError("Excluded workspace cannot contain a vault key")


def _verify_artifacts(manifest: dict[str, Any], extracted: Path) -> None:
    artifacts = _require_list(manifest["artifacts"], "artifacts")
    expected_names = {"manifest.json"}
    for artifact_value in artifacts:
        artifact = _require_mapping(artifact_value, "artifacts[]")
        expected_names.add(str(artifact["path"]))
    actual_names = {path.name for path in extracted.iterdir()}
    if actual_names != expected_names:
        unexpected = sorted(actual_names - expected_names)
        missing = sorted(expected_names - actual_names)
        detail = []
        if unexpected:
            detail.append(f"unexpected={unexpected}")
        if missing:
            detail.append(f"missing={missing}")
        raise SnapshotIntegrityError(
            "Snapshot artifact set differs from manifest: " + ", ".join(detail)
        )

    for artifact_value in artifacts:
        artifact = _require_mapping(artifact_value, "artifacts[]")
        path = extracted / str(artifact["path"])
        if not path.is_file() or path.is_symlink():
            raise SnapshotIntegrityError(f"Snapshot artifact is missing: {artifact['path']}")
        actual_size = path.stat().st_size
        if actual_size != artifact["size"]:
            raise SnapshotIntegrityError(
                f"Size mismatch for {artifact['path']}: expected {artifact['size']}, got {actual_size}"
            )
        actual_checksum = _sha256_file(path)
        if actual_checksum != artifact["sha256"]:
            raise SnapshotIntegrityError(f"Checksum mismatch for {artifact['path']}")


def _workspace_manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    workspace = _require_mapping(manifest.get("workspace"), "workspace")
    entries: dict[str, dict[str, Any]] = {}
    for value in _require_list(workspace.get("files"), "workspace.files"):
        entry = _require_mapping(value, "workspace.files[]")
        path = entry.get("path")
        if not isinstance(path, str):
            raise SnapshotIntegrityError("Workspace file path is missing")
        _safe_relative_path(path)
        if path in entries:
            raise SnapshotIntegrityError(f"Duplicate workspace file: {path}")
        if type(entry.get("size")) is not int or entry["size"] < 0:
            raise SnapshotIntegrityError(f"Invalid workspace file size: {path}")
        checksum = entry.get("sha256")
        if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
            raise SnapshotIntegrityError(f"Invalid workspace file checksum: {path}")
        mode = entry.get("mode")
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise SnapshotIntegrityError(f"Invalid workspace file mode: {path}")
        entries[path] = entry
    return entries


def _verify_workspace_archive(manifest: dict[str, Any], archive_path: Path) -> None:
    expected = _workspace_manifest_files(manifest)
    workspace = _require_mapping(manifest.get("workspace"), "workspace")
    contains_vault_key = _VAULT_KEY_PATH in expected
    if contains_vault_key != (workspace.get("contains_vault_key") is True):
        raise SnapshotIntegrityError("Vault key inclusion flag does not match workspace files")
    if contains_vault_key and expected[_VAULT_KEY_PATH]["mode"] & (stat.S_IRWXG | stat.S_IRWXO):
        raise SnapshotIntegrityError("Vault key permissions in snapshot are too broad")
    secret_paths = set(expected) & set(_SECRET_WORKSPACE_PATHS)
    protection = _require_mapping(manifest.get("protection"), "protection")
    if secret_paths and protection.get("secret_bearing") is not True:
        raise SnapshotIntegrityError("Workspace key material is not marked secret-bearing")
    has_identity_metadata = _FEDERATION_IDENTITY_PATH in expected
    has_identity_key = _FEDERATION_KEY_PATH in expected
    if has_identity_metadata != has_identity_key:
        raise SnapshotIntegrityError("Federation identity metadata/key bundle is incomplete")
    if has_identity_metadata and protection.get("secret_bearing") is not True:
        raise SnapshotIntegrityError("Federation identity bundle is not marked secret-bearing")
    for secret_path in secret_paths:
        if expected[secret_path]["mode"] & (stat.S_IRWXG | stat.S_IRWXO):
            raise SnapshotIntegrityError(
                f"Workspace key permissions in snapshot are too broad: {secret_path}"
            )
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                _safe_relative_path(member.name)
                if not member.isfile() or member.name in seen or member.name not in expected:
                    raise SnapshotIntegrityError(
                        f"Unexpected or unsafe workspace archive member: {member.name!r}"
                    )
                seen.add(member.name)
                entry = expected[member.name]
                if member.size != entry["size"]:
                    raise SnapshotIntegrityError(f"Size mismatch for workspace/{member.name}")
                if stat.S_IMODE(member.mode) != entry["mode"]:
                    raise SnapshotIntegrityError(f"Mode mismatch for workspace/{member.name}")
                source = archive.extractfile(member)
                if source is None:
                    raise SnapshotIntegrityError(f"Could not read workspace/{member.name}")
                digest = hashlib.sha256()
                identity_bytes = bytearray() if member.name == _FEDERATION_IDENTITY_PATH else None
                while chunk := source.read(_CHUNK_SIZE):
                    digest.update(chunk)
                    if identity_bytes is not None:
                        identity_bytes.extend(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise SnapshotIntegrityError(f"Checksum mismatch for workspace/{member.name}")
                if identity_bytes is not None:
                    _parse_federation_identity(bytes(identity_bytes))
    except (tarfile.TarError, OSError) as error:
        raise SnapshotIntegrityError(f"Invalid workspace archive: {error}") from error
    missing = set(expected) - seen
    if missing:
        raise SnapshotIntegrityError(f"Workspace archive is missing: {', '.join(sorted(missing))}")


def _parse_federation_identity(data: bytes) -> dict[str, Any]:
    if len(data) > 1024 * 1024:
        raise SnapshotIntegrityError("Federation identity metadata is unexpectedly large")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotIntegrityError("Federation identity metadata is invalid JSON") from error
    if not isinstance(payload, dict):
        raise SnapshotIntegrityError("Federation identity metadata must be an object")
    key_reference = payload.get("private_key_ref")
    if not isinstance(key_reference, str) or not key_reference.strip():
        raise SnapshotIntegrityError("Federation identity private-key reference is missing")
    return payload


def _verify_database_dump(path: Path) -> None:
    _run_checked(["pg_restore", "--list", str(path)])


def _numeric_version(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _migration_checksums(values: list[Any], field_name: str) -> dict[str, str]:
    migrations: dict[str, str] = {}
    for value in values:
        entry = _require_mapping(value, f"{field_name}[]")
        migration_id = entry.get("id")
        checksum = entry.get("checksum")
        if not isinstance(migration_id, str) or not migration_id:
            raise SnapshotIntegrityError(f"Invalid migration ID in {field_name}")
        if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
            raise SnapshotIntegrityError(f"Invalid migration checksum in {field_name}")
        if migration_id in migrations:
            raise SnapshotIntegrityError(f"Duplicate migration ID in {field_name}: {migration_id}")
        migrations[migration_id] = checksum
    return migrations


def _compatibility(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    compatible = True
    application = _require_mapping(manifest["application"], "application")
    source_version = str(application["version"])
    source_numeric = _numeric_version(source_version)
    current_numeric = _numeric_version(__version__)
    if source_numeric is None or current_numeric is None:
        warnings.append(
            f"Could not compare application versions ({source_version!r} vs {__version__!r})"
        )
        compatible = False
    elif source_numeric[0] > current_numeric[0]:
        warnings.append(
            f"Snapshot application {source_version} is newer than installed {__version__}"
        )
        compatible = False

    database = _require_mapping(manifest["database"], "database")
    if database.get("included") is True:
        schema = _require_mapping(database.get("schema"), "database.schema")
        applied = _require_list(schema.get("applied"), "database.schema.applied")
        available = _require_list(schema.get("available"), "database.schema.available")
        applied_by_id = _migration_checksums(applied, "database.schema.applied")
        available_by_id = _migration_checksums(available, "database.schema.available")
        schema_state = schema.get("state")
        if schema_state not in {"current", "behind", "drift", "untracked"}:
            raise SnapshotIntegrityError("Invalid database schema state")
        if schema_state == "current" and applied_by_id != available_by_id:
            raise SnapshotIntegrityError("Current schema state does not match available migrations")
        if schema_state == "behind" and (
            not set(applied_by_id) < set(available_by_id)
            or any(available_by_id[key] != value for key, value in applied_by_id.items())
        ):
            raise SnapshotIntegrityError("Behind schema state is inconsistent")
        if schema_state == "untracked" and applied_by_id:
            raise SnapshotIntegrityError("Untracked schema cannot declare applied migrations")
        if schema_state in {"drift", "untracked"}:
            warnings.append(f"Snapshot database schema state is {schema.get('state')}")
            compatible = False
        current_by_id = {entry["id"]: entry["checksum"] for entry in _local_migrations()}
        for migration_id, checksum in applied_by_id.items():
            if current_by_id.get(migration_id) != checksum:
                warnings.append(f"Applied migration {migration_id!r} is missing or differs locally")
                compatible = False
    return compatible, warnings


def _verify_extracted(
    snapshot_path: Path,
    extracted: Path,
    manifest: dict[str, Any],
    *,
    encrypted: bool,
) -> VerificationResult:
    _validate_manifest(manifest, encrypted=encrypted)
    _verify_artifacts(manifest, extracted)
    workspace = _require_mapping(manifest["workspace"], "workspace")
    if workspace.get("included") is True:
        _verify_workspace_archive(manifest, extracted / "workspace.tar.gz")
    database = _require_mapping(manifest["database"], "database")
    if database.get("included") is True:
        _verify_database_dump(extracted / "database.dump")
    compatible, warnings = _compatibility(manifest)
    return VerificationResult(
        path=snapshot_path,
        encrypted=encrypted,
        manifest=manifest,
        compatible=compatible,
        warnings=tuple(warnings),
    )


def verify_snapshot(path: Path, *, passphrase: str | None = None) -> VerificationResult:
    """Authenticate, checksum, inspect, and compatibility-check a snapshot."""

    path = path.expanduser().absolute()
    with tempfile.TemporaryDirectory(
        prefix="genus-snapshot-verify-", dir=_staging_parent()
    ) as temp_name:
        staging = Path(temp_name)
        staging.chmod(stat.S_IRWXU)
        archive_path, encrypted = _materialize_snapshot(path, staging, passphrase)
        extracted = staging / "contents"
        _secure_directory(extracted)
        manifest = _extract_outer_archive(archive_path, extracted)
        return _verify_extracted(path, extracted, manifest, encrypted=encrypted)


def _target_path(workspace: Path, relative: str) -> Path:
    relative_path = _safe_relative_path(relative)
    workspace_resolved = workspace.resolve(strict=True)
    current = workspace
    for part in relative_path.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise SnapshotConflictError(f"Restore path contains a symlink: {relative}")
    target = workspace.joinpath(*relative_path.parts)
    try:
        target.resolve(strict=False).relative_to(workspace_resolved)
    except ValueError as error:
        raise SnapshotConflictError(
            f"Restore target escapes workspace (possibly through a symlink): {relative}"
        ) from error
    return target


def _workspace_conflicts(workspace: Path, files: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    conflicts: list[str] = []
    for relative in files:
        target = _target_path(workspace, relative)
        if target.exists() or target.is_symlink():
            conflicts.append(relative)
    return tuple(conflicts)


def _restore_workspace(
    archive_path: Path,
    workspace: Path,
    files: dict[str, dict[str, Any]],
    *,
    force: bool,
) -> int:
    restored = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        for relative, entry in files.items():
            member = members[relative]
            target = _target_path(workspace, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent.is_symlink():
                raise SnapshotConflictError(f"Restore parent is a symlink: {target.parent}")
            source = archive.extractfile(member)
            if source is None:
                raise SnapshotIntegrityError(f"Could not read workspace/{relative}")
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := source.read(_CHUNK_SIZE):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                mode = int(entry["mode"]) & 0o777
                if relative in _SECRET_WORKSPACE_PATHS:
                    mode = stat.S_IRUSR | stat.S_IWUSR
                temp_path.chmod(mode)
                if force:
                    temp_path.replace(target)
                else:
                    try:
                        os.link(temp_path, target)
                    except FileExistsError as error:
                        raise SnapshotConflictError(
                            f"Restore target appeared during restore: {relative}"
                        ) from error
                    temp_path.unlink()
                _fsync_directory(target.parent)
                restored += 1
            finally:
                temp_path.unlink(missing_ok=True)
    return restored


def _relocate_federation_key_reference(workspace: Path, files: dict[str, dict[str, Any]]) -> None:
    """Point restored identity metadata at the key in the target workspace."""

    if _FEDERATION_IDENTITY_PATH not in files:
        return
    identity_path = _target_path(workspace, _FEDERATION_IDENTITY_PATH)
    key_path = _target_path(workspace, _FEDERATION_KEY_PATH).resolve(strict=True)
    payload = _parse_federation_identity(identity_path.read_bytes())
    key_reference = str(key_path)
    if payload["private_key_ref"] == key_reference:
        return
    payload["private_key_ref"] = key_reference
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=".identity.", dir=identity_path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        temp_path.chmod(int(files[_FEDERATION_IDENTITY_PATH]["mode"]) & 0o777)
        temp_path.replace(identity_path)
        _fsync_directory(identity_path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _restore_database(db: DatabaseConfig, dump_path: Path) -> None:
    command = [
        "pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        *_connection_args(db),
        str(dump_path),
    ]
    _run_checked(command, environment=_postgres_env(db))


def restore_snapshot(
    path: Path,
    *,
    workspace: Path,
    database: DatabaseConfig,
    passphrase: str | None = None,
    restore_database: bool = True,
    restore_workspace: bool = True,
    confirm: bool = False,
    force: bool = False,
) -> RestoreResult:
    """Plan a restore by default, or execute it with explicit authorization."""

    if not restore_database and not restore_workspace:
        raise SnapshotError("Restore must select the database, workspace, or both")
    workspace = workspace.expanduser()
    if not workspace.is_dir() or workspace.is_symlink():
        raise SnapshotError(f"Restore workspace is not a real directory: {workspace}")
    path = path.expanduser().absolute()

    with tempfile.TemporaryDirectory(
        prefix="genus-snapshot-restore-", dir=_staging_parent()
    ) as temp_name:
        staging = Path(temp_name)
        staging.chmod(stat.S_IRWXU)
        archive_path, encrypted = _materialize_snapshot(path, staging, passphrase)
        extracted = staging / "contents"
        _secure_directory(extracted)
        manifest = _extract_outer_archive(archive_path, extracted)
        verification = _verify_extracted(path, extracted, manifest, encrypted=encrypted)
        database_manifest = _require_mapping(manifest["database"], "database")
        workspace_manifest = _require_mapping(manifest["workspace"], "workspace")
        do_database = restore_database and database_manifest.get("included") is True
        do_workspace = restore_workspace and workspace_manifest.get("included") is True
        if restore_database and not do_database:
            raise SnapshotError("Snapshot does not contain a database dump")
        if restore_workspace and not do_workspace:
            raise SnapshotError("Snapshot does not contain workspace state")

        workspace_files = _workspace_manifest_files(manifest) if do_workspace else {}
        conflicts = _workspace_conflicts(workspace, workspace_files)
        if not confirm:
            return RestoreResult(
                verification=verification,
                dry_run=True,
                database_restored=False,
                workspace_files_restored=0,
                conflicts=conflicts,
            )
        if not verification.compatible and not force:
            raise SnapshotCompatibilityError(
                "Snapshot is not compatible; inspect verification warnings or use --force"
            )
        if do_database and not force:
            raise SnapshotConflictError(
                "Database restore replaces existing objects and requires --force"
            )
        if conflicts and not force:
            preview = ", ".join(conflicts[:5])
            suffix = "..." if len(conflicts) > 5 else ""
            raise SnapshotConflictError(
                f"Workspace restore would overwrite {len(conflicts)} file(s): {preview}{suffix}"
            )

        if do_database:
            _restore_database(database, extracted / "database.dump")
        restored_count = 0
        if do_workspace:
            restored_count = _restore_workspace(
                extracted / "workspace.tar.gz", workspace, workspace_files, force=force
            )
            _relocate_federation_key_reference(workspace, workspace_files)
        return RestoreResult(
            verification=verification,
            dry_run=False,
            database_restored=do_database,
            workspace_files_restored=restored_count,
            conflicts=conflicts,
        )


def list_snapshots(repository: Path) -> list[SnapshotEntry]:
    """List snapshots managed by the default naming contract, newest first."""

    repository = repository.expanduser()
    if not repository.exists():
        return []
    if repository.is_symlink() or not repository.is_dir():
        raise SnapshotError(f"Snapshot repository is not a real directory: {repository}")
    entries: list[SnapshotEntry] = []
    for path in repository.iterdir():
        name_match = _SNAPSHOT_NAME_RE.fullmatch(path.name)
        if name_match is None or path.is_symlink() or not path.is_file():
            continue
        try:
            file_stat = path.stat()
            encrypted = _is_encrypted(path)
            created_at = datetime.strptime(name_match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        except FileNotFoundError:
            # A concurrent retention process removed it between discovery and
            # inspection. Treat that as an already-completed deletion.
            continue
        entries.append(
            SnapshotEntry(
                path=path,
                size=file_stat.st_size,
                created_at=created_at,
                encrypted=encrypted,
            )
        )
    return sorted(entries, key=lambda entry: (entry.created_at, entry.path.name), reverse=True)


def prune_snapshots(
    repository: Path,
    *,
    keep: int = 7,
    older_than_days: int | None = None,
    confirm: bool = False,
    now: datetime | None = None,
) -> PruneResult:
    """Apply count/age retention; default to a non-destructive dry run."""

    if keep < 1:
        raise SnapshotError("Retention must keep at least one snapshot")
    if older_than_days is not None and older_than_days < 1:
        raise SnapshotError("--older-than-days must be at least one")
    entries = list_snapshots(repository)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days or 0)
    candidates = tuple(
        entry
        for index, entry in enumerate(entries)
        if index >= keep and (older_than_days is None or entry.created_at < cutoff)
    )
    deleted: list[Path] = []
    if confirm:
        for entry in candidates:
            try:
                entry.path.unlink()
            except FileNotFoundError:
                continue
            deleted.append(entry.path)
        if deleted:
            _fsync_directory(repository.expanduser())
    return PruneResult(candidates=candidates, deleted=tuple(deleted), dry_run=not confirm)

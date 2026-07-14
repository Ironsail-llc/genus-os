"""Disaster-recovery snapshot tests (no live PostgreSQL required)."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robothor.config import DatabaseConfig
from robothor.snapshot import (
    SnapshotConflictError,
    SnapshotError,
    SnapshotIntegrityError,
    _artifact,
    _database_schema_metadata,
    _decrypt_archive,
    _dump_database,
    _encrypt_archive,
    _write_outer_archive,
    create_snapshot,
    list_snapshots,
    prune_snapshots,
    restore_snapshot,
    verify_snapshot,
)

PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "docs" / "agents" / "main.yaml").write_text("id: main\n", encoding="utf-8")
    (root / "brain" / "memory").mkdir(parents=True)
    (root / "brain" / "memory" / "notes.md").write_text("durable state\n", encoding="utf-8")
    (root / ".robothor").mkdir()
    (root / ".robothor" / "federation.yaml").write_text(
        "instance_id: test-instance\ninstance_name: Test Genus\n", encoding="utf-8"
    )
    key_path = root / ".vault-key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)
    return root


@pytest.fixture
def database() -> DatabaseConfig:
    return DatabaseConfig(
        host="db.internal",
        port=5433,
        name="genus",
        user="operator",
        password="never-on-command-line",
        ssl_mode="verify-full",
    )


def _fake_dump(_database: DatabaseConfig, destination: Path) -> None:
    destination.write_bytes(b"PGDMP\x01 mocked custom dump")


def _schema() -> dict[str, object]:
    return {
        "ledger": "schema_migrations_v2",
        "state": "current",
        "applied": [],
        "available": [],
    }


def test_create_and_verify_encrypted_secret_snapshot(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    repository = tmp_path / "snapshots"
    identity_key = workspace / ".robothor" / "identity.key"
    identity_key.write_text("-----BEGIN PRIVATE KEY-----\nmock\n", encoding="utf-8")
    identity_key.chmod(0o600)
    (workspace / ".robothor" / "identity.json").write_text(
        '{"id":"test-instance","private_key_ref":".robothor/identity.key"}\n',
        encoding="utf-8",
    )
    with (
        patch("robothor.snapshot._dump_database", side_effect=_fake_dump),
        patch("robothor.snapshot._database_schema_metadata", return_value=_schema()),
        patch("robothor.snapshot._verify_database_dump"),
        patch("robothor.snapshot._local_migrations", return_value=[]),
    ):
        snapshot = create_snapshot(
            workspace=workspace,
            database=database,
            repository=repository,
            include_secrets=True,
            passphrase=PASSPHRASE,
        )
        result = verify_snapshot(snapshot, passphrase=PASSPHRASE)

    assert snapshot.name.startswith("genusos-snapshot-test-instance-")
    assert snapshot.suffix == ".gss"
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert result.encrypted is True
    assert result.compatible is True
    assert result.manifest["format_version"] == 1
    assert result.manifest["instance"] == {"id": "test-instance", "name": "Test Genus"}
    assert result.manifest["database"]["dump_format"] == "custom"
    assert result.manifest["workspace"]["contains_vault_key"] is True
    workspace_paths = {entry["path"] for entry in result.manifest["workspace"]["files"]}
    assert ".vault-key" in workspace_paths
    assert ".robothor/identity.key" in workspace_paths
    assert ".robothor/identity.json" in workspace_paths
    assert "brain/memory/notes.md" in workspace_paths
    assert "docs/agents/main.yaml" in workspace_paths
    assert all(len(entry["sha256"]) == 64 for entry in result.manifest["artifacts"])


def test_pg_dump_uses_safe_args_and_environment(database: DatabaseConfig, tmp_path: Path) -> None:
    destination = tmp_path / "database.dump"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[command.index("--file") + 1]).write_bytes(b"PGDMP mocked")
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "not-for-pg-dump"}),
        patch("robothor.snapshot.subprocess.run", side_effect=fake_run) as run,
    ):
        _dump_database(database, destination)

    command = run.call_args.args[0]
    kwargs = run.call_args.kwargs
    assert command[0] == "pg_dump"
    assert "--format=custom" in command
    assert command[command.index("--host") + 1] == "db.internal"
    assert command[command.index("--dbname") + 1] == "genus"
    assert database.password not in command
    assert kwargs["env"]["PGPASSWORD"] == database.password
    assert kwargs["env"]["PGSSLMODE"] == "verify-full"
    assert "OPENROUTER_API_KEY" not in kwargs["env"]
    assert "shell" not in kwargs


def test_schema_metadata_reads_canonical_ledger_without_live_database(
    database: DatabaseConfig,
) -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("schema_migrations_v2",)
    cursor.fetchall.return_value = [("001_init", "a" * 64)]
    available = [
        {
            "id": "001_init",
            "checksum": "a" * 64,
            "source": "infra",
            "filename": "001_init.sql",
        }
    ]
    with (
        patch("psycopg2.connect", return_value=connection) as connect,
        patch("robothor.snapshot._local_migrations", return_value=available),
    ):
        metadata = _database_schema_metadata(database)

    assert metadata["state"] == "current"
    assert metadata["applied"] == [{"id": "001_init", "checksum": "a" * 64}]
    connect.assert_called_once_with(**database.dict, connect_timeout=10)
    connection.set_session.assert_called_once_with(readonly=True, autocommit=True)
    connection.close.assert_called_once()


def test_secret_snapshot_cannot_be_plaintext(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    with pytest.raises(SnapshotError, match="must be encrypted"):
        create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_database=False,
            include_secrets=True,
            encrypt=False,
        )


def test_encrypted_snapshot_authentication_detects_tampering(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    with patch("robothor.snapshot._local_migrations", return_value=[]):
        snapshot = create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_database=False,
            passphrase=PASSPHRASE,
        )
    payload = bytearray(snapshot.read_bytes())
    payload[len(payload) // 2] ^= 0x01
    snapshot.write_bytes(payload)

    with pytest.raises(SnapshotIntegrityError, match="authentication failed"):
        verify_snapshot(snapshot, passphrase=PASSPHRASE)


def test_encryption_round_trip_spans_multiple_authenticated_chunks(tmp_path: Path) -> None:
    source = tmp_path / "large.tar.gz"
    source.write_bytes((b"genus-state-" * 100_000) + (b"entity-" * 150_000))
    encrypted = tmp_path / "large.gss"
    decrypted = tmp_path / "restored.tar.gz"

    _encrypt_archive(source, encrypted, PASSPHRASE)
    _decrypt_archive(encrypted, decrypted, PASSPHRASE)

    assert decrypted.read_bytes() == source.read_bytes()


def test_verify_rejects_workspace_path_traversal(tmp_path: Path) -> None:
    workspace_archive = tmp_path / "workspace.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("escape", encoding="utf-8")
    with tarfile.open(workspace_archive, "w:gz") as archive:
        archive.add(payload, arcname="../escape")
    manifest = {
        "format": "genusos-snapshot",
        "format_version": 1,
        "created_at": "2026-07-13T12:00:00Z",
        "application": {"name": "Genus OS", "version": "0.1.0"},
        "instance": {"id": "malicious", "name": "Malicious"},
        "database": {"included": False, "schema": {"applied": []}},
        "workspace": {
            "included": True,
            "contains_vault_key": False,
            "files": [{"path": "../escape", "size": 6, "sha256": "a" * 64, "mode": 0o600}],
        },
        "protection": {"encrypted": False, "secret_bearing": False},
        "artifacts": [_artifact(workspace_archive, "workspace-tar-gzip")],
    }
    snapshot = tmp_path / "malicious.tar.gz"
    _write_outer_archive(snapshot, manifest, [workspace_archive])

    with pytest.raises(SnapshotIntegrityError, match="Unsafe snapshot path"):
        verify_snapshot(snapshot)


def test_verify_rejects_unmanifested_artifact(tmp_path: Path) -> None:
    extra = tmp_path / "workspace.tar.gz"
    with tarfile.open(extra, "w:gz"):
        pass
    manifest = {
        "format": "genusos-snapshot",
        "format_version": 1,
        "created_at": "2026-07-13T12:00:00Z",
        "application": {"name": "Genus OS", "version": "0.1.0"},
        "instance": {"id": "test", "name": "Test"},
        "database": {"included": False, "schema": {"applied": []}},
        "workspace": {"included": False, "contains_vault_key": False, "files": []},
        "protection": {
            "encrypted": False,
            "algorithm": None,
            "secret_bearing": False,
        },
        "artifacts": [],
    }
    snapshot = tmp_path / "extra-artifact.tar.gz"
    _write_outer_archive(snapshot, manifest, [extra])

    with pytest.raises(SnapshotIntegrityError, match="artifact set differs"):
        verify_snapshot(snapshot)


def test_snapshot_refuses_symlinked_workspace_state(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    (workspace / "brain" / "external").symlink_to(tmp_path / "outside")
    with pytest.raises(SnapshotError, match="symlinked workspace state"):
        create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_database=False,
            encrypt=False,
        )


def test_snapshot_includes_configured_manifest_directory_inside_workspace(
    workspace: Path,
    database: DatabaseConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = workspace / "config" / "manifests"
    configured.mkdir(parents=True)
    (configured / "custom.yaml").write_text("id: custom\n", encoding="utf-8")
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(configured))
    snapshot = create_snapshot(
        workspace=workspace,
        database=database,
        repository=tmp_path / "snapshots",
        include_database=False,
        encrypt=False,
    )

    result = verify_snapshot(snapshot)
    paths = {entry["path"] for entry in result.manifest["workspace"]["files"]}
    assert "config/manifests/custom.yaml" in paths


def test_snapshot_fails_if_configured_manifests_are_outside_workspace(
    workspace: Path,
    database: DatabaseConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external-manifests"
    external.mkdir()
    monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(external))

    with pytest.raises(SnapshotError, match="must be inside the workspace"):
        create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_database=False,
            encrypt=False,
        )


def test_restore_is_dry_run_and_never_overwrites_without_force(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    source_value = (workspace / "brain" / "memory" / "notes.md").read_text()
    snapshot = create_snapshot(
        workspace=workspace,
        database=database,
        repository=tmp_path / "snapshots",
        include_database=False,
        encrypt=False,
    )
    target = tmp_path / "target"
    (target / "brain" / "memory").mkdir(parents=True)
    target_note = target / "brain" / "memory" / "notes.md"
    target_note.write_text("newer target state\n", encoding="utf-8")

    with patch("robothor.snapshot._local_migrations", return_value=[]):
        plan = restore_snapshot(
            snapshot,
            workspace=target,
            database=database,
            restore_database=False,
        )
        assert plan.dry_run is True
        assert "brain/memory/notes.md" in plan.conflicts
        assert target_note.read_text() == "newer target state\n"

        with pytest.raises(SnapshotConflictError, match="would overwrite"):
            restore_snapshot(
                snapshot,
                workspace=target,
                database=database,
                restore_database=False,
                confirm=True,
            )

        restored = restore_snapshot(
            snapshot,
            workspace=target,
            database=database,
            restore_database=False,
            confirm=True,
            force=True,
        )

    assert restored.dry_run is False
    assert restored.workspace_files_restored >= 1
    assert target_note.read_text() == source_value


def test_restore_relocates_federation_private_key_reference(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    identity_key = workspace / ".robothor" / "identity.key"
    identity_key.write_text("-----BEGIN PRIVATE KEY-----\nmock\n", encoding="utf-8")
    identity_key.chmod(0o600)
    (workspace / ".robothor" / "identity.json").write_text(
        json.dumps(
            {
                "id": "instance-1",
                "private_key_ref": str(identity_key),
            }
        ),
        encoding="utf-8",
    )
    snapshot = create_snapshot(
        workspace=workspace,
        database=database,
        repository=tmp_path / "snapshots",
        include_database=False,
        include_secrets=True,
        passphrase=PASSPHRASE,
    )
    target = tmp_path / "restored-workspace"
    target.mkdir()

    result = restore_snapshot(
        snapshot,
        workspace=target,
        database=database,
        passphrase=PASSPHRASE,
        restore_database=False,
        confirm=True,
    )

    restored_identity = json.loads((target / ".robothor" / "identity.json").read_text())
    assert result.dry_run is False
    assert restored_identity["private_key_ref"] == str(
        (target / ".robothor" / "identity.key").resolve()
    )
    assert (target / ".robothor" / "identity.key").stat().st_mode & 0o777 == 0o600


def test_database_restore_requires_force(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    with (
        patch("robothor.snapshot._dump_database", side_effect=_fake_dump),
        patch("robothor.snapshot._database_schema_metadata", return_value=_schema()),
    ):
        snapshot = create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_workspace=False,
            encrypt=False,
        )

    with (
        patch("robothor.snapshot._verify_database_dump"),
        patch("robothor.snapshot._local_migrations", return_value=[]),
        pytest.raises(SnapshotConflictError, match="requires --force"),
    ):
        restore_snapshot(
            snapshot,
            workspace=workspace,
            database=database,
            restore_workspace=False,
            confirm=True,
        )


def test_verify_reports_schema_drift_as_incompatible(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    drifted_schema = {
        "ledger": "schema_migrations_v2",
        "state": "drift",
        "applied": [{"id": "999_future", "checksum": "f" * 64}],
        "available": [],
    }
    with (
        patch("robothor.snapshot._dump_database", side_effect=_fake_dump),
        patch("robothor.snapshot._database_schema_metadata", return_value=drifted_schema),
    ):
        snapshot = create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_workspace=False,
            encrypt=False,
        )
    with (
        patch("robothor.snapshot._verify_database_dump"),
        patch("robothor.snapshot._local_migrations", return_value=[]),
    ):
        result = verify_snapshot(snapshot)

    assert result.compatible is False
    assert any("schema state is drift" in warning for warning in result.warnings)
    assert any("999_future" in warning for warning in result.warnings)


def test_database_restore_uses_single_transaction_and_safe_credentials(
    database: DatabaseConfig, tmp_path: Path
) -> None:
    from robothor.snapshot import _restore_database

    dump = tmp_path / "database.dump"
    dump.write_bytes(b"PGDMP mocked")
    with patch(
        "robothor.snapshot.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, "", ""),
    ) as run:
        _restore_database(database, dump)

    command = run.call_args.args[0]
    kwargs = run.call_args.kwargs
    assert command[0] == "pg_restore"
    assert "--single-transaction" in command
    assert "--clean" in command
    assert "--if-exists" in command
    assert database.password not in command
    assert kwargs["env"]["PGPASSWORD"] == database.password
    assert "shell" not in kwargs


def test_atomic_create_refuses_existing_output(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    repository = tmp_path / "snapshots"
    output = repository / "fixed.tar.gz"
    output.parent.mkdir()
    output.write_text("do not replace", encoding="utf-8")

    with pytest.raises(SnapshotConflictError, match="already exists"):
        create_snapshot(
            workspace=workspace,
            database=database,
            repository=repository,
            output=output,
            include_database=False,
            encrypt=False,
        )
    assert output.read_text() == "do not replace"


def test_create_rejects_output_inside_selected_workspace_state(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    output = workspace / "brain" / "backups" / "recursive.gss"
    with pytest.raises(SnapshotError, match="inside workspace state"):
        create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "unused",
            output=output,
            include_database=False,
            passphrase=PASSPHRASE,
        )
    assert not output.parent.exists()


def test_list_and_retention_are_scoped_and_dry_run_by_default(tmp_path: Path) -> None:
    repository = tmp_path / "snapshots"
    repository.mkdir()
    now = datetime.now(UTC)
    paths: list[Path] = []
    for index in range(4):
        path = repository / f"genusos-snapshot-test-202601{index + 1:02d}T000000Z.tar.gz"
        path.write_bytes(f"snapshot-{index}".encode())
        modified = now - timedelta(days=index + 1)
        os.utime(path, (modified.timestamp(), modified.timestamp()))
        paths.append(path)
    unrelated = repository / "release.tar.gz"
    unrelated.write_bytes(b"not managed")

    entries = list_snapshots(repository)
    assert len(entries) == 4
    assert unrelated not in {entry.path for entry in entries}

    dry_run = prune_snapshots(repository, keep=2, older_than_days=2, now=now)
    assert dry_run.dry_run is True
    assert len(dry_run.candidates) == 2
    assert all(path.exists() for path in paths)

    applied = prune_snapshots(repository, keep=2, older_than_days=2, now=now, confirm=True)
    assert len(applied.deleted) == 2
    assert unrelated.exists()
    assert len(list_snapshots(repository)) == 2


def test_pg_dump_failure_is_sanitized(database: DatabaseConfig, tmp_path: Path) -> None:
    failure = subprocess.CalledProcessError(1, ["pg_dump"], stderr="connection refused", output="")
    with (
        patch("robothor.snapshot.subprocess.run", side_effect=failure),
        pytest.raises(SnapshotError, match="pg_dump failed: connection refused"),
    ):
        _dump_database(database, tmp_path / "database.dump")


def test_weak_passphrase_fails_closed(
    workspace: Path, database: DatabaseConfig, tmp_path: Path
) -> None:
    with pytest.raises(SnapshotError, match="at least 12"):
        create_snapshot(
            workspace=workspace,
            database=database,
            repository=tmp_path / "snapshots",
            include_database=False,
            passphrase="too short",
        )

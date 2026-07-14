"""CLI contracts for snapshot operations."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from robothor.cli import main
from robothor.snapshot import PruneResult, RestoreResult, VerificationResult

if TYPE_CHECKING:
    from pathlib import Path


def _verification(path: Path) -> VerificationResult:
    return VerificationResult(
        path=path,
        encrypted=True,
        manifest={
            "created_at": "2026-07-13T12:00:00Z",
            "instance": {"id": "instance-1", "name": "Test Genus"},
        },
        compatible=True,
        warnings=(),
    )


def test_create_requires_passphrase_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GENUS_SNAPSHOT_PASSPHRASE", raising=False)
    with patch("robothor.cli.snapshot.create_snapshot") as create:
        result = main(["snapshot", "create", "--skip-database"])
    assert result == 1
    create.assert_not_called()
    assert "intentionally not accepted as command-line arguments" in capsys.readouterr().out


def test_create_wires_safe_defaults(tmp_path: Path, monkeypatch, capsys) -> None:
    output = tmp_path / "genusos-snapshot-test.gss"
    output.write_bytes(b"snapshot")
    monkeypatch.setenv("GENUS_SNAPSHOT_PASSPHRASE", "correct horse battery staple")
    with patch("robothor.cli.snapshot.create_snapshot", return_value=output) as create:
        result = main(
            [
                "snapshot",
                "create",
                "--repository",
                str(tmp_path),
                "--include-secrets",
            ]
        )
    assert result == 0
    assert create.call_args.kwargs["encrypt"] is True
    assert create.call_args.kwargs["include_secrets"] is True
    assert create.call_args.kwargs["include_database"] is True
    assert create.call_args.kwargs["include_workspace"] is True
    assert "AES-256-GCM" in capsys.readouterr().out


def test_restore_defaults_to_verification_only(tmp_path: Path, capsys) -> None:
    snapshot = tmp_path / "snapshot.gss"
    verification = _verification(snapshot)
    planned = RestoreResult(
        verification=verification,
        dry_run=True,
        database_restored=False,
        workspace_files_restored=0,
        conflicts=("brain/GOAL.md",),
    )
    with patch("robothor.cli.snapshot.restore_snapshot", return_value=planned) as restore:
        result = main(["snapshot", "restore", str(snapshot)])
    assert result == 0
    assert restore.call_args.kwargs["confirm"] is False
    assert restore.call_args.kwargs["force"] is False
    assert "DRY RUN" in capsys.readouterr().out


def test_verify_returns_nonzero_for_incompatible_snapshot(tmp_path: Path, capsys) -> None:
    snapshot = tmp_path / "snapshot.gss"
    verification = VerificationResult(
        path=snapshot,
        encrypted=True,
        manifest={
            "created_at": "2026-07-13T12:00:00Z",
            "instance": {"id": "future", "name": "Future Genus"},
        },
        compatible=False,
        warnings=("future schema",),
    )
    with patch("robothor.cli.snapshot.verify_snapshot", return_value=verification):
        result = main(["snapshot", "verify", str(snapshot)])
    assert result == 1
    output = capsys.readouterr().out
    assert "NOT COMPATIBLE" in output
    assert "future schema" in output


def test_prune_defaults_to_dry_run(tmp_path: Path, capsys) -> None:
    with patch(
        "robothor.cli.snapshot.prune_snapshots",
        return_value=PruneResult(candidates=(), deleted=(), dry_run=True),
    ) as prune:
        result = main(["snapshot", "prune", "--repository", str(tmp_path)])
    assert result == 0
    assert prune.call_args.kwargs["confirm"] is False
    assert "selected no snapshots" in capsys.readouterr().out


def test_unknown_snapshot_subcommand_is_nonzero(capsys) -> None:
    result = main(["snapshot"])
    assert result == 1
    assert "Usage: robothor snapshot" in capsys.readouterr().out

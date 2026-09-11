"""`robothor upgrade` must go through the canonical migrator.

The upgrade command used to carry its own migration mechanism: a glob over
``infra/migrations/*.sql`` and ``crm/migrations/*.sql``, applied with no
checksum, no advisory lock, and a YAML side-ledger in
``.robothor/migrations_applied.yaml``.  It disagreed with
``robothor/db/migrate.py`` about which migrations existed and whether they had
run.  These tests pin the collapse onto one path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from robothor.cli import upgrade

LEGACY_HELPERS = (
    "_discover_migrations",
    "_apply_migration",
    "_seed_tracking_if_needed",
    "_load_applied",
    "_save_applied",
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    return tmp_path


def _args(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"dry_run": False, "pull": False, "skip_migrations": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_second_migration_mechanism_is_gone() -> None:
    for helper in LEGACY_HELPERS:
        assert not hasattr(upgrade, helper), f"{helper} is a second migration mechanism"


def test_upgrade_migrates_through_the_canonical_runner(workspace: Path) -> None:
    connection = MagicMock()
    with (
        patch("psycopg2.connect", return_value=connection),
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={}),
        patch(
            "robothor.db.migrate.status",
            return_value=[
                {"migration_id": "001_init", "filename": "001_init.sql", "status": "applied"}
            ],
        ) as mock_status,
        patch("robothor.db.migrate.apply", return_value=["114_new"]) as mock_apply,
    ):
        rc = upgrade.cmd_upgrade(_args())

    assert rc == 0
    mock_status.assert_called_once_with(connection=connection)
    mock_apply.assert_called_once_with(connection=connection)


def test_upgrade_never_reads_a_sql_file_itself(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_paths: list[str] = []
    real_read_text = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        read_paths.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)

    connection = MagicMock()
    with (
        patch("psycopg2.connect", return_value=connection),
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={}),
        patch("robothor.db.migrate.status", return_value=[]),
        patch("robothor.db.migrate.apply", return_value=[]),
    ):
        rc = upgrade.cmd_upgrade(_args())

    assert rc == 0
    assert [path for path in read_paths if path.endswith(".sql")] == []


def test_dry_run_preview_survives_an_unreachable_database(workspace: Path, capsys: Any) -> None:
    """`upgrade --dry-run` reads no schema and changes nothing.

    Before the collapse onto one migrator it never opened a connection at all,
    so a preview worked with PostgreSQL down. It must stay a preview: report
    the unreachable database, don't fail the run.
    """
    with (
        patch("robothor.db.migrate.status", side_effect=OSError("connection refused")),
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={}),
    ):
        rc = upgrade.cmd_upgrade(_args(dry_run=True))

    assert rc == 0
    assert "connection refused" in capsys.readouterr().out


def test_dry_run_still_fails_on_a_migration_safety_finding(workspace: Path, capsys: Any) -> None:
    """A MigrationError is a finding about the schema, not a missing database.

    The connection-class exemption above must not swallow it: a preview that
    reports 'run --adopt-baseline' and then exits 0 reads as a clean preview.
    """
    from robothor.db.migrate import MigrationHistoryError

    with (
        patch(
            "robothor.db.migrate.status",
            side_effect=MigrationHistoryError("ledger empty but schema present"),
        ),
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={}),
    ):
        rc = upgrade.cmd_upgrade(_args(dry_run=True))

    assert rc == 1
    out = capsys.readouterr().out
    assert "PREVIEW BLOCKED" in out
    assert "ledger empty but schema present" in out


def test_upgrade_does_not_run_git_without_the_pull_flag(workspace: Path, capsys: Any) -> None:
    (workspace / ".git").mkdir()

    with (
        patch("robothor.cli.upgrade.subprocess.run") as mock_run,
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={}),
    ):
        rc = upgrade.cmd_upgrade(_args(skip_migrations=True))

    assert rc == 0
    mock_run.assert_not_called()
    # A wheel install has no checkout to pull; say how it updates instead.
    assert "pip install -U genusos" in capsys.readouterr().out


def test_pull_flag_opts_into_the_git_checkout_update(workspace: Path) -> None:
    (workspace / ".git").mkdir()
    completed = SimpleNamespace(returncode=0, stdout="Already up to date.", stderr="")

    with (
        patch("robothor.cli.upgrade.subprocess.run", return_value=completed) as mock_run,
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={}),
    ):
        rc = upgrade.cmd_upgrade(_args(pull=True, skip_migrations=True))

    assert rc == 0
    command = mock_run.call_args[0][0]
    assert command[:3] == ["git", "pull", "--ff-only"]


def _write_state(workspace: Path, filenames: list[str]) -> Path:
    state_file = workspace / ".robothor" / "migrations_applied.yaml"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        yaml.dump(
            {
                "migrations": [
                    {"file": name, "applied_at": "2026-01-01T00:00:00Z"} for name in filenames
                ],
                "template_hashes": {"SOUL.md": "stale"},
            }
        )
    )
    return state_file


def _ledger_rows(applied: list[str], pending: list[str]) -> list[dict[str, Any]]:
    return [
        {"migration_id": name.removesuffix(".sql"), "filename": name, "status": "applied"}
        for name in applied
    ] + [
        {"migration_id": name.removesuffix(".sql"), "filename": name, "status": "pending"}
        for name in pending
    ]


def _upgrade_with_ledger(rows: list[dict[str, Any]]) -> int:
    connection = MagicMock()
    with (
        patch("psycopg2.connect", return_value=connection),
        patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={"SOUL.md": "fresh"}),
        patch("robothor.db.migrate.status", return_value=rows),
        patch("robothor.db.migrate.apply", return_value=[]),
    ):
        return upgrade.cmd_upgrade(_args())


def test_legacy_migrations_key_survives_until_the_v2_ledger_covers_it(
    workspace: Path,
) -> None:
    """That list is the only record of what the retired glob path applied.

    `--adopt-baseline` reads it to decide which migrations must be adopted
    rather than replayed, so deleting it before the v2 ledger covers every
    entry destroys the evidence the adoption depends on.
    """
    state_file = _write_state(workspace, ["001_init.sql", "040_later.sql"])

    rc = _upgrade_with_ledger(_ledger_rows(applied=["001_init.sql"], pending=["040_later.sql"]))

    assert rc == 0
    saved = yaml.safe_load(state_file.read_text())
    assert [entry["file"] for entry in saved["migrations"]] == ["001_init.sql", "040_later.sql"]
    assert saved["template_hashes"] == {"SOUL.md": "fresh"}


def test_legacy_migrations_key_is_retired_once_the_v2_ledger_covers_it(
    workspace: Path,
) -> None:
    state_file = _write_state(workspace, ["001_init.sql", "040_later.sql"])

    rc = _upgrade_with_ledger(_ledger_rows(applied=["001_init.sql", "040_later.sql"], pending=[]))

    assert rc == 0
    saved = yaml.safe_load(state_file.read_text())
    assert "migrations" not in saved
    assert saved["template_hashes"] == {"SOUL.md": "fresh"}


def test_legacy_migrations_key_survives_a_run_that_never_read_the_ledger(
    workspace: Path,
) -> None:
    """--skip-migrations learns nothing about coverage, so it may not decide."""
    state_file = _write_state(workspace, ["001_init.sql"])

    with patch("robothor.cli.upgrade._snapshot_template_hashes", return_value={"SOUL.md": "fresh"}):
        rc = upgrade.cmd_upgrade(_args(skip_migrations=True))

    assert rc == 0
    saved = yaml.safe_load(state_file.read_text())
    assert [entry["file"] for entry in saved["migrations"]] == ["001_init.sql"]

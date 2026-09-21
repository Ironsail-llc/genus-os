"""Staging publishes one verified artifact, never a partial runtime selection."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from robothor.templates.tests.test_fleet_release import source as source_fixture
from robothor.templates.tests.test_fleet_release import spec

source = source_fixture


def test_concurrent_staging_is_idempotent_and_preserves_workspace(source, tmp_path):
    from robothor.templates.fleet_release import build_release, verify_release
    from robothor.templates.fleet_store import stage_release

    candidate, workspace = tmp_path / "candidate", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("Existing state")
    receipt = build_release(source, candidate, spec())

    def stage(_):
        return stage_release(candidate, workspace, expected_digest=receipt["release_id"])

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(stage, range(3)))
    assert len({result.path for result in results}) == 1
    assert sum(not result.already_present for result in results) == 1
    assert results[0].path == workspace / ".robothor/fleet-releases" / receipt["release_id"]
    verify_release(results[0].path, expected_digest=receipt["release_id"])
    assert (workspace / "keep.txt").read_text() == "Existing state"
    assert not (workspace / "docs/agents").exists()


@pytest.mark.parametrize("fault", ["source", "destination", "symlink"])
def test_staging_refuses_drift_or_redirects_without_repairing_in_place(source, tmp_path, fault):
    from robothor.templates.fleet_release import ReleaseError, build_release
    from robothor.templates.fleet_store import stage_release

    candidate, workspace = tmp_path / "candidate", tmp_path / "workspace"
    workspace.mkdir()
    receipt = build_release(source, candidate, spec())
    if fault == "source":
        (candidate / "brain/WORKER.md").write_text("Changed")
    elif fault == "destination":
        installed = stage_release(candidate, workspace, expected_digest=receipt["release_id"])
        (installed.path / "brain/WORKER.md").write_text("Changed")
    else:
        (workspace / ".robothor").symlink_to(source, target_is_directory=True)
    with pytest.raises(ReleaseError):
        stage_release(candidate, workspace, expected_digest=receipt["release_id"])


def test_failed_publication_can_be_retried_without_selecting_partial_files(
    source, tmp_path, monkeypatch
):
    from pathlib import Path

    from robothor.templates.fleet_release import ReleaseError, build_release
    from robothor.templates.fleet_store import stage_release

    candidate, workspace = tmp_path / "candidate", tmp_path / "workspace"
    workspace.mkdir()
    receipt = build_release(source, candidate, spec())
    rename = Path.rename

    def fail(*args):
        raise OSError("simulated publication interruption")

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(ReleaseError):
        stage_release(candidate, workspace, expected_digest=receipt["release_id"])
    destination = workspace / ".robothor/fleet-releases" / receipt["release_id"]
    assert not destination.exists()
    monkeypatch.setattr(Path, "rename", rename)
    assert (
        stage_release(candidate, workspace, expected_digest=receipt["release_id"]).path
        == destination
    )

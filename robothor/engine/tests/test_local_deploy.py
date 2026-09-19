"""Deployment cannot replace a concurrent release or claim an unverified fix."""

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from robothor.engine import local_deploy as deploy


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    state = tmp_path / "shared.json"
    state.write_text(json.dumps({"combined_commit": "old", "snapshot": "/old"}))
    override = tmp_path / "override.conf"
    override.write_text("old unit")
    monkeypatch.setattr(deploy, "STATE", state)
    monkeypatch.setattr(deploy, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(deploy, "OVERRIDE", override)
    monkeypatch.setattr(deploy, "HOST_OVERRIDE", tmp_path / "host.conf")
    monkeypatch.setattr(deploy.Path, "home", lambda: tmp_path)
    commit = "a" * 40
    snapshot = tmp_path / ".local/share/robothor/combined-preview" / commit[:10]
    snapshot.mkdir(parents=True)
    (snapshot / "REVISION").write_text(commit)
    monkeypatch.setattr(deploy, "command", lambda *a, **k: commit)
    run = MagicMock()
    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setattr(deploy, "wait_idle", lambda: None)
    writes = []
    monkeypatch.setattr(deploy, "install_override", lambda content, *args: writes.append(content))
    return tmp_path, state, run, writes


def test_failed_probe_rolls_back_and_does_not_publish(deployment, monkeypatch):
    root, state, run, writes = deployment
    monkeypatch.setattr(
        deploy, "verify_live", MagicMock(side_effect=RuntimeError("browser broken"))
    )
    job = root / "job/state.json"
    with pytest.raises(RuntimeError, match="browser broken"):
        deploy.deploy(root, "candidate", job)
    assert json.loads(state.read_text())["combined_commit"] == "old"
    assert writes[-1] == "old unit"
    assert json.loads(job.read_text())["rolled_back"]


def test_concurrent_release_cannot_be_overwritten(deployment):
    root, state, run, writes = deployment
    run.side_effect = subprocess.CalledProcessError(1, "merge-base")
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy(root, "stale-revision", root / "job/state.json")
    assert not writes


def test_success_publishes_only_after_verification(deployment, monkeypatch):
    root, state, run, writes = deployment
    monkeypatch.setattr(deploy, "verify_live", lambda snapshot: {"ready": True})
    result = deploy.deploy(root, "candidate", root / "job/state.json")
    assert result["status"] == "verified"
    assert json.loads(state.read_text())["combined_commit"] == "a" * 40

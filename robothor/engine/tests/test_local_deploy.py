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


def test_release_switch_preserves_concurrent_feature_configuration(tmp_path):
    original = (
        "[Service]\nEnvironment=ROBOTHOR_AUTONOMY_CHROMIUM_EXECUTABLE=/opt/browser\n"
        'Environment="ANOTHER_FEATURE=some value"\nWorkingDirectory=/old\n'
    )
    first = deploy.release_override(original, tmp_path / "one", "robothor.engine.daemon", tmp_path)
    second = deploy.release_override(first, tmp_path / "two", "robothor.engine.daemon", tmp_path)
    assert original.strip() in second
    assert "/opt/browser" in second
    assert second.count("# BEGIN GENUS LOCAL RELEASE") == 1
    assert "WorkingDirectory=" + str(tmp_path / "two") in second
    assert "WorkingDirectory=" + str(tmp_path / "one") not in second


def test_queue_inherits_instance_authentication(tmp_path, monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(deploy.subprocess, "run", run)
    result = deploy.queue(str(tmp_path), "abc123", {})
    argv = run.call_args.args[0]
    assert "--property=EnvironmentFile=/etc/robothor/robothor.env" in argv
    assert "--property=EnvironmentFile=-/run/robothor/secrets.env" in argv
    assert result["status"] == "queued"

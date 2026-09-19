"""An admitted run retains its verified knowledge across a release change."""

import shutil

import pytest

from robothor.templates.tests.test_fleet_release import source as source_fixture
from robothor.templates.tests.test_fleet_release import spec

source = source_fixture


def test_snapshot_pins_native_config_and_prompt_after_artifact_removal(source, tmp_path):
    from robothor.engine.config import build_system_prompt
    from robothor.templates.fleet_release import build_release
    from robothor.templates.fleet_snapshot import load_snapshot

    release = tmp_path / "release"
    receipt = build_release(source, release, spec())
    snapshot = load_snapshot(release, expected_digest=receipt["release_id"])
    config = snapshot.agent("ticket-router")
    shutil.rmtree(release)
    workspace = tmp_path / "runtime"
    (workspace / "brain").mkdir(parents=True)
    (workspace / "brain/WORKER.md").write_text("Unreviewed runtime override")
    prompt = build_system_prompt(config, workspace).full_text()
    assert "Use the approved knowledge." in prompt
    assert "Unreviewed claims remain unknown." in prompt
    assert "Unreviewed runtime override" not in prompt
    assert config.fleet_release_id == receipt["release_id"]
    assert config.model_primary == "openrouter/x/y"
    assert snapshot.agent("ticket-router") is not config
    with pytest.raises(ValueError):
        snapshot.agent("missing")


def test_two_releases_with_the_same_agent_never_share_prompt_content(source, tmp_path):
    from robothor.engine.config import build_system_prompt
    from robothor.templates.fleet_release import build_release
    from robothor.templates.fleet_snapshot import load_snapshot

    configs = []
    for version in ("first", "second"):
        (source / "brain/KNOWLEDGE.md").write_text(f"Knowledge from {version} release")
        target = tmp_path / version
        built = build_release(source, target, spec(version=version))
        configs.append(
            load_snapshot(target, expected_digest=built["release_id"]).agent("ticket-router")
        )
    for index in (0, 1, 0):
        prompt = build_system_prompt(configs[index], source).full_text()
        assert f"Knowledge from {('first', 'second')[index]} release" in prompt
        assert f"Knowledge from {('second', 'first')[index]} release" not in prompt


def test_drift_before_snapshot_is_refused(source, tmp_path):
    from robothor.templates.fleet_release import ReleaseError, build_release
    from robothor.templates.fleet_snapshot import load_snapshot

    target = tmp_path / "release"
    receipt = build_release(source, target, spec())
    (target / "brain/WORKER.md").write_text("Changed after review")
    with pytest.raises(ReleaseError):
        load_snapshot(target, expected_digest=receipt["release_id"])


def test_snapshot_does_not_fall_back_when_config_references_unpinned_content(source, tmp_path):
    from robothor.engine.config import build_system_prompt
    from robothor.templates.fleet_release import build_release
    from robothor.templates.fleet_snapshot import load_snapshot

    target = tmp_path / "release"
    receipt = build_release(source, target, spec())
    config = load_snapshot(target, expected_digest=receipt["release_id"]).agent("ticket-router")
    config.bootstrap_files.append("brain/UNREVIEWED.md")
    with pytest.raises(ValueError, match="snapshot"):
        build_system_prompt(config, source)


def test_warmup_uses_snapshot_content_and_refuses_unlisted_files(tmp_path):
    from robothor.engine.warmup import _build_context_files_section

    captured = (("brain/CONTEXT.md", "Reviewed context"),)
    rendered = _build_context_files_section(["brain/CONTEXT.md"], tmp_path, snapshot=captured)
    assert "Reviewed context" in rendered
    assert "release snapshot" in rendered
    with pytest.raises(ValueError, match="snapshot"):
        _build_context_files_section(["other.md"], tmp_path, snapshot=captured)


def test_capture_rejects_change_after_initial_verification(source, tmp_path, monkeypatch):
    from robothor.templates import fleet_snapshot
    from robothor.templates.fleet_release import ReleaseError, build_release

    target = tmp_path / "release"
    receipt = build_release(source, target, spec())
    original = fleet_snapshot.verify_release

    def verify_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        (target / "brain/WORKER.md").write_text("Changed during capture")
        return result

    monkeypatch.setattr(fleet_snapshot, "verify_release", verify_then_change)
    with pytest.raises(ReleaseError, match="capturing"):
        fleet_snapshot.load_snapshot(target, expected_digest=receipt["release_id"])

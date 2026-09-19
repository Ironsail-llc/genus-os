"""A fleet release is a complete, verified artifact, not an activated runtime."""

import json
import zipfile

import pytest
import yaml

from robothor.engine.tests.test_manifest_schema import _valid


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for folder in ("docs/agents", "docs/workflows", "brain", "config"):
        (root / folder).mkdir(parents=True)
    agent = {
        **_valid(),
        "instruction_file": "brain/WORKER.md",
        "bootstrap_files": ["brain/KNOWLEDGE.md"],
    }
    (root / "docs/agents/ticket-router.yaml").write_text(yaml.safe_dump(agent))
    (root / "brain/WORKER.md").write_text("# Instructions\nUse the approved knowledge.\n")
    (root / "brain/KNOWLEDGE.md").write_text("# Knowledge\nUnreviewed claims remain unknown.\n")
    (root / "docs/workflows/process.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "process",
                "steps": [{"id": "work", "type": "agent", "agent_id": "ticket-router"}],
            }
        )
    )
    return root


def spec(**changes):
    return {
        "schema_version": 1,
        "id": "example-fleet",
        "version": "v1",
        "source_revision": "a" * 40,
        "platform_revision": "b" * 40,
        "agents": ["docs/agents/ticket-router.yaml"],
        "workflows": ["docs/workflows/process.yaml"],
        "knowledge": ["brain/WORKER.md", "brain/KNOWLEDGE.md"],
        "plugins": [],
        **changes,
    }


def test_build_is_deterministic_and_verifies_the_whole_artifact(source, tmp_path):
    from robothor.templates.fleet_release import build_release, verify_release

    first, second = tmp_path / "first", tmp_path / "second"
    one = build_release(source, first, spec())
    two = build_release(source, second, spec())
    assert one == two
    assert verify_release(first, expected_digest=one["release_id"]) == one
    assert one["activation"] == "not_installed"
    assert len(one["files"]) == 4
    assert (first / "brain/KNOWLEDGE.md").read_text() == (source / "brain/KNOWLEDGE.md").read_text()
    assert not (source / "release.json").exists()


@pytest.mark.parametrize("fault", ["changed", "extra", "removed", "symlink", "metadata"])
def test_artifact_drift_is_refused(source, tmp_path, fault):
    from robothor.templates.fleet_release import ReleaseError, build_release, verify_release

    target = tmp_path / "release"
    built = build_release(source, target, spec())
    member = target / "brain/KNOWLEDGE.md"
    if fault == "changed":
        member.write_text("different")
    elif fault == "extra":
        (target / "unlisted.txt").write_text("extra")
    elif fault == "removed":
        member.unlink()
    elif fault == "symlink":
        member.unlink()
        member.symlink_to(source / "brain/KNOWLEDGE.md")
    else:
        document = json.loads((target / "release.json").read_text())
        document["version"] = "changed"
        (target / "release.json").write_text(json.dumps(document))
    with pytest.raises(ReleaseError):
        verify_release(target, expected_digest=built["release_id"])


@pytest.mark.parametrize(
    "fault", ["reference", "duplicate", "traversal", "secret", "schema", "workflow"]
)
def test_invalid_candidate_never_publishes_a_partial_release(source, tmp_path, fault):
    from robothor.templates.fleet_release import ReleaseError, build_release

    data = spec()
    if fault == "reference":
        data["knowledge"].remove("brain/WORKER.md")
    elif fault == "duplicate":
        data["knowledge"].append("brain/WORKER.md")
    elif fault == "traversal":
        data["knowledge"].append("../outside.md")
    elif fault == "secret":
        (source / "brain/WORKER.md").write_text("api_key: " + "not-a-reference")
    elif fault == "schema":
        (source / "docs/agents/ticket-router.yaml").write_text("id: missing-required-fields\n")
    else:
        (source / "docs/workflows/process.yaml").write_text(
            "id: process\nsteps:\n- id: unknown\n  type: agent\n  agent_id: not-in-release\n"
        )
    target = tmp_path / "release"
    with pytest.raises(ReleaseError):
        build_release(source, target, data)
    assert not target.exists()


def test_existing_release_is_never_overwritten(source, tmp_path):
    from robothor.templates.fleet_release import ReleaseError, build_release

    target = tmp_path / "release"
    target.mkdir()
    (target / "keep.txt").write_text("existing")
    with pytest.raises(ReleaseError):
        build_release(source, target, spec())
    assert (target / "keep.txt").read_text() == "existing"


@pytest.mark.parametrize("extra_declaration", [False, True])
def test_plugin_surface_must_match_in_both_directions(source, tmp_path, extra_declaration):
    from robothor.templates.fleet_release import ReleaseError, build_release

    path = "adapter-1.0-py3-none-any.whl"
    declared = {"genus.services": ["adapter"]}
    if extra_declaration:
        declared["genus.tools"] = ["missing"]
    with zipfile.ZipFile(source / path, "w") as wheel:
        wheel.writestr("adapter-1.0.dist-info/METADATA", "Name: adapter\nVersion: 1.0\n")
        wheel.writestr(
            "adapter-1.0.dist-info/entry_points.txt",
            "[genus.services]\nadapter = adapter:SERVICES\n",
        )
        wheel.writestr(
            "adapter/genus-plugin.yaml",
            yaml.safe_dump(
                {
                    "name": "adapter",
                    "contract_version": 1,
                    "entry_points": declared,
                    "services": ["sales.business.example"],
                }
            ),
        )
        wheel.writestr("adapter/__init__.py", "raise RuntimeError('must not import')")
    if extra_declaration:
        with pytest.raises(ReleaseError, match="entry points"):
            build_release(source, tmp_path / "release", spec(plugins=[path]))
    else:
        result = build_release(source, tmp_path / "release", spec(plugins=[path]))
        assert result["contracts"]["plugins"][0]["name"] == "adapter"


@pytest.mark.parametrize(
    "switch",
    [
        "research_enabled",
        "enrichment_enabled",
        "promotion_enabled",
        "sending_enabled",
        "outcomes_enabled",
    ],
)
def test_release_cannot_carry_enabled_sales_settings(source, tmp_path, switch):
    from robothor.templates.fleet_release import ReleaseError, build_release

    (source / "config/settings.yaml").write_text(yaml.safe_dump({switch: True}))
    with pytest.raises(ReleaseError, match="switches disabled"):
        build_release(source, tmp_path / "release", spec(sales_settings="config/settings.yaml"))

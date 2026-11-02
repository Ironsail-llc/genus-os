"""The engine's manifest store: the file half of the agent builder.

The engine is the process that owns the workspace, so it is the one that reads
and writes ``docs/agents/*.yaml`` and ``brain/*.md``. A bridge that wrote them
itself only worked where every service happened to mount the same volume; on a
deployment where the bridge has none, the fleet list was empty and every write
landed somewhere the engine never reads.

These tests hold the file-level properties down, on a throwaway workspace in
``tmp_path`` (rule 11 — never the operator's real one):

* paths are derived from a validated id and refuse a symlinked root;
* a write is atomic, an exclusive write is a real create, and a failure leaves
  no temp residue in the directory the loader globs;
* the history ring keeps five, and retiring moves rather than unlinks;
* an edit touches only the form-owned paths;
* the scaffold cannot be YAML-injected through operator text.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

EXISTING = {
    "id": "demo-agent",
    "name": "Demo Agent",
    "description": "Does the demo work",
    "version": "2026-09-01",
    "department": "operations",
    "model": {"primary": "openrouter/example/demo-model", "temperature": 0.1},
    "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    "delivery": {"mode": "none"},
    "tools_allowed": ["exec", "read_file", "write_file"],
    "instruction_file": "brain/DEMO_AGENT.md",
    "v2": {"custom_plugin_key": 7},
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    return tmp_path


@pytest.fixture
def manifest_dir(workspace):
    return workspace / "docs" / "agents"


@pytest.fixture
def seeded(manifest_dir):
    path = manifest_dir / "demo-agent.yaml"
    path.write_text(yaml.safe_dump(EXISTING, sort_keys=False))
    return path


@pytest.fixture
def store():
    from robothor.engine import manifest_store

    return manifest_store


class TestPaths:
    def test_the_store_resolves_the_engines_own_workspace(self, store, workspace):
        assert store.workspace() == workspace
        assert store.manifest_dir() == workspace / "docs" / "agents"

    @pytest.mark.parametrize("agent_id", ["../x", "a/b", "A", "", "x\ny", 7, None])
    def test_an_unsafe_id_is_a_422_naming_the_rule(self, store, workspace, agent_id):
        with pytest.raises(store.ManifestError) as refused:
            store.safe_id(agent_id)

        assert refused.value.status_code == 422
        assert str(workspace) not in str(refused.value.detail)

    def test_a_manifest_path_stays_inside_the_manifest_dir(self, store, manifest_dir):
        path = store.manifest_path("demo-agent")

        assert path == manifest_dir.resolve() / "demo-agent.yaml"

    def test_a_symlinked_manifest_root_is_refused(self, store, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        (workspace / "docs").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (workspace / "docs" / "agents").symlink_to(elsewhere, target_is_directory=True)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)

        with pytest.raises(store.ManifestError) as refused:
            store.manifest_path("demo-agent")

        assert refused.value.status_code == 422
        assert "symlink" in str(refused.value.detail).lower()

    def test_an_instruction_file_outside_brain_is_refused(self, store, workspace):
        with pytest.raises(store.ManifestError) as refused:
            store.instruction_path("docs/agents/demo-agent.yaml")

        assert refused.value.status_code == 422

    def test_reading_an_out_of_workspace_instruction_file_is_empty_not_fatal(
        self, store, workspace
    ):
        assert store.read_instructions({"instruction_file": "../../etc/passwd"}) == ""


class TestAtomicWrites:
    def test_an_exclusive_write_refuses_an_existing_file(self, store, seeded):
        before = seeded.read_text()

        with pytest.raises(FileExistsError):
            store.write_atomically(seeded, "id: other\n", exclusive=True)

        assert seeded.read_text() == before
        assert not list(seeded.parent.glob(".*.tmp"))

    def test_an_interrupted_write_leaves_no_temp_file(self, store, manifest_dir, monkeypatch):
        import os

        def _interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "fsync", _interrupt)

        with pytest.raises(KeyboardInterrupt):
            store.write_atomically(manifest_dir / "x.yaml", "id: x\n")

        assert not list(manifest_dir.glob(".*.tmp"))
        assert not (manifest_dir / "x.yaml").exists()


class TestHistoryAndRetire:
    def test_the_history_ring_keeps_five(self, store, seeded):
        for _ in range(7):
            store.snapshot("demo-agent")

        history = list((seeded.parent / ".history" / "demo-agent").glob("*.yaml"))
        assert len(history) == store.HISTORY_DEPTH == 5

    def test_retire_moves_the_manifest_and_the_loader_stops_seeing_it(self, store, seeded):
        from robothor.engine.config import load_manifest_dir

        store.retire("demo-agent", seeded)

        assert not seeded.exists()
        assert (seeded.parent / "retired" / "demo-agent.yaml").is_file()
        assert load_manifest_dir(seeded.parent).manifests == ()

    def test_scan_reads_the_engines_manifest_dir(self, store, seeded):
        scan = store.scan()

        assert [m["id"] for m in scan.manifests] == ["demo-agent"]


class TestEditing:
    def test_merge_owned_writes_only_the_form_owned_paths(self, store):
        document = yaml.safe_load(yaml.safe_dump(EXISTING))

        store.merge_owned(document, {"name": "Renamed", "v2.sandbox": "host"})

        assert document["name"] == "Renamed"
        assert document["v2"] == {"custom_plugin_key": 7}
        assert document["model"]["temperature"] == 0.1

    def test_a_replaced_list_path_must_be_a_list(self, store):
        with pytest.raises(store.ManifestError) as refused:
            store.merge_owned(dict(EXISTING), {"tools_allowed": "exec"})

        assert refused.value.status_code == 422

    def test_bump_version_stamps_today_and_appends_one_entry(self, store):
        from datetime import UTC, datetime

        document = dict(EXISTING)
        store.bump_version(document, "Edited")

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert document["version"] == today
        assert document["changelog"] == [{"date": today, "change": "Edited"}]

    def test_unparseable_yaml_is_a_422_not_a_raise(self, store):
        with pytest.raises(store.ManifestError) as refused:
            store.parse("id: x\nname: [unclosed")

        assert refused.value.status_code == 422


class TestScaffold:
    def test_operator_text_cannot_inject_yaml(self, store, workspace):
        hostile = 'Ops: Bot #1 & "co"'
        body = store.CreateRequest(
            name=hostile, description="line one\nid: other-agent\ndelivery:\n  mode: announce"
        )

        document, relative, instructions = store.scaffold("ops-bot", body)

        assert document["id"] == "ops-bot"
        assert document["name"] == hostile
        assert document["delivery"]["mode"] == "none"
        assert relative == "brain/OPS_BOT.md"
        assert "## Your Role" in instructions

    def test_write_pair_conflicts_instead_of_overwriting(self, store, seeded, workspace):
        with pytest.raises(store.ManifestError) as refused:
            store.write_pair("demo-agent", {"id": "demo-agent"}, "brain/DEMO_AGENT.md", "# x\n")

        assert refused.value.status_code == 409
        assert yaml.safe_load(seeded.read_text())["name"] == "Demo Agent"


class TestTheMainAgent:
    def test_it_is_the_agent_segment_of_the_main_session_key(self, store, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MAIN_SESSION_KEY", "agent:concierge:primary")

        assert store.main_agent_id() == "concierge"


def test_the_bridge_no_longer_carries_its_own_copy_of_the_store():
    """One code path. A bridge that kept a local reader would answer from a
    disk it does not have on a deployment where only the engine mounts the
    workspace."""
    router = (
        Path(__file__).resolve().parents[3] / "crm" / "bridge" / "routers" / "agent_manifests.py"
    ).read_text()

    assert "load_manifest_dir" not in router
    assert "_write_atomically" not in router
    assert "EngineConfig" not in router

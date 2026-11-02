"""The fleet listing is served from a cached scan, and every write drops it.

The dashboard asks for ``GET /api/agent-manifests`` (the bridge forwards it to
``GET /api/admin/agent-manifests`` here) each time a message goes to a named
agent, and every ask re-read and YAML-parsed the whole fleet. These tests hold
the saving down, and the two ways it must never go stale:

* a second listing with nothing changed opens no manifest file;
* anything that changes what a scan would read is seen by the next listing:
  a file edited, added or removed on disk (by the CLI, by ``genus plugin
  sync``, by another process on the same volume), the fleet defaults, the
  schema mode, the environment ``${VAR}`` expands from;
* every write path in the engine drops the cache explicitly, so its own writes
  never depend on the filesystem's clock: the store's writes and retires, a
  reconcile (which every bridge-side install ends with), a manual trigger, and
  the plugin sync, install and remove routes;
* validation before a write still scans fresh: a verdict about a write must not
  rest on a cached view of the fleet.

On a throwaway workspace in ``tmp_path`` (rule 11).
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI
from starlette.testclient import TestClient

from robothor.engine import manifest_listing as cache
from robothor.engine.schedule_reconcile import ReconcileResult

AGENT = {
    "id": "demo-agent",
    "name": "Demo Agent",
    "description": "Does the demo work",
    "version": "2026-09-01",
    "department": "operations",
    "model": {"primary": "openrouter/example/demo-model"},
    "delivery": {"mode": "none"},
    "tools_allowed": ["read_file"],
    "instruction_file": "brain/DEMO_AGENT.md",
}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Three agents in a throwaway workspace."""
    manifests = tmp_path / "docs" / "agents"
    manifests.mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    for index in range(3):
        agent_id = f"demo-agent-{index}"
        document = {**AGENT, "id": agent_id, "name": f"Demo Agent {index}"}
        (manifests / f"{agent_id}.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    return manifests


@pytest.fixture
def reads(monkeypatch):
    """Counts every manifest file, and every defaults file, the loader opens."""
    from robothor.engine import config

    counter = {"files": 0, "defaults": 0}
    real_load = config._load_manifest_classified
    real_defaults = config._load_defaults

    def _counting_load(path, *args, **kwargs):
        counter["files"] += 1
        return real_load(path, *args, **kwargs)

    def _counting_defaults(*args, **kwargs):
        counter["defaults"] += 1
        return real_defaults(*args, **kwargs)

    monkeypatch.setattr(config, "_load_manifest_classified", _counting_load)
    monkeypatch.setattr(config, "_load_defaults", _counting_defaults)
    return counter


def _reset(counter: dict[str, int]) -> None:
    counter["files"] = counter["defaults"] = 0


@pytest.fixture
def store():
    from robothor.engine import manifest_store

    cache.invalidate_listing()
    yield manifest_store
    cache.invalidate_listing()


def _identity_middleware(app: FastAPI) -> None:
    from robothor.auth.deps import AuthContext

    @app.middleware("http")
    async def _as_bridge(request, call_next):
        request.state.auth = AuthContext(
            user_id="genus-bridge",
            tenant_id="test-tenant",
            role="service",
            typ="service",
            scopes=frozenset({"engine:control"}),
        )
        return await call_next(request)


@pytest.fixture
def fake_scheduler():
    scheduler = MagicMock()
    scheduler.reconcile = AsyncMock(
        return_value=ReconcileResult(added=[], replaced=[], pruned=[], blocked={}, clean=True)
    )
    return scheduler


@pytest.fixture
def client(fleet, store, fake_scheduler):
    from robothor.engine import admin_manifests, admin_scheduler

    app = FastAPI()
    _identity_middleware(app)
    admin_manifests.register(app, scheduler=fake_scheduler, runner=MagicMock())
    admin_scheduler.register(app, scheduler=fake_scheduler)
    with patch("robothor.engine.admin_scheduler.registered_tool_names", return_value=["read_file"]):
        yield TestClient(app)


def _pin_mtimes(path, before) -> None:
    """Put a file and its directory back on the clock they had before a write:
    a coarse filesystem clock can give an edit the mtime of the read before it,
    and the explicit invalidation must not depend on the clock."""
    os.utime(path, ns=(before["file"].st_atime_ns, before["file"].st_mtime_ns))
    os.utime(path.parent, ns=(before["dir"].st_atime_ns, before["dir"].st_mtime_ns))


def _stat(path):
    return {"file": path.stat(), "dir": path.parent.stat()}


def _names(client) -> dict[str, str]:
    body = client.get("/api/admin/agent-manifests").json()
    return {row["id"]: row["name"] for row in body["agents"]}


# ─── The saving ──────────────────────────────────────────────────────


class TestASecondListingReadsNoManifestFile:
    def test_through_the_store(self, store, fleet, reads):
        first = cache.listing()
        assert len(first.manifests) == 3
        assert reads["files"] == 3

        _reset(reads)
        second = cache.listing()

        assert second is first
        assert reads == {"files": 0, "defaults": 0}

    def test_through_the_admin_route(self, client, reads):
        first = client.get("/api/admin/agent-manifests")
        assert first.status_code == 200, first.text
        assert first.json()["count"] == 3
        assert reads["files"] == 3

        _reset(reads)
        second = client.get("/api/admin/agent-manifests")

        assert second.status_code == 200
        assert second.json() == first.json()
        assert reads == {"files": 0, "defaults": 0}

    def test_validation_still_scans_fresh(self, client, reads):
        """A verdict about a write is not taken from a cached fleet."""
        client.get("/api/admin/agent-manifests")
        _reset(reads)

        response = client.post(
            "/api/admin/agent-manifests/validate",
            json={"manifest": {**AGENT, "id": "demo-agent-7", "name": "Demo Agent 7"}},
        )

        assert response.status_code == 200, response.text
        assert reads["files"] >= 3


# ─── What changes on disk is seen ────────────────────────────────────


class TestAChangeOnDiskIsSeenByTheNextListing:
    def test_a_file_edited_in_place(self, store, fleet):
        cache.listing()
        path = fleet / "demo-agent-1.yaml"
        document = yaml.safe_load(path.read_text())
        document["name"] = "Renamed Outside The Engine"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        later = time.time() + 5
        os.utime(path, (later, later))

        names = {m["id"]: m["name"] for m in cache.listing().manifests}
        assert names["demo-agent-1"] == "Renamed Outside The Engine"

    def test_a_file_added_or_removed(self, store, fleet):
        assert len(cache.listing().manifests) == 3
        (fleet / "demo-agent-0.yaml").unlink()
        assert len(cache.listing().manifests) == 2
        document = {**AGENT, "id": "demo-agent-9", "name": "Demo Agent 9"}
        (fleet / "demo-agent-9.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
        assert len(cache.listing().manifests) == 3

    def test_a_directory_mtime_change_alone(self, store, fleet, reads):
        """An entry that does not match ``*.yaml`` (a rename in flight, a
        retired move) still moves the directory, and that is enough."""
        cache.listing()
        _reset(reads)
        later = time.time() + 5
        os.utime(fleet, (later, later))

        cache.listing()

        assert reads["files"] == 3

    def test_the_fleet_defaults(self, store, fleet, reads):
        """``_defaults.yaml`` is merged into every manifest at load, so a
        change to it alone re-reads the fleet."""
        cache.listing()
        _reset(reads)
        (fleet / "_defaults.yaml").write_text(
            yaml.safe_dump({"model": {"temperature": 0.2}}, sort_keys=False)
        )
        later = time.time() + 5
        os.utime(fleet / "_defaults.yaml", (later, later))
        os.utime(fleet, ns=(fleet.stat().st_atime_ns, fleet.stat().st_mtime_ns))

        cache.listing()

        # Every agent again (the loader also opens ``_defaults.yaml`` itself
        # under the glob before skipping it).
        assert reads["files"] >= 3
        assert reads["defaults"] == 1

    def test_the_environment_the_manifests_expand_from(self, store, fleet, monkeypatch):
        monkeypatch.setenv("DEMO_AGENT_BLURB", "first blurb")
        path = fleet / "demo-agent-0.yaml"
        document = yaml.safe_load(path.read_text())
        document["description"] = "${DEMO_AGENT_BLURB}"
        path.write_text(yaml.safe_dump(document, sort_keys=False))

        def _blurb() -> str:
            rows = {m["id"]: m for m in cache.listing().manifests}
            return rows["demo-agent-0"]["description"]

        assert _blurb() == "first blurb"
        monkeypatch.setenv("DEMO_AGENT_BLURB", "second blurb")
        assert _blurb() == "second blurb"

    def test_the_schema_mode(self, store, fleet, reads, monkeypatch):
        from robothor.engine import manifest_schema

        cache.listing()
        _reset(reads)
        monkeypatch.setattr(manifest_schema, "schema_mode", lambda: "a-different-mode")

        cache.listing()

        assert reads["files"] == 3

    def test_a_missing_directory_is_never_cached(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path / "nowhere"))
        monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
        assert list(cache.listing().manifests) == []
        manifests = tmp_path / "nowhere" / "docs" / "agents"
        manifests.mkdir(parents=True)
        (manifests / "demo-agent.yaml").write_text(yaml.safe_dump(AGENT, sort_keys=False))
        assert [m["id"] for m in cache.listing().manifests] == ["demo-agent"]


# ─── Every write drops it, whatever the clock says ───────────────────


class TestTheStoresOwnWritesInvalidate:
    def test_replace_manifest(self, store, fleet):
        cache.listing()
        path = fleet / "demo-agent-2.yaml"
        before = _stat(path)
        document = yaml.safe_load(path.read_text())
        # Same length as "Demo Agent 2", so name, size and (pinned) mtimes all
        # match the scan before it: only the explicit invalidation can tell.
        store.replace_manifest(path, {**document, "name": "Demo Agent X"})
        _pin_mtimes(path, before)
        assert path.stat().st_size == before["file"].st_size

        names = {m["id"]: m["name"] for m in cache.listing().manifests}
        assert names["demo-agent-2"] == "Demo Agent X"

    def test_write_atomically_even_when_it_fails(self, store, fleet, reads):
        cache.listing()
        _reset(reads)
        before_dir = fleet.stat()
        with (
            patch("robothor.engine.manifest_store.os.fsync", side_effect=OSError("disk")),
            pytest.raises(OSError),
        ):
            store.write_atomically(fleet / "demo-agent-2.yaml", "id: demo-agent-2\n")
        os.utime(fleet, ns=(before_dir.st_atime_ns, before_dir.st_mtime_ns))

        cache.listing()
        assert reads["files"] == 3

    def test_retire(self, store, fleet):
        cache.listing()
        path = fleet / "demo-agent-0.yaml"
        before_dir = fleet.stat()
        store.retire("demo-agent-0", path)
        os.utime(fleet, ns=(before_dir.st_atime_ns, before_dir.st_mtime_ns))

        assert {m["id"] for m in cache.listing().manifests} == {"demo-agent-1", "demo-agent-2"}

    def test_an_invalidation_during_a_scan_is_not_hidden_by_the_cache(
        self, store, fleet, monkeypatch
    ):
        """A write that lands while a scan is reading drops the cache before
        the scan stores its result; that result must not be kept, or the next
        listing is served from a view older than the write."""
        from robothor.engine import config

        real_scan = config.load_manifest_dir
        calls = {"n": 0}

        def _scan_racing_a_write(directory):
            calls["n"] += 1
            result = real_scan(directory)
            if calls["n"] == 1:
                cache.invalidate_listing()
            return result

        monkeypatch.setattr(config, "load_manifest_dir", _scan_racing_a_write)
        cache.listing()
        cache.listing()

        assert calls["n"] == 2


class TestTheAdminRoutesInvalidate:
    def test_a_disable_through_the_route(self, client, fleet):
        _names(client)
        path = fleet / "demo-agent-2.yaml"
        before = _stat(path)

        response = client.post("/api/admin/agent-manifests/demo-agent-2/disable")
        assert response.status_code == 200, response.text
        _pin_mtimes(path, before)

        body = client.get("/api/admin/agent-manifests").json()
        row = next(row for row in body["agents"] if row["id"] == "demo-agent-2")
        assert row["enabled"] is False

    def test_a_patch_through_the_route(self, client, fleet):
        _names(client)
        path = fleet / "demo-agent-1.yaml"
        before = _stat(path)

        response = client.patch(
            "/api/admin/agent-manifests/demo-agent-1", json={"name": "Patched Name"}
        )
        assert response.status_code == 200, response.text
        _pin_mtimes(path, before)

        assert _names(client)["demo-agent-1"] == "Patched Name"

    def test_a_retire_through_the_route(self, client, fleet):
        _names(client)
        before_dir = fleet.stat()

        response = client.request(
            "DELETE", "/api/admin/agent-manifests/demo-agent-0", json={"confirm": "demo-agent-0"}
        )
        assert response.status_code == 200, response.text
        os.utime(fleet, ns=(before_dir.st_atime_ns, before_dir.st_mtime_ns))

        assert "demo-agent-0" not in _names(client)

    def test_a_reconcile(self, client, reads):
        """Every bridge-side install (the hub install, the setup preset) writes
        its manifests and then asks the engine to reconcile: that call is the
        engine's word that the fleet changed."""
        _names(client)
        _reset(reads)

        response = client.post("/api/admin/scheduler/reconcile")
        assert response.status_code == 200, response.text
        _reset(reads)
        _names(client)

        assert reads["files"] == 3

    def test_a_manual_trigger(self, client, reads):
        _names(client)
        trigger = AsyncMock(return_value={"status": "started", "agent_id": "demo-agent-0"})
        with patch("robothor.engine.manual_trigger.trigger_agent", new=trigger):
            response = client.post("/api/admin/agent-manifests/demo-agent-0/run")
        assert response.status_code == 200, response.text
        _reset(reads)

        _names(client)

        assert reads["files"] == 3


class TestThePluginPathsInvalidate:
    """A plugin that ships agents writes their manifests on sync, install and
    remove. In the engine those run through ``admin_plugins``; the CLI's
    ``genus plugin sync`` runs in its own process, and its writes reach this
    one's cache through the file signature (``TestAChangeOnDiskIsSeen…``)."""

    def _listed_then_reset(self, store, reads) -> None:
        cache.listing()
        _reset(reads)

    def test_sync(self, store, fleet, reads):
        from robothor.engine import admin_plugins

        self._listed_then_reset(store, reads)
        result = MagicMock(ok=True, recorded=[], added=[], updated=[], removed=[])
        with patch("robothor.plugins.lockfile.sync", return_value=result):
            admin_plugins.sync_lockfile()
        cache.listing()

        assert reads["files"] == 3

    def test_a_refused_sync_still_invalidates(self, store, fleet, reads):
        from robothor.engine import admin_plugins

        self._listed_then_reset(store, reads)
        result = MagicMock(ok=False, refused="unreadable")
        with (
            patch("robothor.plugins.lockfile.sync", return_value=result),
            pytest.raises(admin_plugins.LockfileRefusedError),
        ):
            admin_plugins.sync_lockfile()
        cache.listing()

        assert reads["files"] == 3

    def test_install(self, store, fleet, reads):
        from robothor.engine import admin_plugins

        self._listed_then_reset(store, reads)
        outcome = MagicMock()
        outcome.as_json.return_value = {"installed": True}
        with patch("robothor.plugins.installer.install", return_value=outcome):
            admin_plugins.install_plugin("example-plugin")
        cache.listing()

        assert reads["files"] == 3

    def test_an_install_that_raises_still_invalidates(self, store, fleet, reads):
        from robothor.engine import admin_plugins

        self._listed_then_reset(store, reads)
        with (
            patch("robothor.plugins.installer.install", side_effect=RuntimeError("half done")),
            pytest.raises(RuntimeError),
        ):
            admin_plugins.install_plugin("example-plugin")
        cache.listing()

        assert reads["files"] == 3

    def test_remove(self, store, fleet, reads):
        from robothor.engine import admin_plugins

        self._listed_then_reset(store, reads)
        with patch("robothor.plugins.installer.remove", return_value={"removed": True}):
            admin_plugins.remove_plugin("example-plugin")
        cache.listing()

        assert reads["files"] == 3

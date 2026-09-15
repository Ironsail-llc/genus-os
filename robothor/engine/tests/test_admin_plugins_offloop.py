"""The plugin routes must not do their work on the engine's event loop.

Every one of them walks ``importlib.metadata.entry_points()`` over every
distribution on ``sys.path``, reads the lockfile, reads each manifest and — on
a reload, and on a listing that meets a distribution installed since boot —
runs ``ep.load()``, which executes third-party module bodies. Measured on a
real venv with 170 distributions that is ~32 ms per call, and ``ep.load()`` has
no bound at all: one package that opens a socket at import time stalls every
other request the engine is serving.

``admin_providers`` already answers this correctly with ``asyncio.to_thread``,
and the bridge side was added to the genuinely-async allowlist with the comment
"this side owns no blocking work at all" — which is true, and which is what
made the engine side the place all of it landed.

These tests assert the mechanism rather than a duration: a timing assertion on
a loaded CI box is a flake, and what is actually load-bearing is that the
blocking call happens on a worker thread and not on the loop.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_MANIFEST = "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n"


class _Dist:
    def __init__(self, name="acme-tools", version="1.0.0", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, name="probe", group="genus.tools", dist=None):
        self.name, self.group, self.dist = name, group, dist or _Dist()

    def load(self):
        return {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}


def _make_app():
    config = MagicMock()
    config.tenant_id = "test-tenant"
    config.bot_token = ""
    config.port = 18800

    from robothor.engine.health import create_health_app

    with (
        patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
        patch("robothor.engine.dashboards.get_completion_router", return_value=APIRouter()),
        patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
        patch("robothor.db.connection.get_connection"),
    ):
        return create_health_app(config, runner=None, workflow_engine=None)


@pytest.fixture
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


@pytest.fixture
def one_plugin():
    with patch.object(loader, "_discover", lambda: [_EP()]):
        yield


def _on_a_running_loop() -> bool:
    """Whether the caller is executing ON an event loop.

    The exact property, rather than a thread-name heuristic: ``to_thread``'s
    workers are called ``asyncio_N``, so matching on the name would have called
    the offloaded case a failure. A worker thread has no running loop; an
    ``async def`` body that did the work inline does.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def discovery_sites():
    """Records, for each discovery call, whether it ran on the event loop."""
    seen: list[bool] = []

    def _record():
        seen.append(_on_a_running_loop())
        return [_EP()]

    with patch.object(loader, "_discover", _record):
        yield seen


class TestTheProbeDiscriminates:
    """A probe that can only ever answer "off the loop" pins nothing."""

    def test_it_says_true_on_a_loop_and_false_off_one(self):
        assert asyncio.run(_answer_on_the_loop()) is True
        assert asyncio.run(_answer_in_a_thread()) is False
        assert _on_a_running_loop() is False


async def _answer_on_the_loop() -> bool:
    return _on_a_running_loop()


async def _answer_in_a_thread() -> bool:
    return await asyncio.to_thread(_on_a_running_loop)


class TestTheWorkIsOffloaded:
    def test_the_listing_discovers_off_the_loop(self, client, discovery_sites):
        client.get("/api/admin/plugins")
        assert discovery_sites, "discovery never ran — the assertion below would be vacuous"
        assert not any(discovery_sites)

    def test_the_reload_discovers_off_the_loop(self, client, discovery_sites):
        client.post("/api/admin/plugins/reload")
        assert discovery_sites
        assert not any(discovery_sites)

    def test_sync_discovers_off_the_loop(self, client, discovery_sites):
        client.post("/api/admin/plugins/sync")
        assert discovery_sites
        assert not any(discovery_sites)

    def test_enable_writes_off_the_loop(self, client, one_plugin):
        lockfile.sync()
        sites: list[bool] = []
        real = lockfile.set_enabled

        def _record(*args, **kwargs):
            sites.append(_on_a_running_loop())
            return real(*args, **kwargs)

        with patch.object(lockfile, "set_enabled", _record):
            client.post("/api/admin/plugins/acme-tools/disable")
        assert sites
        assert not any(sites)

    def test_a_slow_plugin_does_not_stall_the_loop(self, client):
        """The property the thread name is a proxy for.

        A listing runs ``ep.load()`` on any distribution installed since boot,
        and a plugin whose module body blocks would otherwise hold every other
        request on this process.
        """
        started = threading.Event()
        release = threading.Event()

        class _SlowEP(_EP):
            def load(self):
                started.set()
                assert release.wait(timeout=5), "the loop never got a chance to run"
                return super().load()

        with patch.object(loader, "_discover", lambda: [_SlowEP()]):
            worker = threading.Thread(target=lambda: client.get("/api/admin/plugins"))
            worker.start()
            assert started.wait(timeout=5)
            # The loop is still serving while the plugin import is parked.
            assert client.get("/live").status_code == 200
            release.set()
            worker.join(timeout=10)
        assert not worker.is_alive()


class TestSyncRoute:
    def test_it_records_and_reports_what_it_wrote(self, client, one_plugin):
        body = client.post("/api/admin/plugins/sync").json()
        assert body["recorded"] == ["acme-tools"]
        assert body["added"] == ["acme-tools"]
        assert body["removed"] == []
        assert body["reloaded"] is False
        assert lockfile.read_lockfile().row("acme-tools") is not None

    def test_it_reports_a_distribution_that_is_gone(self, client, one_plugin, plugin_lockfile):
        lockfile.sync()
        with patch.object(loader, "_discover", list):
            body = client.post("/api/admin/plugins/sync").json()
        assert body["removed"] == ["acme-tools"]

    def test_it_keeps_a_disable(self, client, one_plugin):
        lockfile.sync()
        lockfile.set_enabled("acme-tools", False)
        client.post("/api/admin/plugins/sync")
        assert lockfile.read_lockfile().row("acme-tools").enabled is False

    def test_it_refuses_over_an_unreadable_lockfile(self, client, one_plugin, plugin_lockfile):
        lockfile.sync()
        plugin_lockfile.write_text("{{{", encoding="utf-8")
        response = client.post("/api/admin/plugins/sync")
        assert response.status_code == 409
        assert "re-enable" in response.json()["detail"]

    def test_the_refusal_carries_no_path(self, client, one_plugin, plugin_lockfile):
        from pathlib import Path

        lockfile.sync()
        plugin_lockfile.write_text("{{{", encoding="utf-8")
        raw = client.post("/api/admin/plugins/sync").text
        assert str(Path.home()) not in raw
        assert str(plugin_lockfile) not in raw

    def test_an_unwritable_path_is_503_not_409(self, client, tmp_path, monkeypatch):
        """A read-only filesystem is not "re-run with --force".

        Both used to come back 409, so a caller could not tell "your lockfile
        has unreadable rows, force it" — which `--force` fixes — from "the
        disk will not take a write", which it cannot. The status is the
        contract; an accepting-any-5xx assertion pinned neither.
        """
        occupied = tmp_path / "occupied.lock"
        occupied.mkdir()
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(occupied))
        # reset_settings() because ROBOTHOR_PLUGIN_LOCKFILE resolves through the
        # settings SINGLETON, and building the app now reads settings (it asks
        # which plugin channels are armed, to mount their routers). Before that
        # this monkeypatch happened to land before anything had resolved
        # settings at all; relying on that was a latent dependency on nothing
        # else reading configuration first.
        from robothor.settings import reset_settings

        reset_settings()
        with patch.object(loader, "_discover", lambda: [_EP()]):
            response = client.post("/api/admin/plugins/sync")
        assert response.status_code == 503
        assert "IsADirectoryError" in response.text
        assert "--force" not in response.text, "force cannot fix a read-only path"

    def test_undecodable_bytes_are_409_not_a_bare_500(self, client, one_plugin, plugin_lockfile):
        """A third corruption shape, and the one both handlers missed.

        ``Path.read_text`` raises ``UnicodeDecodeError`` — a ``ValueError``,
        not an ``OSError`` — so a lockfile holding one non-UTF-8 byte fell past
        both the 409 and the 503 arm and came back as a bare 500.
        """
        lockfile.sync()
        plugin_lockfile.write_bytes(b'\xff\xfe{"lockfile_version": 1, "plugins": []}')

        response = client.post("/api/admin/plugins/sync")
        assert response.status_code == 409
        assert "--force" in response.json()["detail"]

    def test_undecodable_bytes_do_not_break_the_listing(self, client, one_plugin, plugin_lockfile):
        lockfile.sync()
        plugin_lockfile.write_bytes(b"\xff\xfe not text")
        body = client.get("/api/admin/plugins").json()
        assert body["lockfile"]["malformed"] is True
        assert body["plugins"][0]["state"] == "loaded"

    def test_undecodable_bytes_do_not_break_a_disable(self, client, one_plugin, plugin_lockfile):
        lockfile.sync()
        plugin_lockfile.write_bytes(b"\xff\xfe not text")
        # No readable row, so 404 — the honest answer, and not a 500.
        assert client.post("/api/admin/plugins/acme-tools/disable").status_code == 404

    def test_the_two_failures_have_different_statuses(
        self, client, one_plugin, plugin_lockfile, tmp_path, monkeypatch
    ):
        """409 and 503 must not both mean "sync did not happen"."""
        lockfile.sync()
        plugin_lockfile.write_text("{{{", encoding="utf-8")
        unreadable = client.post("/api/admin/plugins/sync")
        assert unreadable.status_code == 409
        assert "--force" in unreadable.json()["detail"]

        from robothor.settings import reset_settings

        occupied = tmp_path / "occupied.lock"
        occupied.mkdir()
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(occupied))
        reset_settings()  # the path is a cached setting; this test moves it
        assert client.post("/api/admin/plugins/sync").status_code == 503

    def test_disable_on_an_unreadable_path_is_503_not_404(
        self, client, one_plugin, tmp_path, monkeypatch
    ):
        """404 would be a lie about WHY, and would send the operator to `sync`
        — which is about to hit the same unreadable path."""
        target = tmp_path / "ro"
        target.mkdir()
        (target / "plugins.lock").mkdir()
        monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(target / "plugins.lock"))
        # reset_settings() because ROBOTHOR_PLUGIN_LOCKFILE resolves through the
        # settings SINGLETON, and building the app now reads settings (it asks
        # which plugin channels are armed, to mount their routers). Before that
        # this monkeypatch happened to land before anything had resolved
        # settings at all; relying on that was a latent dependency on nothing
        # else reading configuration first.
        from robothor.settings import reset_settings

        reset_settings()
        response = client.post("/api/admin/plugins/acme-tools/disable")
        assert response.status_code == 503
        assert "IsADirectoryError" in response.text

    def test_the_scope_is_engine_control(self):
        from robothor.engine.auth import required_scope

        assert required_scope("POST", "/api/admin/plugins/sync") == "engine:control"

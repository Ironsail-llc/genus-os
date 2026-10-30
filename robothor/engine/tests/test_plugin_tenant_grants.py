"""A child tenant's engine obeys the root tenant's plugin grants.

``tenant_plugin_grants`` (migration 160) lets a root operator force one plugin
``on`` or ``off`` for one child. The engine reads its OWN tenant's rows, once
per plugin-registry generation, and the loader applies them:

* **forced off** is refused before import, with the reason "disabled by the
  root tenant";
* **forced on** loads even though the child's lockfile row says disabled, but
  never excuses a manifest that drifted;
* **fail closed**: when the grant read errors, every plugin that was forced
  off at the last successful read stays refused, and the error is logged.

The engine's admin API reports the forced state and refuses to toggle a forced
plugin, so a child admin cannot undo the root's decision from its own page.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}


def _manifest(name: str, handler: str = "probe") -> str:
    return f"name: {name}\ncontract_version: 1\nhandlers:\n  - {handler}\n"


class _Dist:
    def __init__(self, name: str = "acme-tools", manifest: str | None = None) -> None:
        self.name, self.version = name, "1.2.3"
        self._manifest = manifest if manifest is not None else _manifest(name)
        self.files: tuple[str, ...] = ()

    def read_text(self, filename: str) -> str | None:
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, dist: _Dist, name: str = "probe") -> None:
        self.name, self.group, self.dist = name, "genus.tools", dist
        self.loads = 0

    def load(self) -> dict:
        self.loads += 1
        return {"genus_contract_version": "1.0", "handlers": {self.name: lambda: None}}


@pytest.fixture
def grants_db(monkeypatch):
    """The grant table, as the engine reads it. ``rows`` and ``error`` are live."""
    from robothor.plugins import grants

    db = SimpleNamespace(rows={}, error=None, calls=[])

    def read(tenant_id: str) -> dict[str, str]:
        db.calls.append(tenant_id)
        if db.error is not None:
            raise db.error
        return dict(db.rows)

    monkeypatch.setattr(grants, "_read", read)
    grants.reset_forced_cache()
    yield db
    grants.reset_forced_cache()


@pytest.fixture
def two_plugins(plugin_lockfile):
    """``acme-tools`` and ``acme-extra``, both recorded in the lockfile."""
    eps = [_EP(_Dist("acme-tools"), "probe"), _EP(_Dist("acme-extra"), "extra")]
    with patch.object(loader, "_discover", lambda: eps):
        lockfile.sync()
        yield eps


def _reason(result, distribution: str) -> str | None:
    return next((f.reason for f in result.failures if f.distribution == distribution), None)


# ─── the loader ───────────────────────────────────────────────────────


class TestForcedOff:
    def test_it_is_refused_before_import_with_the_root_reason(self, grants_db, two_plugins):
        from robothor.plugins.grants import ROOT_DISABLED_REASON

        grants_db.rows = {"acme-tools": "off"}
        result = loader.load_plugins(entry_points=two_plugins)

        assert ROOT_DISABLED_REASON == "disabled by the root tenant"
        assert _reason(result, "acme-tools") == ROOT_DISABLED_REASON
        assert two_plugins[0].loads == 0, "a plugin forced off was imported"
        assert two_plugins[1] in result.loaded

    def test_it_is_refused_even_when_nothing_recorded_it(self, grants_db, plugin_lockfile):
        """No lockfile row means "unconstrained" to the child, not to the root."""
        from robothor.plugins.grants import ROOT_DISABLED_REASON

        grants_db.rows = {"acme-tools": "off"}
        ep = _EP(_Dist("acme-tools"))
        result = loader.load_plugins(entry_points=[ep])
        assert _reason(result, "acme-tools") == ROOT_DISABLED_REASON
        assert ep.loads == 0


class TestForcedOn:
    def test_it_loads_although_the_lockfile_row_says_disabled(self, grants_db, two_plugins):
        lockfile.set_enabled("acme-tools", False)
        grants_db.rows = {"acme-tools": "on"}
        result = loader.load_plugins(entry_points=two_plugins)
        assert two_plugins[0] in result.loaded
        assert _reason(result, "acme-tools") is None

    def test_it_never_excuses_a_manifest_that_drifted(self, grants_db, two_plugins):
        lockfile.set_enabled("acme-tools", False)
        grants_db.rows = {"acme-tools": "on"}
        two_plugins[0].dist._manifest = _manifest("acme-tools", "swapped")
        result = loader.load_plugins(entry_points=two_plugins)
        assert _reason(result, "acme-tools") == lockfile.DRIFT_REASON
        assert two_plugins[0].loads == 0


class TestReadOncePerGeneration:
    def test_the_engine_reads_its_own_tenant(self, grants_db, two_plugins):
        from robothor import constants

        loader.load_plugins(entry_points=two_plugins)
        assert grants_db.calls == [constants.DEFAULT_TENANT]

    def test_a_second_load_in_the_same_generation_does_not_read_again(
        self, grants_db, two_plugins
    ):
        loader.load_plugins(entry_points=two_plugins)
        loader.load_plugins(entry_points=two_plugins)
        loader.load_plugins(entry_points=two_plugins)
        assert len(grants_db.calls) == 1

    def test_a_reload_reads_again_and_applies_a_new_grant(self, grants_db, two_plugins):
        from robothor.plugins.grants import ROOT_DISABLED_REASON

        assert _reason(loader.load_plugins(entry_points=two_plugins), "acme-tools") is None
        grants_db.rows = {"acme-tools": "off"}
        # Same generation: the cached read still answers.
        assert _reason(loader.load_plugins(entry_points=two_plugins), "acme-tools") is None

        loader.reload_plugins()
        result = loader.load_plugins(entry_points=two_plugins)
        assert len(grants_db.calls) == 2
        assert _reason(result, "acme-tools") == ROOT_DISABLED_REASON


class TestFailClosed:
    def test_a_failed_read_keeps_the_last_forced_off_set_refused(
        self, grants_db, two_plugins, caplog
    ):
        from robothor.plugins.grants import ROOT_DISABLED_REASON

        grants_db.rows = {"acme-tools": "off"}
        loader.load_plugins(entry_points=two_plugins)

        loader.reload_plugins()
        grants_db.error = RuntimeError("connection refused")
        with caplog.at_level(logging.WARNING, logger="robothor.plugins.grants"):
            result = loader.load_plugins(entry_points=two_plugins)

        assert _reason(result, "acme-tools") == ROOT_DISABLED_REASON
        assert two_plugins[0].loads == 0
        assert any("grant" in r.getMessage().lower() for r in caplog.records), caplog.text

    def test_a_failed_read_drops_a_forced_on_back_to_the_lockfile(self, grants_db, two_plugins):
        """Losing an override is the safe direction: the child's own record rules."""
        lockfile.set_enabled("acme-tools", False)
        grants_db.rows = {"acme-tools": "on"}
        assert two_plugins[0] in loader.load_plugins(entry_points=two_plugins).loaded

        loader.reload_plugins()
        grants_db.error = RuntimeError("connection refused")
        result = loader.load_plugins(entry_points=two_plugins)
        assert _reason(result, "acme-tools") == lockfile.DISABLED_REASON

    def test_a_failed_read_with_no_earlier_success_forces_nothing_and_is_logged(
        self, grants_db, two_plugins, caplog
    ):
        grants_db.error = RuntimeError("connection refused")
        with caplog.at_level(logging.WARNING, logger="robothor.plugins.grants"):
            result = loader.load_plugins(entry_points=two_plugins)
        assert all(ep in result.loaded for ep in two_plugins)
        assert caplog.records

    def test_the_rule_is_stated_where_it_is_implemented(self):
        from robothor.plugins import grants

        doc = (grants.forced_states.__doc__ or "").lower()
        assert "fail closed" in doc
        assert "last successful read" in doc


# ─── the engine's admin API ───────────────────────────────────────────


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


class TestAdminListing:
    def test_each_row_says_whether_and_how_the_root_forced_it(
        self, client, grants_db, two_plugins
    ):
        grants_db.rows = {"acme-tools": "off", "acme-extra": "on"}
        rows = {p["name"]: p for p in client.get("/api/admin/plugins").json()["plugins"]}
        assert rows["acme-tools"]["forced"] == "off"
        assert rows["acme-extra"]["forced"] == "on"

    def test_an_unforced_row_says_null(self, client, grants_db, two_plugins):
        rows = client.get("/api/admin/plugins").json()["plugins"]
        assert all(p["forced"] is None for p in rows)

    def test_a_forced_off_plugin_reads_as_a_decision_not_a_fault(
        self, client, grants_db, two_plugins
    ):
        from robothor.plugins.grants import ROOT_DISABLED_REASON

        grants_db.rows = {"acme-tools": "off"}
        row = next(
            p for p in client.get("/api/admin/plugins").json()["plugins"] if p["name"] == "acme-tools"
        )
        assert row["state"] == "disabled"
        assert row["failure_reason"] == ROOT_DISABLED_REASON


class TestAdminToggleRefusesAForcedPlugin:
    @pytest.mark.parametrize("act", ["enable", "disable"])
    @pytest.mark.parametrize("forced", ["on", "off"])
    def test_it_is_a_409_and_the_lockfile_is_untouched(
        self, client, grants_db, two_plugins, act, forced
    ):
        grants_db.rows = {"acme-tools": forced}
        before = lockfile.read_lockfile().row("acme-tools").enabled

        response = client.post(f"/api/admin/plugins/acme-tools/{act}")

        assert response.status_code == 409
        assert "root tenant" in response.json()["detail"]
        assert lockfile.read_lockfile().row("acme-tools").enabled is before

    def test_an_unforced_plugin_still_toggles(self, client, grants_db, two_plugins):
        grants_db.rows = {"acme-tools": "off"}
        response = client.post("/api/admin/plugins/acme-extra/disable")
        assert response.status_code == 200
        assert response.json()["enabled"] is False

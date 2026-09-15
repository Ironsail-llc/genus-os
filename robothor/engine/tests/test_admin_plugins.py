"""The engine's plugin admin surface.

A plugin is an object in THIS process: what loaded, what was refused and why,
and what the lockfile says about each are facts only the engine holds, so the
engine answers and the bridge proxies — the same split ``admin_channels`` and
``admin_providers`` are built on.

Four properties are load-bearing and each is pinned below:

* **A reload here is the SIGHUP body, not a second implementation of it.** The
  platform has shipped three defects in the shape "a correct function with an
  inert or divergent second caller"; a route that re-implemented the reload
  would be the fourth, and it would diverge silently because both spellings
  would look like they worked.
* **No response carries a path.** The lockfile lives under the operator's home
  directory on a normal install. Which distributions are installed is a
  platform fact; where this instance keeps its files is not, and the CI leak
  gate refuses ``/home/<name>`` for exactly that reason.
* **Enable and disable do not reload.** They write a row. Saying so is the
  difference between a control and a control the operator believes has already
  applied — and the Helm needs the two acts separable to offer the reload.
* **A plugin failure never reaches ``/ready``.** One broken third-party
  distribution must not make the engine look down to a load balancer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.plugins import loader, lockfile
from robothor.plugins.manifest import MANIFEST_NAME

_PAYLOAD = {"genus_contract_version": "1.0", "handlers": {"probe": lambda: None}}
_MANIFEST = "name: acme-tools\ncontract_version: 1\nhandlers:\n  - probe\n"


class _Dist:
    def __init__(self, name="acme-tools", version="1.2.3", manifest=_MANIFEST):
        self.name, self.version, self._manifest = name, version, manifest
        self.files: tuple[str, ...] = ()

    def read_text(self, filename):
        return self._manifest if filename == MANIFEST_NAME else None


class _EP:
    def __init__(self, name="probe", group="genus.tools", dist=None, payload=None):
        self.name, self.group, self.dist = name, group, dist or _Dist()
        self._payload = payload if payload is not None else _PAYLOAD

    def load(self):
        return self._payload


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
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "plugins.lock"
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(path))
    lockfile.forget_warnings()
    return path


@pytest.fixture
def one_plugin(lock_path):
    with patch.object(loader, "_discover", lambda: [_EP()]):
        yield


class TestScope:
    def test_every_plugin_route_demands_engine_control(self) -> None:
        from robothor.engine.auth import required_scope

        for method, path in [
            ("GET", "/api/admin/plugins"),
            ("POST", "/api/admin/plugins/acme-tools/enable"),
            ("POST", "/api/admin/plugins/acme-tools/disable"),
            ("POST", "/api/admin/plugins/reload"),
        ]:
            assert required_scope(method, path) == "engine:control", path


class TestListing:
    def test_it_reports_the_generation_the_lockfile_and_the_plugins(self, client, one_plugin):
        body = client.get("/api/admin/plugins").json()
        assert isinstance(body["generation"], int)
        assert body["lockfile"] == {
            "path_configured": True,
            "present": False,
            "malformed": False,
            "rows": 0,
            "problem": None,
        }
        assert [p["name"] for p in body["plugins"]] == ["acme-tools"]

    def test_a_plugin_row_carries_what_the_page_needs(self, client, one_plugin):
        lockfile.sync()
        row = client.get("/api/admin/plugins").json()["plugins"][0]
        assert row["version"] == "1.2.3"
        assert row["enabled"] is True
        assert row["verdict"] == "unscanned"
        assert row["state"] == "loaded"
        assert row["groups"] == ["genus.tools"]
        assert row["contributions"] == {"tools": 1}
        assert row["failure_reason"] is None
        assert row["manifest"]["contract_version"] == 1
        assert row["manifest"]["declared"] == {"handlers": ["probe"]}

    def test_a_disabled_plugin_reports_disabled_not_failed(self, client, one_plugin):
        lockfile.sync()
        lockfile.set_enabled("acme-tools", False)
        row = client.get("/api/admin/plugins").json()["plugins"][0]
        assert row["state"] == "disabled"
        assert row["enabled"] is False
        assert row["failure_reason"] == lockfile.DISABLED_REASON
        assert row["contributions"] == {}

    def test_a_refused_plugin_names_why(self, client, lock_path):
        broken = _EP(payload={"genus_contract_version": "0.1", "handlers": {"x": 1}})
        with patch.object(loader, "_discover", lambda: [broken]):
            row = client.get("/api/admin/plugins").json()["plugins"][0]
        assert row["state"] == "failed"
        assert "contract version" in row["failure_reason"]

    def test_no_response_carries_a_filesystem_path(self, client, one_plugin):
        lockfile.sync()
        raw = json.dumps(client.get("/api/admin/plugins").json())
        assert "/home/" not in raw
        assert str(Path.home()) not in raw
        assert "plugins.lock" not in raw, "the lockfile's location is not a platform fact"

    def test_a_corrupt_lockfile_is_reported_rather_than_a_500(self, client, one_plugin, lock_path):
        lock_path.write_text("{{{", encoding="utf-8")
        body = client.get("/api/admin/plugins").json()
        assert body["lockfile"]["malformed"] is True
        assert body["plugins"][0]["state"] == "loaded", "a corrupt file must not disable anything"

    def test_the_lockfile_problem_is_the_sentence_the_cli_prints(
        self, client, one_plugin, lock_path
    ):
        """``malformed`` says THAT it is damaged; ``problem`` says which damage.

        The CLI and the doctor both print ``Lockfile.problem``. The Helm had one
        sentence of its own covering all four cases at once — including the
        unreadable PATH, whose remedy is a filesystem and not a rebuild. One
        field, one wording, three surfaces.
        """
        lock_path.write_text("{{{", encoding="utf-8")
        lock = client.get("/api/admin/plugins").json()["lockfile"]
        assert lock["problem"] == lockfile.read_lockfile().problem
        assert "is not valid JSON" in lock["problem"]

    def test_a_lockfile_with_no_plugins_list_says_so(self, client, one_plugin, lock_path):
        lock_path.write_text('{"version": 1}', encoding="utf-8")
        assert (
            client.get("/api/admin/plugins").json()["lockfile"]["problem"]
            == "does not hold a 'plugins' list"
        )

    def test_a_healthy_lockfile_has_no_problem(self, client, one_plugin):
        lockfile.sync()
        body = client.get("/api/admin/plugins").json()
        assert body["lockfile"]["malformed"] is False
        assert body["lockfile"]["problem"] is None, "an empty string is not a problem"

    def test_the_problem_never_names_the_path(self, client, one_plugin, lock_path):
        lock_path.mkdir()
        lock = client.get("/api/admin/plugins").json()["lockfile"]
        assert lock["problem"], "a path that will not read is a problem"
        assert "plugins.lock" not in lock["problem"]
        assert "/" not in lock["problem"]

    def test_a_row_says_whether_this_platform_installed_it(self, client, one_plugin):
        """``source`` present is what lets the Helm offer Remove at all.

        Its ABSENCE is the load-bearing half: ``genus plugin remove`` refuses a
        row this platform did not put there, so a button that offered it anyway
        would be promising an act the engine answers 422 to.
        """
        lockfile.sync()
        row = client.get("/api/admin/plugins").json()["plugins"][0]
        assert row["source"] is None, "sync() records what somebody else installed"


class TestEnableDisable:
    def test_disable_flips_the_row_and_says_it_did_not_reload(self, client, one_plugin):
        lockfile.sync()
        response = client.post("/api/admin/plugins/acme-tools/disable")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["name"] == "acme-tools"
        assert body["reloaded"] is False
        assert lockfile.read_lockfile().row("acme-tools").enabled is False

    def test_enable_puts_it_back(self, client, one_plugin):
        lockfile.sync()
        client.post("/api/admin/plugins/acme-tools/disable")
        assert client.post("/api/admin/plugins/acme-tools/enable").json()["enabled"] is True

    def test_an_unknown_plugin_is_a_404(self, client, one_plugin):
        lockfile.sync()
        assert client.post("/api/admin/plugins/acme-toolz/disable").status_code == 404

    def test_a_name_that_is_not_a_name_is_refused(self, client, one_plugin):
        lockfile.sync()
        assert client.post("/api/admin/plugins/..%2F..%2Fetc/disable").status_code in (404, 422)


class TestReload:
    def test_it_runs_the_same_body_the_signal_handler_runs(self, client, one_plugin):
        from robothor.engine import daemon

        with patch.object(daemon, "perform_plugin_reload", return_value=41) as spy:
            body = client.post("/api/admin/plugins/reload").json()
        assert spy.call_count == 1, "the route re-implemented the reload"
        assert body["generation"] == 41

    def test_it_reports_what_the_new_set_holds(self, client, one_plugin):
        body = client.post("/api/admin/plugins/reload").json()
        assert body["loaded"] == 1
        assert body["failures"] == []

    def test_a_disabled_plugin_is_gone_after_a_reload(self, client, one_plugin):
        lockfile.sync()
        client.post("/api/admin/plugins/acme-tools/disable")
        body = client.post("/api/admin/plugins/reload").json()
        assert body["loaded"] == 0
        assert body["failures"] == [
            {
                "name": "probe",
                "group": "genus.tools",
                "reason": lockfile.DISABLED_REASON,
                "distribution": "acme-tools",
            }
        ]

    def test_a_failure_names_the_distribution_it_belongs_to(self, client, one_plugin):
        """``name`` is the ENTRY POINT, not the distribution.

        One distribution appears here once per group it publishes into, and
        ``genus-hostinfo`` shows up as ``hostinfo``. Without this field the Helm
        had to guess which card to file a refusal under, and a wrong guess
        reports a plugin the operator did not disable as one they did.
        """
        broken = _EP(payload={"genus_contract_version": "0.1", "handlers": {"probe": 1}})
        with patch.object(loader, "_discover", lambda: [broken]):
            failure = client.post("/api/admin/plugins/reload").json()["failures"][0]
        assert failure["name"] == "probe"
        assert failure["distribution"] == "acme-tools"

    def test_a_reload_that_fails_is_reported_not_raised(self, client, one_plugin):
        from robothor.engine import daemon

        with patch.object(daemon, "perform_plugin_reload", return_value=None):
            response = client.post("/api/admin/plugins/reload")
        assert response.status_code == 200
        assert response.json()["generation"] is None


class TestReadiness:
    def test_a_plugin_failure_never_makes_the_engine_look_down(self, tmp_path, lock_path):
        """One broken third-party distribution is not an unready engine.

        Built the way ``test_readiness.py`` builds a client — a real
        ``EngineConfig`` with the dependencies ``/ready`` actually probes stood
        in for — because the claim is about the readiness verdict, and a
        readiness test that never reached the probes would be vacuous.
        """
        from unittest.mock import AsyncMock

        from robothor.engine.config import EngineConfig
        from robothor.engine.health import create_health_app

        (tmp_path / "main.yaml").write_text(
            "id: main\nname: Main\ndescription: A generic fixture agent\n"
            'version: "2026-09-11"\ndepartment: core\n'
        )
        config = EngineConfig(
            workspace=tmp_path,
            manifest_dir=tmp_path,
            allow_empty_fleet=False,
            required_agent_ids=("main",),
        )
        redis_client = MagicMock()
        redis_client.ping = AsyncMock(return_value=True)
        redis_client.aclose = AsyncMock()

        broken = _EP(payload={"genus_contract_version": "nope", "handlers": {"x": 1}})
        with (
            patch.object(loader, "_discover", lambda: [broken]),
            patch("robothor.engine.dashboards.get_dashboard_router", return_value=APIRouter()),
            patch("robothor.engine.dashboards.get_public_router", return_value=APIRouter()),
            patch("robothor.engine.webhooks.get_webhook_router", return_value=APIRouter()),
            patch("robothor.db.connection.get_connection"),
            patch("robothor.engine.tracking.list_schedules", return_value=[]),
            patch("robothor.federation.connections.load_connections", return_value=[]),
            patch("redis.asyncio.Redis", return_value=redis_client),
        ):
            probe = TestClient(create_health_app(config), raise_server_exceptions=False)
            response = probe.get("/ready")
            listing = probe.get("/api/admin/plugins").json()

        assert response.status_code == 200
        assert all(value == "ok" for value in response.json()["checks"].values())
        assert listing["plugins"][0]["state"] == "failed", (
            "the assertion above would be vacuous if nothing had actually been refused"
        )

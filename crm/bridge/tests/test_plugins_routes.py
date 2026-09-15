"""The Plugins page's reads and its two acts.

Everything here is the engine's answer: a plugin is an object in THAT process,
and what loaded, what was refused and which discovery generation the caches are
serving cannot be asked from outside it. So this router is a gate, a proxy and
an audit trail, and the tests are about exactly those three things.

The gate is the one that matters most. ``disable`` takes a capability out of
service on the next reload and ``reload`` re-imports third-party code into the
running daemon; a viewer session reaching either would be a non-operator
changing what the appliance executes. So every route is ``require_operator``
first line, and a rejected caller must never reach the engine at all.

The audit convention is the other half: identifiers only. A plugin name is an
identifier; nothing else from these responses belongs in the trail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE_LISTING = {
    "generation": 3,
    "lockfile": {"path_configured": True, "present": True, "malformed": False, "rows": 1},
    "plugins": [
        {
            "name": "acme-tools",
            "version": "1.2.3",
            "enabled": True,
            "recorded": True,
            "verdict": "unscanned",
            "state": "loaded",
            "drifted": False,
            "groups": ["genus.tools"],
            "contributions": {"tools": 2},
            "failure_reason": None,
            "manifest": {"contract_version": 1, "declared": {"handlers": ["probe"]}},
        }
    ],
}

ENGINE_ROW = {
    "name": "acme-tools",
    "version": "1.2.3",
    "manifest_sha256": "0" * 64,
    "verdict": "unscanned",
    "enabled": False,
    "kinds": ["genus.tools"],
    "recorded_at": "2026-09-15T00:00:00+00:00",
    "reloaded": False,
}

ENGINE_RELOAD = {"generation": 4, "loaded": 1, "failures": []}

ENGINE_SYNC = {
    "recorded": ["acme-tools"],
    "added": ["acme-tools"],
    "updated": [],
    "removed": [],
    "reloaded": False,
}


class FakeEngine:
    """Stands in for the engine's ``/api/admin/plugins`` surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.status = 200

    async def __call__(self, method, path, *, json=None, timeout=30):
        self.calls.append((method, path, json))
        if path.endswith("/reload"):
            return self.status, ENGINE_RELOAD
        if path.endswith("/sync"):
            return self.status, ENGINE_SYNC
        if path.endswith(("/enable", "/disable")):
            return self.status, ENGINE_ROW
        return self.status, ENGINE_LISTING


@pytest.fixture
def fake_engine():
    engine = FakeEngine()
    with patch("routers.plugins.engine_request", new=engine):
        yield engine


# ── The gate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("client_name", ["controls_client_as_viewer", "controls_client_as_service"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/plugins"),
        ("post", "/api/plugins/acme-tools/disable"),
        ("post", "/api/plugins/acme-tools/enable"),
        ("post", "/api/plugins/reload"),
        ("post", "/api/plugins/sync"),
    ],
)
def test_a_non_operator_is_refused_before_the_engine_is_called(
    request, client_name, method, path, fake_engine
):
    client = request.getfixturevalue(client_name)
    assert getattr(client, method)(path).status_code == 403
    assert not fake_engine.calls, "a rejected caller reached the engine"


def test_another_tenants_operator_is_refused(controls_client_as_other_tenant_owner, fake_engine):
    """One engine runs one set of plugins. A second tenant's operator disabling
    one would be taking a capability out of somebody else's instance."""
    assert controls_client_as_other_tenant_owner.get("/api/plugins").status_code == 403


# ── The listing ─────────────────────────────────────────────────────────


def test_it_proxies_the_engine_listing_verbatim(controls_client_as_operator, fake_engine):
    response = controls_client_as_operator.get("/api/plugins")
    assert response.status_code == 200
    assert response.json() == ENGINE_LISTING
    assert ("GET", "/api/admin/plugins", None) in fake_engine.calls


def test_an_unreachable_engine_is_a_502(controls_client_as_operator, fake_engine):
    fake_engine.status = 502
    assert controls_client_as_operator.get("/api/plugins").status_code == 502


# ── The acts ────────────────────────────────────────────────────────────


def test_disable_proxies_and_returns_the_new_row(controls_client_as_operator, fake_engine):
    response = controls_client_as_operator.post("/api/plugins/acme-tools/disable")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert ("POST", "/api/admin/plugins/acme-tools/disable", None) in fake_engine.calls


def test_enable_proxies(controls_client_as_operator, fake_engine):
    assert controls_client_as_operator.post("/api/plugins/acme-tools/enable").status_code == 200
    assert ("POST", "/api/admin/plugins/acme-tools/enable", None) in fake_engine.calls


def test_reload_proxies(controls_client_as_operator, fake_engine):
    response = controls_client_as_operator.post("/api/plugins/reload")
    assert response.json() == ENGINE_RELOAD
    assert ("POST", "/api/admin/plugins/reload", None) in fake_engine.calls


def test_sync_proxies(controls_client_as_operator, fake_engine):
    """The action the Plugins page opens with on a fresh install.

    Until this existed the page was read-only until somebody SSHed in: nothing
    is recorded, so every enable and disable is a 404.
    """
    response = controls_client_as_operator.post("/api/plugins/sync")
    assert response.status_code == 200
    assert response.json() == ENGINE_SYNC
    assert ("POST", "/api/admin/plugins/sync", None) in fake_engine.calls


def test_a_refused_sync_keeps_the_engines_409(controls_client_as_operator, fake_engine):
    """A sync the engine declined must not read as one that worked."""
    fake_engine.status = 409
    assert controls_client_as_operator.post("/api/plugins/sync").status_code == 409


def test_sync_is_not_confused_with_a_plugin_named_sync(controls_client_as_operator, fake_engine):
    """`/{name}/enable` must not swallow the literal path."""
    controls_client_as_operator.post("/api/plugins/sync")
    assert fake_engine.calls[0][1] == "/api/admin/plugins/sync"


def test_an_unknown_plugin_keeps_the_engines_404(controls_client_as_operator, fake_engine):
    fake_engine.status = 404
    assert controls_client_as_operator.post("/api/plugins/nope/disable").status_code == 404


def test_a_name_that_is_not_a_distribution_name_never_reaches_the_engine(
    controls_client_as_operator, fake_engine
):
    response = controls_client_as_operator.post("/api/plugins/..%2F..%2Fetc/disable")
    assert response.status_code in (404, 422)
    assert not fake_engine.calls


# ── The trail ───────────────────────────────────────────────────────────


def test_each_act_writes_one_audit_event_with_identifiers_only(
    controls_client_as_operator, fake_engine
):
    for path, expected in (
        ("/api/plugins/acme-tools/disable", "plugin.disable"),
        ("/api/plugins/acme-tools/enable", "plugin.enable"),
        ("/api/plugins/reload", "plugin.reload"),
        ("/api/plugins/sync", "plugin.sync"),
    ):
        with patch("routers.plugins.audited") as audited:
            controls_client_as_operator.post(path)
        assert audited.call_count == 1, path
        args, kwargs = audited.call_args
        assert args[1] == expected
        # Identifiers only: a name, and nothing out of the engine's answer.
        assert set(kwargs) <= {"action", "status", "plugin"}
        if "plugin" in kwargs:
            assert kwargs["plugin"] == "acme-tools"


def test_the_read_writes_no_audit_event(controls_client_as_operator, fake_engine):
    """A listing is not an act. Auditing every page load buries the disable."""
    with patch("routers.plugins.audited") as audited:
        controls_client_as_operator.get("/api/plugins")
    assert audited.call_count == 0

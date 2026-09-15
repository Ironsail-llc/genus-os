"""Install and remove over HTTP, and what the response may not contain.

The pipeline is pinned in ``test_plugin_installer``; these routes are a thin
offload of it. What is new at this boundary, and what is pinned here:

* **``engine:control``**, the same scope every other ``/api/admin`` mutation
  sits behind. A caller who can install a plugin can change what the appliance
  executes.
* **No response carries a path.** ``pip_command`` holds a temp directory and
  the interpreter's location, so the HTTP copy of an install plan leaves it
  out. The CI leak gate refuses a home path for exactly this reason, and the
  CLI is the only caller that prints it.
* **A refusal is a 4xx with the sentence, never a 500 with a traceback.** A
  blocked wheel is a correct answer, not a server error.
* **A browser may not name a filesystem path.** Explicit wheel installs are
  CLI-only: the route takes a distribution name and nothing else that could
  become a path.
* **The work happens off the event loop.** Installing runs pip; doing that
  inline would stall every other request the engine is serving.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from robothor.plugins import installer, lockfile, scan


def _make_app():
    config = MagicMock()
    config.tenant_id = "test-tenant"
    config.bot_token = ""
    config.port = 18801

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
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTHOR_PLUGIN_LOCKFILE", str(tmp_path / "plugins.lock"))
    lockfile.forget_warnings()
    return TestClient(_make_app(), raise_server_exceptions=False)


def _plan(**kw) -> installer.InstallPlan:
    defaults = {
        "name": "acme-tools",
        "version": "1.2.3",
        "origin": "registry",
        "index_url": "https://example.invalid/index.json",
        "publisher_key_id": "test-key-1",
        "filename": "acme_tools-1.2.3-py3-none-any.whl",
        "sha256": "a" * 64,
        "size": 4096,
        "summary": "A probe",
        "verdict": scan.SAFE,
        "groups": ("genus.tools",),
        "pip_command": (
            "/srv/app/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "--find-links",
            "/srv/app/tmp/genus-plugin-abc",
            "acme-tools==1.2.3",
        ),
    }
    defaults.update(kw)
    return installer.InstallPlan(**defaults)


def test_the_routes_demand_engine_control() -> None:
    from robothor.engine.auth import required_scope

    assert required_scope("POST", "/api/admin/plugins/install") == "engine:control"
    assert required_scope("POST", "/api/admin/plugins/acme-tools/remove") == "engine:control"


def test_install_answers_the_plan_and_the_row(client) -> None:
    outcome = installer.InstallOutcome(
        plan=_plan(), installed=True, row={"name": "acme-tools", "verdict": "safe"}
    )
    with patch.object(installer, "install", return_value=outcome) as install:
        body = client.post("/api/admin/plugins/install", json={"name": "acme-tools"}).json()
    assert install.call_args.args[0] == "acme-tools"
    assert body["installed"] is True
    assert body["plan"]["verdict"] == "safe"
    assert body["plan"]["publisher_key_id"] == "test-key-1"
    assert body["row"]["name"] == "acme-tools"
    assert "SIGHUP" in body["reload_hint"]


def test_no_install_response_carries_a_path(client) -> None:
    outcome = installer.InstallOutcome(plan=_plan(), installed=True)
    with patch.object(installer, "install", return_value=outcome):
        response = client.post("/api/admin/plugins/install", json={"name": "acme-tools"})
    raw = response.text
    assert "pip_command" not in raw
    assert "/srv/app" not in raw


def test_a_blocked_verdict_is_a_refusal_not_a_server_error(client) -> None:
    with patch.object(
        installer,
        "install",
        side_effect=installer.InstallError("acme-tools 1.2.3 is blocked: imports ctypes"),
    ):
        response = client.post("/api/admin/plugins/install", json={"name": "acme-tools"})
    assert response.status_code == 422
    assert "ctypes" in response.json()["detail"]


def test_an_index_refusal_is_also_a_refusal(client) -> None:
    from robothor.plugins.registry import RegistryError

    with patch.object(
        installer, "install", side_effect=RegistryError("The index signature does not verify.")
    ):
        response = client.post("/api/admin/plugins/install", json={"name": "acme-tools"})
    assert response.status_code == 422
    assert "signature" in response.json()["detail"]


def test_a_dry_run_is_passed_through(client) -> None:
    outcome = installer.InstallOutcome(plan=_plan(), installed=False, dry_run=True)
    with patch.object(installer, "install", return_value=outcome) as install:
        body = client.post(
            "/api/admin/plugins/install", json={"name": "acme-tools", "dry_run": True}
        ).json()
    assert install.call_args.kwargs["dry_run"] is True
    assert body["dry_run"] is True
    assert body["installed"] is False


def test_the_version_index_and_accept_review_reach_the_installer(client) -> None:
    outcome = installer.InstallOutcome(plan=_plan(), installed=True)
    with patch.object(installer, "install", return_value=outcome) as install:
        client.post(
            "/api/admin/plugins/install",
            json={
                "name": "acme-tools",
                "version": "1.2.3",
                "index": "https://example.invalid/index.json",
                "accept_review": True,
            },
        )
    kwargs = install.call_args.kwargs
    assert kwargs["version"] == "1.2.3"
    assert kwargs["index"] == "https://example.invalid/index.json"
    assert kwargs["accept_review"] is True


@pytest.mark.parametrize(
    "name",
    ["/srv/app/evil.whl", "../../etc/passwd", "https://evil.invalid/x.whl", "acme tools", ""],
)
def test_a_browser_may_not_name_a_path_or_a_url(client, name) -> None:
    """Explicit wheels are CLI-only. A route that accepted a path would let a
    dashboard read any file the engine can reach, and one that accepted a URL
    would make the engine fetch on a caller's say-so."""
    with patch.object(installer, "install") as install:
        response = client.post("/api/admin/plugins/install", json={"name": name})
    assert response.status_code == 422
    assert install.call_count == 0


def test_sha256_is_not_accepted_over_http(client) -> None:
    """The wheel path is CLI-only, so the field that goes with it is not a
    field this route has. Accepting and ignoring it would be worse."""
    outcome = installer.InstallOutcome(plan=_plan(), installed=True)
    with patch.object(installer, "install", return_value=outcome) as install:
        client.post("/api/admin/plugins/install", json={"name": "acme-tools", "sha256": "a" * 64})
    assert "sha256" not in install.call_args.kwargs


def test_remove_answers_what_it_did(client) -> None:
    with patch.object(
        installer,
        "remove",
        return_value={
            "name": "acme-tools",
            "removed": True,
            "row_dropped": True,
            "reload_hint": installer.RELOAD_HINT,
            "note": "",
        },
    ) as remove:
        body = client.post("/api/admin/plugins/acme-tools/remove").json()
    assert remove.call_args.args[0] == "acme-tools"
    assert body["removed"] is True
    assert "SIGHUP" in body["reload_hint"]


def test_remove_refuses_without_force_and_says_so(client) -> None:
    with patch.object(
        installer,
        "remove",
        side_effect=installer.InstallError(
            "this platform did not install it — re-run with --force"
        ),
    ):
        response = client.post("/api/admin/plugins/acme-tools/remove")
    assert response.status_code == 422
    assert "--force" in response.json()["detail"]


@pytest.mark.parametrize("name", ["evil.whl", "evil.whl==1.0"])
def test_a_route_spec_ending_in_whl_is_refused(client, name) -> None:
    """Dots are legal in a package name, so ``evil.whl`` matched the spec,
    reached the wheel branch and came back "you need --sha256" -- a confusing
    answer today and a latent file-read footgun the moment ``sha256`` becomes a
    route field."""
    with patch.object(installer, "install") as install:
        response = client.post("/api/admin/plugins/install", json={"name": name})
    assert response.status_code == 422
    assert install.call_count == 0


def test_a_pep440_local_version_is_accepted_over_http(client) -> None:
    """``_DIST_NAME`` rejected ``+``, so ``1.0+acme1`` could not be installed
    over HTTP although the inline ``name==version`` form allowed it."""
    outcome = installer.InstallOutcome(plan=_plan(version="1.0+acme1"), installed=True)
    with patch.object(installer, "install", return_value=outcome) as install:
        response = client.post(
            "/api/admin/plugins/install", json={"name": "acme-tools", "version": "1.0+acme1"}
        )
    assert response.status_code == 200
    assert install.call_args.kwargs["version"] == "1.0+acme1"


@pytest.mark.parametrize(
    "version", ["1.0 --pre", "--index-url=https://evil.invalid", "not a version"]
)
def test_a_version_that_is_not_pep440_is_refused(client, version) -> None:
    with patch.object(installer, "install") as install:
        response = client.post(
            "/api/admin/plugins/install", json={"name": "acme-tools", "version": version}
        )
    assert response.status_code == 422
    assert install.call_count == 0


def test_remove_rejects_a_name_that_is_not_a_distribution(client) -> None:
    with patch.object(installer, "remove") as remove:
        response = client.post("/api/admin/plugins/..%2Fetc/remove")
    assert response.status_code in (404, 422)
    assert remove.call_count == 0


def test_the_cap_bounds_the_wall_clock_not_just_the_status_code(client, monkeypatch) -> None:
    """The first version wrapped ``asyncio.to_thread`` in ``wait_for``, which
    cancels the AWAIT and not the thread: a hostile review lowered the cap to
    0.5 s, made the install sleep 3 s, and measured the 504 arriving at 3.00 s
    with the body still saying "did not finish within 0s". A cap that changes
    the status code and not the duration is the opposite of a cap."""
    import time

    from robothor.engine import admin_plugins

    monkeypatch.setattr(admin_plugins, "_OPERATION_TIMEOUT", 0.5)
    started = threading.Event()

    def slow(*_args, **_kwargs):
        started.set()
        time.sleep(3.0)
        raise AssertionError("the cancelled work should have stopped at a checkpoint")

    with patch.object(installer, "install", slow):
        began = time.monotonic()
        response = client.post("/api/admin/plugins/install", json={"name": "acme-tools"})
        elapsed = time.monotonic() - began

    assert response.status_code == 504
    assert elapsed < 2.0, f"the handler waited {elapsed:.2f}s for a 0.5s cap"
    assert started.is_set()
    detail = response.json()["detail"]
    assert "may still be running" in detail


def test_the_install_is_cancelled_at_a_checkpoint_before_pip(tmp_path) -> None:
    """The cancel token is checked BETWEEN pipeline steps and never after pip
    has started, so a cancelled install either did not run pip at all or ran it
    to completion and recorded the row. What it can never be is half-done."""
    import threading as _threading

    cancel = _threading.Event()
    cancel.set()
    pip = MagicMock()
    with pytest.raises(installer.InstallError) as excinfo:
        installer.install(
            "acme-tools",
            indexes=(),
            keys={},
            lock_path=tmp_path / "plugins.lock",
            pip=pip,
            cancel=cancel,
        )
    assert "cancel" in str(excinfo.value).lower()
    assert pip.call_count == 0


def test_pip_cannot_outlive_the_route_cap() -> None:
    """pip's own 300 s timeout was longer than the route's 60 s cap, so the one
    step that cannot be interrupted was the one allowed to run five times the
    budget."""
    from robothor.engine import admin_plugins

    assert installer.PIP_TIMEOUT_SECONDS < admin_plugins._OPERATION_TIMEOUT


def test_a_pip_timeout_is_a_sentence_not_a_traceback(client) -> None:
    """``subprocess.TimeoutExpired`` is a SubprocessError, not an OSError, so it
    slid past the handler's ``except OSError`` and would have been a bare 500."""
    import subprocess

    with patch.object(
        installer, "install", side_effect=subprocess.TimeoutExpired(cmd=["pip"], timeout=30)
    ):
        response = client.post("/api/admin/plugins/install", json={"name": "acme-tools"})
    assert response.status_code == 504
    assert "Traceback" not in response.text
    assert "pip" in response.json()["detail"]


def test_install_and_remove_run_off_the_event_loop() -> None:
    """pip is a subprocess with a five-minute cap. Running it inline would
    stall every other request the engine is serving -- the defect
    ``admin_providers`` already fixed for a cheaper call than this one."""
    import inspect

    from robothor.engine import admin_plugins

    source = inspect.getsource(admin_plugins)
    assert "install_plugin" in source
    install_body = source[source.index("async def install(") :]
    assert "_write(" in install_body.split("@router")[0]

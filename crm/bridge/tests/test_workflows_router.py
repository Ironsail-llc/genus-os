"""Workflows answers: what multi-agent flows exist and what have they run.
Bridge-side and operator-scoped (the engine's /api/workflows* is neither).
Lists workflows that have run at least once — the honesty limitation is surfaced
in the tab copy, not hidden."""

import pytest
from routers import workflows


@pytest.fixture
def fake_workflows(monkeypatch):
    monkeypatch.setattr(
        workflows,
        "_list_workflows",
        lambda tenant_id: [
            {
                "workflow_id": "intel",
                "runs": 3,
                "last_run_at": None,
                "last_status": "completed",
                "failures": 0,
            },
        ],
    )
    monkeypatch.setattr(
        workflows,
        "_workflow_runs",
        lambda wid, limit, tenant_id: (
            [{"id": "wr1", "status": "completed"}] if wid == "intel" else []
        ),
    )
    monkeypatch.setattr(workflows, "_defined_workflows", lambda: [])


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/workflows").status_code == 403


def test_list_returns_workflows(controls_client_as_operator, fake_workflows):
    r = controls_client_as_operator.get("/api/workflows")
    assert r.status_code == 200
    assert r.json()[0]["workflow_id"] == "intel"


def test_runs_history(controls_client_as_operator, fake_workflows):
    r = controls_client_as_operator.get("/api/workflows/intel/runs")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "wr1"


def test_workflows_router_is_read_only():
    methods = {m for route in workflows.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}


@pytest.fixture
def ran_and_defined(monkeypatch):
    monkeypatch.setattr(
        workflows,
        "_list_workflows",
        lambda tenant_id: [
            {
                "workflow_id": "intel",
                "runs": 3,
                "last_run_at": "2026-01-01T00:00:00+00:00",
                "last_status": "completed",
                "failures": 0,
            },
        ],
    )
    monkeypatch.setattr(
        workflows,
        "_defined_workflows",
        lambda: [
            {"workflow_id": "intel", "name": "Intel", "triggers": [], "source": "workspace"},
            {
                "workflow_id": "desk-pass",
                "name": "Desk pass",
                "triggers": ["cron 0 7 * * *"],
                "source": "plugin:acme-desk",
            },
        ],
    )


def test_a_defined_workflow_that_never_ran_is_listed(controls_client_as_operator, ran_and_defined):
    rows = {r["workflow_id"]: r for r in controls_client_as_operator.get("/api/workflows").json()}
    assert rows["desk-pass"]["runs"] == 0
    assert rows["desk-pass"]["source"] == "plugin:acme-desk"
    assert rows["desk-pass"]["last_status"] is None
    assert rows["desk-pass"]["failures"] == 0


def test_a_workflow_that_ran_is_listed_once_with_its_source(
    controls_client_as_operator, ran_and_defined
):
    body = controls_client_as_operator.get("/api/workflows").json()
    assert [r["workflow_id"] for r in body] == ["intel", "desk-pass"]
    assert body[0]["runs"] == 3
    assert body[0]["source"] == "workspace"


def test_defined_workflows_are_read_from_the_workspace(env_workspace):
    """Parsed from docs/workflows/*.yaml; the ledger names the plugin that shipped one."""
    import yaml

    wf_dir = env_workspace / "docs" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "desk-pass.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "desk-pass",
                "name": "Desk pass",
                "triggers": [{"type": "cron", "cron": "0 7 * * *"}],
                "steps": [{"id": "s1", "type": "noop"}],
            }
        )
    )
    (wf_dir / "local.yaml").write_text(
        yaml.safe_dump({"id": "local-flow", "steps": [{"id": "s1", "type": "noop"}]})
    )
    (wf_dir / "broken.yaml").write_text("id: [unclosed")
    ledger = env_workspace / ".robothor" / "plugin-assets.yaml"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "acme-desk": {
                        "version": "1.0.0",
                        "files": {"docs/workflows/desk-pass.yaml": {"sha256": "0" * 64}},
                    }
                }
            }
        )
    )
    rows = {r["workflow_id"]: r for r in workflows._defined_workflows()}
    assert set(rows) == {"desk-pass", "local-flow"}
    assert rows["desk-pass"]["source"] == "plugin:acme-desk"
    assert rows["desk-pass"]["triggers"] == ["cron 0 7 * * *"]
    assert rows["local-flow"]["source"] == "workspace"
    assert rows["local-flow"]["name"] == "local-flow"


def test_a_child_tenant_lists_only_what_ran_there(_controls_auth_key, ran_and_defined):
    """The workflow directory the bridge can read is the ROOT engine's, so a
    child tenant's admin sees that tenant's run history and no definitions
    borrowed from the root workspace."""
    from tests.conftest import _make_controls_client_as_role

    client = _make_controls_client_as_role("admin", "acme-child-a")
    r = client.get("/api/workflows")
    assert r.status_code == 200
    assert [row["workflow_id"] for row in r.json()] == ["intel"]
    assert r.json()[0]["source"] is None


def test_a_workflow_directory_fault_does_not_break_the_listing(
    controls_client_as_operator, monkeypatch
):
    """Run history still answers when the defined-workflow scan cannot."""
    monkeypatch.setattr(
        workflows,
        "_list_workflows",
        lambda tenant_id: [{"workflow_id": "intel", "runs": 1, "last_run_at": None, "failures": 0}],
    )

    def boom():
        raise PermissionError("workflow directory unreadable")

    monkeypatch.setattr(workflows, "_defined_workflows", boom)
    r = controls_client_as_operator.get("/api/workflows")
    assert r.status_code == 200
    assert [row["workflow_id"] for row in r.json()] == ["intel"]


def test_an_unreadable_ledger_labels_no_source_and_is_logged(env_workspace, caplog):
    """A damaged ledger must not relabel a plugin's workflow as the operator's."""
    import logging

    import yaml

    wf_dir = env_workspace / "docs" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "desk-pass.yaml").write_text(
        yaml.safe_dump({"id": "desk-pass", "steps": [{"id": "s1", "type": "noop"}]})
    )
    ledger = env_workspace / ".robothor" / "plugin-assets.yaml"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("plugins: [unclosed")
    with caplog.at_level(logging.WARNING):
        rows = {r["workflow_id"]: r for r in workflows._defined_workflows()}
    assert rows["desk-pass"]["source"] is None
    assert "plugin-assets.yaml" in caplog.text
    assert str(env_workspace) not in caplog.text

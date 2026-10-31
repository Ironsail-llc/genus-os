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
        lambda: [
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
        lambda wid, limit: [{"id": "wr1", "status": "completed"}] if wid == "intel" else [],
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
        lambda: [
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


def test_a_workflow_directory_fault_does_not_break_the_listing(
    controls_client_as_operator, monkeypatch
):
    """Run history still answers when the defined-workflow scan cannot."""
    monkeypatch.setattr(
        workflows,
        "_list_workflows",
        lambda: [{"workflow_id": "intel", "runs": 1, "last_run_at": None, "failures": 0}],
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


# ── run detail: what each step was given and what it returned ──────────

RUN_ID = "6f1c2a3e-0000-4000-8000-000000000001"


@pytest.fixture
def fake_run_detail(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _detail(workflow_id, run_id):
        calls.append((workflow_id, run_id))
        if workflow_id != "intel" or run_id != RUN_ID:
            return None
        return {
            "id": RUN_ID,
            "workflow_id": "intel",
            "status": "failed",
            "steps_failed": 1,
            "steps": [
                {
                    "step_id": "fetch",
                    "status": "completed",
                    "tool_name": "search_records",
                    "tool_input": {"query": "Alice"},
                    "tool_output": {"results": []},
                    "error_message": None,
                },
                {
                    "step_id": "lookup",
                    "status": "failed",
                    "tool_name": "get_record",
                    "tool_input": {"id": "rec-1"},
                    "tool_output": {"error": "not found"},
                    "error_message": "not found",
                },
            ],
        }

    monkeypatch.setattr(workflows, "_workflow_run", _detail, raising=False)
    return calls


@pytest.mark.parametrize("client", ["controls_client_as_viewer", "controls_client_as_user"])
def test_run_detail_requires_operator(request, client, fake_run_detail):
    c = request.getfixturevalue(client)
    r = c.get(f"/api/workflows/intel/runs/{RUN_ID}")
    assert r.status_code == 403
    assert fake_run_detail == []


def test_run_detail_returns_each_steps_tool_input_output_and_error(
    controls_client_as_operator, fake_run_detail
):
    r = controls_client_as_operator.get(f"/api/workflows/intel/runs/{RUN_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == RUN_ID
    steps = {s["step_id"]: s for s in body["steps"]}
    assert steps["fetch"]["tool_name"] == "search_records"
    assert steps["fetch"]["tool_input"] == {"query": "Alice"}
    assert steps["fetch"]["tool_output"] == {"results": []}
    assert steps["lookup"]["error_message"] == "not found"


def test_run_detail_for_admin_role(controls_client_as_admin, fake_run_detail):
    r = controls_client_as_admin.get(f"/api/workflows/intel/runs/{RUN_ID}")
    assert r.status_code == 200


def test_run_detail_is_not_cached(controls_client_as_operator, fake_run_detail):
    r = controls_client_as_operator.get(f"/api/workflows/intel/runs/{RUN_ID}")
    assert r.headers.get("cache-control") == "no-store"


def test_run_detail_unknown_run_is_404(controls_client_as_operator, fake_run_detail):
    other = "6f1c2a3e-0000-4000-8000-000000000002"
    r = controls_client_as_operator.get(f"/api/workflows/intel/runs/{other}")
    assert r.status_code == 404
    assert r.headers.get("cache-control") == "no-store"


def test_run_detail_of_another_workflow_is_404(controls_client_as_operator, fake_run_detail):
    r = controls_client_as_operator.get(f"/api/workflows/other/runs/{RUN_ID}")
    assert r.status_code == 404


def test_run_detail_malformed_run_id_is_404_without_a_query(
    controls_client_as_operator, fake_run_detail
):
    r = controls_client_as_operator.get("/api/workflows/intel/runs/not-a-uuid")
    assert r.status_code == 404
    assert fake_run_detail == []


def test_runs_list_reports_steps_failed(monkeypatch):
    """The per-run list carries steps_failed, so a list can show ok/failed counts."""
    seen: list[str] = []

    def _rows(sql, params):
        seen.append(sql)
        return [{"id": RUN_ID, "steps_completed": 1, "steps_failed": 1}]

    monkeypatch.setattr(workflows, "_rows", _rows)
    rows = workflows._workflow_runs("intel", 5)
    assert "steps_failed" in seen[0]
    assert rows[0]["steps_failed"] == 1


def test_run_detail_reads_steps_with_tool_columns(monkeypatch):
    """The detail query selects the tool columns and scopes the run to its workflow."""
    seen: list[tuple[str, tuple]] = []

    def _rows(sql, params):
        seen.append((sql, params))
        if "FROM workflow_runs" in sql:
            return [{"id": RUN_ID, "workflow_id": "intel", "status": "completed"}]
        return [
            {
                "step_id": "fetch",
                "tool_name": "search_records",
                "tool_input": {"query": "Alice"},
                "tool_output": {"ok": True},
                "error_message": None,
            }
        ]

    monkeypatch.setattr(workflows, "_rows", _rows)
    detail = workflows._workflow_run("intel", RUN_ID)
    run_sql, run_params = seen[0]
    assert "workflow_id = %s" in run_sql and run_params == (RUN_ID, "intel")
    step_sql, _ = seen[1]
    for column in ("tool_name", "tool_input", "tool_output", "error_message"):
        assert column in step_sql
    assert detail["steps"][0]["tool_input"] == {"query": "Alice"}


def test_run_detail_helper_returns_none_for_an_unknown_run(monkeypatch):
    monkeypatch.setattr(workflows, "_rows", lambda sql, params: [])
    assert workflows._workflow_run("intel", RUN_ID) is None


def test_a_plugin_jobs_runs_are_listed_under_its_job_id(controls_client_as_operator, monkeypatch):
    """Scheduled plugin jobs record their runs in workflow_runs keyed by
    ``plugin:<name>``; the existing runs route must answer for that id."""
    monkeypatch.setattr(
        workflows,
        "_workflow_runs",
        lambda wid, limit: (
            [{"id": "pj1", "status": "failed", "trigger_type": "plugin_job"}]
            if wid == "plugin:nightly_sweep"
            else []
        ),
    )
    r = controls_client_as_operator.get("/api/workflows/plugin:nightly_sweep/runs")
    assert r.status_code == 200
    assert r.json() == [{"id": "pj1", "status": "failed", "trigger_type": "plugin_job"}]

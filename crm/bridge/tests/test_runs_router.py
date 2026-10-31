"""Runs answers: what happened in this run — every step, block, and cost."""

import pytest
from routers import runs

#: Run ids are UUIDs; the detail route refuses anything else with a 404.
RUN_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def fake_runs(monkeypatch):
    monkeypatch.setattr(
        runs,
        "_list_runs",
        lambda **filters: [
            {"id": "r1", "agent_id": "main", "status": "completed", "total_cost_usd": 0.01},
        ],
    )
    monkeypatch.setattr(
        runs,
        "_get_run",
        lambda run_id: (
            {"id": RUN_ID, "agent_id": "main", "status": "completed"} if run_id == RUN_ID else None
        ),
    )
    monkeypatch.setattr(
        runs,
        "_get_steps",
        lambda run_id: [
            {"step_number": 1, "step_type": "tool_call", "tool_name": "exec"},
        ],
    )
    monkeypatch.setattr(
        runs,
        "_get_guardrail_events",
        lambda run_id: [
            {"guardrail_name": "exec_allowlist_strict", "action": "blocked", "tool_name": "exec"},
        ],
    )


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/runs").status_code == 403


def test_list_returns_runs(controls_client_as_operator, fake_runs):
    r = controls_client_as_operator.get("/api/runs")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "r1"


def test_list_clamps_limit(controls_client_as_operator, monkeypatch):
    seen = {}

    # Keyword-only since the list gained status/trigger/since/offset filters.
    def _fake_list_runs(**filters):
        seen["limit"] = filters["limit"]
        return []

    monkeypatch.setattr(runs, "_list_runs", _fake_list_runs)
    controls_client_as_operator.get("/api/runs?limit=9999")
    assert seen["limit"] == 200


def test_detail_bundles_steps_and_events(controls_client_as_operator, fake_runs):
    body = controls_client_as_operator.get(f"/api/runs/{RUN_ID}").json()
    assert body["run"]["id"] == RUN_ID
    assert body["steps"][0]["tool_name"] == "exec"
    assert body["guardrail_events"][0]["action"] == "blocked"


def test_detail_404(controls_client_as_operator, fake_runs):
    assert controls_client_as_operator.get("/api/runs/nope").status_code == 404


def test_runs_router_is_read_only():
    methods = {m for route in runs.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}


def test_run_columns_carry_the_delivery_and_verification_truth():
    """The engine has been writing these four columns for months and the Runs
    list did not select them, so "completed" on screen could mean a run whose
    answer never left the box. Asserted on the column list because that is the
    single place the omission happened."""
    for column in ("delivered_at", "delivery_channel", "delivery_mode", "verified_status"):
        assert column in runs._RUN_COLUMNS


# ─── Filters, paging, summary, and a detail that explains the run ─────────


@pytest.fixture
def captured_sql(monkeypatch):
    """Record every query the router builds, and answer with no rows."""
    calls: list[tuple[str, tuple]] = []

    def _fake_rows(sql, params):
        calls.append((sql, params))
        return []

    monkeypatch.setattr(runs, "_rows", _fake_rows)
    return calls


def test_list_filters_reach_the_query_as_parameters(controls_client_as_operator, captured_sql):
    r = controls_client_as_operator.get(
        "/api/runs?status=failed&agent=example-agent&trigger=cron&since=24&limit=25&offset=50"
    )
    assert r.status_code == 200
    sql, params = captured_sql[-1]
    assert "status = %s" in sql
    assert "agent_id = %s" in sql
    assert "trigger_type = %s" in sql
    assert "started_at >= NOW() - make_interval(hours => %s)" in sql
    assert "LIMIT %s OFFSET %s" in sql
    assert params == ("failed", "example-agent", "cron", 24, 25, 50)
    # User input never reaches the SQL text itself.
    assert "example-agent" not in sql


def test_list_without_filters_has_no_where_clause(controls_client_as_operator, captured_sql):
    controls_client_as_operator.get("/api/runs")
    sql, params = captured_sql[-1]
    assert "WHERE" not in sql
    assert params == (50, 0)


def test_list_clamps_offset_and_rejects_a_bad_period(controls_client_as_operator, captured_sql):
    controls_client_as_operator.get("/api/runs?offset=-5")
    assert captured_sql[-1][1][-1] == 0
    assert controls_client_as_operator.get("/api/runs?since=abc").status_code == 422
    assert controls_client_as_operator.get("/api/runs?since=0").status_code == 422


def test_summary_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/runs/summary").status_code == 403


def test_summary_counts_by_status_delivery_and_agent(controls_client_as_operator, monkeypatch):
    seen = {}

    def _fake_rows(sql, params):
        seen["sql"], seen["params"] = sql, params
        return [
            {
                "agent_id": "main",
                "status": "completed",
                "delivery": "delivered",
                "runs": 3,
                "cost": 0.3,
            },
            {"agent_id": "main", "status": "failed", "delivery": "failed", "runs": 1, "cost": 0.05},
            {
                "agent_id": "worker",
                "status": "completed",
                "delivery": "none_expected",
                "runs": 2,
                "cost": 0.0,
            },
        ]

    monkeypatch.setattr(runs, "_rows", _fake_rows)
    r = controls_client_as_operator.get("/api/runs/summary?since=24&agent=main")
    assert r.status_code == 200
    body = r.json()
    assert body["since_hours"] == 24
    assert body["total"] == 6
    assert body["by_status"] == {"completed": 5, "failed": 1}
    assert body["by_delivery"] == {
        "delivered": 3,
        "none_expected": 2,
        "failed": 1,
        "pending": 0,
        "not_recorded": 0,
    }
    assert body["by_agent"][0] == {"agent_id": "main", "runs": 4, "failed": 1, "cost_usd": 0.35}
    assert body["by_agent"][1]["agent_id"] == "worker"
    assert body["total_cost_usd"] == pytest.approx(0.35)
    assert "agent_id = %s" in seen["sql"]
    assert seen["params"] == ("main", 24)


def test_summary_excludes_benchmark_runs(controls_client_as_operator, monkeypatch, captured_sql):
    monkeypatch.setattr(runs, "exclude_benchmark_filter", lambda alias="": "(NOT benchmark)")
    controls_client_as_operator.get("/api/runs/summary")
    assert "(NOT benchmark)" in captured_sql[-1][0]


def test_summary_is_routed_before_the_run_id(controls_client_as_operator, monkeypatch, fake_runs):
    """`/summary` must not be read as a run whose id is "summary"."""
    monkeypatch.setattr(runs, "_rows", lambda sql, params: [])
    r = controls_client_as_operator.get("/api/runs/summary")
    assert r.status_code == 200
    assert "by_delivery" in r.json()


def test_delivery_category_sql_names_every_intentional_non_send():
    """Intentional non-sends are their own category, never counted as failures."""
    for status in (
        "silent",
        "suppressed_sub_agent",
        "suppressed_trivial",
        "suppressed_filtered",
        "no_output",
        "published",
    ):
        assert f"'{status}'" in runs._DELIVERY_CATEGORY_SQL
    assert "blocked_by_hook:%" in runs._DELIVERY_CATEGORY_SQL
    for failure in ("failed%", "partial:%", "unknown_mode:%"):
        assert failure in runs._DELIVERY_CATEGORY_SQL


def test_detail_selects_the_columns_that_explain_the_run(captured_sql, monkeypatch):
    runs._get_run("r1")
    run_sql = captured_sql[-1][0]
    for column in (
        "task_text",
        "trigger_detail",
        "outcome_notes",
        "verification",
        "models_attempted",
        "cache_creation_tokens",
        "cache_read_tokens",
        "parent_run_id",
        "nesting_depth",
    ):
        assert column in run_sql
    runs._get_steps("r1")
    step_sql = captured_sql[-1][0]
    for column in (
        "tool_input",
        "tool_output",
        "started_at",
        "completed_at",
        "cache_creation_tokens",
        "cache_read_tokens",
    ):
        assert column in step_sql


def test_detail_is_not_cached(controls_client_as_operator, fake_runs):
    r = controls_client_as_operator.get(f"/api/runs/{RUN_ID}")
    assert r.headers.get("cache-control") == "no-store"


def test_detail_requires_operator(controls_client_as_viewer, fake_runs):
    assert controls_client_as_viewer.get(f"/api/runs/{RUN_ID}").status_code == 403


def test_list_order_has_a_tie_break(controls_client_as_operator, captured_sql):
    """Runs that started at the same instant must not swap between pages."""
    controls_client_as_operator.get("/api/runs?offset=50")
    assert "ORDER BY started_at DESC NULLS LAST, id DESC LIMIT %s OFFSET %s" in captured_sql[-1][0]


def test_a_run_awaiting_approval_is_pending_delivery():
    """`awaiting_approval` has not finished, so no delivery yet is pending."""
    assert "status IN ('pending', 'running', 'awaiting_approval')" in runs._DELIVERY_CATEGORY_SQL


def test_detail_404_for_an_id_that_is_not_a_uuid(controls_client_as_operator, monkeypatch):
    """A malformed id is an unknown run, not a database error."""
    called = []
    monkeypatch.setattr(runs, "_get_run", lambda run_id: called.append(run_id))
    r = controls_client_as_operator.get("/api/runs/not-a-uuid")
    assert r.status_code == 404
    assert called == []


def test_delivery_buckets_normalise_case_whitespace_and_empty():
    """SQL and run-truth.ts read every input the same way: trimmed, lower-cased,
    and an empty status is no status."""
    sql = runs._DELIVERY_CATEGORY_SQL
    assert "lower(btrim(delivery_status))" in sql
    assert "delivery_status IN" not in sql
    assert "NULLIF(btrim(delivery_status), '') IS NULL" in sql


def test_detail_404_for_a_valid_uuid_that_is_gone(controls_client_as_operator, fake_runs):
    """A well-formed id with no row is the "run is gone" answer, not a 500."""
    r = controls_client_as_operator.get("/api/runs/22222222-2222-4222-8222-222222222222")
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown run"
    assert r.headers.get("cache-control") == "no-store"

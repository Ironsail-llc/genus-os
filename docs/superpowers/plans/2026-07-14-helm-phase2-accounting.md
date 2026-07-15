# The Helm — Phase 2 (Accounting Tabs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four read-only operator accounting tabs — Fleet, Runs, Workflows, Health — to the dashboard, each backed by a new operator-scoped, read-only bridge API, so the operator can see the whole running system (what each agent *can* do vs what it *did*, every run's steps/blocks/cost, workflow history, and box health).

**Architecture:** New bridge routers under `crm/bridge/routers/` expose `GET`-only JSON, gated by the same operator check Phase 1 introduced (extracted to a shared module in Task 1). They read through the canonical RLS-scoped connection `robothor.db.connection.get_connection()`, which binds `app.tenant_id` to the bridge's platform tenant automatically — so the operator sees the platform tenant's fleet and runs, and nothing else. The Next.js dashboard gets four new client views mirroring Phase 1's `controls-view.tsx`, fetching through the existing `/api/bridge/[...path]` proxy. Every tab carries Phase 1's honesty rule forward: a capability without a constraint, a never-run backup, a failed unit — none render green; they render as a flagged question.

**Tech Stack:** FastAPI + psycopg2 (bridge), Next.js 16 / React 19 + Tailwind (dashboard), pytest (bridge unit lane, mocked DB), vitest + Testing Library (frontend).

## Global Constraints

- **Read-only.** Phase 2 adds NO write path. Every new router exposes only `GET`. Each router's test asserts its `router.routes` contain no method in `{POST, PUT, PATCH, DELETE}`. The single mutation surface in the whole program stays the Phase 1 control PATCH.
- **Operator-only, exactly as Phase 1.** Every handler calls `require_operator(request)` first. The gate rejects: `auth is None`, `auth.is_service` (agents), `auth.role not in OPERATOR_ROLES` (`{"owner","admin"}`), and `auth.tenant_id != PLATFORM_TENANT`. Do not weaken any of the four conditions.
- **Tenant scope through `get_connection()` only.** Read routers use `from robothor.db.connection import get_connection` with `with get_connection() as conn:`. NEVER use `crm/bridge/crm_dal.py::_conn()` (deprecated, hands out an env-bound raw connection) and NEVER call `psycopg2.connect` directly in a router. `get_connection()` applies `_apply_tenant_scope` so RLS is honored.
- **Bridge tests run in the no-DB unit lane.** Bridge test files do NOT set `pytestmark = pytest.mark.integration`. DB access is isolated behind a module-level `_query_*` helper per router; unit tests `monkeypatch.setattr` that helper (exactly as Phase 1 patches `routers.controls.store`). A separate `pytestmark = pytest.mark.integration` test file per router exercises the real SQL against the `db_conn`/`db_cursor` fixtures (disposable `robothor_test` DB) to prove the query is valid — NEVER against production `robothor_memory`.
- **Frontend tabs mount-always, hide-when-hidden.** Each view takes `visible: boolean`, renders a root with `style={{ display: visible ? "flex" : "none" }}` and `data-testid="<name>-view"`, and fetches in a `useCallback` fired by `useEffect(() => { if (visible) fetch...(); }, [visible, ...])` — the exact pattern in `controls-view.tsx`.
- **Honesty rule (carried from Phase 1).** No stale/never/failed/unconstrained state renders green. ENFORCING/healthy/fresh → emerald; a flagged concern (capability-without-constraint, stale backup, failed unit, never-run) → amber; neutral → zinc. Reuse the same class helper shape as `badgeClassFor` in `controls-view.tsx`.
- **Fetch base + proxy.** Frontend fetches `\`/api/bridge/api/<thing>\`` (the `/api/bridge` prefix is the Next BFF proxy; the rest is the bridge path). `BRIDGE_URL = "/api/bridge"` as in `controls-view.tsx`.
- **Cursor factory.** Router SQL uses `conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)` so rows are dicts.

---

## File Structure

**New (bridge):**
- `crm/bridge/routers/_operator.py` — shared operator gate (`require_operator`, `OPERATOR_ROLES`, `PLATFORM_TENANT`), extracted from `controls.py`.
- `crm/bridge/routers/fleet.py` — `GET /api/fleet`, `GET /api/fleet/{agent_id}`.
- `crm/bridge/routers/runs.py` — `GET /api/runs`, `GET /api/runs/{run_id}`.
- `crm/bridge/routers/workflows.py` — `GET /api/workflows`, `GET /api/workflows/{workflow_id}/runs`.
- `crm/bridge/routers/system_health.py` — `GET /api/health/system`.

**New (bridge tests):** one unit test + one integration test per router, under `crm/bridge/tests/`.

**New (frontend):**
- `app/src/components/views/fleet-view.tsx`, `runs-view.tsx`, `workflows-view.tsx`, `health-view.tsx`
- matching `__tests__/*.test.tsx` for each.

**Modified:**
- `crm/bridge/routers/controls.py` — import the gate from `_operator` (keep behavior identical).
- `crm/bridge/bridge_service.py` — import + `include_router` the four new routers.
- `app/src/components/layout/sidebar.tsx` — extend `ViewId`, add `navItems`.
- `app/src/components/layout/app-shell.tsx` — import views, extend `viewTitles`, render with `visible=`.
- `app/src/components/layout/mobile-tab-bar.tsx` — extend `MobileViewId` / tab list.

**Architecture decisions locked here (do not re-litigate mid-build):**
1. **Workflows read from the bridge, not the engine.** The engine's `/api/workflows*` (`robothor/engine/health.py`) is NOT operator-scoped and lives on a different service the dashboard proxy doesn't reach. Phase 2 adds a bridge `workflows.py` that queries `workflow_runs` directly under the operator gate — one auth model for the whole tab set. Consequence: the Workflows tab lists workflows that *have run at least once* (distinct `workflow_id` in `workflow_runs`). A defined-but-never-run workflow won't appear; the tab states this limitation in its empty/footer copy rather than implying full coverage.
2. **`system_health` runs read-only host probes.** `GET /api/health/system` shells out to `systemctl` (list failed units; read backup timer `LastTriggerUSec`) and reads `pg_stat_archiver` via SQL and `shutil.disk_usage`. All read-only, no root. Each probe is independently wrapped so one failing probe degrades that field to an explicit `"unknown"`, never 500s the endpoint.

---

### Task 1: Extract the shared operator gate

**Files:**
- Create: `crm/bridge/routers/_operator.py`
- Modify: `crm/bridge/routers/controls.py:34-72` (import gate instead of defining it)
- Test: `crm/bridge/tests/test_operator_gate.py`

**Interfaces:**
- Produces: `require_operator(request: Request) -> str` (returns `f"operator:{auth.actor_id}"`, raises `HTTPException(403)` otherwise); `OPERATOR_ROLES: frozenset[str]`; `PLATFORM_TENANT: str`. All Phase 2 routers consume these.

- [ ] **Step 1: Write the failing test**

```python
# crm/bridge/tests/test_operator_gate.py
"""The operator gate is the single authorization primitive for the whole Helm.
It must reject agents, non-operator humans, and cross-tenant operators identically
whether it guards Controls (Phase 1) or the accounting APIs (Phase 2)."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from routers._operator import OPERATOR_ROLES, PLATFORM_TENANT, require_operator


def _request_with_auth(auth):
    scope = {"type": "http", "state": {"auth": auth}}
    return Request(scope)


def _auth(*, role="owner", tenant=None, is_service=False, actor="u1"):
    tenant = PLATFORM_TENANT if tenant is None else tenant
    return SimpleNamespace(
        role=role, tenant_id=tenant, is_service=is_service, actor_id=actor
    )


def test_platform_operator_passes():
    assert require_operator(_request_with_auth(_auth())) == "operator:u1"


def test_missing_auth_is_rejected():
    with pytest.raises(HTTPException) as e:
        require_operator(_request_with_auth(None))
    assert e.value.status_code == 403


def test_service_token_is_rejected():
    with pytest.raises(HTTPException) as e:
        require_operator(_request_with_auth(_auth(is_service=True)))
    assert e.value.status_code == 403


@pytest.mark.parametrize("role", ["member", "user", "viewer", "auditor"])
def test_non_operator_human_roles_rejected(role):
    with pytest.raises(HTTPException):
        require_operator(_request_with_auth(_auth(role=role)))


def test_operator_of_another_tenant_rejected():
    with pytest.raises(HTTPException):
        require_operator(_request_with_auth(_auth(tenant="some-other-tenant")))


def test_operator_roles_are_owner_and_admin():
    assert OPERATOR_ROLES == frozenset({"owner", "admin"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd crm/bridge && python -m pytest tests/test_operator_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routers._operator'`

- [ ] **Step 3: Create the shared module** (move the code verbatim from `controls.py`)

```python
# crm/bridge/routers/_operator.py
"""The Helm's single authorization primitive.

Extracted from ``controls.py`` (Phase 1) so every operator-scoped Helm route —
Controls, Fleet, Runs, Workflows, Health — shares ONE gate. Four independent
conditions, none of which may be weakened:

1. ``auth is None``            → no verified session at all.
2. ``auth.is_service``         → agent/service tokens, structurally barred.
3. ``auth.role not in OPERATOR_ROLES`` → dashboard SSO admits any verified org
   member; only owner/admin are operators.
4. ``auth.tenant_id != PLATFORM_TENANT`` → the flags/health/fleet surfaces are
   platform-global; an operator of another tenant must not see or touch them.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request

PLATFORM_TENANT = (
    os.environ.get("ROBOTHOR_PLATFORM_TENANT")
    or os.environ.get("ROBOTHOR_DEFAULT_TENANT")
    or "robothor-primary"
)

OPERATOR_ROLES = frozenset({"owner", "admin"})


def require_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", None)
    if (
        auth is None
        or auth.is_service
        or auth.role not in OPERATOR_ROLES
        or auth.tenant_id != PLATFORM_TENANT
    ):
        raise HTTPException(status_code=403, detail="operator role required")
    return f"operator:{auth.actor_id}"
```

- [ ] **Step 4: Point `controls.py` at the shared gate** — replace its local `PLATFORM_TENANT`, `OPERATOR_ROLES`, and `_require_operator` (lines 44-72) with an import, keeping the old name as an alias so the rest of the file is untouched:

```python
# in controls.py, replace lines 44-72 with:
from routers._operator import OPERATOR_ROLES, PLATFORM_TENANT, require_operator

# keep the private name the handlers already call:
_require_operator = require_operator
```

(Leave `list_controls`/`set_control` bodies calling `_require_operator(request)` unchanged.)

- [ ] **Step 5: Run gate test + the full Phase 1 controls test to confirm no regression**

Run: `cd crm/bridge && python -m pytest tests/test_operator_gate.py tests/test_controls_router.py -v`
Expected: PASS (gate tests green; all Phase 1 controls tests still green)

- [ ] **Step 6: Commit**

```bash
git add crm/bridge/routers/_operator.py crm/bridge/routers/controls.py crm/bridge/tests/test_operator_gate.py
git commit -m "refactor(bridge): extract shared operator gate for the Helm accounting APIs"
```

---

### Task 2: Fleet read API

**Files:**
- Create: `crm/bridge/routers/fleet.py`
- Test: `crm/bridge/tests/test_fleet_router.py` (unit, mocked), `crm/bridge/tests/test_fleet_query_integration.py` (integration)
- Modify: `crm/bridge/bridge_service.py` (register router)

**Interfaces:**
- Consumes: `require_operator` (Task 1); `robothor.engine.config.load_all_manifests`; `robothor.db.connection.get_connection`.
- Produces: `GET /api/fleet -> list[dict]` (one entry per agent), `GET /api/fleet/{agent_id} -> dict`. Each fleet entry has keys: `agent_id`, `name`, `department`, `model`, `sandbox`, `delivery_mode`, `tools_allowed` (list), `exec_allowlist` (list), `enabled` (bool|None), `next_run_at`, `last_run_at`, `last_status`, `consecutive_errors`, `runs_7d`, `failures_7d`, `findings` (list of `{code, message}`). `findings` is Phase 2's honesty output.

**The capability-without-a-constraint predicate (exact):** an agent is flagged when it can run shell but nothing constrains it.
- `has_exec = ("exec" in tools_allowed) or (not tools_allowed)` — an empty/absent `tools_allowed` means "all tools", which includes `exec`.
- `constrained = bool(exec_allowlist)`
- `sandboxed = sandbox not in (None, "", "host", "local")`
- Finding `EXEC_NO_ALLOWLIST` when `has_exec and not constrained`.
- Finding `EXEC_UNSANDBOXED` when `has_exec and not sandboxed and not constrained` (unconstrained AND uncontained is the real danger; a tight allowlist on host is an accepted answer per the hardening plan, so a sandboxed-or-allowlisted agent is not flagged for containment).

- [ ] **Step 1: Write the failing unit test**

```python
# crm/bridge/tests/test_fleet_router.py
"""Fleet answers: what can each agent DO vs what did it do. The honesty carry-over
is `findings`: an agent holding exec with no allowlist is flagged like an inert
control — a capability without a constraint is a finding, not a fact."""
import pytest

from routers import fleet


@pytest.fixture
def fake_fleet(monkeypatch):
    manifests = [
        {"id": "main", "name": "Main", "department": "core",
         "model": {"primary": "m"}, "sandbox": "host",
         "delivery": {"mode": "announce"},
         "tools_allowed": ["exec", "web_fetch"], "exec_allowlist": ["git status"]},
        {"id": "loose", "name": "Loose", "department": "core",
         "model": {"primary": "m"}, "sandbox": "host",
         "delivery": {"mode": "none"},
         "tools_allowed": ["exec"], "exec_allowlist": []},
    ]
    schedule_rows = {
        "main": {"enabled": True, "next_run_at": None, "last_run_at": None,
                 "last_status": "completed", "consecutive_errors": 0},
    }
    run_stats = {"main": {"runs_7d": 5, "failures_7d": 1}}
    monkeypatch.setattr(fleet, "_load_manifests", lambda: manifests)
    monkeypatch.setattr(fleet, "_schedule_rows", lambda: schedule_rows)
    monkeypatch.setattr(fleet, "_run_stats", lambda: run_stats)


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/fleet").status_code == 403


def test_list_returns_all_agents_for_operator(controls_client_as_operator, fake_fleet):
    r = controls_client_as_operator.get("/api/fleet")
    assert r.status_code == 200
    ids = {a["agent_id"] for a in r.json()}
    assert ids == {"main", "loose"}


def test_unconstrained_exec_agent_is_flagged(controls_client_as_operator, fake_fleet):
    by_id = {a["agent_id"]: a for a in controls_client_as_operator.get("/api/fleet").json()}
    assert by_id["main"]["findings"] == []          # exec + allowlist → clean
    codes = {f["code"] for f in by_id["loose"]["findings"]}
    assert "EXEC_NO_ALLOWLIST" in codes             # exec + no allowlist → flagged


def test_detail_404_for_unknown_agent(controls_client_as_operator, fake_fleet):
    assert controls_client_as_operator.get("/api/fleet/nope").status_code == 404


def test_fleet_router_is_read_only():
    methods = {m for route in fleet.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
```

Add a `controls_client_as_viewer` fixture if not present — reuse `_make_controls_client_as_role("viewer")` from `crm/bridge/tests/conftest.py`. If that helper isn't already exposed as a fixture, add:

```python
# append to crm/bridge/tests/conftest.py
@pytest.fixture
def controls_client_as_viewer(_make_controls_client_as_role):
    return _make_controls_client_as_role("viewer")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd crm/bridge && python -m pytest tests/test_fleet_router.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'routers.fleet'`)

- [ ] **Step 3: Implement the router**

```python
# crm/bridge/routers/fleet.py
"""Operator-scoped, read-only fleet accounting.

For each agent it shows what it CAN do (tools, exec allowlist, sandbox, delivery)
beside what it DID (schedule state, 7d run/failure counts). The honesty carry-over
from Phase 1 is ``findings``: a capability without a constraint (exec with no
allowlist) is flagged, exactly as an inert control is.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request

from robothor.db.connection import get_connection
from robothor.engine.config import load_all_manifests
from routers._operator import require_operator

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

_MANIFEST_DIR = Path(
    os.environ.get("ROBOTHOR_AGENTS_DIR")
    or (Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))) / "docs" / "agents")
)


def _load_manifests() -> list[dict]:
    return load_all_manifests(_MANIFEST_DIR)


def _schedule_rows() -> dict[str, dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT agent_id, enabled, next_run_at, last_run_at, last_status, "
                "consecutive_errors FROM agent_schedules"
            )
            return {r["agent_id"]: dict(r) for r in cur.fetchall()}


def _run_stats() -> dict[str, dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT agent_id, "
                "COUNT(*) AS runs_7d, "
                "COUNT(*) FILTER (WHERE status IN ('failed','timeout')) AS failures_7d "
                "FROM agent_runs WHERE started_at > now() - interval '7 days' "
                "GROUP BY agent_id"
            )
            return {r["agent_id"]: {"runs_7d": r["runs_7d"], "failures_7d": r["failures_7d"]}
                    for r in cur.fetchall()}


def _findings(tools_allowed: list[str], exec_allowlist: list[str], sandbox: str | None) -> list[dict]:
    has_exec = ("exec" in tools_allowed) or (not tools_allowed)
    constrained = bool(exec_allowlist)
    sandboxed = sandbox not in (None, "", "host", "local")
    out: list[dict] = []
    if has_exec and not constrained:
        out.append({"code": "EXEC_NO_ALLOWLIST",
                    "message": "holds exec with no exec_allowlist — unconstrained shell"})
        if not sandboxed:
            out.append({"code": "EXEC_UNSANDBOXED",
                        "message": "unconstrained exec runs on the host (no sandbox)"})
    return out


def _entry(m: dict, sched: dict, stats: dict) -> dict:
    agent_id = m.get("id")
    tools_allowed = m.get("tools_allowed") or []
    exec_allowlist = m.get("exec_allowlist") or []
    sandbox = m.get("sandbox")
    s = sched.get(agent_id, {})
    st = stats.get(agent_id, {})
    return {
        "agent_id": agent_id,
        "name": m.get("name"),
        "department": m.get("department"),
        "model": (m.get("model") or {}).get("primary"),
        "sandbox": sandbox,
        "delivery_mode": (m.get("delivery") or {}).get("mode"),
        "tools_allowed": tools_allowed,
        "exec_allowlist": exec_allowlist,
        "enabled": s.get("enabled"),
        "next_run_at": s.get("next_run_at").isoformat() if s.get("next_run_at") else None,
        "last_run_at": s.get("last_run_at").isoformat() if s.get("last_run_at") else None,
        "last_status": s.get("last_status"),
        "consecutive_errors": s.get("consecutive_errors"),
        "runs_7d": st.get("runs_7d", 0),
        "failures_7d": st.get("failures_7d", 0),
        "findings": _findings(tools_allowed, exec_allowlist, sandbox),
    }


@router.get("")
def list_fleet(request: Request) -> list[dict]:
    require_operator(request)
    manifests = _load_manifests()
    sched = _schedule_rows()
    stats = _run_stats()
    return [_entry(m, sched, stats) for m in manifests]


@router.get("/{agent_id}")
def get_agent(agent_id: str, request: Request) -> dict:
    require_operator(request)
    sched = _schedule_rows()
    stats = _run_stats()
    for m in _load_manifests():
        if m.get("id") == agent_id:
            return _entry(m, sched, stats)
    raise HTTPException(status_code=404, detail="unknown agent")
```

- [ ] **Step 4: Register the router** in `crm/bridge/bridge_service.py` — add to the import block (~line 42) and the include block (~line 152):

```python
from routers.fleet import router as fleet_router      # import block
app.include_router(fleet_router)                       # include block
```

- [ ] **Step 5: Write the integration test for the real SQL**

```python
# crm/bridge/tests/test_fleet_query_integration.py
"""Proves the fleet SQL is valid against a real schema (disposable robothor_test),
never production. Only the query helpers touch the DB; the HTTP layer is unit-tested."""
import pytest

pytestmark = pytest.mark.integration

from routers import fleet


def test_schedule_rows_query_runs(db_conn, monkeypatch):
    # _schedule_rows opens its own connection; point get_connection at the test conn.
    from contextlib import contextmanager

    @contextmanager
    def _fake_conn():
        yield db_conn

    monkeypatch.setattr(fleet, "get_connection", _fake_conn)
    rows = fleet._schedule_rows()
    assert isinstance(rows, dict)


def test_run_stats_query_runs(db_conn, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _fake_conn():
        yield db_conn

    monkeypatch.setattr(fleet, "get_connection", _fake_conn)
    stats = fleet._run_stats()
    assert isinstance(stats, dict)
```

- [ ] **Step 6: Run unit + integration tests**

Run: `cd crm/bridge && python -m pytest tests/test_fleet_router.py -v` (no DB) then
`cd /home/philip/robothor && ROBOTHOR_DB_NAME=robothor_test python -m pytest crm/bridge/tests/test_fleet_query_integration.py -v -m integration`
Expected: unit PASS with no DB; integration PASS against `robothor_test`.

- [ ] **Step 7: Commit**

```bash
git add crm/bridge/routers/fleet.py crm/bridge/tests/test_fleet_router.py crm/bridge/tests/test_fleet_query_integration.py crm/bridge/tests/conftest.py crm/bridge/bridge_service.py
git commit -m "feat(bridge): operator-scoped fleet read API with capability-without-constraint findings"
```

---

### Task 3: Runs read API

**Files:**
- Create: `crm/bridge/routers/runs.py`
- Test: `crm/bridge/tests/test_runs_router.py` (unit), `crm/bridge/tests/test_runs_query_integration.py` (integration)
- Modify: `crm/bridge/bridge_service.py`

**Interfaces:**
- Consumes: `require_operator` (Task 1); `get_connection`.
- Produces: `GET /api/runs?agent=<id>&limit=<n> -> list[dict]` (recent runs, newest first, `limit` default 50 max 200); `GET /api/runs/{run_id} -> dict` with keys `run` (the agent_runs row), `steps` (list of agent_run_steps), `guardrail_events` (list). 404 if run absent.

- [ ] **Step 1: Write the failing unit test**

```python
# crm/bridge/tests/test_runs_router.py
"""Runs answers: what happened in this run — every step, block, and cost."""
import pytest

from routers import runs


@pytest.fixture
def fake_runs(monkeypatch):
    monkeypatch.setattr(runs, "_list_runs", lambda agent, limit: [
        {"id": "r1", "agent_id": "main", "status": "completed", "total_cost_usd": 0.01},
    ])
    monkeypatch.setattr(runs, "_get_run", lambda run_id: (
        {"id": "r1", "agent_id": "main", "status": "completed"}
        if run_id == "r1" else None
    ))
    monkeypatch.setattr(runs, "_get_steps", lambda run_id: [
        {"step_number": 1, "step_type": "tool_call", "tool_name": "exec"},
    ])
    monkeypatch.setattr(runs, "_get_guardrail_events", lambda run_id: [
        {"guardrail_name": "exec_allowlist_strict", "action": "blocked", "tool_name": "exec"},
    ])


def test_list_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/runs").status_code == 403


def test_list_returns_runs(controls_client_as_operator, fake_runs):
    r = controls_client_as_operator.get("/api/runs")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "r1"


def test_list_clamps_limit(controls_client_as_operator, monkeypatch):
    seen = {}
    monkeypatch.setattr(runs, "_list_runs", lambda agent, limit: seen.setdefault("limit", limit) or [])
    controls_client_as_operator.get("/api/runs?limit=9999")
    assert seen["limit"] == 200


def test_detail_bundles_steps_and_events(controls_client_as_operator, fake_runs):
    body = controls_client_as_operator.get("/api/runs/r1").json()
    assert body["run"]["id"] == "r1"
    assert body["steps"][0]["tool_name"] == "exec"
    assert body["guardrail_events"][0]["action"] == "blocked"


def test_detail_404(controls_client_as_operator, fake_runs):
    assert controls_client_as_operator.get("/api/runs/nope").status_code == 404


def test_runs_router_is_read_only():
    methods = {m for route in runs.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd crm/bridge && python -m pytest tests/test_runs_router.py -v`
Expected: FAIL (no module `routers.runs`)

- [ ] **Step 3: Implement the router**

```python
# crm/bridge/routers/runs.py
"""Operator-scoped, read-only run accounting: recent runs, and per-run steps +
guardrail blocks + cost. Reads through the RLS-scoped connection, so the operator
sees the platform tenant's runs only."""
from __future__ import annotations

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

router = APIRouter(prefix="/api/runs", tags=["runs"])

_MAX_LIMIT = 200

_RUN_COLUMNS = (
    "id, tenant_id, agent_id, trigger_type, status, started_at, completed_at, "
    "duration_ms, model_used, input_tokens, output_tokens, total_cost_usd, "
    "error_message, delivery_status, outcome_assessment"
)


def _rows(sql: str, params: tuple) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _serialize(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else (
            float(v) if isinstance(v, (int,)) is False and hasattr(v, "quantize") else v
        )
    return out


def _list_runs(agent: str | None, limit: int) -> list[dict]:
    if agent:
        sql = (f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE agent_id = %s "
               "ORDER BY started_at DESC NULLS LAST LIMIT %s")
        rows = _rows(sql, (agent, limit))
    else:
        sql = (f"SELECT {_RUN_COLUMNS} FROM agent_runs "
               "ORDER BY started_at DESC NULLS LAST LIMIT %s")
        rows = _rows(sql, (limit,))
    return [_serialize(r) for r in rows]


def _get_run(run_id: str) -> dict | None:
    rows = _rows(f"SELECT {_RUN_COLUMNS}, output_text, error_traceback "
                 "FROM agent_runs WHERE id = %s", (run_id,))
    return _serialize(rows[0]) if rows else None


def _get_steps(run_id: str) -> list[dict]:
    rows = _rows(
        "SELECT step_number, step_type, tool_name, model, input_tokens, output_tokens, "
        "duration_ms, error_message FROM agent_run_steps WHERE run_id = %s "
        "ORDER BY step_number", (run_id,))
    return [_serialize(r) for r in rows]


def _get_guardrail_events(run_id: str) -> list[dict]:
    rows = _rows(
        "SELECT step_number, guardrail_name, action, tool_name, reason, mode, created_at "
        "FROM agent_guardrail_events WHERE run_id = %s ORDER BY step_number", (run_id,))
    return [_serialize(r) for r in rows]


@router.get("")
def list_runs(request: Request, agent: str | None = None, limit: int = 50) -> list[dict]:
    require_operator(request)
    limit = max(1, min(limit, _MAX_LIMIT))
    return _list_runs(agent, limit)


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    require_operator(request)
    run = _get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return {
        "run": run,
        "steps": _get_steps(run_id),
        "guardrail_events": _get_guardrail_events(run_id),
    }
```

Note on `_serialize`: keep it simple and correct — replace the tricky numeric branch with an explicit `Decimal` check:

```python
from decimal import Decimal

def _serialize(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out
```

(Use this `_serialize`; it is the one the tests and the frontend rely on.)

- [ ] **Step 4: Register the router** in `bridge_service.py`:

```python
from routers.runs import router as runs_router
app.include_router(runs_router)
```

- [ ] **Step 5: Integration test**

```python
# crm/bridge/tests/test_runs_query_integration.py
import pytest

pytestmark = pytest.mark.integration

from contextlib import contextmanager

from routers import runs


def _bind(monkeypatch, db_conn):
    @contextmanager
    def _fake():
        yield db_conn
    monkeypatch.setattr(runs, "get_connection", _fake)


def test_list_runs_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    assert isinstance(runs._list_runs(None, 5), list)


def test_steps_and_events_sql_valid(db_conn, monkeypatch):
    _bind(monkeypatch, db_conn)
    assert isinstance(runs._get_steps("00000000-0000-0000-0000-000000000000"), list)
    assert isinstance(runs._get_guardrail_events("00000000-0000-0000-0000-000000000000"), list)
```

- [ ] **Step 6: Run unit + integration**

Run: `cd crm/bridge && python -m pytest tests/test_runs_router.py -v` then
`cd /home/philip/robothor && ROBOTHOR_DB_NAME=robothor_test python -m pytest crm/bridge/tests/test_runs_query_integration.py -v -m integration`
Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add crm/bridge/routers/runs.py crm/bridge/tests/test_runs_router.py crm/bridge/tests/test_runs_query_integration.py crm/bridge/bridge_service.py
git commit -m "feat(bridge): operator-scoped runs read API (list + per-run steps/blocks/cost)"
```

---

### Task 4: Workflows read API

**Files:**
- Create: `crm/bridge/routers/workflows.py`
- Test: `crm/bridge/tests/test_workflows_router.py` (unit), `crm/bridge/tests/test_workflows_query_integration.py` (integration)
- Modify: `crm/bridge/bridge_service.py`

**Interfaces:**
- Consumes: `require_operator`; `get_connection`.
- Produces: `GET /api/workflows -> list[dict]` (distinct `workflow_id` that have run, each with `runs`, `last_run_at`, `last_status`, `failures`); `GET /api/workflows/{workflow_id}/runs?limit=<n> -> list[dict]` (run history, newest first, limit default 20 max 100).

- [ ] **Step 1: Write the failing unit test**

```python
# crm/bridge/tests/test_workflows_router.py
"""Workflows answers: what multi-agent flows exist and what have they run.
Bridge-side and operator-scoped (the engine's /api/workflows* is neither).
Lists workflows that have run at least once — the honesty limitation is surfaced
in the tab copy, not hidden."""
import pytest

from routers import workflows


@pytest.fixture
def fake_workflows(monkeypatch):
    monkeypatch.setattr(workflows, "_list_workflows", lambda: [
        {"workflow_id": "intel", "runs": 3, "last_run_at": None,
         "last_status": "completed", "failures": 0},
    ])
    monkeypatch.setattr(workflows, "_workflow_runs", lambda wid, limit: (
        [{"id": "wr1", "status": "completed"}] if wid == "intel" else []
    ))


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd crm/bridge && python -m pytest tests/test_workflows_router.py -v`
Expected: FAIL (no module)

- [ ] **Step 3: Implement**

```python
# crm/bridge/routers/workflows.py
"""Operator-scoped, read-only workflow accounting, from workflow_runs.

Deliberately bridge-side: the engine's /api/workflows* is not operator-scoped and
the dashboard proxy targets the bridge. Lists workflows that have RUN (distinct
workflow_id in workflow_runs); a defined-but-never-run workflow is not shown, and
the tab states that limitation rather than implying full registry coverage.
"""
from __future__ import annotations

import psycopg2.extras
from fastapi import APIRouter, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_MAX_LIMIT = 100


def _rows(sql: str, params: tuple) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _iso(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _list_workflows() -> list[dict]:
    rows = _rows(
        "SELECT workflow_id, COUNT(*) AS runs, MAX(started_at) AS last_run_at, "
        "COUNT(*) FILTER (WHERE status IN ('failed','timeout')) AS failures "
        "FROM workflow_runs GROUP BY workflow_id ORDER BY MAX(started_at) DESC NULLS LAST",
        (),
    )
    out = []
    for r in rows:
        last = _rows("SELECT status FROM workflow_runs WHERE workflow_id = %s "
                     "ORDER BY started_at DESC NULLS LAST LIMIT 1", (r["workflow_id"],))
        d = _iso(r)
        d["last_status"] = last[0]["status"] if last else None
        out.append(d)
    return out


def _workflow_runs(workflow_id: str, limit: int) -> list[dict]:
    rows = _rows(
        "SELECT id, workflow_id, status, trigger_type, steps_total, steps_completed, "
        "steps_failed, steps_skipped, duration_ms, started_at, completed_at, error_message "
        "FROM workflow_runs WHERE workflow_id = %s "
        "ORDER BY started_at DESC NULLS LAST LIMIT %s", (workflow_id, limit))
    return [_iso(r) for r in rows]


@router.get("")
def list_workflows(request: Request) -> list[dict]:
    require_operator(request)
    return _list_workflows()


@router.get("/{workflow_id}/runs")
def workflow_runs(workflow_id: str, request: Request, limit: int = 20) -> list[dict]:
    require_operator(request)
    limit = max(1, min(limit, _MAX_LIMIT))
    return _workflow_runs(workflow_id, limit)
```

- [ ] **Step 4: Register** in `bridge_service.py`:

```python
from routers.workflows import router as workflows_router
app.include_router(workflows_router)
```

- [ ] **Step 5: Integration test**

```python
# crm/bridge/tests/test_workflows_query_integration.py
import pytest

pytestmark = pytest.mark.integration

from contextlib import contextmanager

from routers import workflows


def test_workflow_queries_valid(db_conn, monkeypatch):
    @contextmanager
    def _fake():
        yield db_conn
    monkeypatch.setattr(workflows, "get_connection", _fake)
    assert isinstance(workflows._list_workflows(), list)
    assert isinstance(workflows._workflow_runs("none", 5), list)
```

- [ ] **Step 6: Run unit + integration**

Run: `cd crm/bridge && python -m pytest tests/test_workflows_router.py -v` then
`cd /home/philip/robothor && ROBOTHOR_DB_NAME=robothor_test python -m pytest crm/bridge/tests/test_workflows_query_integration.py -v -m integration`
Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add crm/bridge/routers/workflows.py crm/bridge/tests/test_workflows_router.py crm/bridge/tests/test_workflows_query_integration.py crm/bridge/bridge_service.py
git commit -m "feat(bridge): operator-scoped workflows read API from workflow_runs"
```

---

### Task 5: System health read API

**Files:**
- Create: `crm/bridge/routers/system_health.py`
- Test: `crm/bridge/tests/test_system_health_router.py` (unit)
- Modify: `crm/bridge/bridge_service.py`

**Interfaces:**
- Consumes: `require_operator`; `get_connection`; `shutil`, `subprocess`.
- Produces: `GET /api/health/system -> dict` with keys `wal` (`{archived_count, failed_count, last_archived_time, status}`), `backups` (list of `{unit, last_trigger, age_hours, status}`), `failed_units` (list of unit names), `disks` (list of `{mount, used_pct, free_gb, status}`), `generated_at`. Each top-level probe is independently try/except-wrapped: on failure the value is `{"status": "unknown", "error": "..."}` (or `[]`), never a 500.

- [ ] **Step 1: Write the failing unit test**

```python
# crm/bridge/tests/test_system_health_router.py
"""Health answers: is the box OK — WAL archiving, backups, failed units, disks.
Honesty rule: a stale backup or failed unit is a flagged status, never green;
a probe that itself fails degrades to 'unknown', never a false-healthy."""
import pytest

from routers import system_health as sh


@pytest.fixture
def fake_probes(monkeypatch):
    monkeypatch.setattr(sh, "_wal_status", lambda: {"archived_count": 10, "failed_count": 0,
                                                    "last_archived_time": None, "status": "ok"})
    monkeypatch.setattr(sh, "_backup_status", lambda: [
        {"unit": "robothor-backup-local.timer", "last_trigger": None,
         "age_hours": 2.0, "status": "ok"}])
    monkeypatch.setattr(sh, "_failed_units", lambda: [])
    monkeypatch.setattr(sh, "_disk_status", lambda: [
        {"mount": "/", "used_pct": 40.0, "free_gb": 100.0, "status": "ok"}])


def test_requires_operator(controls_client_as_viewer):
    assert controls_client_as_viewer.get("/api/health/system").status_code == 403


def test_returns_all_sections(controls_client_as_operator, fake_probes):
    body = controls_client_as_operator.get("/api/health/system").json()
    assert set(body) >= {"wal", "backups", "failed_units", "disks", "generated_at"}
    assert body["wal"]["status"] == "ok"


def test_a_failing_probe_degrades_to_unknown_not_500(controls_client_as_operator, monkeypatch):
    def boom():
        raise RuntimeError("systemctl missing")
    monkeypatch.setattr(sh, "_wal_status", lambda: {"archived_count": 0, "failed_count": 0,
                                                    "last_archived_time": None, "status": "ok"})
    monkeypatch.setattr(sh, "_backup_status", boom)
    monkeypatch.setattr(sh, "_failed_units", lambda: [])
    monkeypatch.setattr(sh, "_disk_status", lambda: [])
    r = controls_client_as_operator.get("/api/health/system")
    assert r.status_code == 200
    assert r.json()["backups"]["status"] == "unknown"


def test_health_router_is_read_only():
    methods = {m for route in sh.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd crm/bridge && python -m pytest tests/test_system_health_router.py -v`
Expected: FAIL (no module)

- [ ] **Step 3: Implement**

```python
# crm/bridge/routers/system_health.py
"""Operator-scoped, read-only host health: WAL archiving, backup timers, failed
systemd units, disks. All probes are read-only (no root). Each probe is isolated:
one that raises degrades its own section to 'unknown' — the endpoint never 500s,
and a probe failure never reads as healthy."""
from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

import psycopg2.extras
from fastapi import APIRouter, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

router = APIRouter(prefix="/api/health", tags=["health"])

_BACKUP_TIMERS = ("robothor-backup-local.timer", "robothor-backup-offsite.timer")
_WATCH_MOUNTS = ("/", "/mnt/robothor-backup")
_STALE_BACKUP_HOURS = 26.0


def _wal_status() -> dict:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT archived_count, failed_count, last_archived_time "
                        "FROM pg_stat_archiver")
            row = cur.fetchone() or {}
    failed = row.get("failed_count") or 0
    last = row.get("last_archived_time")
    return {
        "archived_count": row.get("archived_count") or 0,
        "failed_count": failed,
        "last_archived_time": last.isoformat() if last else None,
        "status": "ok" if failed == 0 and last is not None else "warn",
    }


def _systemctl(*args: str) -> str:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                          timeout=10).stdout.strip()


def _backup_status() -> list[dict]:
    now = datetime.now(UTC)
    out = []
    for unit in _BACKUP_TIMERS:
        raw = _systemctl("show", unit, "-p", "LastTriggerUSec", "--value")
        age_hours = None
        last_iso = None
        # LastTriggerUSec is a human date string like "Mon 2026-07-14 03:00:00 UTC"
        if raw and raw not in ("", "0", "n/a"):
            try:
                dt = datetime.strptime(raw.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                age_hours = (now - dt).total_seconds() / 3600.0
                last_iso = dt.isoformat()
            except ValueError:
                pass
        status = "ok" if (age_hours is not None and age_hours <= _STALE_BACKUP_HOURS) else "warn"
        out.append({"unit": unit, "last_trigger": last_iso, "age_hours": age_hours, "status": status})
    return out


def _failed_units() -> list[str]:
    raw = _systemctl("list-units", "--state=failed", "--no-legend", "--plain")
    return [line.split()[0] for line in raw.splitlines() if line.strip()]


def _disk_status() -> list[dict]:
    out = []
    for mount in _WATCH_MOUNTS:
        try:
            u = shutil.disk_usage(mount)
        except (FileNotFoundError, PermissionError):
            out.append({"mount": mount, "used_pct": None, "free_gb": None, "status": "unknown"})
            continue
        used_pct = (u.used / u.total * 100.0) if u.total else 0.0
        out.append({"mount": mount, "used_pct": round(used_pct, 1),
                    "free_gb": round(u.free / 1e9, 1),
                    "status": "warn" if used_pct >= 90.0 else "ok"})
    return out


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # a probe failure must never 500 or read as healthy
        return {"status": "unknown", "error": str(exc)}


@router.get("/system")
def system_health(request: Request) -> dict:
    require_operator(request)
    return {
        "wal": _safe(_wal_status),
        "backups": _safe(_backup_status),
        "failed_units": _safe(_failed_units),
        "disks": _safe(_disk_status),
        "generated_at": datetime.now(UTC).isoformat(),
    }
```

Note: `_safe` returns a dict with `status: "unknown"` on failure for every section (the test asserts `body["backups"]["status"] == "unknown"`). The frontend treats a section that is a dict with `status == "unknown"` as amber, and a list as the normal case.

- [ ] **Step 4: Register** in `bridge_service.py`:

```python
from routers.system_health import router as system_health_router
app.include_router(system_health_router)
```

- [ ] **Step 5: Run unit tests** (no integration test needed — probes are unit-mocked; the WAL SQL is trivial and covered by the live smoke in the deploy step)

Run: `cd crm/bridge && python -m pytest tests/test_system_health_router.py -v`
Expected: PASS with no DB.

- [ ] **Step 6: Commit**

```bash
git add crm/bridge/routers/system_health.py crm/bridge/tests/test_system_health_router.py crm/bridge/bridge_service.py
git commit -m "feat(bridge): operator-scoped system health read API (WAL/backups/units/disks)"
```

---

### Task 6: Fleet tab (frontend)

**Files:**
- Create: `app/src/components/views/fleet-view.tsx`, `app/src/components/views/__tests__/fleet-view.test.tsx`
- Modify: `app/src/components/layout/sidebar.tsx`, `app/src/components/layout/app-shell.tsx`, `app/src/components/layout/mobile-tab-bar.tsx`

**Interfaces:**
- Consumes: `GET /api/fleet` via `/api/bridge/api/fleet`.
- Produces: `FleetView({ visible }: { visible?: boolean })`. Root `data-testid="fleet-view"`. Each agent row carries `data-testid={\`fleet-agent-${agent_id}\`}`; a flagged agent (non-empty `findings`) carries `data-finding="true"` and renders amber.

- [ ] **Step 1: Write the failing test**

```tsx
// app/src/components/views/__tests__/fleet-view.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { FleetView } from "../fleet-view";

afterEach(() => vi.restoreAllMocks());

function mockFleet(rows: unknown[]) {
  vi.spyOn(global, "fetch").mockResolvedValue({
    ok: true, json: async () => rows,
  } as Response);
}

describe("FleetView", () => {
  it("renders agents and flags a capability without a constraint", async () => {
    mockFleet([
      { agent_id: "main", name: "Main", model: "m", sandbox: "host",
        tools_allowed: ["exec"], exec_allowlist: ["git status"], findings: [] },
      { agent_id: "loose", name: "Loose", model: "m", sandbox: "host",
        tools_allowed: ["exec"], exec_allowlist: [],
        findings: [{ code: "EXEC_NO_ALLOWLIST", message: "unconstrained shell" }] },
    ]);
    render(<FleetView visible />);
    const flagged = await screen.findByTestId("fleet-agent-loose");
    expect(flagged.getAttribute("data-finding")).toBe("true");
    expect(flagged.className).not.toMatch(/emerald|green/i);
    const clean = screen.getByTestId("fleet-agent-main");
    expect(clean.getAttribute("data-finding")).toBe("false");
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<FleetView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && npx vitest run src/components/views/__tests__/fleet-view.test.tsx`
Expected: FAIL (cannot resolve `../fleet-view`)

- [ ] **Step 3: Implement the view**

```tsx
// app/src/components/views/fleet-view.tsx
"use client";

import { useCallback, useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type Finding = { code: string; message: string };
type Agent = {
  agent_id: string;
  name?: string;
  department?: string;
  model?: string;
  sandbox?: string;
  delivery_mode?: string;
  tools_allowed?: string[];
  exec_allowlist?: string[];
  enabled?: boolean | null;
  last_run_at?: string | null;
  last_status?: string | null;
  runs_7d?: number;
  failures_7d?: number;
  findings?: Finding[];
};

export function FleetView({ visible = true }: { visible?: boolean }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const fetchFleet = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/fleet`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setAgents(await res.json());
      setError(null);
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

  useEffect(() => {
    if (visible) fetchFleet();
  }, [visible, fetchFleet]);

  return (
    <div
      data-testid="fleet-view"
      className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}
    >
      <h2 className="text-lg font-semibold text-zinc-100">Fleet</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      <div className="flex flex-col gap-2">
        {agents.map((a) => {
          const flagged = (a.findings?.length ?? 0) > 0;
          return (
            <div
              key={a.agent_id}
              data-testid={`fleet-agent-${a.agent_id}`}
              data-finding={flagged ? "true" : "false"}
              className={`rounded-md border p-3 ${
                flagged ? "border-amber-500/50 bg-amber-500/5" : "border-zinc-700 bg-zinc-900"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-zinc-100">{a.name ?? a.agent_id}</span>
                <span className="text-xs text-zinc-400">{a.model}</span>
              </div>
              <div className="mt-1 flex flex-wrap gap-2 text-xs text-zinc-400">
                <span>sandbox: {a.sandbox ?? "—"}</span>
                <span>delivery: {a.delivery_mode ?? "—"}</span>
                <span>exec_allowlist: {(a.exec_allowlist?.length ?? 0)} rule(s)</span>
                <span>7d: {a.runs_7d ?? 0} run(s), {a.failures_7d ?? 0} fail</span>
                <span>last: {a.last_status ?? "—"}</span>
              </div>
              {flagged && (
                <ul className="mt-2 list-disc pl-5 text-xs text-amber-300">
                  {a.findings!.map((f) => (
                    <li key={f.code}>{f.message}</li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default FleetView;
```

- [ ] **Step 4: Register the tab** — three edits:

In `app/src/components/layout/sidebar.tsx`: extend the `ViewId` union with `"fleet"` and add a `navItems` entry `{ id: "fleet", icon: <Users className="h-5 w-5" />, label: "Fleet" }` (import `Users` from `lucide-react`).

In `app/src/components/layout/app-shell.tsx`: `import { FleetView } from "@/components/views/fleet-view";`, add `fleet: "Fleet"` to `viewTitles`, and render `<FleetView visible={sidebarView === "fleet"} />` alongside the others.

In `app/src/components/layout/mobile-tab-bar.tsx`: add `"fleet"` to `MobileViewId` and its tab list (or, if space-constrained, leave Fleet desktop-only and add a comment — but prefer including it).

- [ ] **Step 5: Run the view test + typecheck**

Run: `cd app && npx vitest run src/components/views/__tests__/fleet-view.test.tsx && npx tsc --noEmit`
Expected: test PASS; tsc clean.

- [ ] **Step 6: Commit**

```bash
git add app/src/components/views/fleet-view.tsx app/src/components/views/__tests__/fleet-view.test.tsx app/src/components/layout/sidebar.tsx app/src/components/layout/app-shell.tsx app/src/components/layout/mobile-tab-bar.tsx
git commit -m "feat(app): Fleet tab — capability vs constraint per agent"
```

---

### Task 7: Runs tab (frontend)

**Files:**
- Create: `app/src/components/views/runs-view.tsx`, `app/src/components/views/__tests__/runs-view.test.tsx`
- Modify: `sidebar.tsx`, `app-shell.tsx`, `mobile-tab-bar.tsx`

**Interfaces:**
- Consumes: `GET /api/runs`, `GET /api/runs/{id}` via the proxy.
- Produces: `RunsView({ visible })`. Root `data-testid="runs-view"`. Each run row `data-testid={\`run-row-${id}\`}`; clicking loads detail into a panel `data-testid="run-detail"` showing steps and guardrail events (a blocked event renders amber).

- [ ] **Step 1: Write the failing test**

```tsx
// app/src/components/views/__tests__/runs-view.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { RunsView } from "../runs-view";

afterEach(() => vi.restoreAllMocks());

describe("RunsView", () => {
  it("lists runs and opens detail with guardrail blocks flagged", async () => {
    vi.spyOn(global, "fetch").mockImplementation((url: string | URL | Request) => {
      const u = String(url);
      if (u.endsWith("/api/runs")) {
        return Promise.resolve({ ok: true, json: async () => [
          { id: "r1", agent_id: "main", status: "completed", total_cost_usd: 0.01 },
        ] } as Response);
      }
      return Promise.resolve({ ok: true, json: async () => ({
        run: { id: "r1", agent_id: "main", status: "completed" },
        steps: [{ step_number: 1, step_type: "tool_call", tool_name: "exec" }],
        guardrail_events: [{ guardrail_name: "exec_allowlist_strict", action: "blocked", tool_name: "exec" }],
      }) } as Response);
    });
    render(<RunsView visible />);
    const row = await screen.findByTestId("run-row-r1");
    fireEvent.click(row);
    const detail = await screen.findByTestId("run-detail");
    expect(detail.textContent).toMatch(/blocked/i);
    const block = await screen.findByTestId("guardrail-event-0");
    expect(block.className).toMatch(/amber/i);
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<RunsView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && npx vitest run src/components/views/__tests__/runs-view.test.tsx`
Expected: FAIL (cannot resolve `../runs-view`)

- [ ] **Step 3: Implement**

```tsx
// app/src/components/views/runs-view.tsx
"use client";

import { useCallback, useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type Run = { id: string; agent_id?: string; status?: string; total_cost_usd?: number;
  started_at?: string | null; duration_ms?: number | null };
type Step = { step_number: number; step_type?: string; tool_name?: string; error_message?: string | null };
type GEvent = { guardrail_name?: string; action?: string; tool_name?: string; reason?: string };
type Detail = { run: Run; steps: Step[]; guardrail_events: GEvent[] };

export function RunsView({ visible = true }: { visible?: boolean }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/runs`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setRuns(await res.json());
      setError(null);
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

  const openRun = useCallback(async (id: string) => {
    const res = await fetch(`${BRIDGE_URL}/api/runs/${id}`);
    if (res.ok) setDetail(await res.json());
  }, []);

  useEffect(() => {
    if (visible) fetchRuns();
  }, [visible, fetchRuns]);

  return (
    <div data-testid="runs-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Runs</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      <div className="flex gap-4">
        <div className="flex flex-col gap-1 min-w-[16rem]">
          {runs.map((r) => (
            <button key={r.id} data-testid={`run-row-${r.id}`} onClick={() => openRun(r.id)}
              className="rounded border border-zinc-700 bg-zinc-900 p-2 text-left text-sm text-zinc-200 hover:border-zinc-500">
              <span className="font-medium">{r.agent_id}</span>{" "}
              <span className="text-zinc-400">{r.status}</span>
              {typeof r.total_cost_usd === "number" && (
                <span className="text-zinc-500"> · ${r.total_cost_usd.toFixed(4)}</span>
              )}
            </button>
          ))}
        </div>
        {detail && (
          <div data-testid="run-detail" className="flex-1 rounded border border-zinc-700 bg-zinc-900 p-3 text-sm">
            <div className="font-medium text-zinc-100">
              {detail.run.agent_id} — {detail.run.status}
            </div>
            <ol className="mt-2 list-decimal pl-5 text-zinc-300">
              {detail.steps.map((s) => (
                <li key={s.step_number}>{s.step_type}{s.tool_name ? `: ${s.tool_name}` : ""}</li>
              ))}
            </ol>
            <div className="mt-2 flex flex-col gap-1">
              {detail.guardrail_events.map((e, i) => (
                <div key={i} data-testid={`guardrail-event-${i}`}
                  className={`rounded px-2 py-1 text-xs ${
                    e.action === "blocked"
                      ? "bg-amber-500/10 text-amber-300"
                      : "bg-zinc-800 text-zinc-300"
                  }`}>
                  {e.guardrail_name} {e.action} {e.tool_name}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default RunsView;
```

- [ ] **Step 4: Register the tab** — same three files: `ViewId` += `"runs"`, navItem `{ id: "runs", icon: <Activity className="h-5 w-5" />, label: "Runs" }` (import `Activity`), `viewTitles.runs = "Runs"`, render `<RunsView visible={sidebarView === "runs"} />`, add to `MobileViewId`/tab list.

- [ ] **Step 5: Run test + typecheck**

Run: `cd app && npx vitest run src/components/views/__tests__/runs-view.test.tsx && npx tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 6: Commit**

```bash
git add app/src/components/views/runs-view.tsx app/src/components/views/__tests__/runs-view.test.tsx app/src/components/layout/sidebar.tsx app/src/components/layout/app-shell.tsx app/src/components/layout/mobile-tab-bar.tsx
git commit -m "feat(app): Runs tab — per-run steps, guardrail blocks, cost"
```

---

### Task 8: Workflows tab (frontend)

**Files:**
- Create: `app/src/components/views/workflows-view.tsx`, `__tests__/workflows-view.test.tsx`
- Modify: `sidebar.tsx`, `app-shell.tsx`, `mobile-tab-bar.tsx`

**Interfaces:**
- Consumes: `GET /api/workflows`, `GET /api/workflows/{id}/runs`.
- Produces: `WorkflowsView({ visible })`. Root `data-testid="workflows-view"`. Each workflow row `data-testid={\`workflow-row-${workflow_id}\`}`. Includes a footer note stating the tab shows workflows that have run at least once (the honest limitation from the architecture decision).

- [ ] **Step 1: Write the failing test**

```tsx
// app/src/components/views/__tests__/workflows-view.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { WorkflowsView } from "../workflows-view";

afterEach(() => vi.restoreAllMocks());

describe("WorkflowsView", () => {
  it("lists workflows and states the run-history limitation", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true, json: async () => [
        { workflow_id: "intel", runs: 3, last_status: "completed", failures: 0 },
      ],
    } as Response);
    render(<WorkflowsView visible />);
    expect(await screen.findByTestId("workflow-row-intel")).toBeTruthy();
    expect(screen.getByTestId("workflows-view").textContent).toMatch(/run at least once/i);
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<WorkflowsView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && npx vitest run src/components/views/__tests__/workflows-view.test.tsx`
Expected: FAIL (cannot resolve)

- [ ] **Step 3: Implement**

```tsx
// app/src/components/views/workflows-view.tsx
"use client";

import { useCallback, useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type Workflow = { workflow_id: string; runs?: number; last_run_at?: string | null;
  last_status?: string | null; failures?: number };

export function WorkflowsView({ visible = true }: { visible?: boolean }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const fetchWorkflows = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/workflows`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setWorkflows(await res.json());
      setError(null);
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

  useEffect(() => {
    if (visible) fetchWorkflows();
  }, [visible, fetchWorkflows]);

  return (
    <div data-testid="workflows-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Workflows</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      <div className="flex flex-col gap-2">
        {workflows.map((w) => {
          const failed = (w.failures ?? 0) > 0;
          return (
            <div key={w.workflow_id} data-testid={`workflow-row-${w.workflow_id}`}
              className={`rounded-md border p-3 ${
                failed ? "border-amber-500/50 bg-amber-500/5" : "border-zinc-700 bg-zinc-900"
              }`}>
              <div className="flex items-center justify-between">
                <span className="font-medium text-zinc-100">{w.workflow_id}</span>
                <span className="text-xs text-zinc-400">{w.last_status ?? "—"}</span>
              </div>
              <div className="mt-1 text-xs text-zinc-400">
                {w.runs ?? 0} run(s), {w.failures ?? 0} failed
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-zinc-500">
        Shows workflows that have run at least once. A defined-but-never-run workflow
        will not appear here.
      </p>
    </div>
  );
}

export default WorkflowsView;
```

- [ ] **Step 4: Register the tab** — `ViewId` += `"workflows"`, navItem `{ id: "workflows", icon: <Workflow className="h-5 w-5" />, label: "Workflows" }` (import `Workflow`), `viewTitles.workflows = "Workflows"`, render `<WorkflowsView visible={sidebarView === "workflows"} />`, add to mobile tab list.

- [ ] **Step 5: Run test + typecheck**

Run: `cd app && npx vitest run src/components/views/__tests__/workflows-view.test.tsx && npx tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 6: Commit**

```bash
git add app/src/components/views/workflows-view.tsx app/src/components/views/__tests__/workflows-view.test.tsx app/src/components/layout/sidebar.tsx app/src/components/layout/app-shell.tsx app/src/components/layout/mobile-tab-bar.tsx
git commit -m "feat(app): Workflows tab — run history from workflow_runs"
```

---

### Task 9: Health tab (frontend)

**Files:**
- Create: `app/src/components/views/health-view.tsx`, `__tests__/health-view.test.tsx`
- Modify: `sidebar.tsx`, `app-shell.tsx`, `mobile-tab-bar.tsx`

**Interfaces:**
- Consumes: `GET /api/health/system`.
- Produces: `HealthView({ visible })`. Root `data-testid="health-view"`. Sections carry `data-testid="health-wal" | "health-backups" | "health-units" | "health-disks"`. A `warn`/`unknown` status or a non-empty failed-units list renders amber; `ok` renders emerald. A backup section that is a dict with `status: "unknown"` renders amber (probe failed — never green).

- [ ] **Step 1: Write the failing test**

```tsx
// app/src/components/views/__tests__/health-view.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { HealthView } from "../health-view";

afterEach(() => vi.restoreAllMocks());

describe("HealthView", () => {
  it("renders sections; a failed unit and a stale backup are amber, never green", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true, json: async () => ({
        wal: { archived_count: 10, failed_count: 0, last_archived_time: "x", status: "ok" },
        backups: [{ unit: "robothor-backup-local.timer", age_hours: 40, status: "warn" }],
        failed_units: ["robothor-foo.service"],
        disks: [{ mount: "/", used_pct: 40, free_gb: 100, status: "ok" }],
        generated_at: "x",
      }),
    } as Response);
    render(<HealthView visible />);
    const units = await screen.findByTestId("health-units");
    expect(units.className).toMatch(/amber/i);          // a failed unit is amber
    const backups = screen.getByTestId("health-backups");
    expect(backups.className).toMatch(/amber/i);        // stale backup is amber
    expect(backups.className).not.toMatch(/emerald|green/i);
    const wal = screen.getByTestId("health-wal");
    expect(wal.className).toMatch(/emerald/i);          // ok is green
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    render(<HealthView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd app && npx vitest run src/components/views/__tests__/health-view.test.tsx`
Expected: FAIL (cannot resolve)

- [ ] **Step 3: Implement**

```tsx
// app/src/components/views/health-view.tsx
"use client";

import { useCallback, useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type WalStatus = { archived_count?: number; failed_count?: number;
  last_archived_time?: string | null; status?: string; error?: string };
type Backup = { unit: string; age_hours?: number | null; status?: string };
type Disk = { mount: string; used_pct?: number | null; free_gb?: number | null; status?: string };
type Health = {
  wal?: WalStatus;
  backups?: Backup[] | { status: string; error?: string };
  failed_units?: string[] | { status: string; error?: string };
  disks?: Disk[] | { status: string; error?: string };
  generated_at?: string;
};

// emerald only for a genuinely-good status; everything else (warn/unknown/failed) amber.
function toneFor(good: boolean): string {
  return good
    ? "border-emerald-500/50 bg-emerald-500/5"
    : "border-amber-500/50 bg-amber-500/5";
}

export function HealthView({ visible = true }: { visible?: boolean }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchHealth = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/health/system`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setHealth(await res.json());
      setError(null);
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

  useEffect(() => {
    if (visible) fetchHealth();
  }, [visible, fetchHealth]);

  const wal = health?.wal;
  const walGood = wal?.status === "ok";
  const backups = health?.backups;
  const backupsArr = Array.isArray(backups) ? backups : [];
  const backupsGood = Array.isArray(backups) && backups.every((b) => b.status === "ok");
  const units = health?.failed_units;
  const unitsArr = Array.isArray(units) ? units : [];
  const unitsGood = Array.isArray(units) && units.length === 0;
  const disks = health?.disks;
  const disksArr = Array.isArray(disks) ? disks : [];
  const disksGood = Array.isArray(disks) && disks.every((d) => d.status === "ok");

  return (
    <div data-testid="health-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Health</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      {health && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div data-testid="health-wal" className={`rounded-md border p-3 ${toneFor(walGood)}`}>
            <div className="font-medium text-zinc-100">WAL archiving</div>
            <div className="text-xs text-zinc-400">
              archived {wal?.archived_count ?? "—"}, failed {wal?.failed_count ?? "—"},
              last {wal?.last_archived_time ?? "never"}
            </div>
          </div>

          <div data-testid="health-backups" className={`rounded-md border p-3 ${toneFor(backupsGood)}`}>
            <div className="font-medium text-zinc-100">Backups</div>
            {backupsArr.length === 0 && (
              <div className="text-xs text-amber-300">probe unknown</div>
            )}
            {backupsArr.map((b) => (
              <div key={b.unit} className="text-xs text-zinc-400">
                {b.unit}: {b.age_hours == null ? "never" : `${b.age_hours.toFixed(1)}h ago`} ({b.status})
              </div>
            ))}
          </div>

          <div data-testid="health-units" className={`rounded-md border p-3 ${toneFor(unitsGood)}`}>
            <div className="font-medium text-zinc-100">Failed units</div>
            <div className="text-xs text-zinc-400">
              {unitsArr.length === 0 ? "none" : unitsArr.join(", ")}
            </div>
          </div>

          <div data-testid="health-disks" className={`rounded-md border p-3 ${toneFor(disksGood)}`}>
            <div className="font-medium text-zinc-100">Disks</div>
            {disksArr.map((d) => (
              <div key={d.mount} className="text-xs text-zinc-400">
                {d.mount}: {d.used_pct ?? "—"}% used, {d.free_gb ?? "—"} GB free ({d.status})
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default HealthView;
```

- [ ] **Step 4: Register the tab** — `ViewId` += `"health"`, navItem `{ id: "health", icon: <HeartPulse className="h-5 w-5" />, label: "Health" }` (import `HeartPulse`), `viewTitles.health = "Health"`, render `<HealthView visible={sidebarView === "health"} />`, add to mobile tab list.

- [ ] **Step 5: Run test + typecheck + the full app test suite** (registration touched shared files)

Run: `cd app && npx vitest run src/components/views/__tests__/health-view.test.tsx && npx vitest run && npx tsc --noEmit`
Expected: health test PASS; whole app suite green (proves the four registrations didn't break AppShell); tsc clean.

- [ ] **Step 6: Commit**

```bash
git add app/src/components/views/health-view.tsx app/src/components/views/__tests__/health-view.test.tsx app/src/components/layout/sidebar.tsx app/src/components/layout/app-shell.tsx app/src/components/layout/mobile-tab-bar.tsx
git commit -m "feat(app): Health tab — WAL, backups, failed units, disks with honesty tones"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

1. **Whole-branch review** (most capable model) via superpowers:requesting-code-review — invariants to confirm: every new router is GET-only; every handler calls `require_operator` first; no router uses `_conn()` or raw `psycopg2.connect`; the four frontend tabs never render green for a flagged/unknown/failed state; the Phase 1 controls tests still pass.
2. **Full suites:** `cd crm/bridge && python -m pytest tests/ -v` (no-DB lane), the integration lane against `robothor_test`, and `cd app && npx pnpm@10 install --frozen-lockfile && npx vitest run && npx tsc --noEmit`. Confirm nothing in today's hardening regressed (RLS still scopes; `RLS IS INERT` stays silent).
3. **finishing-a-development-branch** — open the PR (Conventional Commit title, e.g. `feat(helm): operator accounting tabs — fleet, runs, workflows, health`), let CI run, merge, deploy (bridge restart + `npx pnpm@10 build` + `robothor-app` restart), and live-smoke each endpoint with an operator token (`/api/fleet`, `/api/runs`, `/api/workflows`, `/api/health/system` → 200 as operator, 403 as service).
</content>
</invoke>

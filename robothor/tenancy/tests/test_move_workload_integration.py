"""``move_workload`` against real Postgres: a workload's history changes tenant.

A workload is a set of agents and workflows. Moving it to another tenant's
engine has to carry its history along — runs and their steps, schedules,
workflow runs, chat sessions — in one transaction, refusing anything it cannot
move cleanly. A mock cannot show any of it: the guarantees are the database's
(one transaction, unique keys, foreign keys, the catalog itself).

Seeds and cleans up through its own committed connection, under tenant and
agent ids suffixed per test, because ``move_workload`` opens and commits its
own transaction.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from robothor.tenancy import move_workload as mw

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "ROBOTHOR_TEST_DB_DSN",
    "dbname=robothor_test user=robothor host=/var/run/postgresql",
)


def _connect() -> Any:
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(PG_DSN)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no test database reachable at ROBOTHOR_TEST_DB_DSN: {exc}")
    if not conn.get_dsn_parameters().get("dbname", "").endswith("_test"):
        pytest.skip("ROBOTHOR_TEST_DB_DSN must name a *_test database")
    return conn


def _run(cur: Any, tenant: str, agent: str, **cols: Any) -> str:
    fields = {"tenant_id": tenant, "agent_id": agent, "trigger_type": "manual", **cols}
    names = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    cur.execute(
        f"INSERT INTO agent_runs ({names}) VALUES ({marks}) RETURNING id",  # noqa: S608
        tuple(fields.values()),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def world() -> Iterator[dict[str, Any]]:
    """acme-parent (root) with children acme-child-a and acme-child-b, and a
    second root acme-other. The parent holds the workload (agent ``tri_…``,
    workflow ``intake-…``) and a neighbour's history (agent ``bob-…``) that
    must stay put. Everything is removed afterwards."""
    sfx = uuid.uuid4().hex[:8]
    parent, child_a, child_b, other = (
        f"acme-parent-{sfx}",
        f"acme-child-a-{sfx}",
        f"acme-child-b-{sfx}",
        f"acme-other-{sfx}",
    )
    # An underscore is a LIKE wildcard: a key match built on LIKE would also
    # take ``triX…``'s sessions.
    agent, lookalike, neighbour = f"tri_{sfx}", f"triX{sfx}", f"bob-{sfx}"
    helper, stepper = f"helper-{sfx}", f"stepper-{sfx}"
    workflow, other_workflow = f"intake-{sfx}", f"billing-{sfx}"
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Acme Parent')", (parent,))
    cur.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Acme Other')", (other,))
    for tid, name in ((child_a, "Acme Child A"), (child_b, "Acme Child B")):
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, %s, %s)",
            (tid, name, parent),
        )
    cur.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name) "
        "VALUES (%s, 'Alice', 'Example') RETURNING id",
        (parent,),
    )
    person = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO crm_tasks (tenant_id, title) VALUES (%s, 'Follow up') RETURNING id", (parent,)
    )
    task = str(cur.fetchone()[0])

    # The workload's agent runs: one linked to a source-tenant contact and task,
    # its sub-agent run, a run spawned under the neighbour's run, and the run a
    # workflow step started for an agent nobody named.
    run_a1 = _run(cur, parent, agent, status="completed", person_id=person, task_id=task)
    run_child = _run(cur, parent, helper, status="completed", parent_run_id=run_a1)
    run_b = _run(cur, parent, neighbour, status="completed")
    run_a2 = _run(cur, parent, agent, status="completed", parent_run_id=run_b)
    run_step = _run(cur, parent, stepper, status="completed", trigger_type="workflow")
    for n in (1, 2):
        cur.execute(
            "INSERT INTO agent_run_steps (run_id, step_number, step_type) VALUES (%s, %s, 'llm_call')",
            (run_a1, n),
        )
    cur.execute(
        "INSERT INTO agent_runtime_controls (tenant_id, run_id, action) VALUES (%s, %s, 'pause')",
        (parent, run_a1),
    )
    for a, last in ((agent, run_a1), (neighbour, run_b)):
        cur.execute(
            "INSERT INTO agent_schedules (agent_id, tenant_id, cron_expr, last_run_id) "
            "VALUES (%s, %s, '0 * * * *', %s)",
            (a, parent, last),
        )

    cur.execute(
        "INSERT INTO workflow_runs (tenant_id, workflow_id, status) "
        "VALUES (%s, %s, 'completed') RETURNING id",
        (parent, workflow),
    )
    wf_run = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO workflow_runs (tenant_id, workflow_id, status) "
        "VALUES (%s, %s, 'completed') RETURNING id",
        (parent, other_workflow),
    )
    other_wf_run = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO workflow_run_steps (run_id, step_id, step_type, agent_id, agent_run_id) "
        "VALUES (%s, 'triage', 'agent', %s, %s)",
        (wf_run, stepper, run_step),
    )
    cur.execute(
        "INSERT INTO workflow_approvals (tenant_id, run_id, workflow_id, step_id, prompt, "
        "expires_at) VALUES (%s, %s, %s, 'approve', 'OK?', NOW() + interval '1 day')",
        (parent, wf_run, workflow),
    )

    sessions: dict[str, str] = {}
    for key in (
        f"agent:{agent}:primary",
        f"agent:{agent}:user:u1",
        f"agent:{lookalike}:primary",
        f"agent:{neighbour}:primary",
    ):
        cur.execute(
            "INSERT INTO chat_sessions (tenant_id, session_key) VALUES (%s, %s) RETURNING id",
            (parent, key),
        )
        sessions[key] = str(cur.fetchone()[0])
    for n in range(3):
        cur.execute(
            "INSERT INTO chat_messages (session_id, message) VALUES (%s, %s)",
            (sessions[f"agent:{agent}:primary"], f'{{"n": {n}}}'),
        )
    cur.execute(
        "INSERT INTO chat_approval_receipts (tenant_id, request_id, session_key, plan_id, "
        "plan_state) VALUES (%s, %s, %s, 'p1', '{}')",
        (parent, f"req-{sfx}", f"agent:{agent}:primary"),
    )
    # An audit row about the agent: the trail stays where it was written.
    cur.execute(
        "INSERT INTO audit_log (event_type, category, actor, action, target, tenant_id) "
        "VALUES ('agent.run', 'agent', %s, 'ran', %s, %s)",
        (agent, f"agent:{agent}", parent),
    )
    try:
        yield {
            "sfx": sfx,
            "parent": parent,
            "child_a": child_a,
            "child_b": child_b,
            "other": other,
            "agent": agent,
            "neighbour": neighbour,
            "workflow": workflow,
            "runs": {
                "a1": run_a1,
                "child": run_child,
                "b": run_b,
                "a2": run_a2,
                "step": run_step,
            },
            "wf_run": wf_run,
            "other_wf_run": other_wf_run,
            "sessions": sessions,
            "person": person,
            "task": task,
            "cur": cur,
        }
    finally:
        tenants = (parent, child_a, child_b, other)
        cur.execute("DELETE FROM audit_log WHERE tenant_id IN %s", (tenants,))
        cur.execute(
            "DELETE FROM audit_log WHERE event_type = 'tenant.workload_moved' "
            "AND details->>'from_tenant' IN %s",
            (tenants,),
        )
        for table in (
            "pursuit_goal_attempts",
            "pursuit_goals",
            "chat_approval_receipts",
            "chat_sessions",
            "workflow_approvals",
            "workflow_runs",
            "agent_schedules",
            "agent_runtime_controls",
        ):
            cur.execute(f"DELETE FROM {table} WHERE tenant_id IN %s", (tenants,))  # noqa: S608
        cur.execute(
            "DELETE FROM agent_runs WHERE tenant_id IN %s AND parent_run_id IS NOT NULL",
            (tenants,),
        )
        for table in ("agent_runs", "crm_tasks", "crm_people"):
            cur.execute(f"DELETE FROM {table} WHERE tenant_id IN %s", (tenants,))  # noqa: S608
        cur.execute("DELETE FROM crm_tenants WHERE parent_tenant_id = %s", (parent,))
        cur.execute("DELETE FROM crm_tenants WHERE id IN %s", (tenants,))
        conn.close()


def _move(world: dict[str, Any], to: str | None = None, **kw: Any) -> mw.WorkloadMoveReport:
    kw.setdefault("agents", [world["agent"]])
    kw.setdefault("workflows", [world["workflow"]])
    kw.setdefault("from_tenant", world["parent"])
    return mw.move_workload(to_tenant=to or world["child_a"], actor="cli:test", **kw)


def _tenant_of(cur: Any, table: str, column: str, value: str) -> list[str]:
    cur.execute(
        f"SELECT tenant_id FROM {table} WHERE {column}::text = %s ORDER BY 1",  # noqa: S608
        (value,),
    )
    return [r[0] for r in cur.fetchall()]


def _audit(cur: Any, world: dict[str, Any]) -> list[tuple[Any, ...]]:
    cur.execute(
        "SELECT actor, details, tenant_id FROM audit_log "
        "WHERE event_type = 'tenant.workload_moved' AND details->>'from_tenant' = %s",
        (world["parent"],),
    )
    return cur.fetchall()


#: What the world's workload is, table by table (only the non-zero counts).
EXPECTED_MOVED = {
    "agent_runs": 4,  # a1, its sub-agent run, a2, and the workflow step's run
    "agent_runtime_controls": 1,
    "agent_schedules": 1,
    "workflow_runs": 1,
    "workflow_approvals": 1,
    "chat_sessions": 2,
    "chat_approval_receipts": 1,
}
EXPECTED_FOLLOWED = {"agent_run_steps": 2, "workflow_run_steps": 1, "chat_messages": 3}
EXPECTED_CLEARED = {
    "agent_runs.person_id": 1,
    "agent_runs.task_id": 1,
    "agent_runs.parent_run_id": 1,  # a2's parent is the neighbour's run
}


def _nonzero(counts: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in counts.items() if v}


# ─── the move ────────────────────────────────────────────────────────────


def test_live_move_moves_the_workloads_history_and_nothing_else(world) -> None:
    cur, runs, child = world["cur"], world["runs"], world["child_a"]
    report = _move(world)

    assert _nonzero(report.counts) == EXPECTED_MOVED
    assert _nonzero(report.followed) == EXPECTED_FOLLOWED
    assert report.links_cleared == EXPECTED_CLEARED
    assert not report.dry_run

    for key in ("a1", "child", "a2", "step"):
        assert _tenant_of(cur, "agent_runs", "id", runs[key]) == [child], key
    assert _tenant_of(cur, "agent_runs", "id", runs["b"]) == [world["parent"]]
    assert _tenant_of(cur, "agent_runtime_controls", "run_id", runs["a1"]) == [child]
    assert _tenant_of(cur, "agent_schedules", "agent_id", world["agent"]) == [child]
    assert _tenant_of(cur, "agent_schedules", "agent_id", world["neighbour"]) == [world["parent"]]
    assert _tenant_of(cur, "workflow_runs", "id", world["wf_run"]) == [child]
    assert _tenant_of(cur, "workflow_runs", "id", world["other_wf_run"]) == [world["parent"]]
    assert _tenant_of(cur, "workflow_approvals", "run_id", world["wf_run"]) == [child]
    moved_keys = {f"agent:{world['agent']}:primary", f"agent:{world['agent']}:user:u1"}
    for key, sid in world["sessions"].items():
        expected = child if key in moved_keys else world["parent"]
        assert _tenant_of(cur, "chat_sessions", "id", sid) == [expected], key
    assert _tenant_of(
        cur, "chat_approval_receipts", "session_key", f"agent:{world['agent']}:primary"
    ) == [child]

    # Steps, workflow steps and chat messages carry no tenant: they are still
    # attached to the rows that moved.
    cur.execute("SELECT COUNT(*) FROM agent_run_steps WHERE run_id = %s", (runs["a1"],))
    assert cur.fetchone()[0] == 2

    # No moved row points at a row the target tenant cannot see.
    cur.execute(
        "SELECT person_id, task_id FROM agent_runs WHERE id = %s",
        (runs["a1"],),
    )
    assert cur.fetchone() == (None, None)
    cur.execute("SELECT parent_run_id FROM agent_runs WHERE id = %s", (runs["a2"],))
    assert cur.fetchone()[0] is None
    cur.execute("SELECT parent_run_id::text FROM agent_runs WHERE id = %s", (runs["child"],))
    assert cur.fetchone()[0] == runs["a1"]  # both moved: the link holds

    # The audit trail keeps its original tenant.
    assert _tenant_of(cur, "audit_log", "target", f"agent:{world['agent']}") == [world["parent"]]


def test_the_move_is_audited_once_under_the_source_root(world) -> None:
    cur = world["cur"]
    report = _move(world)
    rows = _audit(cur, world)
    assert len(rows) == 1
    actor, details, tenant = rows[0]
    assert actor == "cli:test"
    assert tenant == world["parent"]
    assert details["from_tenant"] == world["parent"]
    assert details["to_tenant"] == world["child_a"]
    assert details["agents"] == [world["agent"]]
    assert details["workflows"] == [world["workflow"]]
    assert _nonzero(details["counts"]) == EXPECTED_MOVED
    assert report.audit_id is not None


def test_a_move_between_siblings_is_audited_under_their_root(world) -> None:
    _move(world)
    report = _move(world, to=world["child_b"], from_tenant=world["child_a"])
    assert _nonzero(report.counts) == EXPECTED_MOVED
    cur = world["cur"]
    cur.execute(
        "SELECT tenant_id FROM audit_log "
        "WHERE event_type = 'tenant.workload_moved' AND details->>'from_tenant' = %s",
        (world["child_a"],),
    )
    assert [r[0] for r in cur.fetchall()] == [world["parent"]]


def test_dry_run_reports_the_same_counts_and_writes_nothing(world) -> None:
    cur, runs = world["cur"], world["runs"]
    report = _move(world, dry_run=True)
    assert report.dry_run
    assert _nonzero(report.counts) == EXPECTED_MOVED
    assert _nonzero(report.followed) == EXPECTED_FOLLOWED
    assert report.links_cleared == EXPECTED_CLEARED
    assert report.audit_id is None
    assert _tenant_of(cur, "agent_runs", "id", runs["a1"]) == [world["parent"]]
    cur.execute("SELECT person_id::text FROM agent_runs WHERE id = %s", (runs["a1"],))
    assert cur.fetchone()[0] == world["person"]
    assert _audit(cur, world) == []


def test_workflows_alone_move_their_runs_and_the_runs_their_steps_started(world) -> None:
    cur, runs = world["cur"], world["runs"]
    report = _move(world, agents=[])
    assert report.counts["workflow_runs"] == 1
    assert report.counts["agent_runs"] == 1
    assert _tenant_of(cur, "agent_runs", "id", runs["step"]) == [world["child_a"]]
    assert _tenant_of(cur, "agent_runs", "id", runs["a1"]) == [world["parent"]]


# ─── refusals: nothing written ───────────────────────────────────────────


def _assert_untouched(world: dict[str, Any]) -> None:
    cur = world["cur"]
    cur.execute(
        "SELECT DISTINCT tenant_id FROM agent_runs WHERE id = ANY(%s::uuid[])",
        (list(world["runs"].values()),),
    )
    assert [r[0] for r in cur.fetchall()] == [world["parent"]]
    assert _tenant_of(cur, "workflow_runs", "id", world["wf_run"]) == [world["parent"]]
    assert _audit(cur, world) == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"from_tenant": "acme-missing"}, "does not exist"),
        ({"to": "acme-missing"}, "does not exist"),
        ({"to": "SAME"}, "same tenant"),
        ({"agents": [], "workflows": []}, "name at least one"),
        ({"to": "OTHER"}, "not under the same root"),
    ],
)
def test_refusals(world, overrides, message) -> None:
    to = overrides.pop("to", None)
    to = {"SAME": world["parent"], "OTHER": world["other"]}.get(to or "", to)
    with pytest.raises(mw.WorkloadMoveError, match=message):
        _move(world, to=to, **overrides)
    _assert_untouched(world)


def test_a_move_up_to_an_ancestor_is_refused(world) -> None:
    _move(world)
    with pytest.raises(mw.WorkloadMoveError, match="ancestor"):
        _move(world, to=world["parent"], from_tenant=world["child_a"])


def test_an_inactive_target_is_refused(world) -> None:
    world["cur"].execute("UPDATE crm_tenants SET active = false WHERE id = %s", (world["child_a"],))
    with pytest.raises(mw.WorkloadMoveError, match="not active"):
        _move(world)
    _assert_untouched(world)


def test_a_key_collision_in_the_target_is_refused_and_named(world) -> None:
    key = f"agent:{world['agent']}:primary"
    world["cur"].execute(
        "INSERT INTO chat_sessions (tenant_id, session_key) VALUES (%s, %s)",
        (world["child_a"], key),
    )
    with pytest.raises(mw.WorkloadMoveError, match="chat_sessions") as exc:
        _move(world)
    assert key in str(exc.value)
    _assert_untouched(world)


def test_a_collision_is_refused_in_a_dry_run_too(world) -> None:
    world["cur"].execute(
        "INSERT INTO chat_sessions (tenant_id, session_key) VALUES (%s, %s)",
        (world["child_a"], f"agent:{world['agent']}:primary"),
    )
    with pytest.raises(mw.WorkloadMoveError, match="chat_sessions"):
        _move(world, dry_run=True)


def test_a_goal_pursuit_on_a_workload_run_is_refused(world) -> None:
    cur, gid = world["cur"], str(uuid.uuid4())
    cur.execute(
        "INSERT INTO pursuit_goals (tenant_id, id, data, status) VALUES (%s, %s, '{}', 'running')",
        (world["parent"], gid),
    )
    cur.execute(
        "INSERT INTO pursuit_goal_attempts (tenant_id, id, goal_id, run_id) VALUES (%s, %s, %s, %s)",
        (world["parent"], str(uuid.uuid4()), gid, world["runs"]["a1"]),
    )
    with pytest.raises(mw.WorkloadMoveError, match="pursuit_goal_attempts"):
        _move(world)
    _assert_untouched(world)


@pytest.mark.parametrize("status", ["running", "pending", "awaiting_approval"])
def test_a_live_run_is_refused(world, status) -> None:
    world["cur"].execute(
        "UPDATE agent_runs SET status = %s WHERE id = %s", (status, world["runs"]["child"])
    )
    with pytest.raises(mw.WorkloadMoveError, match="still"):
        _move(world)
    _assert_untouched(world)


def test_a_live_workflow_run_is_refused(world) -> None:
    world["cur"].execute(
        "UPDATE workflow_runs SET status = 'running' WHERE id = %s", (world["wf_run"],)
    )
    with pytest.raises(mw.WorkloadMoveError, match="still"):
        _move(world)
    _assert_untouched(world)


def test_an_unclassified_tenant_table_refuses_the_move(world, monkeypatch) -> None:
    """A table a migration added after this module was written: the move does
    not guess whether its rows are history."""
    monkeypatch.delitem(mw.UNTOUCHED_TABLES, "crm_notes")
    with pytest.raises(mw.WorkloadMoveError, match="crm_notes"):
        _move(world)
    _assert_untouched(world)


def test_a_failure_part_way_leaves_every_table_untouched(world, monkeypatch) -> None:
    """Every UPDATE has run by the time the audit row is written."""

    def boom(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(mw, "_record_audit", boom)
    with pytest.raises(RuntimeError, match="audit write failed"):
        _move(world)
    _assert_untouched(world)
    cur = world["cur"]
    cur.execute("SELECT person_id::text FROM agent_runs WHERE id = %s", (world["runs"]["a1"],))
    assert cur.fetchone()[0] == world["person"]


# ─── the classification covers the catalog ───────────────────────────────


def _catalog(cur: Any, sql: str) -> list[tuple[Any, ...]]:
    cur.execute(sql)
    return cur.fetchall()


_TENANT_TABLES = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
              AND a.attname = 'tenant_id' AND NOT a.attisdropped)
ORDER BY 1
"""

_FKS = """
SELECT cl.relname, fr.relname, cardinality(c.conkey),
       EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.conrelid
               AND a.attname = 'tenant_id' AND NOT a.attisdropped)
FROM pg_constraint c
JOIN pg_class cl ON cl.oid = c.conrelid
JOIN pg_class fr ON fr.oid = c.confrelid
WHERE c.contype = 'f'
"""


def test_every_tenant_bearing_table_is_classified_exactly_once() -> None:
    """A migration that adds a tenant table reds this until someone decides
    whether a workload's rows in it move, stay, or block the move."""
    cur = _connect().cursor()
    tables = {r[0] for r in _catalog(cur, _TENANT_TABLES)}
    classes = (mw.MOVED_TABLES, mw.UNTOUCHED_TABLES, mw.REFUSED_IF_PRESENT)
    classified = [t for cls in classes for t in cls]
    assert len(classified) == len(set(classified)), "a table is in more than one class"
    unclassified = sorted(tables - set(classified))
    assert not unclassified, (
        f"tenant-bearing table(s) with no workload-move decision: {unclassified}. "
        "Add each to MOVED_TABLES, UNTOUCHED_TABLES or REFUSED_IF_PRESENT in "
        "robothor/tenancy/move_workload.py, with its reason."
    )
    stale = sorted(set(classified) - tables)
    assert not stale, f"classified table(s) no longer in the schema: {stale}"


def test_every_untouched_and_refused_table_says_why() -> None:
    for table, reason in mw.UNTOUCHED_TABLES.items():
        assert len(reason) > 20, table
    for table, (predicate, reason) in mw.REFUSED_IF_PRESENT.items():
        assert predicate and len(reason) > 20, table


def test_every_untenanted_child_of_a_moved_table_is_declared() -> None:
    """Steps and messages carry no tenant_id: they follow their parent by its
    id. A new one must be declared so the report counts it."""
    cur = _connect().cursor()
    children = {
        (child, parent)
        for child, parent, _n, has_tenant in _catalog(cur, _FKS)
        if parent in mw.MOVED_TABLES and not has_tenant
    }
    declared = {(child, parent) for child, (parent, _col) in mw.FOLLOWS_PARENT.items()}
    assert children <= declared, sorted(children - declared)


def test_no_tenant_table_referencing_a_moved_table_is_left_behind() -> None:
    """A row left in the source that references a moved row would point
    across tenants. Every tenant table with a foreign key onto a moved table is
    moved itself or blocks the move."""
    cur = _connect().cursor()
    referrers = {
        child
        for child, parent, _n, has_tenant in _catalog(cur, _FKS)
        if parent in mw.MOVED_TABLES and has_tenant
    }
    allowed = set(mw.MOVED_TABLES) | set(mw.REFUSED_IF_PRESENT)
    assert referrers <= allowed, sorted(referrers - allowed)


def test_no_moved_table_takes_part_in_a_composite_foreign_key() -> None:
    """Moved rows are re-tenanted in place (one UPDATE per table). That is only
    sound while no composite key such as ``(tenant_id, id)`` refers to or from
    a moved table — there is no ON UPDATE CASCADE. A migration that adds one
    must teach the move to copy parent rows before children (and delete them
    after) instead."""
    cur = _connect().cursor()
    composite = sorted(
        (child, parent)
        for child, parent, n, _t in _catalog(cur, _FKS)
        if n > 1 and (child in mw.MOVED_TABLES or parent in mw.MOVED_TABLES)
    )
    assert composite == []


# ─── under RLS, as the application role, from a root shell ───────────────


def test_the_move_works_with_rls_enforcing(db_conn, app_role, rls_tenants) -> None:  # noqa: F811
    """Bound to the platform tenant, a connection sees neither child's rows and
    the WITH CHECK refuses a write into another tenant; the move widens for
    its one transaction."""
    a, b = rls_tenants["a"], rls_tenants["b"]
    agent = f"tri_{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        run = _run(cur, a, agent, status="completed")
        cur.execute(
            "INSERT INTO chat_sessions (tenant_id, session_key) VALUES (%s, %s)",
            (a, f"agent:{agent}:primary"),
        )
    report = mw.move_workload(
        from_tenant=a, to_tenant=b, agents=[agent], workflows=[], actor="cli:test"
    )
    assert report.counts["agent_runs"] == 1
    assert report.counts["chat_sessions"] == 1
    with db_conn.cursor() as cur:
        cur.execute("RESET ROLE")
        assert _tenant_of(cur, "agent_runs", "id", run) == [b]


from tests.rls_app_role import app_role, rls_tenants  # noqa: E402, F401 - pytest fixtures

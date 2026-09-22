"""Real PostgreSQL transaction tests in a private, disposable cluster.

No shared database, Redis namespace, credentials or daemon is used.
"""

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from robothor.goals import compat, store
from robothor.goals.model import DEFAULT_TOKEN_BUDGET, CreateGoal, GoalUpdate


@pytest.fixture(scope="module")
def private_database(tmp_path_factory):
    binary = next(
        (
            p
            for p in (Path("/usr/lib/postgresql/18/bin"), Path("/usr/lib/postgresql/16/bin"))
            if (p / "initdb").exists()
        ),
        None,
    )
    if binary is None:
        found = shutil.which("initdb")
        if found:
            binary = Path(found).parent
        else:
            pytest.skip("PostgreSQL binaries required for disposable-cluster integration tests")
    root = tmp_path_factory.mktemp("goalpg")
    data = root / "data"
    socket = root / "socket"
    socket.mkdir()

    def command(name, *args):
        subprocess.run(
            [str(binary / name), *map(str, args)], check=True, capture_output=True, text=True
        )

    command(
        "initdb", "-D", data, "-U", "goaltest", "--auth=trust", "--no-locale", "--encoding=UTF8"
    )
    command(
        "pg_ctl",
        "-D",
        data,
        "-l",
        root / "postgres.log",
        "-o",
        f"-F -h '' -k {socket}",
        "-w",
        "start",
    )
    try:
        command("createdb", "-h", socket, "-U", "goaltest", "goal_pursuit_test")
        dsn = f"dbname=goal_pursuit_test user=goaltest host={socket}"
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE crm_tenants(id TEXT PRIMARY KEY,display_name TEXT)")
            cur.execute("""CREATE TABLE crm_tasks(id UUID PRIMARY KEY,tenant_id TEXT NOT NULL,title TEXT,body TEXT,
                objective TEXT,status TEXT,resolution TEXT,deleted_at TIMESTAMPTZ,tags TEXT[],session_goal_meta JSONB)""")
            cur.execute("""CREATE TABLE crm_agent_notifications(id UUID,tenant_id TEXT,from_agent TEXT,
                to_agent TEXT,notification_type TEXT,subject TEXT,body TEXT,metadata JSONB)""")
            migrations = Path(__file__).resolve().parents[3] / "crm/migrations"
            for name in (
                "126_goal_pursuit.sql",
                "127_goal_pursuit_cost.sql",
                "128_goal_pursuit_task_release.sql",
                "138_goal_provider_reservations.sql",
                "139_goal_task_family_controls.sql",
            ):
                sql = (migrations / name).read_text()
                cur.execute(sql)
                cur.execute(sql)  # every goal migration is re-entrant
        yield dsn
    finally:
        command("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


@pytest.fixture
def db(private_database, monkeypatch):
    @contextmanager
    def connection():
        conn = psycopg2.connect(private_database)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, "get_connection", connection)
    # The migration probe is cached per process and these tests run alongside
    # thousands of others that hold fake cursors. Start each one from an
    # unprobed state so this suite's result never depends on file order.
    compat.reset_probe()
    tenant = register_tenant(str(uuid4()))
    store.set_enabled(tenant, True, "operator:test")
    return tenant


def register_tenant(name):
    """pursuit_goals.tenant_id is a foreign key, so a test tenant must exist.

    That is the point of the key: a typo'd tenant used to produce a goal whose
    every finish() rolled back on the notification insert and held its lease.
    """
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (name, name),
        )
    return name


def create(tenant, **kwargs):
    return store.create(
        tenant,
        CreateGoal(objective="Deliver report", success_criteria=["Delivered"], **kwargs),
        "operator:test",
    )


def change(tenant, g, action, **kwargs):
    return store.update(
        tenant,
        g["id"],
        GoalUpdate(action=action, version=g["version"], **kwargs),
        "operator:test",
        operator=True,
    )


def test_claim_is_exclusive_and_continuation_is_durable(db):
    g = create(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(store.claim, [db, db]))
    active = [c for c in claims if c]
    assert len(active) == 1
    claimed, attempt = active[0]
    store.finish(db, g["id"], attempt, checkpoint="Report draft saved", tokens=50)
    assert store.get(db, g["id"])["status"] == "queued"
    again = store.claim(db)
    assert again[0]["tokens_used"] == 50
    assert again[0]["last_output"] == "Report draft saved"
    store.finish(db, g["id"], attempt, tokens=999)  # old lease cannot account twice
    assert store.get(db, g["id"])["tokens_used"] == 50


def test_pause_and_disable_stop_live_execution(db):
    g = create(db)
    g, attempt = store.claim(db)
    change(db, g, "pause")
    assert not store.heartbeat(db, g["id"], attempt)
    store.finish(db, g["id"], attempt, tokens=12)
    assert store.claim(db) is None
    g = store.get(db, g["id"])
    change(db, g, "resume")
    store.set_enabled(db, False, "operator:test")
    assert store.claim(db) is None
    assert store.get(db, g["id"])["tokens_used"] == 12


def test_events_wake_once_and_never_cross_tenants(db):
    g = change(
        db,
        create(db, kind="long"),
        "wait",
        note="Waiting for a reply",
        event_type="email.new",
        event_match={"thread_id": "abc"},
    )
    assert store.claim(db) is None
    store.ingest_event("another-tenant", "e1", "email.new", {"thread_id": "abc"})
    store.ingest_event(db, "wrong", "email.new", {"thread_id": "different"})
    assert store.claim(db) is None
    store.ingest_event(db, "e1", "email.new", {"thread_id": "abc"})
    store.ingest_event(db, "e1", "email.new", {"thread_id": "abc"})
    claimed, attempt = store.claim(db)
    assert claimed["id"] == g["id"]
    assert store.claim(db) is None
    assert len([h for h in store.get(db, g["id"])["history"] if h["action"] == "wake"]) == 1


def test_task_changes_commit_with_wake_and_tenant_link_is_checked(db):
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Report','TODO')",
            (task_id, db),
        )
    g = change(db, create(db, kind="long"), "wait", note="Waiting for task", task_id=task_id)
    assert store.claim(db) is None
    with store.transaction() as cur:
        cur.execute("UPDATE crm_tasks SET status='DONE' WHERE id=%s", (task_id,))
    assert store.claim(db)[0]["id"] == g["id"]
    foreign = register_tenant("another-tenant")
    other = create(foreign)
    with pytest.raises(ValueError, match="task not found"):
        change(foreign, other, "link_task", task_id=task_id)


def test_expired_lease_requires_reconciliation(db):
    g = create(db)
    _, attempt = store.claim(db)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET lease_until=now()-interval '1 minute' WHERE tenant_id=%s",
            (db,),
        )
    recovered, new_attempt = store.claim(db)
    assert recovered["recovery_required"]
    assert attempt != new_attempt
    assert store.get(db, g["id"])["runs"][1]["status"] == "interrupted"


def test_parent_reassesses_and_pause_cascades(db):
    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    parent = change(db, parent, "wait", note="Child is working")
    child = change(
        db, child, "evidence", criterion=0, reference="report:1", satisfied=True, note="Verified"
    )
    child = change(db, child, "complete", note="Delivered")
    assert child["status"] == "review"
    change(db, child, "approve")
    assert store.get(db, parent["id"])["status"] == "queued"
    child2 = create(db, parent_goal_id=parent["id"], request_key="second-child")
    parent = store.get(db, parent["id"])
    change(db, parent, "pause")
    assert store.get(db, child2["id"])["status"] == "paused"
    assert store.get(db, child["id"])["status"] == "complete"


def test_budget_blocks_and_ready_goals_rotate(db):
    first = create(db, token_budget=10)
    second = create(db)
    g, a = store.claim(db)
    assert g["id"] == first["id"]
    store.finish(db, g["id"], a, tokens=10)
    assert store.get(db, first["id"])["status"] == "blocked"
    assert store.claim(db)[0]["id"] == second["id"]


def test_cost_attempt_and_deadline_ceilings_block_at_claim(db):
    from robothor.goals.model import DEFAULT_MAX_ATTEMPTS

    cost = create(db, request_key="cost", cost_budget_usd=0.5)
    g, attempt = store.claim(db)
    store.finish(db, g["id"], attempt, cost=0.5)
    assert "cost" in store.get(db, cost["id"])["blocker"]

    attempts = create(db, request_key="attempts", max_attempts=2)
    for _ in range(2):
        g, attempt = store.claim(db)
        assert g["id"] == attempts["id"]
        store.finish(db, g["id"], attempt)
    assert store.claim(db) is None
    stopped = store.get(db, attempts["id"])
    assert stopped["status"] == "blocked" and "attempt" in stopped["blocker"]
    assert stopped["attempts"] <= DEFAULT_MAX_ATTEMPTS

    overdue = create(db, request_key="deadline")
    with store.transaction() as cur:
        overdue["deadline_at"] = "2000-01-01T00:00:00+00:00"
        store.save(cur, db, overdue)
    assert store.claim(db) is None
    assert "deadline" in store.get(db, overdue["id"])["blocker"]


def test_goals_over_a_ceiling_do_not_each_cost_a_paced_turn(db):
    """One claim retires every over-limit goal ahead of the runnable one.

    Runs are paced at one per MIN_RUN_INTERVAL_SECONDS, so returning None per
    blocked goal would leave a tenant with a handful of exhausted goals unable
    to start its healthy one for minutes.
    """
    spent = [create(db, request_key=f"spent-{i}") for i in range(3)]
    with store.transaction() as cur:
        for g in spent:
            g["deadline_at"] = "2000-01-01T00:00:00+00:00"
            store.save(cur, db, g)
    healthy = create(db, request_key="healthy")
    claimed, _ = store.claim(db)
    assert claimed["id"] == healthy["id"]
    assert all(store.get(db, g["id"])["status"] == "blocked" for g in spent)


def test_an_unknown_tenant_cannot_own_a_goal(db):
    """Without the foreign key, a typo'd tenant produced a goal whose every
    finish() rolled back on the notification insert and held its lease."""
    import psycopg2

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        create("tenant-that-was-never-created")


def test_a_failed_notification_cannot_strand_the_lease(db):
    first = create(db, request_key="notified")
    second = create(db, request_key="next")
    g, attempt = store.claim(db)
    assert g["id"] == first["id"]
    with store.transaction() as cur:
        cur.execute(
            "ALTER TABLE crm_agent_notifications ADD CONSTRAINT refuse CHECK (false) NOT VALID"
        )
    try:
        store.finish(db, first["id"], attempt, tokens=DEFAULT_TOKEN_BUDGET)
    finally:
        with store.transaction() as cur:
            cur.execute("ALTER TABLE crm_agent_notifications DROP CONSTRAINT refuse")
    assert store.get(db, first["id"])["status"] == "blocked"
    # The lease was released despite the undelivered notification, so the
    # tenant's next goal can run.
    assert store.claim(db)[0]["id"] == second["id"]


def test_stale_update_and_tenant_reads(db):
    g = create(db)
    change(db, g, "pause")
    with pytest.raises(ValueError, match="stale"):
        change(db, g, "cancel")
    with pytest.raises(ValueError, match="not found"):
        store.get("another-tenant", g["id"])


def test_child_creation_and_legacy_adoption_are_idempotent(db):
    parent = create(db, kind="long", mode="ongoing")
    first = create(db, parent_goal_id=parent["id"])
    assert create(db, parent_goal_id=parent["id"])["id"] == first["id"]
    # A new ongoing assessment period may create the same work again.
    with store.transaction() as cur:
        parent["assessment"] = {"at": "2030-01-01T00:00:00Z", "status": "missing"}
        store.save(cur, db, parent)
    assert create(db, parent_goal_id=parent["id"])["id"] != first["id"]
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            """INSERT INTO crm_tasks(id,tenant_id,title,status,tags,session_goal_meta)
                       VALUES (%s,%s,'Legacy report','TODO',ARRAY['session_goal'],
                               '{"success_criteria":["Delivered"],"evidence":[{"summary":"old receipt"}]}')""",
            (task_id, db),
        )
    adopted = store.adopt(db, task_id, "operator")
    assert store.adopt(db, task_id, "operator")["id"] == adopted["id"]
    assert adopted["legacy_evidence"] and not adopted["evidence"]


def test_recovered_usage_is_not_billed_twice(db):
    g = create(db, token_budget=100)
    g, attempt = store.claim(db)
    store.heartbeat(db, g["id"], attempt, tokens=30, cost=0.01)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET lease_until=now()-interval '1 minute' WHERE tenant_id=%s",
            (db,),
        )
    recovered, next_attempt = store.claim(db)
    assert recovered["tokens_used"] == 30
    store.finish(db, g["id"], attempt, tokens=30, cost=0.01)
    store.finish(db, g["id"], next_attempt, tokens=20, cost=0.02)
    assert store.get(db, g["id"])["tokens_used"] == 50


def test_parent_pause_resume_restores_child_and_task_dispatch(db):
    from robothor.goals.compat import task_gate
    from robothor.goals.runtime import task_runnable

    # The migration-optional fallback must not become permanent: with 126
    # applied, the gate is the predicate, not the constant TRUE.
    with store.transaction() as cur:
        assert "pursuit_task_runnable" in task_gate(cur, "crm_tasks.id", "crm_tasks.tenant_id")

    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Work','TODO')",
            (task_id, db),
        )
    change(db, child, "link_task", task_id=task_id)
    assert task_runnable(task_id, db)
    parent = change(db, parent, "pause")
    assert not task_runnable(task_id, db)
    change(db, parent, "resume")
    assert store.get(db, child["id"])["status"] == "queued"
    assert task_runnable(task_id, db)


def link_a_task(db, goal, title="Work"):
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,%s,'TODO')",
            (task_id, db, title),
        )
    change(db, goal, "link_task", task_id=task_id)
    return task_id


def test_a_finished_goal_releases_its_tasks_to_the_inbox(db):
    """A goal that COMPLETES must not strand its linked tasks forever.

    `pursuit_task_runnable` listed 'complete' and 'canceled' next to
    paused/blocked/review, and nothing anywhere unlinks a task, so a long-term
    goal that linked twenty tasks and then finished removed all twenty from
    `list_agent_tasks` and `list_threads` permanently — while the tasks were
    still TODO and still assigned, invisible to every agent, recoverable only
    by a manual DELETE against production.
    """
    from robothor.goals.runtime import task_runnable

    goal = create(db, kind="long")
    task_id = link_a_task(db, goal)
    assert task_runnable(task_id, db)

    goal = change(db, store.get(db, goal["id"]), "pause")
    assert not task_runnable(task_id, db), "a paused goal does hold its work"
    change(db, goal, "resume")
    assert task_runnable(task_id, db)

    # Disabling the tenant switch is documented as the runtime rollback. A
    # rollback that hides ordinary CRM tasks is not a rollback.
    store.set_enabled(db, False, "operator:test")
    assert task_runnable(task_id, db)
    store.set_enabled(db, True, "operator:test")

    goal = change(
        db,
        store.get(db, goal["id"]),
        "evidence",
        criterion=0,
        reference="artifact:report",
        satisfied=True,
        note="Verified",
    )
    goal = change(db, goal, "complete", note="Delivered")
    assert goal["status"] == "review"
    assert not task_runnable(task_id, db), "work remains held during review"
    assert change(db, goal, "approve")["status"] == "complete"
    assert task_runnable(task_id, db), "a finished goal owns nothing"


def test_a_canceled_goal_also_releases_its_tasks(db):
    from robothor.goals.runtime import task_runnable

    goal = create(db, kind="long")
    task_id = link_a_task(db, goal)
    assert change(db, store.get(db, goal["id"]), "cancel")["status"] == "canceled"
    assert task_runnable(task_id, db)


def test_an_operator_can_unlink_a_task_from_a_live_goal(db):
    """The only way out of the link used to be a manual DELETE in production."""
    from robothor.goals.runtime import task_runnable

    goal = create(db, kind="long")
    task_id = link_a_task(db, goal)
    assert [str(t["id"]) for t in store.get(db, goal["id"])["tasks"]] == [task_id]
    goal = change(db, store.get(db, goal["id"]), "pause")
    assert not task_runnable(task_id, db)
    change(db, goal, "unlink_task", task_id=task_id)
    assert store.get(db, goal["id"])["tasks"] == []
    assert task_runnable(task_id, db)


def test_parent_budget_accounts_for_execution_children(db):
    parent = create(db, kind="long", token_budget=100)
    child = create(db, parent_goal_id=parent["id"])
    change(db, parent, "wait", note="Child executing")
    g, attempt = store.claim(db)
    assert g["id"] == child["id"]
    store.finish(db, g["id"], attempt, tokens=50, cost=0.01)
    assert store.get(db, parent["id"])["tokens_used"] == 50


def test_rls_policies_enforce_tenant_scope(db):
    g = create(db)
    other = create(register_tenant("other-rls-tenant"))
    with store.transaction() as cur:
        cur.execute("CREATE ROLE goal_reader")
        cur.execute("GRANT SELECT ON pursuit_goals TO goal_reader")
        cur.execute("SET LOCAL ROLE goal_reader")
        cur.execute("SELECT set_config('app.tenant_id',%s,true)", (db,))
        cur.execute("SELECT id::text FROM pursuit_goals")
        ids = [r["id"] for r in cur.fetchall()]
        assert g["id"] in ids and other["id"] not in ids


def test_task_creation_retry_reuses_linked_task(db):
    from robothor.goals.runtime import Binding, binding, link_created_task, prepare_task

    goal = create(db)
    task_id = str(uuid4())
    token = binding.set(Binding(db, goal["id"], str(uuid4())))
    try:
        with store.transaction() as cur:
            cur.execute("ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS body TEXT")
            cur.execute("ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS assigned_to_agent TEXT")
            assert prepare_task(cur, db, "Report", "Draft", "writer") is None
            cur.execute(
                """INSERT INTO crm_tasks(id,tenant_id,title,body,assigned_to_agent,status)
                           VALUES (%s,%s,'Report','Draft','writer','TODO')""",
                (task_id, db),
            )
            link_created_task(cur, db, task_id)
        with store.transaction() as cur:
            assert prepare_task(cur, db, "Report", "Draft", "writer") == task_id
            assert prepare_task(cur, db, "Different report", "Draft", "writer") is None
    finally:
        binding.reset(token)

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

from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate


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

    command("initdb", "-D", data, "-U", "goaltest", "--auth=trust", "--no-locale", "-E", "UTF8")
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
            cur.execute("""CREATE TABLE crm_tasks(id UUID PRIMARY KEY,tenant_id TEXT NOT NULL,title TEXT,
                body TEXT,objective TEXT,status TEXT,resolution TEXT,deleted_at TIMESTAMPTZ,tags TEXT[],session_goal_meta JSONB)""")
            cur.execute("""CREATE TABLE crm_agent_notifications(id UUID,tenant_id TEXT,from_agent TEXT,
                to_agent TEXT,notification_type TEXT,subject TEXT,body TEXT,metadata JSONB)""")
            migration = Path(__file__).resolve().parents[3] / "crm/migrations/126_goal_pursuit.sql"
            cur.execute(migration.read_text())
            cur.execute(migration.read_text())  # migration is re-entrant
            reservation = migration.with_name("138_goal_provider_reservations.sql")
            cur.execute(reservation.read_text())
            cur.execute(reservation.read_text())
            family_controls = migration.with_name("139_goal_task_family_controls.sql")
            cur.execute(family_controls.read_text())
            cur.execute(family_controls.read_text())
            cur.execute(migration.with_name("141_runtime_effects.sql").read_text())
            cur.execute(migration.with_name("142_goal_effect_lookup.sql").read_text())
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
    tenant = str(uuid4())
    store.set_enabled(tenant, True, "operator:test")
    return tenant


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
    other = create("another-tenant")
    with pytest.raises(ValueError, match="task not found"):
        change("another-tenant", other, "link_task", task_id=task_id)


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
    child = change(db, child, "approve")
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
    from robothor.goals.runtime import task_runnable

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
    other = create("other-rls-tenant")
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


def test_provider_reservation_survives_lost_worker_and_stale_attempt_cannot_spend(db):
    g = create(db, token_budget=1000)
    _, attempt = store.claim(db)
    store.reserve_provider_usage(db, g["id"], attempt, 700)
    with pytest.raises(ValueError, match="budget"):
        store.reserve_provider_usage(db, g["id"], attempt, 1001)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET lease_until=now()-interval '1 minute' WHERE tenant_id=%s",
            (db,),
        )
    _, next_attempt = store.claim(db)
    assert next_attempt != attempt
    assert store.get(db, g["id"])["tokens_used"] == 700
    with pytest.raises(ValueError, match="lease"):
        store.reserve_provider_usage(db, g["id"], attempt, 100)


@pytest.mark.parametrize("pause_child_first", [True, False])
def test_parent_resume_preserves_an_explicit_child_pause(db, pause_child_first):
    from robothor.goals.runtime import task_runnable

    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Still open','TODO')",
            (task_id, db),
        )
    child = change(db, child, "link_task", task_id=task_id)
    if pause_child_first:
        child = change(db, child, "pause", note="Keep this child paused separately")
    parent = change(db, parent, "pause")
    if not pause_child_first:
        child = change(
            db, store.get(db, child["id"]), "pause", note="Keep this child paused separately"
        )
    parent = change(db, parent, "resume")
    assert parent["status"] == "queued"
    child = store.get(db, child["id"])
    assert child["status"] == "paused"
    assert not task_runnable(task_id, db)
    assert child["tasks"][0]["status"] == "TODO"
    assert child["evidence"] == []
    change(db, child, "resume")
    assert task_runnable(task_id, db)


def test_child_resume_cannot_bypass_a_paused_parent(db):
    from robothor.goals.runtime import task_runnable

    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Work','TODO')",
            (task_id, db),
        )
    change(db, child, "link_task", task_id=task_id)
    change(db, parent, "pause")
    child = store.get(db, child["id"])
    with pytest.raises(ValueError, match="parent"):
        change(db, child, "resume")
    assert store.get(db, child["id"])["status"] == "paused"
    assert not task_runnable(task_id, db)


def test_blocked_parent_prevents_dispatch_of_a_queued_child_task(db):
    from robothor.goals.runtime import task_runnable

    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Child work','TODO')",
            (task_id, db),
        )
    change(db, child, "link_task", task_id=task_id)
    assert task_runnable(task_id, db)
    for _ in range(3):
        parent = change(db, parent, "block", note="Waiting for operator decision")
    assert parent["status"] == "blocked"
    assert store.get(db, child["id"])["status"] == "queued"
    assert not task_runnable(task_id, db)
    change(db, parent, "resume")
    assert task_runnable(task_id, db)


@pytest.mark.parametrize(
    "status", ["paused", "blocked", "review", "complete", "canceled", "waiting", "queued"]
)
def test_task_guard_checks_persisted_ancestor_state_even_with_queued_child(db, status):
    from robothor.goals.runtime import task_runnable

    parent = create(db, kind="long")
    child = create(db, parent_goal_id=parent["id"])
    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Work','TODO')",
            (task_id, db),
        )
    change(db, child, "link_task", task_id=task_id)
    with store.transaction() as cur:
        parent["status"] = status
        store.save(cur, db, parent)  # legacy/interrupted cascade may leave the child queued
    assert task_runnable(task_id, db) is (status in {"queued", "waiting"})


@pytest.mark.parametrize("parent_kind", ["missing", "foreign"])
def test_task_guard_does_not_borrow_an_unresolved_or_foreign_parent(db, parent_kind):
    from robothor.goals.runtime import task_runnable

    child = create(db)
    task_id = str(uuid4())
    parent_id = (
        create("other-" + db, kind="long")["id"] if parent_kind == "foreign" else str(uuid4())
    )
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Work','TODO')",
            (task_id, db),
        )
    child = change(db, child, "link_task", task_id=task_id)
    with store.transaction() as cur:
        child["parent_goal_id"] = parent_id
        store.save(cur, db, child)
    assert not task_runnable(task_id, db)


def test_family_guard_keeps_unlinked_tasks_independent_of_goal_enablement(db):
    from robothor.goals.runtime import task_runnable

    task_id = str(uuid4())
    with store.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Ordinary task','TODO')",
            (task_id, db),
        )
    store.set_enabled(db, False, "operator:test")
    assert task_runnable(task_id, db)

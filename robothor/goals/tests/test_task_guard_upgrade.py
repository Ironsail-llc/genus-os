"""Exercise the task-guard upgrade against populated, disposable goal state."""

from pathlib import Path
from uuid import uuid4

from psycopg2 import sql

from robothor.goals import store
from robothor.goals.runtime import task_runnable
from robothor.goals.tests import test_store

private_database = test_store.private_database
db = test_store.db


def snapshot(tenant):
    tables = (
        "goal_pursuit_settings",
        "pursuit_goals",
        "pursuit_goal_tasks",
        "pursuit_goal_history",
        "pursuit_goal_attempts",
        "crm_tasks",
    )
    with store.transaction() as cur:
        result = {}
        for table in tables:
            cur.execute(
                sql.SQL(
                    "SELECT to_jsonb(t) AS row FROM {} t WHERE tenant_id=%s ORDER BY to_jsonb(t)::text"
                ).format(sql.Identifier(table)),
                (tenant,),
            )
            result[table] = cur.fetchall()
        return result


def install(source):
    with store.get_connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(source)


def test_upgrade_changes_admission_without_rewriting_existing_work(db):
    parent = test_store.create(db, kind="long")
    child = test_store.create(db, parent_goal_id=parent["id"])
    linked, ordinary = str(uuid4()), str(uuid4())
    with store.transaction() as cur:
        for task in (linked, ordinary):
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,'Existing work','TODO')",
                (task, db),
            )
    test_store.change(db, child, "link_task", task_id=linked)
    for _ in range(3):
        parent = test_store.change(db, parent, "block", note="Needs operator decision")
    migrations = Path(__file__).resolve().parents[3] / "crm/migrations"
    old_sql = (migrations / "126_goal_pursuit.sql").read_text()
    original = (
        old_sql[old_sql.index("CREATE OR REPLACE FUNCTION pursuit_task_runnable") :].split(
            "$$;", 1
        )[0]
        + "$$;"
    )
    upgraded = (migrations / "139_goal_task_family_controls.sql").read_text()
    try:
        install(original)
        assert task_runnable(linked, db)  # reproduce the accepted database's original guard
        before = snapshot(db)
        for _ in range(2):
            install(upgraded)
            assert not task_runnable(linked, db)
            assert task_runnable(ordinary, db)
            assert snapshot(db) == before
    finally:
        install(upgraded)

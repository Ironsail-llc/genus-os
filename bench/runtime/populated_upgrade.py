"""Upgrade the prior canonical schema with populated synthetic business records."""

from pathlib import Path
from uuid import uuid4

from psycopg2 import sql
from psycopg2.extras import Json

from robothor.db import migrate

ADDITIONS = {
    "127_runtime_contract",
    "138_goal_provider_reservations",
    "139_goal_task_family_controls",
    "140_chat_approval_receipts",
    "141_runtime_effects",
    "142_goal_effect_lookup",
}
TABLES = (
    "chat_sessions",
    "crm_tasks",
    "goal_pursuit_settings",
    "pursuit_goals",
    "pursuit_goal_tasks",
    "pursuit_goal_history",
    "pursuit_goal_attempts",
    "agent_runs",
    "agent_run_steps",
    "agent_run_checkpoints",
    "calendar_operations",
)


def snapshot(conn):
    result = {}
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(
                sql.SQL(
                    "SELECT to_jsonb(t) - 'runtime_context' FROM {} t ORDER BY to_jsonb(t)::text"
                ).format(sql.Identifier(table))
            )
            result[table] = cur.fetchall()
    return result


def upgrade(conn, directory):
    original = migrate._MIGRATION_MANIFEST
    prior = directory / "prior-manifest.txt"
    prior.write_text(
        "\n".join(
            line for line in original.read_text().splitlines() if Path(line).stem not in ADDITIONS
        )
        + "\n"
    )
    try:
        migrate._MIGRATION_MANIFEST = prior
        migrate.apply(connection=conn)
    finally:
        migrate._MIGRATION_MANIFEST = original

    parent, child, linked, ordinary, run, attempt, operation = [str(uuid4()) for _ in range(7)]
    approved = {
        "status": "approved",
        "plan_id": str(uuid4()),
        "approval_request_id": str(uuid4()),
        "plan_text": "Existing approved synthetic work",
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chat_sessions(tenant_id,session_key,plan_state) VALUES ('default','web:upgrade',%s)",
            (Json(approved),),
        )
        cur.execute(
            "INSERT INTO goal_pursuit_settings(tenant_id,enabled,updated_by) VALUES ('default',true,'fixture')"
        )
        for goal, status, parent_id in [(parent, "blocked", None), (child, "queued", parent)]:
            cur.execute(
                "INSERT INTO pursuit_goals(tenant_id,id,data,status) VALUES ('default',%s,%s,%s)",
                (
                    goal,
                    Json(
                        {
                            "id": goal,
                            "parent_goal_id": parent_id,
                            "objective": "Preserve existing work",
                            "revision": 2,
                        }
                    ),
                    status,
                ),
            )
        for task in (linked, ordinary):
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,'default','Existing synthetic task','TODO')",
                (task,),
            )
        cur.execute("INSERT INTO pursuit_goal_tasks VALUES ('default',%s,%s)", (child, linked))
        cur.execute(
            "INSERT INTO pursuit_goal_history(tenant_id,goal_id,action,actor,detail) VALUES ('default',%s,'block','fixture','{}')",
            (parent,),
        )
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,user_id,user_role,agent_id,trigger_type,status,error_message) VALUES (%s,'default','operator','owner','main','manual','cancelled','Interrupted fixture')",
            (run,),
        )
        cur.execute(
            "INSERT INTO pursuit_goal_attempts(tenant_id,id,goal_id,run_id,status,tokens) VALUES ('default',%s,%s,%s,'interrupted',17)",
            (attempt, child, run),
        )
        cur.execute(
            "INSERT INTO calendar_operations(id,tenant_id,user_id,agent_id,calendar_id,event_id,arguments,status) VALUES (%s,'default','operator','main','fixture','fixture','{}','executing')",
            (operation,),
        )
        cur.execute(
            "INSERT INTO agent_run_steps(run_id,step_number,step_type,tool_name,tool_output) VALUES (%s,1,'tool_call','gws_calendar_add_attendees',%s)",
            (run, Json({"operation_id": operation})),
        )
        cur.execute(
            "INSERT INTO agent_run_checkpoints(run_id,step_number,messages) VALUES (%s,1,%s)",
            (run, Json([{"role": "user", "content": "Existing synthetic request"}])),
        )
        cur.execute("SELECT pursuit_task_runnable(%s,'default')", (linked,))
        assert cur.fetchone()[0] is True
    conn.commit()
    before = snapshot(conn)
    assert set(migrate.apply(connection=conn)) == ADDITIONS
    assert snapshot(conn) == before
    assert migrate.apply(connection=conn) == []
    assert snapshot(conn) == before
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pursuit_task_runnable(%s,'default'), pursuit_task_runnable(%s,'default')",
            (linked, ordinary),
        )
        assert cur.fetchone() == (False, True)
        cur.execute("SELECT runtime_context FROM agent_runs WHERE id=%s", (run,))
        assert cur.fetchone()[0] == {
            "runtime_id": "current",
            "runtime_version": "1",
            "checkpoint_version": 1,
        }
        cur.execute(
            "SELECT plan_state FROM chat_approval_receipts WHERE tenant_id='default' AND session_key='web:upgrade'"
        )
        assert cur.fetchall() == [(approved,)]
        cur.execute("SELECT count(*) FROM pursuit_goal_provider_reservations")
        assert cur.fetchone()[0] == 0
    return {
        "added_migrations": sorted(ADDITIONS),
        "preserved_tables": list(TABLES),
        "repeat_apply_noop": True,
        "blocked_ancestor_denies_linked_task": True,
        "ordinary_task_runnable": True,
        "legacy_runtime_identity_added": True,
        "existing_approval_receipt_preserved": True,
    }

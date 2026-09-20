"""Stopped synthetic work must survive real daemon restarts without admission."""

from uuid import uuid4

import psycopg2
from psycopg2.extras import Json


def seed(dsn):
    run, goal, operation = [str(uuid4()) for _ in range(3)]
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO goal_pursuit_settings(tenant_id,enabled,updated_by) VALUES ('default',true,'restart-fixture') ON CONFLICT (tenant_id) DO UPDATE SET enabled=true"
        )
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,user_id,user_role,agent_id,trigger_type,status,error_message) VALUES (%s,'default','operator','owner','main','manual','cancelled','daemon_restart')",
            (run,),
        )
        cur.execute(
            "INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version) VALUES (%s,1,%s,1)",
            (run, Json([{"role": "user", "content": "Stopped synthetic work"}])),
        )
        cur.execute(
            "INSERT INTO agent_runtime_controls(tenant_id,run_id,action,note) VALUES ('default',%s,'cancel','Explicit synthetic stop')",
            (run,),
        )
        cur.execute(
            "INSERT INTO pursuit_goals(tenant_id,id,status,data) VALUES ('default',%s,'paused',%s)",
            (goal, Json({"id": goal, "objective": "Remain paused"})),
        )
        cur.execute(
            "INSERT INTO calendar_operations(id,tenant_id,user_id,agent_id,calendar_id,event_id,arguments,status,result) VALUES (%s,'default','operator','main','fixture','fixture','{}','blocked',%s)",
            (
                operation,
                Json({"verification": "unverified", "error": "Synthetic unresolved outcome"}),
            ),
        )
        foreign = str(uuid4())
        cur.execute(
            "INSERT INTO crm_tenants(id,display_name) VALUES ('foreign-fixture','Foreign synthetic tenant')"
        )
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,user_id,user_role,agent_id,trigger_type,status,error_message) VALUES (%s,'foreign-fixture','other','owner','main','manual','cancelled','daemon_restart')",
            (foreign,),
        )
        cur.execute(
            "INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version) VALUES (%s,1,'[]',1)",
            (foreign,),
        )
        stale_local, stale_foreign, wf_local, wf_foreign = [str(uuid4()) for _ in range(4)]
        for identifier, tenant in [(stale_local, "default"), (stale_foreign, "foreign-fixture")]:
            cur.execute(
                "INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status,started_at) VALUES (%s,%s,'orphan-fixture','manual','running',now()-interval '3 hours')",
                (identifier, tenant),
            )
        for identifier, tenant in [(wf_local, "default"), (wf_foreign, "foreign-fixture")]:
            cur.execute(
                "INSERT INTO workflow_runs(id,tenant_id,workflow_id,status,started_at) VALUES (%s,%s,'fixture','running',now()-interval '3 hours')",
                (identifier, tenant),
            )
    return run, goal, operation, foreign, stale_local, stale_foreign, wf_local, wf_foreign


def verify(dsn, identifiers):
    run, goal, operation, foreign, stale_local, stale_foreign, wf_local, wf_foreign = identifiers
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status,COALESCE(resume_attempts,0) FROM agent_runs WHERE id=%s", (run,))
        assert cur.fetchone() == ("cancelled", 0), (
            "Stopped run was admitted or charged for resumption"
        )
        cur.execute("SELECT messages FROM agent_run_checkpoints WHERE run_id=%s", (run,))
        assert cur.fetchone()[0] == [{"role": "user", "content": "Stopped synthetic work"}]
        cur.execute("SELECT action,note FROM agent_runtime_controls WHERE run_id=%s", (run,))
        assert cur.fetchone() == ("cancel", "Explicit synthetic stop")
        cur.execute("SELECT status,data FROM pursuit_goals WHERE id=%s", (goal,))
        assert cur.fetchone() == ("paused", {"id": goal, "objective": "Remain paused"})
        cur.execute("SELECT status,result FROM calendar_operations WHERE id=%s", (operation,))
        assert cur.fetchone() == (
            "blocked",
            {"verification": "unverified", "error": "Synthetic unresolved outcome"},
        )
        cur.execute("SELECT count(*) FROM agent_run_steps WHERE run_id=%s", (run,))
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT status,COALESCE(resume_attempts,0) FROM agent_runs WHERE id=%s", (foreign,)
        )
        assert cur.fetchone() == ("cancelled", 0), "Daemon selected another tenant for resume"

        for identifier, expected in [(stale_local, "timeout"), (stale_foreign, "running")]:
            cur.execute("SELECT status FROM agent_runs WHERE id=%s", (identifier,))
            assert cur.fetchone()[0] == expected, (
                "Agent cleanup crossed tenant boundary or missed local orphan"
            )
        for identifier, expected in [(wf_local, "timeout"), (wf_foreign, "running")]:
            cur.execute("SELECT status FROM workflow_runs WHERE id=%s", (identifier,))
            assert cur.fetchone()[0] == expected, (
                "Workflow cleanup crossed tenant boundary or missed local orphan"
            )

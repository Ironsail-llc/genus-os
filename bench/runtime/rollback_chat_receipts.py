"""Probe a saved receipt while the terminal run-row update is still pending."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json


def probe(code, env, dsn):
    assert "host=/tmp/runtime-migrated-" in dsn
    run_id, effect_id, request_id = (str(uuid4()) for _ in range(3))
    from robothor.engine.runtime.chat_control import request_key

    goal_id = str(uuid4())
    correlation = request_key(
        SimpleNamespace(tenant_id="default", user_id="rollback-receipts"),
        "rollback-goals",
        request_id,
    )
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runs(id,tenant_id,user_id,agent_id,trigger_type,status,correlation_id)
               VALUES (%s,'default','rollback-receipts','main','webchat','running',%s)""",
            (run_id, correlation),
        )
        cur.execute(
            """INSERT INTO agent_runtime_effects
               (id,tenant_id,principal_id,request_id,run_id,agent_id,tool_name,fingerprint,state,resolution)
               VALUES (%s,'default','rollback-receipts',%s,%s,'main','create_note',%s,'confirmed',
                       '{"reference":"synthetic-note","result":{"id":"synthetic-note"}}')""",
            (effect_id, request_id, run_id, "c" * 64),
        )
        cur.execute(
            "INSERT INTO pursuit_goals(tenant_id,id,data,status) VALUES ('default',%s,'{}','paused')",
            (goal_id,),
        )
        cur.execute(
            """INSERT INTO pursuit_goal_history(tenant_id,goal_id,action,actor,detail)
               VALUES ('default',%s,'pause','rollback-receipts',%s)""",
            (
                goal_id,
                Json(
                    {
                        "control_receipt": {
                            "run_id": run_id,
                            "principal_id": "rollback-receipts",
                            "status": "paused",
                            "version": 2,
                        }
                    }
                ),
            ),
        )
    script = """
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from robothor.engine.models import RunStatus
from robothor.engine.last_resort import all_models_failed_error
try:
    from robothor.engine import chat_delivery
except ImportError:
    print(json.dumps({'compatible':False,'reason':'No initial receipt delivery'}))
else:
    assert Path(chat_delivery.__file__).resolve().is_relative_to(Path.cwd())
    error=str(all_models_failed_error([],local_state='absent'))
    run=SimpleNamespace(id=sys.argv[1],status=RunStatus.FAILED,output_text=None,error_message=error)
    auth=SimpleNamespace(tenant_id='default',user_id='rollback-receipts')
    result=asyncio.run(chat_delivery.final_result(run,auth))
    foreign=asyncio.run(chat_delivery.final_result(run,SimpleNamespace(tenant_id='default',user_id='other')))
    recovered='CRM note was created' in result['text'] and result.get('status')=='failed'
    scoped='CRM note was created' not in foreign['text']
    honest='without doing the work' not in error
    goal_recovered='paused' in result['text'] and sys.argv[2] in result['text']
    scoped=scoped and sys.argv[2] not in foreign['text']
    from robothor.db.connection import get_connection
    from robothor.engine.chat_recovery import read_outcome
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE agent_runs SET status='failed' WHERE id=%s", (sys.argv[1],))
        conn.commit()
    reconnect=read_outcome(auth,'rollback-goals',sys.argv[3])
    reconnect_ok=sys.argv[2] in reconnect['text'] and 'paused' in reconnect['text'] and not reconnect['verified']
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE agent_runs SET status='running' WHERE id=%s", (sys.argv[1],))
        conn.commit()
    print(json.dumps({'compatible':recovered and scoped and honest and goal_recovered and reconnect_ok,
                      'goal_control_reconnect_recovered':reconnect_ok,
                      'goal_control_receipt_recovered':goal_recovered,
                      'receipt_recovered_before_terminal_write':recovered,
                      'foreign_principal_denied':scoped,'no_false_no_work_claim':honest}))
"""
    worker_env = {k: v for k, v in env.items() if k.startswith("ROBOTHOR_DB_")}
    worker_env.update(PATH=os.environ["PATH"], PYTHONPATH=str(code), PYTHONNOUSERSITE="1")
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, run_id, goal_id, request_id],
            cwd=code,
            env=worker_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Rollback receipt delivery probe failed: " + result.stderr)
        report = json.loads(result.stdout)
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM agent_runs WHERE id=%s", (run_id,))
            assert cur.fetchone() == ("running",), "Receipt projection changed durable run state"
            cur.execute("SELECT state FROM agent_runtime_effects WHERE id=%s", (effect_id,))
            assert cur.fetchone() == ("confirmed",), "Receipt projection changed action state"
        return report
    finally:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pursuit_goal_history WHERE tenant_id='default' AND goal_id=%s",
                (goal_id,),
            )
            cur.execute("DELETE FROM pursuit_goals WHERE tenant_id='default' AND id=%s", (goal_id,))
            cur.execute("DELETE FROM agent_runtime_effects WHERE id=%s", (effect_id,))
            cur.execute("DELETE FROM agent_runs WHERE id=%s", (run_id,))

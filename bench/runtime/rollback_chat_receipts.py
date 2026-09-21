"""Probe a saved receipt while the terminal run-row update is still pending."""

import json
import os
import subprocess
import sys
from uuid import uuid4

import psycopg2


def probe(code, env, dsn):
    assert "host=/tmp/runtime-migrated-" in dsn
    run_id, effect_id, request_id = (str(uuid4()) for _ in range(3))
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runs(id,tenant_id,user_id,agent_id,trigger_type,status)
               VALUES (%s,'default','rollback-receipts','main','webchat','running')""",
            (run_id,),
        )
        cur.execute(
            """INSERT INTO agent_runtime_effects
               (id,tenant_id,principal_id,request_id,run_id,agent_id,tool_name,fingerprint,state,resolution)
               VALUES (%s,'default','rollback-receipts',%s,%s,'main','create_note',%s,'confirmed',
                       '{"reference":"synthetic-note","result":{"id":"synthetic-note"}}')""",
            (effect_id, request_id, run_id, "c" * 64),
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
    print(json.dumps({'compatible':recovered and scoped and honest,
                      'receipt_recovered_before_terminal_write':recovered,
                      'foreign_principal_denied':scoped,'no_false_no_work_claim':honest}))
"""
    worker_env = {k: v for k, v in env.items() if k.startswith("ROBOTHOR_DB_")}
    worker_env.update(PATH=os.environ["PATH"], PYTHONPATH=str(code), PYTHONNOUSERSITE="1")
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, run_id],
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
            cur.execute("DELETE FROM agent_runtime_effects WHERE id=%s", (effect_id,))
            cur.execute("DELETE FROM agent_runs WHERE id=%s", (run_id,))

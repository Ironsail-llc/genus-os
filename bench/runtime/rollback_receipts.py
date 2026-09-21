"""Behavioral downgrade gate for saved responses, using private storage only."""

import json
import os
import subprocess
import sys
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

from robothor.engine.runtime.effects import fingerprint


def probe(code, env, dsn):
    from bench.runtime.rollback_deadlines import probe as probe_deadlines
    from bench.runtime.rollback_report_boundaries import probe as probe_reports

    reports = probe_reports(code)
    deadlines = probe_deadlines(code, env, dsn)
    saved = _probe_one(code, env, dsn)
    task = _probe_one(code, env, dsn, task_report=True)
    return {
        **saved,
        "report_boundaries_compatible": reports["compatible"],
        "report_boundary_probe": reports,
        "saved_deadlines_compatible": deadlines["compatible"],
        "deadline_probe": deadlines,
        "task_report_identity_compatible": task["reuses_saved_response"],
        "task_report_probe": task,
    }


def _probe_one(code, env, dsn, *, task_report=False):
    if "host=/tmp/runtime-migrated-" not in dsn:
        raise ValueError("Rollback receipt probe requires disposable canonical storage")
    identifier, request, run = (str(uuid4()) for _ in range(3))
    args = {"title": "Synthetic acknowledged action"}
    tool, state = ("create_task", "confirmed") if task_report else ("synthetic_write", "finished")
    if task_report:
        args["finalReport"] = True
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runtime_effects
            (id,tenant_id,principal_id,request_id,run_id,agent_id,tool_name,fingerprint,state,resolution)
            VALUES (%s,'default','rollback-probe',%s,%s,'main',%s,%s,%s,%s)""",
            (
                identifier,
                request,
                run,
                tool,
                fingerprint(tool, args),
                state,
                Json({"source": "tool_response", "result": {"id": "synthetic", "ok": True}}),
            ),
        )
    probe_env = {key: value for key, value in env.items() if key.startswith("ROBOTHOR_DB_")}
    probe_env.update(PATH=os.environ["PATH"], PYTHONPATH=str(code), PYTHONNOUSERSITE="1")
    script = """
import json, sys
from pathlib import Path
from robothor.engine.runtime import ExecutionContext, effects
assert Path(effects.__file__).resolve().is_relative_to(Path.cwd())
settings=json.loads(sys.argv[1])
ctx=ExecutionContext('default','rollback-probe',settings['request'])
row=effects.begin(ctx,settings['run'],'main',settings['tool'],settings['args'])
print(json.dumps({'reuses_saved_response': str(row['id']) == settings['id'],
                  'state': row['state']}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            json.dumps(
                {"request": request, "run": run, "args": args, "id": identifier, "tool": tool}
            ),
        ],
        cwd=code,
        env=probe_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Rollback receipt probe failed: " + result.stderr)
    report = json.loads(result.stdout)
    # A probe may reserve another attempt on incompatible code, but never
    # dispatches a handler or calls a model. All rows live in the private DB.
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent_runtime_effects WHERE request_id=%s", (request,))
    return report

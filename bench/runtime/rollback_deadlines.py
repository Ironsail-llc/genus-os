"""Probe saved deadline admission in a fresh rollback process and private database."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json

_SCRIPT = """
import asyncio, json, sys
from pathlib import Path
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.contracts import StateEnvelope
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
import robothor.engine.runtime.current as module
assert Path(module.__file__).resolve().is_relative_to(Path.cwd())
settings = json.loads(sys.argv[1])
class Entered(Exception): pass
async def main():
    results = {}
    for case, run_id in settings['runs'].items():
        observed = None
        async def execute(**kwargs):
            nonlocal observed
            value = active_context.get().deadline
            observed = value.isoformat() if value else None
            raise Entered()
        request = RunRequest(ExecutionContext('default', 'rollback-probe', run_id),
            'main', 'Synthetic continuation', resume_from=run_id, checkpoint=StateEnvelope())
        outcome = 'returned'
        try:
            await CurrentRuntime(execute).run(request)
        except RuntimeDeadlineError:
            outcome = 'expired'
        except ValueError:
            outcome = 'rejected'
        except Entered:
            outcome = 'entered'
        results[case] = {'outcome': outcome, 'deadline': observed}
    print(json.dumps(results))
asyncio.run(main())
"""


def probe(code, env, dsn):
    if "host=/tmp/runtime-migrated-" not in dsn:
        raise ValueError("Rollback deadline probe requires disposable canonical storage")
    deadlines = {
        "expired": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        "future": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "malformed": "not-a-deadline",
    }
    runs = {case: str(uuid4()) for case in deadlines}
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        for case, run_id in runs.items():
            cur.execute(
                """INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status,runtime_context)
                VALUES (%s,'default','main','event','failed',%s)""",
                (run_id, Json({"deadline": deadlines[case]})),
            )
            cur.execute(
                """INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version)
                VALUES (%s,1,'[]',1)""",
                (run_id,),
            )
    probe_env = {key: value for key, value in env.items() if key.startswith("ROBOTHOR_DB_")}
    probe_env.update(PATH=os.environ["PATH"], PYTHONPATH=str(code), PYTHONNOUSERSITE="1")
    try:
        process = subprocess.run(
            [sys.executable, "-c", _SCRIPT, json.dumps({"runs": runs})],
            cwd=code,
            env=probe_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if process.returncode:
            raise RuntimeError("Rollback deadline probe failed: " + process.stderr)
        results = json.loads(process.stdout)
        return {
            "compatible": results["expired"]["outcome"] == "expired"
            and results["malformed"]["outcome"] == "rejected"
            and results["future"] == {"outcome": "entered", "deadline": deadlines["future"]},
            "cases": results,
        }
    finally:
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_run_checkpoints WHERE run_id=ANY(%s::uuid[])",
                (list(runs.values()),),
            )
            cur.execute("DELETE FROM agent_runs WHERE id=ANY(%s::uuid[])", (list(runs.values()),))

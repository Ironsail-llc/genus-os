"""Synthetic accumulated history probe; never a live runtime performance cohort."""

import json
import os
import statistics
import time
from pathlib import Path

from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)
from robothor.goals import store
from robothor.goals.effect_summary import summaries
from robothor.goals.model import CreateGoal


def test_goal_evidence_with_large_finished_history(effect_db, monkeypatch):  # noqa: F811
    ctx = context()
    monkeypatch.setattr(store, "get_connection", effect_db)
    goals = [
        store.create(
            ctx.tenant_id,
            CreateGoal(objective=f"Synthetic goal {i}", success_criteria=["Checked"]),
            "operator",
        )
        for i in range(20)
    ]
    ids = [g["id"] for g in goals]
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runtime_effects
            (id,tenant_id,principal_id,request_id,run_id,agent_id,goal_id,tool_name,fingerprint,state)
            SELECT gen_random_uuid(),%s,'operator','request','run','main',(%s::text[])[1+(i%%20)],
                   'synthetic_write',i::text,CASE WHEN i<=20 THEN 'uncertain' ELSE 'finished' END
            FROM generate_series(1,100000) i""",
            (ctx.tenant_id, ids),
        )
        cur.execute("ANALYZE agent_runtime_effects")
        cur.execute("ANALYZE pursuit_goals")
    timings = []
    for _ in range(30):
        with store.transaction() as cur:
            start = time.perf_counter()
            result = summaries(cur, ctx.tenant_id, ids)
            timings.append((time.perf_counter() - start) * 1000)
            assert all(result[identifier] == {"pending": 1, "confirmed": 0} for identifier in ids)
    with store.transaction() as cur:
        summaries(cur, ctx.tenant_id, ids)
        query = cur.query.decode()
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query)
        plan = cur.fetchone()["QUERY PLAN"]
    output = os.environ.get("ROBOTHOR_GOAL_EVIDENCE_HISTORY_ARTIFACT")
    if output:
        with Path(output).open("x") as stream:
            json.dump(
                {
                    "scope": "Local synthetic SQL lookup, 20 goals, 100000 records (99980 finished); 30 repetitions; not model/provider performance",
                    "samples_ms": timings,
                    "median_ms": statistics.median(timings),
                    "p95_ms": statistics.quantiles(timings, n=100)[94],
                    "plan": plan,
                },
                stream,
                indent=2,
            )

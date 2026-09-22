"""Measured product-store load with waiting goals beside interactive controls.

This does not certify GPU/provider capacity. Every tenant is synthetic and the
PostgreSQL server belongs to this test module's disposable fixture.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from bench.interactive.statistics import summary
from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.tests.test_store import db, private_database, register_tenant  # noqa: F401


@pytest.mark.parametrize("tenants", [1, 5, 20])
def test_mixed_waiting_goals_and_operator_requests(db, tenants):  # noqa: F811
    def work(_):
        tenant = register_tenant(str(uuid4()))
        store.set_enabled(tenant, True, "operator")
        goal = store.create(
            tenant,
            CreateGoal(objective="Check tomorrow", success_criteria=["New receipt"], kind="long"),
            "operator",
        )
        goal = store.update(
            tenant,
            goal["id"],
            GoalUpdate(action="wait", version=goal["version"], note="Waiting for tomorrow"),
            "operator",
        )
        measurements = []
        for index in range(30):
            start = time.perf_counter()
            assert store.claim(tenant) is None  # Waiting invokes no runtime/model.
            fresh = store.get(tenant, goal["id"])
            goal = store.update(
                tenant,
                goal["id"],
                GoalUpdate(
                    action="steer", version=fresh["version"], note=f"Review receipt {index}"
                ),
                "operator",
                operator=True,
            )
            assert goal["status"] == "waiting"
            measurements.append((time.perf_counter() - start) * 1000)
        return measurements

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=tenants) as workers:
        observations = [value for values in workers.map(work, range(tenants)) for value in values]
    report = {
        "concurrent_tenants": tenants,
        "samples": len(observations),
        "model_calls": 0,
        "scope": "waiting-goal claim + detail read + interactive steering; disposable PostgreSQL",
        "request_ms": summary(observations),
        "wall_seconds": time.perf_counter() - started,
    }
    output = os.environ.get("ROBOTHOR_RUNTIME_CAPACITY_OUTPUT")
    if output:
        with Path(output).open("a") as file:
            file.write(json.dumps(report) + "\n")
    assert report["request_ms"]["p95"] < 2000

"""Synthetic tool gateway for native runtime regression tests."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from bench.interactive.statistics import quantile, summary
from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate


@dataclass
class FixtureGateway:
    tenant: str
    # Explicit synthetic state: no business credentials or network handlers.
    values: dict[str, str] = field(default_factory=dict)
    stopped: bool = False
    writes: int = 0
    dispatches: int = 0

    @property
    def schemas(self) -> Any:
        return [
            {
                "type": "function",
                "function": {
                    "name": "record",
                    "description": "Store an authorized value",
                    "parameters": SCHEMA,
                },
            }
        ]

    @property
    def verified(self) -> Any:
        return self.values.get("report") == "delivered" and self.writes == 1

    async def invoke(self, tenant: Any, name: Any, arguments: Any) -> Any:
        self.admit(tenant)
        if name != "record":
            raise ValueError("framework tool bypass denied")
        return await self.dispatch(tenant, **arguments)

    def admit(self, tenant: str) -> None:
        if tenant != self.tenant or self.stopped:
            raise ValueError("tenant authority denied or stopped")

    async def dispatch(
        self, tenant: str, key: str | None = None, value: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        self.admit(tenant)
        self.dispatches += 1
        if extra or (key, value) != ("report", "delivered"):
            return {"error": "Only storing key report with value delivered is authorized."}
        assert key is not None and value is not None
        if key in self.values and self.values[key] != value:
            raise ValueError("conflicting operation; reconcile instead of retrying")
        if key not in self.values:
            self.values[key] = value
            self.writes += 1
        return {"verification": "verified", "key": key, "value": self.values[key]}


SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
    "required": ["key", "value"],
    "additionalProperties": False,
}
PROMPT = (
    "Store key report with value delivered using record. Stop after independently verified success."
)
SYSTEM = "Complete only the authorized request. Use record to store the requested value."


def seed_unfinished_work(tenant: Any, *, kind: Any = "short") -> Any:
    goal = store.create(
        tenant,
        CreateGoal(
            objective="Finish the requested work",
            kind=kind,
            success_criteria=["The requested work has been checked"],
        ),
        "uat-operator",
    )
    remaining = str(uuid4())
    for task_id, title, status in [
        (str(uuid4()), "Prepare the first item", "DONE"),
        (remaining, "Check the remaining item", "TODO"),
    ]:
        with store.transaction() as cur:
            cur.execute(
                "INSERT INTO crm_tasks(id,tenant_id,title,status) VALUES (%s,%s,%s,%s)",
                (task_id, tenant, title, status),
            )
        goal = store.update(
            tenant,
            goal["id"],
            GoalUpdate(action="link_task", version=goal["version"], task_id=task_id),
            "uat-operator",
        )
    store.update(
        tenant,
        goal["id"],
        GoalUpdate(
            action="wait",
            version=goal["version"],
            task_id=remaining,
            note="One requested task still needs checking",
        ),
        "uat-agent",
    )


def batch_summary(samples, metric):
    """Resample rounds together because callers within a round share load."""
    result = summary([sample[metric] for sample in samples])
    groups = {}
    for sample in samples:
        groups.setdefault(sample["repetition"], []).append(sample[metric])
    batches = list(groups.values())
    rng = random.Random(20260920)
    estimates = [
        quantile([value for batch in rng.choices(batches, k=len(batches)) for value in batch], 0.95)
        for _ in range(1000)
    ]
    result["p95_ci95"] = [quantile(estimates, 0.025), quantile(estimates, 0.975)]
    result["uncertainty"] = "1000 bootstrap resamples of whole concurrent rounds"
    return result

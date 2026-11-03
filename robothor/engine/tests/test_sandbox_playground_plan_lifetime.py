"""A live run's execution plan is never forgotten while the run is alive.

Plans are also kept in a bounded table, and a busy instance starts more runs
than that table holds. If a long-lived run's plan fell out of it, a member
whose plan said `refuse` would find no plan at all and run on the host: a
containment bypass. The live session carries the plan for its whole lifetime.
"""

from __future__ import annotations

from robothor.engine import sandbox_policy as sp
from robothor.engine import session_registry
from robothor.engine.sandbox import RuntimeStatus

ABSENT = RuntimeStatus(binary="podman", available=False, reason="neither podman nor docker")


class _Cfg:
    id = "example-agent"
    sandbox = "docker"
    tools_allowed = ["exec"]
    tools_denied: list[str] = []


class _Run:
    def __init__(self, run_id: str) -> None:
        self.id = run_id
        self.user_role = "member"
        self.parent_run_id = None
        self.is_benchmark = False


class _Session:
    def __init__(self, run_id: str) -> None:
        self.run = _Run(run_id)
        self.run_id = run_id


async def test_a_live_runs_plan_survives_the_table_filling_up(monkeypatch) -> None:
    monkeypatch.setattr(sp, "_MAX_PLANS", 2)
    monkeypatch.setattr("robothor.engine.sandbox.runtime_status", lambda **_k: ABSENT)
    monkeypatch.setattr(sp, "_record", lambda *_a, **_k: None)
    session = _Session("run-long-lived")
    session_registry.register(session)  # type: ignore[arg-type]
    try:
        await sp.establish_execution_plan(_Cfg(), "off", agent_id="example-agent", run=session.run)
        for n in range(5):
            sp.set_exec_plan(f"run-other-{n}", sp.ExecPlan(where="host"))
        plan = sp.exec_plan_for("run-long-lived")
        assert plan is not None and plan.where == "refuse"
    finally:
        session_registry.unregister("run-long-lived")

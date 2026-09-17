"""The task's budget has to reach the engine as a BUDGET, not as a hint.

Measured 2026-09-17, `02_Code_Intelligence` `connect_the_dots_hard`: the task
declared 1200s, the harness backstop was 1500s, and the engine resolved its own
ceiling to **1600** — the manifest number multiplied by the model's tempo
factor. Every clock the engine owned was aiming past the point at which the
container would be destroyed, so it was: `agent.log` holds one line,
`HARNESS KILL after 1500s`, `transcript.jsonl` was never written, and the
grader scored 0.0 on a `FileNotFoundError`. The same task scored 0.545 the day
before.

`BENCH_TASK_TIMEOUT` was already exported, and `run_one.py` already set it as
the agent's `timeout_seconds` — which is exactly the value tempo scaling is
entitled to inflate. `ROBOTHOR_RUN_BUDGET_SECONDS` is the name the engine reads
for a budget imposed from OUTSIDE it, and an imposed budget is taken exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bench.wildclaw import harness

if TYPE_CHECKING:
    from pathlib import Path


def _env(tmp_path: Path, monkeypatch, timeout: int = 1200) -> dict[str, str]:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    task = {"task_id": "02_Code_task_12", "timeout_seconds": timeout}
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out_dir = tmp_path / "out" / "task"
    out_dir.mkdir(parents=True)
    _cmd, env_file = harness._container_command(task, workspace, out_dir, None)
    pairs: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
    return pairs


def test_the_task_budget_reaches_the_engine_as_a_budget(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    assert env["ROBOTHOR_RUN_BUDGET_SECONDS"] == "1200"


def test_it_is_the_same_number_the_backstop_counts(tmp_path, monkeypatch) -> None:
    """One budget, two readers. Two numbers is how 1600 met a 1500s kill."""
    env = _env(tmp_path, monkeypatch, timeout=900)
    assert env["ROBOTHOR_RUN_BUDGET_SECONDS"] == env["BENCH_TASK_TIMEOUT"] == "900"


def test_the_engine_resolves_that_budget_without_scaling_it(tmp_path, monkeypatch) -> None:
    """The end of the chain: what the container exports is what the run gets."""
    from types import SimpleNamespace

    from robothor.engine.run_deadline import resolve_run_budget

    env = _env(tmp_path, monkeypatch)
    monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_SECONDS", env["ROBOTHOR_RUN_BUDGET_SECONDS"])
    monkeypatch.setattr("robothor.engine.model_registry.chain_tempo_factor", lambda m: 1.3333)
    agent = SimpleNamespace(
        timeout_seconds=1200, model_primary="openrouter/test/model", model_fallbacks=[]
    )
    budget = resolve_run_budget(agent)
    assert budget.seconds == 1200
    assert budget.source == "external"


class TestTheBackstopMustNeverFire:
    @staticmethod
    def _summary(results: list[dict]) -> dict:
        return {
            "category": "02_Code_Intelligence",
            "mean_score": 0.45,
            "tasks_attempted": len(results),
            "tasks_graded": len(results),
            "results": results,
        }

    def test_a_kill_records_why_and_not_just_that(self) -> None:
        """A bare count leaves the next reader rediscovering which defect."""
        from bench.wildclaw.rotation import ledger_entry

        entry = ledger_entry(
            self._summary(
                [
                    {
                        "task_id": "t1",
                        "score": 0.0,
                        "total_tokens": 10,
                        "harness_kill": True,
                        "harness_kill_reason": "engine did not end the run at its 1200s budget",
                    },
                    {"task_id": "t2", "score": 0.9, "total_tokens": 10},
                ]
            ),
            {},
            when="2026-09-17T04:15:00Z",
        )
        assert entry["harness_kills"] == 1
        assert entry["harness_kill_reasons"] == ["engine did not end the run at its 1200s budget"]

    def test_a_clean_category_records_no_reasons(self) -> None:
        from bench.wildclaw.rotation import ledger_entry

        entry = ledger_entry(
            self._summary([{"task_id": "t1", "score": 0.9, "total_tokens": 10}]),
            {},
            when="2026-09-17T04:15:00Z",
        )
        assert entry["harness_kills"] == 0
        assert entry["harness_kill_reasons"] == []

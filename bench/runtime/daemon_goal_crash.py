"""Synthetic provider and state checks for a full daemon goal crash drill."""

import asyncio
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import psycopg2

MANIFEST = """id: main
name: Synthetic main
model:
  primary: openrouter/test/model
  fallbacks: []
schedule:
  enabled: false
  timeout_seconds: 30
delivery:
  mode: none
tools_allowed: [get_pursuit_goal, update_pursuit_goal]
task_protocol: false
notification_inbox: false
shared_working_state: false
bootstrap_files: []
v2:
  difficulty_class: simple
"""


def install():
    """Only loaded by the disposable daemon's sitecustomize."""
    import litellm

    from robothor.goals import store
    from robothor.goals.runtime import binding

    calls = 0

    async def provider(**kwargs):
        nonlocal calls
        current = binding.get()
        if current is None:
            raise RuntimeError("Synthetic provider only supports the drill goal")
        calls += 1
        phase = os.environ["RUNTIME_GOAL_PHASE"]
        state = store.control(current.tenant, current.goal_id)
        if phase == "crash" and calls == 2:
            assert state["checkpoint"] == "Saved before daemon crash"
            Path(os.environ["RUNTIME_DRILL_ROOT"], "goal-crash-ready.json").write_text(
                json.dumps({"goal": current.goal_id, "attempt": current.attempt})
            )
            await asyncio.Future()
        action = "progress" if phase == "crash" else ("reconciled" if calls == 2 else "wait")
        args = {"goal_id": current.goal_id, "version": state["version"], "action": action}
        if action == "progress":
            args.update(note="Saved before daemon crash", next_action="Check saved progress")
        elif action == "reconciled":
            args.update(note="Synthetic prior progress checked")
        else:
            args.update(note="Await synthetic reply", event_type="fixture.reply")
        return litellm.ModelResponse(
            model="openrouter/test/model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"{phase}_{calls}",
                                "type": "function",
                                "function": {
                                    "name": "update_pursuit_goal",
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

    litellm.acompletion = provider


def await_state(root, daemon, env, phase):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and daemon.poll() is None:
        if phase == "crash":
            if (root / "goal-crash-ready.json").exists():
                return
        else:
            with psycopg2.connect(env["ROBOTHOR_TEST_DB_DSN"]) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT status,lease_id FROM pursuit_goals WHERE id=%s",
                    (env["RUNTIME_GOAL_ID"],),
                )
                row = cur.fetchone()
                if row == ("waiting", None):
                    return
        time.sleep(0.1)
    raise AssertionError("Goal phase did not finish: " + (root / "daemon.log").read_text()[-8000:])


def drill(root, env):
    from bench.runtime.daemon_drill import run
    from robothor.goals import store
    from robothor.goals.model import CreateGoal

    @contextmanager
    def connect():
        with psycopg2.connect(env["ROBOTHOR_TEST_DB_DSN"]) as conn:
            yield conn

    with patch.object(store, "get_connection", connect):
        store.set_enabled("default", True, "drill")
        goal = store.create(
            "default",
            CreateGoal(
                objective="Recover daemon goal",
                success_criteria=["Reply received"],
                token_budget=1000000,
            ),
            "drill",
        )
    env = {**env, "RUNTIME_GOAL_ID": goal["id"]}
    outcomes = []
    for phase in ("crash", "recover"):
        location = root / ("goal-" + phase)
        location.mkdir()
        outcomes.append(run(location, env, resume=True, goal_phase=phase))
        if phase == "crash":
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE pursuit_goals SET lease_until=now()-interval '1 minute' WHERE id=%s",
                    (goal["id"],),
                )
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status,data FROM pursuit_goals WHERE id=%s", (goal["id"],))
        status, data = cur.fetchone()
        assert status == "waiting" and not data["recovery_required"]
        cur.execute(
            "SELECT status,tokens FROM pursuit_goal_attempts WHERE goal_id=%s ORDER BY started_at",
            (goal["id"],),
        )
        attempts = cur.fetchall()
        assert (
            len(attempts) == 2 and attempts[0][0] == "interrupted" and attempts[1][0] == "finished"
        ), attempts
        assert attempts[0][1] > 150 and attempts[1][1] == 450
        assert data["tokens_used"] == sum(a[1] for a in attempts)
        cur.execute(
            "SELECT s.tool_input->>'action' FROM agent_run_steps s JOIN agent_runs r ON r.id=s.run_id WHERE r.runtime_context->>'goal_id'=%s AND s.tool_name='update_pursuit_goal' ORDER BY r.started_at,s.step_number",
            (goal["id"],),
        )
        assert [r[0] for r in cur.fetchall()] == ["progress", "wait", "reconciled", "wait"]
        cur.execute(
            "SELECT COALESCE(resume_attempts,0) FROM agent_runs WHERE runtime_context->>'goal_id'=%s",
            (goal["id"],),
        )
        assert all(r[0] == 0 for r in cur.fetchall())
    return {
        "cycles": outcomes,
        "attempts": attempts,
        "tokens_used": data["tokens_used"],
        "progress_effects": 1,
    }

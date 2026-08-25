"""Run ONE WildClawBench task through Genus. Executes inside the container.

Reads the prompt on stdin, runs the agent against /tmp_workspace, and writes
the transcript and usage the benchmark's graders consume:

    /out/transcript.jsonl   the conversation, in WildClawBench's shape
    /out/usage.json         tokens, cost and wall-clock for this task

Nothing here grades anything. Grading is the benchmark's own `grade()`, run
separately against this transcript, so the harness under test never gets a
say in its own score.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

OUT = Path(os.environ.get("BENCH_OUT", "/out"))
WORKSPACE = os.environ.get("BENCH_WORKSPACE", "/tmp_workspace")


async def _run(prompt: str, timeout_seconds: int) -> dict:
    from robothor.engine.config import EngineConfig, load_agent_config
    from robothor.engine.models import TriggerType
    from robothor.engine.runner import AgentRunner

    config = EngineConfig.from_env()
    agent_config = load_agent_config("wildclaw", config.manifest_dir)
    if agent_config is None:
        raise RuntimeError("wildclaw agent manifest did not load")

    model_override = os.environ.get("ROBOTHOR_BENCH_MODEL", "").strip()
    if model_override:
        agent_config.model_primary = model_override
    # The task's own budget, exactly — it is the only wall-clock owner, and
    # it is also the budget every competing harness is killed at.
    #
    # This used to add 120s of grace, which was generous in the wrong
    # direction. `deadline_warning()` fires at 80% of the agent's ceiling, so
    # padding a 900s task to 1020s moved the warning to 816s — 91% of the real
    # budget, long past the point where an agent could still write partial
    # results. The pad also let the run continue past where every other
    # harness had already been stopped. Generosity that moves a control out of
    # range is not generosity.
    agent_config.timeout_seconds = int(timeout_seconds)

    # Capture the live session so the transcript is the FULL conversation.
    # `agent_run_steps` records tool calls but not the assistant's prose, and
    # the safety graders read what the agent said as closely as what it ran.
    # The registry is the one place the session is reachable from outside the
    # run; wrapping `register` observes it without changing any behaviour.
    from robothor.engine import session_registry

    captured: list = []
    original_register = session_registry.register

    def _capture(session):
        captured.append(session)
        return original_register(session)

    session_registry.register = _capture

    runner = AgentRunner(config)
    started = time.perf_counter()
    run = await runner.execute(
        "wildclaw",
        prompt,
        agent_config=agent_config,
        trigger_type=TriggerType.CRON,
        trigger_detail="wildclawbench",
    )
    elapsed = time.perf_counter() - started
    session_registry.register = original_register

    session = captured[0] if captured else None
    messages = list(getattr(session, "messages", []) or [])
    return {"run": run, "messages": messages, "elapsed": elapsed}


def _persisted_steps(run_id: str) -> list[dict]:
    """Every step this run recorded, from `agent_run_steps`.

    The complete record. `session.messages` is only the window the model is
    still carrying, and on a long run it has shed most of its history: one
    Productivity Flow task recorded 174 tool calls and kept 62.
    """
    if not run_id:
        return []
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT step_number, step_type, tool_name, tool_input, tool_output "
                "FROM agent_run_steps WHERE run_id = %s ORDER BY step_number",
                (run_id,),
            )
            return [
                {
                    "step_number": r[0],
                    "step_type": r[1],
                    "tool_name": r[2],
                    "tool_input": r[3],
                    "tool_output": r[4],
                }
                for r in cur.fetchall()
            ]
    except Exception as exc:
        print(f"could not read persisted steps: {exc}", file=sys.stderr)
        return []


def main() -> int:
    prompt = sys.stdin.read().strip()
    if not prompt:
        print("no prompt on stdin", file=sys.stderr)
        return 2
    timeout_seconds = int(os.environ.get("BENCH_TASK_TIMEOUT", "600"))

    OUT.mkdir(parents=True, exist_ok=True)
    Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
    os.chdir(WORKSPACE)

    from bench.wildclaw.transcript import steps_to_transcript

    error = None
    try:
        result = asyncio.run(_run(prompt, timeout_seconds))
    except Exception as exc:  # a crashed harness is a result, not a mystery
        error = f"{type(exc).__name__}: {exc}"
        result = {"run": None, "messages": [], "elapsed": 0.0}

    run_id = str(getattr(result["run"], "id", "") or "")
    steps = _persisted_steps(run_id)
    entries = steps_to_transcript(steps, result["messages"])
    print(
        f"transcript: {len(steps)} persisted steps, {len(result['messages'])} surviving messages",
        file=sys.stderr,
    )
    with (OUT / "transcript.jsonl").open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    run = result["run"]
    usage = {
        "input_tokens": getattr(run, "input_tokens", 0) or 0,
        "output_tokens": getattr(run, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(run, "cache_read_tokens", 0) or 0,
        "cache_write_tokens": getattr(run, "cache_creation_tokens", 0) or 0,
        "cost_usd": round(float(getattr(run, "total_cost_usd", 0.0) or 0.0), 4),
        "request_count": len(getattr(run, "steps", []) or []),
        "elapsed_time": round(result["elapsed"], 2),
        "status": str(getattr(getattr(run, "status", ""), "value", "") or "error"),
        "model_used": getattr(run, "model_used", "") or "",
        "error": error or getattr(run, "error_message", None),
    }
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    (OUT / "usage.json").write_text(json.dumps(usage, indent=2), encoding="utf-8")

    (OUT / "final_output.txt").write_text(
        str(getattr(run, "output_text", "") or ""), encoding="utf-8"
    )
    print(json.dumps({"transcript_entries": len(entries), **usage}))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

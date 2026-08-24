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
    # The task's own budget is the only wall-clock owner. Anything smaller
    # firing first files a harness kill as an agent failure — the exact defect
    # that corrupted our fleet grades on 2026-08-24.
    agent_config.timeout_seconds = max(int(timeout_seconds) + 120, 600)

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


def main() -> int:
    prompt = sys.stdin.read().strip()
    if not prompt:
        print("no prompt on stdin", file=sys.stderr)
        return 2
    timeout_seconds = int(os.environ.get("BENCH_TASK_TIMEOUT", "600"))

    OUT.mkdir(parents=True, exist_ok=True)
    Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
    os.chdir(WORKSPACE)

    from bench.wildclaw.transcript import to_wildclaw_transcript

    error = None
    try:
        result = asyncio.run(_run(prompt, timeout_seconds))
    except Exception as exc:  # a crashed harness is a result, not a mystery
        error = f"{type(exc).__name__}: {exc}"
        result = {"run": None, "messages": [], "elapsed": 0.0}

    entries = to_wildclaw_transcript(result["messages"])
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

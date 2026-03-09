#!/usr/bin/env python3
"""GLM-5 Fleet Smoke Test — fires each agent with a simple task, checks tool-calling works.

Usage:
    python scripts/smoke_test_glm5.py              # test all GLM-5 agents
    python scripts/smoke_test_glm5.py email-classifier calendar-monitor   # test specific agents

Reports: model used, tool calls made, duration, pass/fail for each agent.
"""

from __future__ import annotations

import asyncio
import sys
import time

# ── Test definitions ──────────────────────────────────────────────

# Each agent gets a lightweight message that should trigger at least one tool call.
# delivery is forced to NONE by the smoke test (no messages to Philip).
AGENT_TESTS: dict[str, str] = {
    # GLM-5 primary agents
    "email-classifier": "Check the triage inbox and classify any new emails.",
    "email-analyst": "Summarize the current email pipeline status.",
    "calendar-monitor": "Check for any upcoming calendar events in the next 24 hours.",
    "vision-monitor": "Report the current vision system status.",
    "conversation-inbox": "Check the notification inbox for any pending items.",
    "crm-steward": "Review CRM data quality — any contacts missing emails?",
    "conversation-resolver": "Check for any open conversations that need resolution.",
    "chat-monitor": "Check for any new chat messages that need processing.",
    "chat-responder": "Check chat status and report if any responses are pending.",
    "canary": "Run a quick health check.",
    "engine-report": "Generate a brief engine health summary.",
    # Sonnet primary agents (GLM-5 as fallback — we test these to confirm manifests load)
    # We don't force GLM-5 on these since they're Sonnet-primary
}

# Agents expected to use GLM-5 as primary
GLM5_PRIMARY_AGENTS = {
    "email-classifier",
    "email-analyst",
    "calendar-monitor",
    "vision-monitor",
    "conversation-inbox",
    "crm-steward",
    "conversation-resolver",
    "chat-monitor",
    "chat-responder",
    "canary",
    "engine-report",
}


async def run_smoke_test(agent_id: str, message: str) -> dict:
    """Fire a single agent run and collect results."""
    # Import here so the script can print usage without loading the engine
    from robothor.engine.config import EngineConfig, load_agent_config
    from robothor.engine.models import DeliveryMode, TriggerType
    from robothor.engine.runner import AgentRunner

    engine_config = EngineConfig.from_env()
    runner = AgentRunner(engine_config)

    # Load and patch config: force no delivery, cap iterations
    agent_config = load_agent_config(agent_id, engine_config.manifest_dir)
    if agent_config is None:
        return {"agent_id": agent_id, "status": "SKIP", "reason": "manifest not found"}

    agent_config.delivery_mode = DeliveryMode.NONE
    agent_config.max_iterations = min(agent_config.max_iterations, 10)

    t0 = time.monotonic()
    try:
        run = await runner.execute(
            agent_id=agent_id,
            message=message,
            trigger_type=TriggerType.MANUAL,
            trigger_detail="glm5-smoke-test",
            agent_config=agent_config,
        )
        elapsed = time.monotonic() - t0

        # Pull tool names from DB steps
        tools_called = []
        if run.id:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT tool_name FROM agent_run_steps WHERE run_id = %s AND tool_name IS NOT NULL ORDER BY step_number",
                    (run.id,),
                )
                tools_called = [row[0] for row in cur.fetchall()]

        return {
            "agent_id": agent_id,
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "model_used": run.model_used or "?",
            "tools_called": tools_called,
            "tool_count": len(tools_called),
            "output_preview": (run.output_text or "")[:200],
            "input_tokens": run.input_tokens or 0,
            "output_tokens": run.output_tokens or 0,
            "duration_s": round(elapsed, 1),
            "error": run.error_message,
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {
            "agent_id": agent_id,
            "status": "ERROR",
            "error": str(e),
            "duration_s": round(elapsed, 1),
            "tools_called": [],
            "tool_count": 0,
        }


def print_result(r: dict) -> None:
    """Pretty-print a single test result."""
    agent = r["agent_id"]
    status = r["status"]
    model = r.get("model_used", "?")
    tools = r.get("tools_called", [])
    dur = r.get("duration_s", 0)
    error = r.get("error")
    inp = r.get("input_tokens", 0)
    out = r.get("output_tokens", 0)

    # Determine pass/fail
    is_glm5 = "glm-5" in str(model).lower()
    expected_glm5 = agent in GLM5_PRIMARY_AGENTS
    model_ok = is_glm5 if expected_glm5 else True
    completed = status == "completed"
    has_tools = len(tools) > 0

    verdict = "PASS" if (completed and model_ok and has_tools) else "FAIL"
    if status == "SKIP":
        verdict = "SKIP"

    icon = {"PASS": "\u2705", "FAIL": "\u274c", "SKIP": "\u23ed\ufe0f"}.get(verdict, "?")

    print(f"\n{icon} {agent}")
    print(f"   Status:   {status}")
    print(f"   Model:    {model}")
    print(f"   Duration: {dur}s")
    print(f"   Tokens:   {inp} in / {out} out")
    print(f"   Tools:    {', '.join(tools) if tools else '(none)'} ({len(tools)} calls)")
    if r.get("output_preview"):
        preview = r["output_preview"].replace("\n", " ")[:120]
        print(f"   Output:   {preview}...")
    if error:
        print(f"   Error:    {error[:200]}")
    if not model_ok:
        print(f"   WARNING:  Expected GLM-5 but got {model}")

    return verdict


async def main():
    # Determine which agents to test
    if len(sys.argv) > 1:
        agents = {a: AGENT_TESTS.get(a, "Run your standard task.") for a in sys.argv[1:]}
    else:
        agents = AGENT_TESTS

    print("=== GLM-5 Fleet Smoke Test ===")
    print(f"Testing {len(agents)} agents sequentially...\n")

    results = []
    verdicts = {"PASS": 0, "FAIL": 0, "SKIP": 0}

    for agent_id, message in agents.items():
        print(f"--- Running {agent_id}...", flush=True)
        result = await run_smoke_test(agent_id, message)
        results.append(result)
        v = print_result(result)
        verdicts[v] = verdicts.get(v, 0) + 1

    # Summary
    print(f"\n{'=' * 50}")
    print(f"SUMMARY: {verdicts['PASS']} pass, {verdicts['FAIL']} fail, {verdicts['SKIP']} skip")
    total_dur = sum(r.get("duration_s", 0) for r in results)
    print(f"Total time: {total_dur:.0f}s")

    if verdicts["FAIL"] > 0:
        print("\nFailed agents:")
        for r in results:
            if r["status"] != "completed" or (
                "glm-5" not in str(r.get("model_used", "")).lower()
                and r["agent_id"] in GLM5_PRIMARY_AGENTS
            ):
                print(f"  - {r['agent_id']}: {r.get('error', r['status'])}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

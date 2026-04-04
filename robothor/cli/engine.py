"""Engine commands (run, start, stop, status, list, history, agents shortcut, costs)."""

from __future__ import annotations

import argparse  # noqa: TC003
import sys
from typing import Any


def cmd_run(args: argparse.Namespace) -> int:
    """Non-interactive single-shot agent execution with pipe support."""
    import asyncio
    import json as json_mod

    from robothor.engine.config import EngineConfig, load_agent_config
    from robothor.engine.models import TriggerType

    config = EngineConfig.from_env()
    agent_id = args.agent or config.default_chat_agent

    # Read message from args or stdin
    message = args.message
    if message is None:
        if not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            print("Error: No message provided.", file=sys.stderr)
            print('Usage: robothor run "task description"', file=sys.stderr)
            print('  Or pipe: echo "task" | robothor run', file=sys.stderr)
            return 1

    if not message:
        print("Error: Empty message.", file=sys.stderr)
        return 1

    agent_config = load_agent_config(agent_id, config.manifest_dir)
    if not agent_config:
        print(f"Error: Agent '{agent_id}' not found in {config.manifest_dir}", file=sys.stderr)
        return 1

    if not args.print_only and not args.json_output:
        print(f"Agent: {agent_config.name} ({agent_id})", file=sys.stderr)
        print(f"Model: {agent_config.model_primary}", file=sys.stderr)

    async def _run() -> Any:
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(config)
        return await runner.execute(
            agent_id=agent_id,
            message=message,
            trigger_type=TriggerType.MANUAL,
            agent_config=agent_config,
            model_override=args.model,
        )

    run = asyncio.run(_run())

    if args.json_output:
        result = {
            "status": run.status.value,
            "output": run.output_text,
            "error": run.error_message,
            "duration_ms": run.duration_ms,
            "tokens": {"input": run.input_tokens, "output": run.output_tokens},
            "cost_usd": run.total_cost_usd,
            "model": run.model_used,
        }
        print(json_mod.dumps(result, indent=2))
    elif args.print_only:
        if run.output_text:
            print(run.output_text)
        if run.error_message:
            print(run.error_message, file=sys.stderr)
    else:
        print(f"\nStatus: {run.status.value}")
        if run.duration_ms is not None:
            print(f"Duration: {run.duration_ms}ms")
        if run.model_used:
            print(f"Model used: {run.model_used}")
        print(f"Tokens: {run.input_tokens} in / {run.output_tokens} out")
        if run.total_cost_usd:
            print(f"Cost: ${run.total_cost_usd:.4f}")
        if run.output_text:
            print(f"\n{run.output_text}")
        if run.error_message:
            print(f"\nError: {run.error_message}", file=sys.stderr)

    return 0 if run.status.value == "completed" else 1


def cmd_agents() -> int:
    """List configured agents (shortcut for engine list)."""
    from robothor.engine.config import EngineConfig

    config = EngineConfig.from_env()
    manifest_dir = config.manifest_dir

    yamls = sorted(manifest_dir.glob("*.yaml"))
    yamls = [y for y in yamls if not y.name.startswith("_")]

    if not yamls:
        print(f"No agent manifests found in {manifest_dir}")
        return 0

    print(f"{'ID':<25} {'Name':<30} {'Model':<35}")
    print("-" * 90)
    for yp in yamls:
        try:
            import yaml

            with yp.open() as f:
                data = yaml.safe_load(f) or {}
            agent_id = yp.stem
            name = data.get("name", agent_id)
            model = data.get("model", {}).get("primary", "default")
            print(f"{agent_id:<25} {name:<30} {model:<35}")
        except Exception as e:
            print(f"{yp.stem:<25} {'(error)':<30} {e}")

    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    """Show cost breakdown by querying the running engine."""
    import json as json_mod
    import urllib.request

    hours = getattr(args, "hours", 24)
    url = f"http://127.0.0.1:18800/costs?hours={hours}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json_mod.loads(resp.read())
    except Exception as e:
        print(f"Error: Could not reach engine at {url}: {e}", file=sys.stderr)
        print("Is the engine running? (robothor engine start)", file=sys.stderr)
        return 1

    print(f"Cost breakdown (last {hours}h):")
    print(f"{'Agent':<25} {'Runs':<8} {'Cost':<12}")
    print("-" * 45)
    total = 0.0
    for agent in data.get("agents", []):
        cost = agent.get("total_cost_usd", 0)
        total += cost
        print(f"{agent.get('agent_id', '?'):<25} {agent.get('run_count', 0):<8} ${cost:.4f}")
    print("-" * 45)
    print(f"{'Total':<25} {'':<8} ${total:.4f}")
    return 0


def cmd_engine(args: argparse.Namespace) -> int:
    sub = getattr(args, "engine_command", None)

    if sub == "run":
        return _cmd_engine_run(args)
    if sub == "start":
        return _cmd_engine_start()
    if sub == "stop":
        return _cmd_engine_stop()
    if sub == "status":
        return _cmd_engine_status()
    if sub == "list":
        return _cmd_engine_list()
    if sub == "history":
        return _cmd_engine_history(args)
    if sub == "workflow":
        from robothor.cli.workflow import cmd_engine_workflow

        return cmd_engine_workflow(args)
    print("Usage: robothor engine {run|start|stop|status|list|history|workflow}")
    return 0


def _cmd_engine_run(args: argparse.Namespace) -> int:
    """Run a single agent and print the result."""
    import asyncio
    from datetime import UTC, datetime

    from robothor.engine.config import EngineConfig, load_agent_config
    from robothor.engine.models import TriggerType

    config = EngineConfig.from_env()
    agent_id = args.agent_id
    trigger = TriggerType(args.trigger) if args.trigger != "manual" else TriggerType.MANUAL

    # Deep mode: bypass agent loop, call RLM directly
    if getattr(args, "deep", False):
        return _cmd_engine_run_deep(args, config)

    agent_config = load_agent_config(agent_id, config.manifest_dir)
    if not agent_config:
        print(f"Error: Agent '{agent_id}' not found in {config.manifest_dir}")
        return 1

    # Build message
    message = args.message
    if not message:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        message = (
            f"Current time: {now}\n\n"
            f"You are {agent_config.name} ({agent_config.id}). "
            f"Execute your scheduled tasks as described in your instructions."
        )

    print(f"Running agent: {agent_config.name} ({agent_id})")
    print(f"Model: {agent_config.model_primary}")
    print(f"Tools: {len(agent_config.tools_allowed)} allowed")
    print()

    async def _run() -> Any:
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(config)
        return await runner.execute(
            agent_id=agent_id,
            message=message,
            trigger_type=trigger,
            agent_config=agent_config,
        )

    run = asyncio.run(_run())

    print(f"Status: {run.status.value}")
    print(f"Duration: {run.duration_ms}ms")
    print(f"Model: {run.model_used}")
    print(f"Tokens: {run.input_tokens} in / {run.output_tokens} out")
    print(f"Steps: {len(run.steps)}")
    print()

    if run.output_text:
        print("--- Output ---")
        print(run.output_text)
    if run.error_message:
        print("--- Error ---")
        print(run.error_message)

    return 0 if run.status.value == "completed" else 1


def _cmd_engine_run_deep(args: argparse.Namespace, config: Any) -> int:
    """Run deep reasoning (RLM) from the CLI."""
    import asyncio
    import time

    message = args.message
    if not message:
        print("Error: --deep requires --message/-m")
        return 1

    print(f"Deep reasoning: {message[:80]}{'...' if len(message) > 80 else ''}")
    print()

    start = time.monotonic()

    async def _run() -> Any:
        from robothor.engine.runner import AgentRunner

        runner = AgentRunner(config)

        async def on_progress(progress: dict[str, Any]) -> None:
            elapsed = progress.get("elapsed_s", 0)
            sys.stdout.write(f"\r... {elapsed}s elapsed")
            sys.stdout.flush()

        return await runner.execute_deep(
            query=message,
            on_progress=on_progress,
        )

    run = asyncio.run(_run())

    elapsed = time.monotonic() - start
    sys.stdout.write("\r" + " " * 40 + "\r")  # Clear progress line

    print(f"Status: {run.status.value}")
    print(f"Duration: {elapsed:.1f}s")
    print(f"Cost: ${run.total_cost_usd:.4f}")
    print(f"Steps: {len(run.steps)}")
    print()

    if run.output_text:
        print("--- Output ---")
        print(run.output_text)
    if run.error_message:
        print("--- Error ---")
        print(run.error_message)

    return 0 if run.status.value == "completed" else 1


def _cmd_engine_start() -> int:
    """Start the engine daemon."""
    from robothor.engine.daemon import run

    run()
    return 0


def _cmd_engine_stop() -> int:
    """Stop the engine daemon via systemctl."""
    import subprocess

    result = subprocess.run(
        ["sudo", "systemctl", "stop", "robothor-engine"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("Engine stopped.")
    else:
        print(f"Failed to stop engine: {result.stderr}")
    return result.returncode


def _cmd_engine_status() -> int:
    """Show engine daemon status."""
    import httpx

    from robothor.engine.config import EngineConfig

    config = EngineConfig.from_env()
    url = f"http://127.0.0.1:{config.port}/health"

    try:
        resp = httpx.get(url, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        print(f"Engine:    {data.get('status', 'unknown')}")
        print(f"Version:   {data.get('engine_version', '?')}")
        print(f"Tenant:    {data.get('tenant_id', '?')}")
        print(f"Bot:       {'configured' if data.get('bot_configured') else 'disabled'}")
        print()
        agents = data.get("agents", {})
        if agents:
            print(f"{'Agent':<25} {'Status':<12} {'Last Run':<20} {'Duration':<10} {'Errors'}")
            print("-" * 80)
            for aid, info in agents.items():
                print(
                    f"{aid:<25} {info.get('last_status', '-'):<12} "
                    f"{info.get('last_run_at', '-'):<20} "
                    f"{info.get('last_duration_ms', '-')!s:<10} "
                    f"{info.get('consecutive_errors', 0)}"
                )
        return 0
    except Exception as e:
        print(f"Engine not running or unreachable: {e}")
        return 1


def _cmd_engine_list() -> int:
    """List configured agents from YAML manifests."""
    from robothor.engine.config import EngineConfig, load_all_manifests, manifest_to_agent_config

    config = EngineConfig.from_env()
    manifests = load_all_manifests(config.manifest_dir)

    if not manifests:
        print(f"No manifests found in {config.manifest_dir}")
        return 1

    print(f"{'Agent ID':<25} {'Name':<25} {'Cron':<20} {'Model':<35} {'Delivery'}")
    print("-" * 120)
    for m in manifests:
        ac = manifest_to_agent_config(m)
        model_short = ac.model_primary.split("/")[-1] if ac.model_primary else "-"
        print(
            f"{ac.id:<25} {ac.name:<25} {ac.cron_expr or '-':<20} "
            f"{model_short:<35} {ac.delivery_mode.value}"
        )

    print(f"\n{len(manifests)} agents configured")
    return 0


def _cmd_engine_history(args: argparse.Namespace) -> int:
    """Show recent agent runs."""
    from robothor.engine.tracking import list_runs

    try:
        runs = list_runs(agent_id=getattr(args, "agent", None), limit=args.limit)
    except Exception as e:
        print(f"Error: Cannot query runs: {e}")
        return 1

    if not runs:
        print("No runs found.")
        return 0

    print(
        f"{'Agent':<25} {'Status':<12} {'Duration':<10} {'Trigger':<10} {'Model':<20} {'Created'}"
    )
    print("-" * 100)
    for r in runs:
        duration = f"{r.get('duration_ms', 0) or 0}ms"
        model_short = (r.get("model_used") or "-").split("/")[-1]
        created = str(r.get("created_at", ""))[:19]
        print(
            f"{r['agent_id']:<25} {r['status']:<12} {duration:<10} "
            f"{r.get('trigger_type', '-'):<10} {model_short:<20} {created}"
        )

    return 0

"""
Robothor CLI — entry point for all operations.

Usage:
    robothor init           # Interactive setup wizard
    robothor serve          # Start the API server
    robothor engine         # Manage the agent engine
    robothor status         # Show system status
    robothor mcp            # Start the MCP server
    robothor version        # Show version
    robothor migrate        # Run database migrations
    robothor pipeline       # (coming in v0.2)
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="robothor",
        description="Robothor — An AI brain with persistent memory, vision, and self-healing.",
    )
    parser.add_argument("--version", action="store_true", help="Show version and exit")

    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init", help="Interactive setup wizard")
    init_parser.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode")
    init_parser.add_argument("--docker", action="store_true", help="Use Docker for infrastructure")
    init_parser.add_argument("--skip-models", action="store_true", help="Skip Ollama model pulling")
    init_parser.add_argument("--skip-db", action="store_true", help="Skip database migration")
    init_parser.add_argument("--workspace", type=str, help="Workspace dir (default: ~/robothor)")

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="Run database migrations")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="Print SQL without executing"
    )
    migrate_parser.add_argument(
        "--check", action="store_true", help="Check if required tables exist"
    )

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the API server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    serve_parser.add_argument("--port", type=int, default=9099, help="Port")

    # mcp
    subparsers.add_parser("mcp", help="Start the MCP server (stdio transport)")

    # status
    subparsers.add_parser("status", help="Show system status")

    # pipeline (stub — v0.2)
    pipeline_parser = subparsers.add_parser(
        "pipeline", help="Run intelligence pipeline (coming in v0.2)"
    )
    pipeline_parser.add_argument(
        "--tier",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="Pipeline tier (1=ingest, 2=analysis, 3=deep)",
    )


    # version
    subparsers.add_parser("version", help="Show version")

    # engine
    eng_parser = subparsers.add_parser("engine", help="Manage the agent engine")
    eng_sub = eng_parser.add_subparsers(dest="engine_command")
    eng_run = eng_sub.add_parser("run", help="Run a single agent")
    eng_run.add_argument("agent_id", help="Agent ID (from YAML manifest)")
    eng_run.add_argument("--message", "-m", default=None, help="User message (default: cron payload)")
    eng_run.add_argument("--trigger", default="manual", help="Trigger type")
    eng_sub.add_parser("start", help="Start the engine daemon")
    eng_sub.add_parser("stop", help="Stop the engine daemon")
    eng_sub.add_parser("status", help="Show engine status")
    eng_sub.add_parser("list", help="List configured agents")
    eng_history = eng_sub.add_parser("history", help="Show recent agent runs")
    eng_history.add_argument("--agent", help="Filter by agent ID")
    eng_history.add_argument("--limit", type=int, default=20, help="Max results")

    args = parser.parse_args(argv)

    if args.version or args.command == "version":
        from robothor import __version__

        print(f"robothor {__version__}")
        return 0

    if args.command == "init":
        return _cmd_init(args)
    elif args.command == "migrate":
        return _cmd_migrate(args)
    elif args.command == "serve":
        return _cmd_serve(args)
    elif args.command == "mcp":
        return _cmd_mcp()
    elif args.command == "status":
        return _cmd_status(args)
    elif args.command == "pipeline":
        return _cmd_pipeline(args)
    elif args.command == "engine":
        return _cmd_engine(args)
    else:
        parser.print_help()
        return 0


def _cmd_init(args: argparse.Namespace) -> int:
    from robothor.setup import run_init

    return run_init(args)


def _find_migration_sql() -> str | None:
    """Find the migration SQL file bundled with the package."""
    from pathlib import Path

    # Bundled in wheel via force-include
    bundled = Path(__file__).parent / "migrations" / "001_init.sql"
    if bundled.exists():
        return bundled.read_text()

    # Development: look in infra/migrations relative to repo root
    repo_root = Path(__file__).parent.parent
    dev_path = repo_root / "infra" / "migrations" / "001_init.sql"
    if dev_path.exists():
        return dev_path.read_text()

    return None


# Required tables that must exist for a working Robothor installation
REQUIRED_TABLES = [
    "memory_facts",
    "memory_entities",
    "memory_relations",
    "short_term_memory",
    "long_term_memory",
    "agent_memory_blocks",
    "contact_identifiers",
    "ingested_items",
    "ingestion_watermarks",
    "audit_log",
    "crm_people",
    "crm_companies",
    "crm_notes",
    "crm_tasks",
    "crm_conversations",
    "crm_messages",
    "telemetry",
]


def _cmd_migrate(args: argparse.Namespace) -> int:
    sql = _find_migration_sql()
    if sql is None:
        print("Error: Migration SQL not found.")
        print("Expected at: robothor/migrations/001_init.sql")
        return 1

    if args.check:
        return _cmd_migrate_check()

    if args.dry_run:
        print("-- Dry run: the following SQL would be executed --")
        print(sql)
        return 0

    # Execute migration
    try:
        import psycopg2

        from robothor.config import get_config

        cfg = get_config().db
        print(f"Connecting to {cfg.host}:{cfg.port}/{cfg.name}...")
        conn = psycopg2.connect(**cfg.dict, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.close()
        print("Migration completed successfully.")

        # Verify
        return _cmd_migrate_check()

    except ImportError:
        print("Error: psycopg2 is required. Install with: pip install robothor")
        return 1
    except Exception as e:
        print(f"Error: Migration failed: {e}")
        print("Check ROBOTHOR_DB_* environment variables and ensure PostgreSQL is running.")
        return 1


def _cmd_migrate_check() -> int:
    """Check if required tables exist in the database."""
    try:
        import psycopg2

        from robothor.config import get_config

        cfg = get_config().db
        conn = psycopg2.connect(**cfg.dict, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            existing = {row[0] for row in cur.fetchall()}
        conn.close()

        missing = [t for t in REQUIRED_TABLES if t not in existing]
        if missing:
            print(f"Missing tables ({len(missing)}/{len(REQUIRED_TABLES)}):")
            for t in missing:
                print(f"  - {t}")
            print("\nRun 'robothor migrate' to create them.")
            return 1
        else:
            print(f"All {len(REQUIRED_TABLES)} required tables present.")
            return 0

    except Exception as e:
        print(f"Error: Cannot check tables: {e}")
        return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is required. Install with: pip install robothor[api]")
        return 1

    print(f"Starting Robothor RAG Orchestrator on {args.host}:{args.port}...")
    print("Agent engine runs separately: robothor engine start")
    uvicorn.run("robothor.api.orchestrator:app", host=args.host, port=args.port)
    return 0


def _cmd_mcp() -> int:
    import asyncio

    try:
        from robothor.api.mcp import run_server
    except ImportError as e:
        print(f"Error: MCP dependencies missing: {e}")
        print("Install with: pip install mcp")
        return 1

    asyncio.run(run_server())
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from robothor import __version__
    from robothor.config import get_config

    cfg = get_config()
    print(f"Robothor v{__version__}")
    print()

    # PostgreSQL
    print(f"  PostgreSQL:  {cfg.db.host}:{cfg.db.port}/{cfg.db.name}")
    try:
        import psycopg2

        conn = psycopg2.connect(**cfg.db.dict, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            pg_version = cur.fetchone()[0].split(",")[0]
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
            )
            table_count = cur.fetchone()[0]
            # Check pgvector
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            pgvector_ver = row[0] if row else "not installed"
        conn.close()
        print(f"               Connected — {pg_version}")
        print(f"               pgvector {pgvector_ver}, {table_count} tables")
    except Exception as e:
        print(f"               UNREACHABLE — {e}")

    # Redis
    print(f"  Redis:       {cfg.redis.host}:{cfg.redis.port}")
    try:
        import redis as redis_lib

        r = redis_lib.Redis(
            host=cfg.redis.host,
            port=cfg.redis.port,
            db=cfg.redis.db,
            password=cfg.redis.password or None,
            socket_connect_timeout=3,
        )
        info: dict = r.info("server")  # type: ignore[assignment]
        print(f"               Connected — Redis {info.get('redis_version', '?')}")
    except Exception as e:
        print(f"               UNREACHABLE — {e}")

    # Ollama
    print(f"  Ollama:      {cfg.ollama.base_url}")
    try:
        import httpx

        resp = httpx.get(f"{cfg.ollama.base_url}/api/tags", timeout=3)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        print(f"               Connected — {len(models)} model(s) loaded")
    except Exception as e:
        print(f"               UNREACHABLE — {e}")

    # Engine
    print(f"  Engine:      port 18800")
    try:
        import httpx as _httpx

        resp = _httpx.get("http://127.0.0.1:18800/health", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        agent_count = len(data.get("agents", {}))
        print(f"               {data.get('status', '?')} — {agent_count} agents, bot={'yes' if data.get('bot_configured') else 'no'}")
    except Exception:
        print(f"               Not running — start with: robothor engine start")

    print()
    print(f"  Workspace:   {cfg.workspace}")
    return 0



def _cmd_pipeline(args: argparse.Namespace) -> int:
    print(f"Pipeline tier {args.tier} not yet implemented. Coming in v0.2.")
    return 0


def _cmd_engine(args: argparse.Namespace) -> int:
    sub = getattr(args, "engine_command", None)

    if sub == "run":
        return _cmd_engine_run(args)
    elif sub == "start":
        return _cmd_engine_start()
    elif sub == "stop":
        return _cmd_engine_stop()
    elif sub == "status":
        return _cmd_engine_status()
    elif sub == "list":
        return _cmd_engine_list()
    elif sub == "history":
        return _cmd_engine_history(args)
    else:
        print("Usage: robothor engine {run|start|stop|status|list|history}")
        return 0


def _cmd_engine_run(args: argparse.Namespace) -> int:
    """Run a single agent and print the result."""
    import asyncio
    from datetime import UTC, datetime

    from robothor.engine.config import EngineConfig, build_system_prompt, load_agent_config
    from robothor.engine.models import TriggerType

    config = EngineConfig.from_env()
    agent_id = args.agent_id
    trigger = TriggerType(args.trigger) if args.trigger != "manual" else TriggerType.MANUAL

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

    async def _run():
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
        print("─── Output ───")
        print(run.output_text)
    if run.error_message:
        print("─── Error ───")
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
        capture_output=True, text=True,
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
            print("─" * 80)
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
    print("─" * 120)
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

    print(f"{'Agent':<25} {'Status':<12} {'Duration':<10} {'Trigger':<10} {'Model':<20} {'Created'}")
    print("─" * 100)
    for r in runs:
        duration = f"{r.get('duration_ms', 0) or 0}ms"
        model_short = (r.get("model_used") or "-").split("/")[-1]
        created = str(r.get("created_at", ""))[:19]
        print(
            f"{r['agent_id']:<25} {r['status']:<12} {duration:<10} "
            f"{r.get('trigger_type', '-'):<10} {model_short:<20} {created}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

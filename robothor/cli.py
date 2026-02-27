"""
Robothor CLI — entry point for all operations.

Usage:
    robothor init           # Interactive setup wizard
    robothor serve          # Start the API server (+ gateway)
    robothor status         # Show system status
    robothor mcp            # Start the MCP server
    robothor version        # Show version
    robothor migrate        # Run database migrations
    robothor gateway        # Manage the OpenClaw gateway
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

    # gateway
    gw_parser = subparsers.add_parser("gateway", help="Manage the OpenClaw gateway")
    gw_sub = gw_parser.add_subparsers(dest="gateway_command")
    gw_sub.add_parser("build", help="Build gateway (pnpm install && pnpm build)")
    gw_sub.add_parser("rebuild", help="Clean build")
    gw_sub.add_parser("status", help="Show gateway status")
    gw_start = gw_sub.add_parser("start", help="Start gateway process")
    gw_start.add_argument("--foreground", action="store_true", help="Run in foreground")
    gw_sub.add_parser("stop", help="Stop gateway process")
    gw_sub.add_parser("restart", help="Restart gateway process")
    gw_config = gw_sub.add_parser("config", help="Regenerate config from manifests")
    gw_config.add_argument("--dry-run", action="store_true", help="Print without writing")
    gw_sub.add_parser("sync", help="Pull upstream OpenClaw changes")
    gw_sub.add_parser("install-service", help="Install systemd service unit")
    gw_migrate = gw_sub.add_parser("migrate", help="Migrate from ~/moltbot/ layout")
    gw_migrate.add_argument("--dry-run", action="store_true", help="Print without executing")

    # init flags
    init_parser.add_argument(
        "--skip-gateway", action="store_true", help="Skip gateway build"
    )

    # serve flags
    serve_parser.add_argument(
        "--no-gateway", action="store_true", help="Don't start gateway alongside orchestrator"
    )

    # version
    subparsers.add_parser("version", help="Show version")

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
    elif args.command == "gateway":
        return _cmd_gateway(args)
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
    import signal

    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is required. Install with: pip install robothor[api]")
        return 1

    gw_proc = None
    if not getattr(args, "no_gateway", False):
        from robothor.gateway.process import GatewayProcess

        gw = GatewayProcess()
        try:
            pid = gw.start(foreground=False)
            print(f"  Gateway started (PID {pid})")
            gw_proc = gw
        except FileNotFoundError as e:
            print(f"  Gateway not available: {e}")
            print("  Continuing without gateway. Run 'robothor gateway build' first.")

    def _cleanup(signum, frame):
        if gw_proc:
            gw_proc.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    print(f"Starting Robothor RAG Orchestrator on {args.host}:{args.port}...")
    try:
        uvicorn.run("robothor.api.orchestrator:app", host=args.host, port=args.port)
    finally:
        if gw_proc:
            gw_proc.stop()
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

    # Gateway
    from robothor.gateway.manager import GatewayManager
    from robothor.gateway.process import GatewayProcess

    gw_mgr = GatewayManager(
        gateway_dir=cfg.gateway.gateway_dir,
        config_dir=cfg.gateway.config_dir,
    )
    gw_status = gw_mgr.status()
    gw_proc = GatewayProcess(
        gateway_dir=cfg.gateway.gateway_dir,
        config_dir=cfg.gateway.config_dir,
        port=cfg.gateway.port,
    )
    health = gw_proc.health_check()

    print(f"  Gateway:     v{gw_status.version} (OpenClaw)")
    if health["healthy"]:
        print(f"               Port {cfg.gateway.port}, healthy")
    elif gw_status.built:
        print(f"               Built, not running")
    else:
        print(f"               Not built — run 'robothor gateway build'")

    print()
    print(f"  Workspace:   {cfg.workspace}")
    return 0


def _cmd_gateway(args: argparse.Namespace) -> int:
    from robothor.config import get_config
    from robothor.gateway.manager import GatewayManager
    from robothor.gateway.process import GatewayProcess

    cfg = get_config()
    mgr = GatewayManager(
        gateway_dir=cfg.gateway.gateway_dir,
        config_dir=cfg.gateway.config_dir,
    )
    proc = GatewayProcess(
        gateway_dir=cfg.gateway.gateway_dir,
        config_dir=cfg.gateway.config_dir,
        port=cfg.gateway.port,
    )

    sub = getattr(args, "gateway_command", None)

    if sub == "build":
        print("Building gateway...")
        prereqs = mgr.check_prerequisites()
        for p in prereqs:
            mark = "+" if p.ok else "x"
            print(f"  {mark} {p.name} {p.version}" if p.ok else f"  {mark} {p.name}: {p.hint}")
        if not all(p.ok for p in prereqs):
            print("Prerequisites not met.")
            return 1
        if mgr.build():
            print(f"Build complete — v{mgr.get_version()}")
            return 0
        else:
            print("Build failed. Check logs.")
            return 1

    elif sub == "rebuild":
        print("Rebuilding gateway (clean)...")
        if mgr.rebuild():
            print(f"Rebuild complete — v{mgr.get_version()}")
            return 0
        else:
            print("Rebuild failed.")
            return 1

    elif sub == "status":
        status = mgr.status()
        health = proc.health_check()
        print(f"  Version:     {status.version}")
        print(f"  Built:       {'yes' if status.built else 'no'}")
        print(f"  Running:     {'yes' if proc.is_running() else 'no'}")
        print(f"  Healthy:     {'yes' if health['healthy'] else 'no'}")
        print(f"  Gateway dir: {status.gateway_dir}")
        print(f"  Config dir:  {status.config_dir}")
        for p in status.prereqs:
            mark = "+" if p.ok else "x"
            detail = p.version if p.ok else p.hint
            print(f"  {mark} {p.name}: {detail}")
        return 0

    elif sub == "start":
        fg = getattr(args, "foreground", False)
        try:
            pid = proc.start(foreground=fg)
            if not fg:
                print(f"Gateway started (PID {pid})")
        except FileNotFoundError as e:
            print(f"Error: {e}")
            return 1
        return 0

    elif sub == "stop":
        if proc.stop():
            print("Gateway stopped.")
        else:
            print("Gateway not running.")
        return 0

    elif sub == "restart":
        proc.stop()
        try:
            pid = proc.start()
            print(f"Gateway restarted (PID {pid})")
        except FileNotFoundError as e:
            print(f"Error: {e}")
            return 1
        return 0

    elif sub == "config":
        dry_run = getattr(args, "dry_run", False)
        try:
            from robothor.gateway.config_gen import generate_and_deploy

            return generate_and_deploy(
                manifest_dir=cfg.workspace / "docs" / "agents",
                config_dir=cfg.gateway.config_dir,
                dry_run=dry_run,
            )
        except ImportError:
            print("Config generator not available.")
            return 1

    elif sub == "sync":
        print("Syncing upstream OpenClaw...")
        if mgr.sync_upstream():
            print("Sync complete.")
            return 0
        else:
            print("Sync failed — check for merge conflicts.")
            return 1

    elif sub == "install-service":
        try:
            path = proc.install_systemd_unit()
            print(f"Service installed: {path}")
            print("Start with: sudo systemctl start robothor-gateway")
            return 0
        except Exception as e:
            print(f"Error: {e}")
            return 1

    elif sub == "migrate":
        from robothor.gateway.migrate import migrate

        return migrate(dry_run=getattr(args, "dry_run", False))

    else:
        print("Usage: robothor gateway {build|rebuild|status|start|stop|restart|config|sync|install-service|migrate}")
        return 0


def _cmd_pipeline(args: argparse.Namespace) -> int:
    print(f"Pipeline tier {args.tier} not yet implemented. Coming in v0.2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

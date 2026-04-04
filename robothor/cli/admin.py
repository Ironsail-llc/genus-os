"""Admin, config, infrastructure, and service commands."""

from __future__ import annotations

import argparse  # noqa: TC003
import os
from typing import Any

# Service names for start/stop
_SERVICES = ["robothor-engine", "robothor-bridge", "robothor-voice"]


# Required tables that must exist for a working Genus OS installation
REQUIRED_TABLES = [
    "memory_facts",
    "memory_entities",
    "memory_relations",
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
    "vault_secrets",
    "federation_identity",
    "federation_connections",
    "federation_events",
]


def _find_migration_sql() -> str | None:
    """Find the migration SQL file bundled with the package."""
    from pathlib import Path

    # Bundled in wheel via force-include
    bundled = Path(__file__).parent.parent / "migrations" / "001_init.sql"
    if bundled.exists():
        return bundled.read_text()

    # Development: look in infra/migrations relative to repo root
    repo_root = Path(__file__).parent.parent.parent
    dev_path = repo_root / "infra" / "migrations" / "001_init.sql"
    if dev_path.exists():
        return dev_path.read_text()

    return None


def cmd_init(args: argparse.Namespace) -> int:
    from robothor.setup import run_init

    return run_init(args)


def cmd_migrate(args: argparse.Namespace) -> int:
    sql = _find_migration_sql()
    if sql is None:
        print("Error: Migration SQL not found.")
        print("Expected at: robothor/migrations/001_init.sql")
        return 1

    if args.check:
        return cmd_migrate_check()

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
        return cmd_migrate_check()

    except ImportError:
        print("Error: psycopg2 is required. Install with: pip install robothor")
        return 1
    except Exception as e:
        print(f"Error: Migration failed: {e}")
        print("Check ROBOTHOR_DB_* environment variables and ensure PostgreSQL is running.")
        return 1


def cmd_migrate_check() -> int:
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
        print(f"All {len(REQUIRED_TABLES)} required tables present.")
        return 0

    except Exception as e:
        print(f"Error: Cannot check tables: {e}")
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is required. Install with: pip install robothor[api]")
        return 1

    print(f"Starting Genus OS RAG Orchestrator on {args.host}:{args.port}...")
    print("Agent engine runs separately: robothor engine start")
    uvicorn.run("robothor.api.orchestrator:app", host=args.host, port=args.port)
    return 0


def cmd_mcp() -> int:
    import asyncio

    try:
        from robothor.api.mcp import run_server
    except ImportError as e:
        print(f"Error: MCP dependencies missing: {e}")
        print("Install with: pip install mcp")
        return 1

    asyncio.run(run_server())
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start all Genus OS services."""
    import subprocess

    print("  Starting Genus OS services...")
    print()
    for svc in _SERVICES:
        print(f"    {svc} ...", end=" ", flush=True)
        result = subprocess.run(
            ["sudo", "systemctl", "start", svc],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("started")
        else:
            # Service might not exist — check if unit file is present
            check = subprocess.run(
                ["systemctl", "list-unit-files", f"{svc}.service"],
                capture_output=True,
                text=True,
            )
            if svc in check.stdout:
                print(f"FAILED ({result.stderr.strip()})")
            else:
                print("skipped (not installed)")

    print()
    return cmd_status(args)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop all Genus OS services."""
    import subprocess

    print("  Stopping Genus OS services...")
    print()
    for svc in _SERVICES:
        print(f"    {svc} ...", end=" ", flush=True)
        result = subprocess.run(
            ["sudo", "systemctl", "stop", svc],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("stopped")
        else:
            check = subprocess.run(
                ["systemctl", "list-unit-files", f"{svc}.service"],
                capture_output=True,
                text=True,
            )
            if svc in check.stdout:
                print(f"FAILED ({result.stderr.strip()})")
            else:
                print("skipped (not installed)")

    print()
    print("  All services stopped.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from robothor import __version__
    from robothor.config import get_config

    cfg = get_config()
    print(f"Genus OS v{__version__}")
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
        info: dict[str, Any] = r.info("server")  # type: ignore[assignment]
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
    print("  Engine:      port 18800")
    try:
        import httpx as _httpx

        resp = _httpx.get("http://127.0.0.1:18800/health", timeout=3)
        resp.raise_for_status()
        data = resp.json()
        agent_count = len(data.get("agents", {}))
        wf_count = data.get("workflow_count", 0)
        print(
            f"               {data.get('status', '?')} — {agent_count} agents, {wf_count} workflows"
        )
    except Exception:
        print("               Not running — start with: robothor engine start")

    # Vault
    print("  Vault:      ", end="")
    try:
        from robothor.vault.dal import count_secrets

        count = int(count_secrets())
        print(f" {count} secret(s) stored")
    except Exception:
        print(" not configured — run: robothor vault init")

    # Optional services (check if ports are listening)
    _check_optional_service("TTS", cfg.tts_port, "/v1/models")

    monitoring_port = int(os.environ.get("ROBOTHOR_MONITORING_PORT", "3010"))
    _check_optional_service("Monitoring", monitoring_port, "/")

    camera_rtsp_port = int(os.environ.get("ROBOTHOR_CAMERA_RTSP_PORT", "0"))
    if camera_rtsp_port:
        _check_optional_service("Camera", camera_rtsp_port, None)

    print()
    print(f"  Workspace:   {cfg.workspace}")
    return 0


def _check_optional_service(name: str, port: int, health_path: str | None) -> None:
    """Check if an optional service is running on a given port."""
    if port == 0:
        return
    import socket

    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
        print(f"  {name + ':':<13} port {port:<10} — Connected")
    except (ConnectionRefusedError, OSError, TimeoutError):
        # Only show if profiles indicate it should be running
        profiles = os.environ.get("COMPOSE_PROFILES", "")
        if profiles:
            print(f"  {name + ':':<13} port {port:<10} — Not running")


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "validate":
        return _cmd_config_validate()
    print("Usage: robothor config validate")
    return 0


def _cmd_config_validate() -> int:
    """Run configuration validation checks."""
    from robothor.config import validate

    print("Running configuration validation...\n")
    results = validate()

    pass_count = sum(1 for _, ok, _ in results if ok)
    fail_count = sum(1 for _, ok, _ in results if not ok)

    for name, ok, detail in results:
        icon = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        print(f"  {icon} {name}: {detail}")

    print(f"\n{pass_count} passed, {fail_count} failed")
    return 0 if fail_count == 0 else 1


def cmd_pipeline(args: argparse.Namespace) -> int:
    print(f"Pipeline tier {args.tier} not yet implemented. Coming in v0.2.")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """Launch the terminal chat interface."""
    try:
        from robothor.tui import check_textual

        if not check_textual():
            print("Error: Textual is required for the TUI.")
            print("Install with: pip install robothor[tui]")
            return 1

        from robothor.tui.app import RobothorApp

        url = getattr(args, "url", "http://127.0.0.1:18800")
        session = getattr(args, "session", None)
        app = RobothorApp(engine_url=url, session_key=session)
        app.run()
        return 0

    except ImportError:
        print("Error: Textual is required for the TUI.")
        print("Install with: pip install robothor[tui]")
        return 1


def cmd_tunnel(args: argparse.Namespace) -> int:
    sub = getattr(args, "tunnel_command", None)

    if sub == "generate":
        from robothor.tunnel import generate_tunnel_config

        provider = args.provider or os.environ.get("ROBOTHOR_TUNNEL_PROVIDER", "cloudflare")
        domain = args.domain or os.environ.get("ROBOTHOR_DOMAIN", "")
        if not domain:
            print("Error: No domain set. Use --domain or ROBOTHOR_DOMAIN env var.")
            return 1
        profiles = [
            p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()
        ]
        try:
            out_path = generate_tunnel_config(provider, domain, profiles)
            print(f"Generated {provider} config: {out_path}")
            return 0
        except Exception as e:
            print(f"Error: {e}")
            return 1

    if sub == "status":
        from robothor.tunnel import check_tunnel_status

        provider = os.environ.get("ROBOTHOR_TUNNEL_PROVIDER", "none")
        if provider == "none":
            print("No tunnel configured. Set ROBOTHOR_TUNNEL_PROVIDER in .env")
            return 0
        result = check_tunnel_status(provider)
        status = "Connected" if result["connected"] else "Not connected"
        print(f"Tunnel ({provider}): {status}")
        return 0 if result["connected"] else 1

    print("Usage: robothor tunnel {generate|status}")
    return 0

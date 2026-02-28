"""
Robothor CLI — entry point for all operations.

Usage:
    robothor                # Launch the TUI (terminal chat)
    robothor tui            # Launch the TUI (explicit)
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
import os
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

    # tui
    tui_parser = subparsers.add_parser("tui", help="Launch the terminal chat interface")
    tui_parser.add_argument("--url", default="http://127.0.0.1:18800", help="Engine URL")
    tui_parser.add_argument(
        "--session", default=None, help="Session key (auto-generated if omitted)"
    )

    # tunnel
    tunnel_parser = subparsers.add_parser("tunnel", help="Manage tunnel/ingress config")
    tunnel_sub = tunnel_parser.add_subparsers(dest="tunnel_command")
    tunnel_gen = tunnel_sub.add_parser(
        "generate", help="Generate tunnel config from enabled services"
    )
    tunnel_gen.add_argument(
        "--provider", default=None, help="Provider: cloudflare, caddy (default: from env)"
    )
    tunnel_gen.add_argument("--domain", default=None, help="Domain (default: from env)")
    tunnel_sub.add_parser("status", help="Check tunnel connectivity")

    # vault
    vault_parser = subparsers.add_parser("vault", help="Manage the secret vault")
    vault_sub = vault_parser.add_subparsers(dest="vault_command")
    vault_sub.add_parser("init", help="Generate vault master key")
    vault_set_p = vault_sub.add_parser("set", help="Store a secret")
    vault_set_p.add_argument("key", help="Secret key (e.g. telegram/bot_token)")
    vault_set_p.add_argument("value", nargs="?", default=None, help="Value (prompted if omitted)")
    vault_set_p.add_argument(
        "--category", default="credential", help="Category (default: credential)"
    )
    vault_get_p = vault_sub.add_parser("get", help="Retrieve a secret")
    vault_get_p.add_argument("key", help="Secret key")
    vault_list_p = vault_sub.add_parser("list", help="List secret keys")
    vault_list_p.add_argument("--category", default=None, help="Filter by category")
    vault_del_p = vault_sub.add_parser("delete", help="Delete a secret")
    vault_del_p.add_argument("key", help="Secret key to delete")
    vault_import_p = vault_sub.add_parser("import-env", help="Import secrets from .env file")
    vault_import_p.add_argument("file", help="Path to .env file")
    vault_sub.add_parser("export-env", help="Export all secrets as KEY=VALUE")

    # agent
    agent_parser = subparsers.add_parser("agent", help="Agent management")
    agent_sub = agent_parser.add_subparsers(dest="agent_command")
    scaffold_parser = agent_sub.add_parser("scaffold", help="Scaffold a new agent")
    scaffold_parser.add_argument("agent_id", help="Agent ID (kebab-case, e.g., ticket-router)")
    scaffold_parser.add_argument("--description", "-d", default="", help="One-line description")

    # engine
    eng_parser = subparsers.add_parser("engine", help="Manage the agent engine")
    eng_sub = eng_parser.add_subparsers(dest="engine_command")
    eng_run = eng_sub.add_parser("run", help="Run a single agent")
    eng_run.add_argument("agent_id", help="Agent ID (from YAML manifest)")
    eng_run.add_argument(
        "--message", "-m", default=None, help="User message (default: cron payload)"
    )
    eng_run.add_argument("--trigger", default="manual", help="Trigger type")
    eng_sub.add_parser("start", help="Start the engine daemon")
    eng_sub.add_parser("stop", help="Stop the engine daemon")
    eng_sub.add_parser("status", help="Show engine status")
    eng_sub.add_parser("list", help="List configured agents")
    eng_history = eng_sub.add_parser("history", help="Show recent agent runs")
    eng_history.add_argument("--agent", help="Filter by agent ID")
    eng_history.add_argument("--limit", type=int, default=20, help="Max results")

    # engine workflow subcommands
    eng_wf = eng_sub.add_parser("workflow", help="Manage workflows")
    eng_wf_sub = eng_wf.add_subparsers(dest="workflow_command")
    eng_wf_sub.add_parser("list", help="List loaded workflows")
    eng_wf_run = eng_wf_sub.add_parser("run", help="Run a workflow")
    eng_wf_run.add_argument("workflow_id", help="Workflow ID")

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
    elif args.command == "tunnel":
        return _cmd_tunnel(args)
    elif args.command == "vault":
        return _cmd_vault(args)
    elif args.command == "agent":
        return _cmd_agent(args)
    elif args.command == "engine":
        return _cmd_engine(args)
    elif args.command == "tui":
        return _cmd_tui(args)
    elif args.command is None:
        # No subcommand — launch the TUI
        return _cmd_tui(args)
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
    "vault_secrets",
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

        count = count_secrets()
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


def _cmd_pipeline(args: argparse.Namespace) -> int:
    print(f"Pipeline tier {args.tier} not yet implemented. Coming in v0.2.")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
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


def _cmd_tunnel(args: argparse.Namespace) -> int:
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


def _cmd_vault(args: argparse.Namespace) -> int:
    sub = getattr(args, "vault_command", None)
    from pathlib import Path

    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))

    if sub == "init":
        from robothor.vault.crypto import init_master_key

        key_path = init_master_key(workspace)
        print(f"Vault master key: {key_path}")
        return 0

    if sub == "set":
        import getpass

        from robothor.vault import set as vault_set
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        value = args.value
        if value is None:
            value = getpass.getpass(f"Value for {args.key}: ")
        vault_set(args.key, value, category=args.category)
        print(f"Stored: {args.key} [{args.category}]")
        return 0

    if sub == "get":
        from robothor.vault import get as vault_get
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        value = vault_get(args.key)
        if value is None:
            print(f"Not found: {args.key}")
            return 1
        print(value)
        return 0

    if sub == "list":
        from robothor.vault import list as vault_list
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        keys = vault_list(category=args.category)
        if not keys:
            print("Vault is empty.")
        else:
            for k in keys:
                print(f"  {k}")
            print(f"\n{len(keys)} secret(s)")
        return 0

    if sub == "delete":
        from robothor.vault import delete as vault_delete

        deleted = vault_delete(args.key)
        print(f"{'Deleted' if deleted else 'Not found'}: {args.key}")
        return 0 if deleted else 1

    if sub == "import-env":
        from robothor.vault import set as vault_set
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        env_path = Path(args.file)
        if not env_path.exists():
            print(f"Error: File not found: {env_path}")
            return 1
        count = 0
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = (
                key.strip().lower().replace("_", "/", 1)
            )  # TELEGRAM_BOT_TOKEN → telegram/bot_token
            value = value.strip().strip("'\"")
            if value:
                vault_set(key, value, category="credential")
                count += 1
        print(f"Imported {count} secret(s)")
        return 0

    if sub == "export-env":
        from robothor.vault import export_env
        from robothor.vault.crypto import get_master_key

        try:
            get_master_key(workspace)
        except FileNotFoundError:
            print("Error: No vault master key. Run 'robothor vault init' first.")
            return 1
        secrets = export_env()
        for k, v in sorted(secrets.items()):
            print(f"{k}={v}")
        return 0

    print("Usage: robothor vault {init|set|get|list|delete|import-env|export-env}")
    return 0


def _cmd_agent(args: argparse.Namespace) -> int:
    sub = getattr(args, "agent_command", None)
    if sub == "scaffold":
        return _cmd_agent_scaffold(args)
    else:
        print("Usage: robothor agent {scaffold}")
        return 0


def _cmd_agent_scaffold(args: argparse.Namespace) -> int:
    """Scaffold a new agent — create manifest + instruction file from templates."""
    import re
    from datetime import UTC, datetime
    from pathlib import Path

    agent_id = args.agent_id
    description = args.description or f"A new agent: {agent_id}"

    # Validate kebab-case
    if not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", agent_id):
        print(f"Error: agent_id must be kebab-case (e.g., 'ticket-router'), got: {agent_id}")
        return 1

    # Derive names
    agent_name = agent_id.replace("-", " ").title()
    instruction_filename = agent_id.upper().replace("-", "_") + ".md"
    version = datetime.now(UTC).strftime("%Y-%m-%d")
    status_file = f"brain/memory/{agent_id}-status.md"

    # Paths
    workspace = Path.home() / "robothor"
    manifest_dir = workspace / "docs" / "agents"
    brain_dir = workspace / "brain"
    template_dir = workspace / "templates"

    manifest_path = manifest_dir / f"{agent_id}.yaml"
    instruction_path = brain_dir / instruction_filename

    # Check for conflicts
    if manifest_path.exists():
        print(f"Error: Manifest already exists: {manifest_path}")
        return 1
    if instruction_path.exists():
        print(f"Error: Instruction file already exists: {instruction_path}")
        return 1

    # Load templates
    manifest_template = template_dir / "agent-manifest.yaml"
    instruction_template = template_dir / "agent-instructions.md"

    if not manifest_template.exists():
        print(f"Error: Template not found: {manifest_template}")
        return 1
    if not instruction_template.exists():
        print(f"Error: Template not found: {instruction_template}")
        return 1

    replacements = {
        "{AGENT_ID}": agent_id,
        "{AGENT_NAME}": agent_name,
        "{DESCRIPTION}": description,
        "{VERSION}": version,
        "{INSTRUCTION_FILENAME}": instruction_filename,
        "{STATUS_FILE}": status_file,
    }

    # Write manifest
    manifest_content = manifest_template.read_text()
    for placeholder, value in replacements.items():
        manifest_content = manifest_content.replace(placeholder, value)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest_content)

    # Write instruction file
    instruction_content = instruction_template.read_text()
    for placeholder, value in replacements.items():
        instruction_content = instruction_content.replace(placeholder, value)
    brain_dir.mkdir(parents=True, exist_ok=True)
    instruction_path.write_text(instruction_content)

    print(f"Scaffolded agent: {agent_name} ({agent_id})")
    print()
    print(f"  Manifest:     {manifest_path}")
    print(f"  Instructions: {instruction_path}")
    print()
    print("Next steps:")
    print(f"  1. Edit the manifest:     {manifest_path}")
    print(f"  2. Edit the instructions: {instruction_path}")
    print(f"  3. Validate:              python scripts/validate_agents.py --agent {agent_id}")
    print("  4. Restart engine:        sudo systemctl restart robothor-engine")
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
    elif sub == "workflow":
        return _cmd_engine_workflow(args)
    else:
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

    print(
        f"{'Agent':<25} {'Status':<12} {'Duration':<10} {'Trigger':<10} {'Model':<20} {'Created'}"
    )
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


def _cmd_engine_workflow(args: argparse.Namespace) -> int:
    """Manage workflows."""
    wf_sub = getattr(args, "workflow_command", None)

    if wf_sub == "list":
        return _cmd_workflow_list()
    elif wf_sub == "run":
        return _cmd_workflow_run(args)
    else:
        print("Usage: robothor engine workflow {list|run}")
        return 0


def _cmd_workflow_list() -> int:
    """List loaded workflow definitions."""
    from robothor.engine.config import EngineConfig
    from robothor.engine.workflow import WorkflowEngine

    config = EngineConfig.from_env()

    # We don't need a full runner just to list workflows
    class _StubRunner:
        pass

    engine = WorkflowEngine(config, _StubRunner())
    engine.load_workflows(config.workflow_dir)

    workflows = engine.list_workflows()
    if not workflows:
        print(f"No workflows found in {config.workflow_dir}")
        return 1

    print(f"{'Workflow ID':<25} {'Name':<30} {'Steps':<8} {'Triggers'}")
    print("-" * 90)
    for wf in workflows:
        trigger_strs = []
        for t in wf.triggers:
            if t.type == "hook":
                trigger_strs.append(f"hook:{t.stream}.{t.event_type}")
            elif t.type == "cron":
                trigger_strs.append(f"cron:{t.cron}")
        print(f"{wf.id:<25} {wf.name:<30} {len(wf.steps):<8} {', '.join(trigger_strs)}")

    print(f"\n{len(workflows)} workflows configured")
    return 0


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    """Run a workflow by ID."""
    import asyncio

    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.engine.workflow import WorkflowEngine

    config = EngineConfig.from_env()
    runner = AgentRunner(config)
    engine = WorkflowEngine(config, runner)
    engine.load_workflows(config.workflow_dir)

    workflow_id = args.workflow_id
    wf = engine.get_workflow(workflow_id)
    if not wf:
        print(f"Error: Workflow '{workflow_id}' not found in {config.workflow_dir}")
        return 1

    print(f"Running workflow: {wf.name} ({wf.id})")
    print(f"Steps: {len(wf.steps)}")
    print()

    async def _run():
        return await engine.execute(
            workflow_id=workflow_id,
            trigger_type="manual",
            trigger_detail="cli",
        )

    run = asyncio.run(_run())

    print(f"Status: {run.status.value}")
    print(f"Duration: {run.duration_ms}ms")
    print(f"Steps executed: {len(run.step_results)}")
    print()

    for result in run.step_results:
        icon = {
            "completed": "+",
            "failed": "!",
            "skipped": "~",
        }.get(result.status.value, "?")
        line = f"  [{icon}] {result.step_id} ({result.step_type.value}): {result.status.value}"
        if result.duration_ms:
            line += f" ({result.duration_ms}ms)"
        if result.condition_branch:
            line += f" -> {result.condition_branch}"
        if result.error_message:
            line += f" ERROR: {result.error_message}"
        print(line)

    if run.error_message:
        print(f"\nError: {run.error_message}")

    return 0 if run.status.value == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())

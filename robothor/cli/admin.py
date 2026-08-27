"""Admin, config, infrastructure, and service commands."""

from __future__ import annotations

import argparse  # noqa: TC003
import os
from pathlib import Path
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
    """Find the legacy baseline SQL (backward-compatible helper)."""
    from pathlib import Path

    # Bundled in wheel via force-include
    bundled = Path(__file__).parent.parent / "migrations" / "infra" / "001_init.sql"
    if bundled.exists():
        return bundled.read_text(encoding="utf-8")

    # Development: look in infra/migrations relative to repo root
    repo_root = Path(__file__).parent.parent.parent
    dev_path = repo_root / "infra" / "migrations" / "001_init.sql"
    if dev_path.exists():
        return dev_path.read_text(encoding="utf-8")

    return None


def cmd_init(args: argparse.Namespace) -> int:
    from robothor.setup import run_init

    return run_init(args)


def cmd_migrate(args: argparse.Namespace) -> int:
    try:
        import psycopg2

        from robothor.config import get_config
        from robothor.db.migrate import MigrationError, apply

        if args.dry_run:
            print("-- Dry run: canonical migration chain (no database connection) --")
            apply(dry_run=True)
            return 0

        cfg = get_config().db
        print(f"Connecting to {cfg.host}:{cfg.port}/{cfg.name}...")
        conn = psycopg2.connect(**cfg.dict, connect_timeout=5)
        try:
            if args.check:
                return cmd_migrate_check(connection=conn)

            applied = apply(connection=conn)
            print(f"Migration completed successfully ({len(applied)} applied).")
            return cmd_migrate_check(connection=conn)
        finally:
            conn.close()

    except ImportError:
        print("Error: psycopg2 is required. Install with: pip install robothor")
        return 1
    except MigrationError as e:
        print(f"Error: Migration safety check failed: {e}")
        return 1
    except Exception as e:
        print(f"Error: Migration failed: {e}")
        print("Check ROBOTHOR_DB_* environment variables and ensure PostgreSQL is running.")
        return 1


def cmd_migrate_check(*, connection: Any | None = None) -> int:
    """Check canonical migration state and required runtime tables."""

    conn = connection
    owns_connection = connection is None
    try:
        import psycopg2

        from robothor.config import get_config
        from robothor.db.migrate import status

        if conn is None:
            cfg = get_config().db
            conn = psycopg2.connect(**cfg.dict, connect_timeout=5)

        migration_rows = status(connection=conn)
        incomplete = [row for row in migration_rows if row["status"] != "applied"]
        if incomplete:
            print(f"Schema is not current ({len(incomplete)} migration issue(s)):")
            for row in incomplete:
                print(f"  - {row['migration_id']}: {row['status']}")
            return 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            existing = {row[0] for row in cur.fetchall()}

        missing = [t for t in REQUIRED_TABLES if t not in existing]
        if missing:
            print(f"Missing tables ({len(missing)}/{len(REQUIRED_TABLES)}):")
            for t in missing:
                print(f"  - {t}")
            print("\nRun 'robothor migrate' to create them.")
            return 1
        print(f"All {len(REQUIRED_TABLES)} required tables present.")
        print(f"All {len(migration_rows)} canonical migrations applied without drift.")
        return 0

    except Exception as e:
        print(f"Error: Cannot check tables: {e}")
        return 1
    finally:
        if owns_connection and conn is not None:
            conn.close()


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


# In-process alternatives for services that have one, shown when their
# systemd unit isn't installed (e.g. a plain pip install with no infra setup).
_IN_PROCESS_ALTERNATIVES = {
    "robothor-engine": "robothor engine start",
}


def _in_process_hint(svc: str) -> str:
    alt = _IN_PROCESS_ALTERNATIVES.get(svc)
    return f" — run: {alt}" if alt else ""


def cmd_start(args: argparse.Namespace) -> int:
    """Start all Genus OS services."""
    import subprocess

    print("  Starting Genus OS services...")
    print()
    for svc in _SERVICES:
        print(f"    {svc} ...", end=" ", flush=True)
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "start", svc],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            # No systemd/sudo on this box at all — nothing to shell out to.
            print(f"skipped (systemd not available){_in_process_hint(svc)}")
            continue

        if result.returncode == 0:
            print("started")
        else:
            # Service might not exist — check if unit file is present
            try:
                check = subprocess.run(
                    ["systemctl", "list-unit-files", f"{svc}.service"],
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                print(f"skipped (systemd not available){_in_process_hint(svc)}")
                continue
            if svc in check.stdout:
                print(f"FAILED ({result.stderr.strip()})")
            else:
                print(f"skipped (not installed){_in_process_hint(svc)}")

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
    try:
        from robothor.config import get_config as _gc

        _engine_url = _gc().engine_url
        _engine_port = _gc().engine_port
    except Exception:
        _engine_url = "http://127.0.0.1:18800"
        _engine_port = 18800
    print(f"  Engine:      port {_engine_port}")
    try:
        import httpx as _httpx

        resp = _httpx.get(f"{_engine_url}/health", timeout=3)
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

    # Silently operating on another user's vault is the worst outcome in the
    # new-user path; say so rather than printing a path they may not read.
    try:
        _home: Path | None = Path.home()
    except RuntimeError:
        _home = None
    from robothor.engine.instance_env import DEFAULT_ENV_FILE

    _note = describe_instance_ownership(
        Path(cfg.workspace),
        home=_home,
        env_file=DEFAULT_ENV_FILE if DEFAULT_ENV_FILE.exists() else None,
    )
    if _note:
        print()
        print(f"  WARNING: {_note}")
    return 0


def describe_instance_ownership(
    workspace: Path,
    *,
    home: Path | None = None,
    env_file: Path | None = None,
) -> str | None:
    """Warn when the resolved workspace belongs to someone other than the caller.

    `robothor.cli.main()` adopts /etc/robothor/robothor.env so a CLI run sees
    the daemon's guardrail flags instead of reading them back as off — that is
    deliberate and load-bearing. A side effect is that it also adopts
    ROBOTHOR_WORKSPACE.

    Measured on a real box: a fresh clone of the public repo, installed into a
    temp venv and run with `env -i` and a different HOME, reported the
    operator's 118-table database, 15 vault secrets, and
    Workspace: /home/philip/robothor. Postgres peer auth over the Unix socket
    supplies the database; the system env file supplies the workspace.

    Intended on a single-operator box. On a shared machine — which is what
    "installable by other people" means — the second user silently gets the
    first user's workspace, database and vault. This does not change the
    behaviour, which would reintroduce the guardrail bypass; it says so.

    Returns None when the workspace is the caller's own, or when ownership
    cannot be determined (no HOME and no passwd entry is a real container
    case, and status must print rather than traceback).
    """
    if home is None:
        return None
    try:
        ws = Path(workspace).resolve()
        h = Path(home).resolve()
    except (OSError, RuntimeError):
        return None
    if ws == h or h in ws.parents:
        return None

    source = f" (set by {env_file})" if env_file else ""
    return (
        f"attached to an instance outside your home directory: {ws}{source}. "
        f"Its database, vault and agents belong to whoever owns that workspace. "
        f"Set ROBOTHOR_WORKSPACE to use your own."
    )


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

        try:
            from robothor.config import get_config as _gc

            _default_engine_url = _gc().engine_url
        except Exception:
            _default_engine_url = "http://127.0.0.1:18800"
        url = getattr(args, "url", _default_engine_url)
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

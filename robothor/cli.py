"""
Robothor CLI — entry point for all operations.

Usage:
    robothor serve          # Start the API server
    robothor migrate        # Run database migrations
    robothor status         # Show system status
    robothor pipeline       # Run intelligence pipeline
    robothor version        # Show version
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="robothor",
        description="Robothor — An AI brain with persistent memory, vision, and self-healing.",
    )
    parser.add_argument(
        "--version", action="store_true", help="Show version and exit"
    )

    subparsers = parser.add_subparsers(dest="command")

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="Run database migrations")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="Show SQL without executing"
    )

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the API server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    serve_parser.add_argument("--port", type=int, default=9099, help="Port")

    # mcp
    subparsers.add_parser("mcp", help="Start the MCP server (stdio transport)")

    # status
    subparsers.add_parser("status", help="Show system status")

    # pipeline
    pipeline_parser = subparsers.add_parser("pipeline", help="Run intelligence pipeline")
    pipeline_parser.add_argument(
        "--tier", type=int, choices=[1, 2, 3], default=1,
        help="Pipeline tier (1=ingest, 2=analysis, 3=deep)",
    )

    # version
    subparsers.add_parser("version", help="Show version")

    args = parser.parse_args(argv)

    if args.version or args.command == "version":
        from robothor import __version__
        print(f"robothor {__version__}")
        return 0

    if args.command == "migrate":
        return _cmd_migrate(args)
    elif args.command == "serve":
        return _cmd_serve(args)
    elif args.command == "mcp":
        return _cmd_mcp()
    elif args.command == "status":
        return _cmd_status(args)
    elif args.command == "pipeline":
        return _cmd_pipeline(args)
    else:
        parser.print_help()
        return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    print("Migration runner not yet implemented. Coming in v0.2.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is required. Install with: pip install robothor[api]")
        return 1

    print(f"Starting Robothor RAG Orchestrator on {args.host}:{args.port}...")
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
    print(f"  Workspace: {cfg.workspace}")
    print(f"  Database:  {cfg.db.host}:{cfg.db.port}/{cfg.db.name}")
    print(f"  Redis:     {cfg.redis.host}:{cfg.redis.port}")
    print(f"  Ollama:    {cfg.ollama.base_url}")
    return 0


def _cmd_pipeline(args: argparse.Namespace) -> int:
    print(f"Pipeline tier {args.tier} not yet implemented. Coming in v0.2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

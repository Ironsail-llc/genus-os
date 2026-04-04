"""
Genus OS CLI — entry point for all operations.

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
import sys

# Re-export public API for backward compatibility.
# These are imported by robothor.setup and tests.
from robothor.cli.admin import REQUIRED_TABLES as REQUIRED_TABLES  # noqa: F401
from robothor.cli.admin import _find_migration_sql as _find_migration_sql  # noqa: F401
from robothor.cli.admin import cmd_tui as _cmd_tui
from robothor.cli.agent import _cmd_agent_setup as _cmd_agent_setup_impl


def _cmd_agent_setup() -> int:
    """Backward-compat wrapper used by robothor.setup."""
    return _cmd_agent_setup_impl()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="robothor",
        description="Genus OS — An AI brain with persistent memory, vision, and self-healing.",
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

    # config
    config_parser = subparsers.add_parser("config", help="Configuration management")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("validate", help="Validate system configuration and connectivity")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the API server")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    serve_parser.add_argument("--port", type=int, default=9099, help="Port")

    # mcp
    subparsers.add_parser("mcp", help="Start the MCP server (stdio transport)")

    # status
    subparsers.add_parser("status", help="Show system status")

    # start
    subparsers.add_parser("start", help="Start all Genus OS services")

    # stop
    subparsers.add_parser("stop", help="Stop all Genus OS services")

    # pipeline (stub -- v0.2)
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
    vault_sub.add_parser("audit", help="Audit secret usage across the codebase")

    # agent
    agent_parser = subparsers.add_parser("agent", help="Agent management")
    agent_sub = agent_parser.add_subparsers(dest="agent_command")
    scaffold_parser = agent_sub.add_parser("scaffold", help="Scaffold a new agent")
    scaffold_parser.add_argument("agent_id", help="Agent ID (kebab-case, e.g., ticket-router)")
    scaffold_parser.add_argument("--description", "-d", default="", help="One-line description")

    # Template system commands
    agent_sub.add_parser("list", help="List installed agents with source/version")

    catalog_parser = agent_sub.add_parser("catalog", help="Browse available templates")
    catalog_parser.add_argument("--department", "-d", default=None, help="Filter by department")

    install_parser = agent_sub.add_parser("install", help="Install agent from template")
    install_parser.add_argument("source", help="Template path or agent ID")
    install_parser.add_argument("--preset", default=None, help="Install a preset group")
    install_parser.add_argument("--yes", "-y", action="store_true", help="Non-interactive")
    install_parser.add_argument(
        "--set", nargs="*", default=[], help="Override variables (key=value)"
    )

    remove_parser = agent_sub.add_parser("remove", help="Remove an installed agent")
    remove_parser.add_argument("agent_id", help="Agent ID to remove")
    remove_parser.add_argument("--archive", action="store_true", help="Archive instead of delete")

    update_parser = agent_sub.add_parser("update", help="Update agent from template")
    update_parser.add_argument("agent_id", nargs="?", default=None, help="Agent ID (or all)")
    update_parser.add_argument("--template", default=None, help="New template path")

    resolve_parser = agent_sub.add_parser("resolve", help="Preview variable resolution")
    resolve_parser.add_argument("path", help="Template bundle path")
    resolve_parser.add_argument("--dry-run", action="store_true", default=True, help="Preview only")
    resolve_parser.add_argument(
        "--set", nargs="*", default=[], help="Override variables (key=value)"
    )

    import_parser = agent_sub.add_parser(
        "import", help="Reverse-engineer existing agent to template"
    )
    import_parser.add_argument("agent_id", help="Agent ID to import")
    import_parser.add_argument("--output", "-o", default=None, help="Output directory")

    agent_sub.add_parser("setup", help="Interactive onboarding wizard")

    search_parser = agent_sub.add_parser("search", help="Search the hub for agents")
    search_parser.add_argument("query", nargs="?", default="", help="Search query")
    search_parser.add_argument("--department", "-d", default=None, help="Filter by department")

    publish_parser = agent_sub.add_parser("publish", help="Publish template to hub")
    publish_parser.add_argument("repo_url", help="GitHub repo URL to publish")

    bind_parser = agent_sub.add_parser("bind", help="Bind agent to channel/cron schedule")
    bind_parser.add_argument("agent_id", help="Agent ID to bind")
    bind_parser.add_argument("--channel", help="Delivery channel (e.g. telegram)")
    bind_parser.add_argument("--cron", help="Cron expression (e.g. '0 * * * *')")
    bind_parser.add_argument("--to", help="Delivery target (e.g. chat ID)")

    unbind_parser = agent_sub.add_parser("unbind", help="Clear cron and set delivery to none")
    unbind_parser.add_argument("agent_id", help="Agent ID to unbind")

    # federation
    fed_parser = subparsers.add_parser("federation", help="Peer-to-peer instance networking")
    fed_sub = fed_parser.add_subparsers(dest="federation_command")
    fed_sub.add_parser("init", help="Initialize instance identity (Ed25519 keypair)")
    fed_invite = fed_sub.add_parser("invite", help="Generate a connection invite token")
    fed_invite.add_argument("--name", default="", help="Display name for the peer")
    fed_invite.add_argument(
        "--relationship",
        choices=["parent", "child", "peer"],
        default="peer",
        help="Relationship to the connecting instance",
    )
    fed_invite.add_argument("--ttl", type=int, default=24, help="Token TTL in hours (default 24)")
    fed_connect = fed_sub.add_parser("connect", help="Accept a connection invite token")
    fed_connect.add_argument("token", help="Invite token from the peer instance")
    fed_connect.add_argument(
        "--trust",
        action="store_true",
        help="Skip signature verification (use for pre-shared tokens on trusted networks)",
    )
    fed_sub.add_parser("status", help="Show all connections and their health")
    fed_sub.add_parser("list", help="List connected instances")
    fed_export = fed_sub.add_parser("export", help="Expose a capability to a peer")
    fed_export.add_argument("connection", help="Connection ID")
    fed_export.add_argument("capability", help="Capability to export")
    fed_suspend = fed_sub.add_parser("suspend", help="Suspend a connection")
    fed_suspend.add_argument("connection", help="Connection ID")
    fed_remove = fed_sub.add_parser("remove", help="Disconnect from a peer")
    fed_remove.add_argument("connection", help="Connection ID")

    # engine
    # run -- quick single-shot agent execution
    run_parser = subparsers.add_parser("run", help="Run agent with a message (non-interactive)")
    run_parser.add_argument(
        "message", nargs="?", default=None, help="Task description (reads stdin if omitted)"
    )
    run_parser.add_argument("--agent", "-a", default=None, help="Agent ID (default: main)")
    run_parser.add_argument("--model", "-m", default=None, help="Model override")
    run_parser.add_argument(
        "--print", dest="print_only", action="store_true", help="Print final output only"
    )
    run_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Output as JSON"
    )
    run_parser.add_argument("--timeout", type=int, default=600, help="Timeout in seconds")

    # chat -- alias for tui
    subparsers.add_parser("chat", help="Interactive chat (launches TUI)")

    # agents -- shortcut to list agents
    subparsers.add_parser("agents", help="List configured agents (shortcut)")

    # costs -- shortcut to show costs
    costs_parser = subparsers.add_parser("costs", help="Show cost breakdown")
    costs_parser.add_argument("--hours", type=int, default=24, help="Lookback hours")

    eng_parser = subparsers.add_parser("engine", help="Manage the agent engine")
    eng_sub = eng_parser.add_subparsers(dest="engine_command")
    eng_run = eng_sub.add_parser("run", help="Run a single agent")
    eng_run.add_argument("agent_id", help="Agent ID (from YAML manifest)")
    eng_run.add_argument(
        "--message", "-m", default=None, help="User message (default: cron payload)"
    )
    eng_run.add_argument("--trigger", default="manual", help="Trigger type")
    eng_run.add_argument(
        "--deep",
        action="store_true",
        default=False,
        help="Use deep reasoning (RLM) instead of the normal agent loop",
    )
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

    # Dispatch to submodules (lazy imports inside branches for fast startup)
    if args.command == "init":
        from robothor.cli.admin import cmd_init

        return cmd_init(args)
    if args.command == "migrate":
        from robothor.cli.admin import cmd_migrate

        return cmd_migrate(args)
    if args.command == "serve":
        from robothor.cli.admin import cmd_serve

        return cmd_serve(args)
    if args.command == "mcp":
        from robothor.cli.admin import cmd_mcp

        return cmd_mcp()
    if args.command == "status":
        from robothor.cli.admin import cmd_status

        return cmd_status(args)
    if args.command == "start":
        from robothor.cli.admin import cmd_start

        return cmd_start(args)
    if args.command == "stop":
        from robothor.cli.admin import cmd_stop

        return cmd_stop(args)
    if args.command == "pipeline":
        from robothor.cli.admin import cmd_pipeline

        return cmd_pipeline(args)
    if args.command == "tunnel":
        from robothor.cli.admin import cmd_tunnel

        return cmd_tunnel(args)
    if args.command == "vault":
        from robothor.cli.vault import cmd_vault

        return cmd_vault(args)
    if args.command == "agent":
        from robothor.cli.agent import cmd_agent

        return cmd_agent(args)
    if args.command == "federation":
        from robothor.cli.federation import cmd_federation

        return cmd_federation(args)
    if args.command == "run":
        from robothor.cli.engine import cmd_run

        return cmd_run(args)
    if args.command == "chat":
        return _cmd_tui(args)
    if args.command == "agents":
        from robothor.cli.engine import cmd_agents

        return cmd_agents()
    if args.command == "costs":
        from robothor.cli.engine import cmd_costs

        return cmd_costs(args)
    if args.command == "engine":
        from robothor.cli.engine import cmd_engine

        return cmd_engine(args)
    if args.command == "config":
        from robothor.cli.admin import cmd_config

        return cmd_config(args)
    if args.command == "tui":
        return _cmd_tui(args)
    if args.command is None:
        # No subcommand -- launch the TUI
        return _cmd_tui(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

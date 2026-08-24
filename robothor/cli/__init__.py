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
    robothor snapshot       # Backup, verify, restore, and retain instance state
    robothor auth           # Manage user accounts / identity
    robothor user           # Register/link users into the identity graph
    robothor export         # Export configuration as a portable bundle
    robothor import         # Import configuration from another agent platform
    robothor tenant         # Create, list, and inspect tenants
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
    # Adopt the instance's systemd environment before anything reads a flag.
    # Without this a CLI run inherits only the caller's shell, so every
    # rollout-gated guardrail reads back as off/observe while the daemon
    # enforces it — a shell (or an agent shelling out) would silently bypass
    # the controls. Explicitly-set variables still win.
    from robothor.engine.instance_env import load_instance_env

    load_instance_env()

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

    # upgrade
    upgrade_parser = subparsers.add_parser("upgrade", help="Upgrade platform to latest version")
    upgrade_parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    upgrade_parser.add_argument("--skip-pull", action="store_true", help="Skip git pull")
    upgrade_parser.add_argument(
        "--skip-migrations", action="store_true", help="Skip database migrations"
    )

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="Run database migrations")
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="Print SQL without executing"
    )
    migrate_parser.add_argument(
        "--check", action="store_true", help="Check if required tables exist"
    )

    # snapshot — versioned disaster recovery for PostgreSQL + workspace state
    snapshot_parser = subparsers.add_parser(
        "snapshot", help="Create, verify, restore, and retain instance snapshots"
    )
    snapshot_sub = snapshot_parser.add_subparsers(dest="snapshot_command")

    snapshot_create = snapshot_sub.add_parser("create", help="Create an atomic snapshot")
    snapshot_create.add_argument("--repository", help="Snapshot repository directory")
    snapshot_create.add_argument("--output", help="Exact output file path")
    snapshot_create.add_argument("--workspace", help="Workspace to snapshot")
    snapshot_create.add_argument(
        "--include-secrets",
        action="store_true",
        help=(
            "Include .vault-key and existing federation identity key "
            "(requires encryption; never includes env secrets)"
        ),
    )
    snapshot_create.add_argument(
        "--plaintext",
        action="store_true",
        help="Explicitly disable encryption (forbidden with --include-secrets)",
    )
    snapshot_create.add_argument(
        "--passphrase-env",
        default="GENUS_SNAPSHOT_PASSPHRASE",
        help="Environment variable holding the encryption passphrase",
    )
    snapshot_create.add_argument(
        "--skip-database", action="store_true", help="Create a workspace-only snapshot"
    )
    snapshot_create.add_argument(
        "--skip-workspace", action="store_true", help="Create a database-only snapshot"
    )
    snapshot_create.add_argument(
        "--force", action="store_true", help="Replace the exact --output path atomically"
    )

    snapshot_list = snapshot_sub.add_parser("list", help="List managed snapshots")
    snapshot_list.add_argument("--repository", help="Snapshot repository directory")

    snapshot_verify = snapshot_sub.add_parser(
        "verify", help="Authenticate, checksum, inspect, and compatibility-check a snapshot"
    )
    snapshot_verify.add_argument("snapshot", help="Snapshot file")
    snapshot_verify.add_argument(
        "--passphrase-env",
        default="GENUS_SNAPSHOT_PASSPHRASE",
        help="Environment variable holding the decryption passphrase",
    )

    snapshot_restore = snapshot_sub.add_parser(
        "restore", help="Verify/dry-run by default; restore only with explicit confirmation"
    )
    snapshot_restore.add_argument("snapshot", help="Snapshot file")
    snapshot_restore.add_argument("--workspace", help="Target workspace")
    snapshot_restore.add_argument(
        "--passphrase-env",
        default="GENUS_SNAPSHOT_PASSPHRASE",
        help="Environment variable holding the decryption passphrase",
    )
    restore_selection = snapshot_restore.add_mutually_exclusive_group()
    restore_selection.add_argument(
        "--database-only", action="store_true", help="Restore only PostgreSQL"
    )
    restore_selection.add_argument(
        "--workspace-only", action="store_true", help="Restore only workspace state"
    )
    snapshot_restore.add_argument(
        "--confirm", action="store_true", help="Execute the restore instead of a dry run"
    )
    snapshot_restore.add_argument(
        "--force",
        action="store_true",
        help="Authorize destructive DB cleaning and workspace replacement",
    )

    snapshot_prune = snapshot_sub.add_parser(
        "prune", help="Apply retention policy (dry-run unless --confirm)"
    )
    snapshot_prune.add_argument("--repository", help="Snapshot repository directory")
    snapshot_prune.add_argument(
        "--keep", type=int, default=7, help="Always keep at least this many newest snapshots"
    )
    snapshot_prune.add_argument(
        "--older-than-days", type=int, default=None, help="Delete only snapshots older than N days"
    )
    snapshot_prune.add_argument("--confirm", action="store_true", help="Delete selected snapshots")

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

    # goal — long-running session goal (operator objective).
    # Backed by a crm_task with the session_goal tag; see migration 065.
    goal_parser = subparsers.add_parser("goal", help="Manage the active long-running session goal")
    goal_parser.add_argument(
        "--tenant",
        default=None,
        help="Tenant ID (defaults to ROBOTHOR_DEFAULT_TENANT or 'default')",
    )
    goal_parser.add_argument(
        "--agent",
        default="",
        help="Agent ID for a per-agent goal (workspace goal otherwise, owner=main)",
    )
    goal_sub = goal_parser.add_subparsers(dest="goal_command")

    goal_set = goal_sub.add_parser("set", help="Create the active session goal")
    goal_set.add_argument("objective", help="Goal objective (one sentence)")
    goal_set.add_argument(
        "--criteria",
        action="append",
        default=[],
        help="Success criterion; repeat to provide an explicit completion contract",
    )
    goal_set.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")

    goal_status = goal_sub.add_parser("status", help="Show the active session goal")
    goal_status.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")

    goal_evidence = goal_sub.add_parser("evidence", help="Record typed evidence")
    goal_evidence.add_argument(
        "--kind",
        required=True,
        choices=["test_run", "commit", "ci_run", "note"],
        help=(
            "test_run: pytest:passed:N or run UUID; "
            "commit: git SHA validated via git cat-file; "
            "ci_run: https URL; "
            "note: free-form (does not satisfy completion)"
        ),
    )
    goal_evidence.add_argument("--summary", required=True, help="Short evidence summary")
    goal_evidence.add_argument("--reference", default="", help="Verifiable reference for this kind")
    goal_evidence.add_argument(
        "--json", dest="json_output", action="store_true", help="Output JSON"
    )

    goal_complete = goal_sub.add_parser("complete", help="Mark the active session goal complete")
    goal_complete.add_argument("note", help="Completion note")
    goal_complete.add_argument(
        "--json", dest="json_output", action="store_true", help="Output JSON"
    )

    goal_edit_obj = goal_sub.add_parser(
        "edit-objective", help="Replace the goal's objective in place"
    )
    goal_edit_obj.add_argument("objective", help="New objective text")
    goal_edit_obj.add_argument("--json", dest="json_output", action="store_true")

    goal_add_crit = goal_sub.add_parser(
        "add-criterion", help="Append a success criterion to the goal"
    )
    goal_add_crit.add_argument("text", help="Criterion text")
    goal_add_crit.add_argument("--json", dest="json_output", action="store_true")

    goal_set_target = goal_sub.add_parser(
        "set-target",
        help="Add or replace a metric target on the goal",
    )
    goal_set_target.add_argument("metric", help="Metric name (e.g. benchmark_pass_rate)")
    goal_set_target.add_argument("target", help='Target comparator e.g. ">=0.85" or "<0.05"')
    goal_set_target.add_argument("--weight", type=float, default=1.0)
    goal_set_target.add_argument("--window-days", type=int, default=7)
    goal_set_target.add_argument(
        "--category",
        default="correctness",
        choices=["reach", "quality", "efficiency", "correctness"],
    )
    goal_set_target.add_argument(
        "--id",
        dest="target_id",
        default=None,
        help="Stable id for this target (defaults to metric)",
    )
    goal_set_target.add_argument("--json", dest="json_output", action="store_true")

    goal_remove_target = goal_sub.add_parser("remove-target", help="Remove a metric target by id")
    goal_remove_target.add_argument("target_id", help="Target id (typically the metric name)")
    goal_remove_target.add_argument("--json", dest="json_output", action="store_true")

    # memory-eval — memory retrieval benchmark (recall/temporal/verbatim/persona)
    memeval_parser = subparsers.add_parser(
        "memory-eval", help="Run the memory retrieval benchmark suite"
    )
    memeval_parser.add_argument(
        "--suite",
        default="docs/benchmarks/memory/suite.yaml",
        help="Path to the eval suite YAML",
    )
    memeval_parser.add_argument(
        "--tenant", default=None, help="Isolated eval tenant (default: memory-eval)"
    )
    memeval_parser.add_argument(
        "--keep", action="store_true", help="Skip cleanup of seeded facts (debugging)"
    )
    memeval_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Output JSON"
    )
    memeval_parser.add_argument(
        "--record",
        action="store_true",
        help="Write the result to benchmark_results so the fleet grader can see it",
    )
    memeval_parser.add_argument(
        "--triggered-by",
        default="manual",
        help="Provenance recorded alongside the result (e.g. cron)",
    )

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
    try:
        from robothor.config import get_config as _gc

        _tui_default_url = _gc().engine_url
    except Exception:
        _tui_default_url = "http://127.0.0.1:18800"
    tui_parser.add_argument("--url", default=_tui_default_url, help="Engine URL")
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

    # skills
    skills_parser = subparsers.add_parser("skills", help="Skill library maintenance")
    skills_sub = skills_parser.add_subparsers(dest="skills_command")
    skills_sub.add_parser(
        "migrate-state",
        help=(
            "Move runtime keys (usage_count, last_used, state) out of tracked "
            "meta.json files into gitignored state.json sidecars (idempotent)"
        ),
    )

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

    # auth — user account / identity administration
    auth_parser = subparsers.add_parser("auth", help="Manage user accounts and identity")
    auth_sub = auth_parser.add_subparsers(dest="auth_command")
    auth_bootstrap = auth_sub.add_parser(
        "bootstrap", help="Seed the owner.yaml operator as the tenant 'owner' account"
    )
    auth_bootstrap.add_argument(
        "--json", dest="json_output", action="store_true", help="Output JSON"
    )
    auth_grant = auth_sub.add_parser(
        "grant-binding",
        help="Arm a one-shot grant binding an existing account to its next SSO sign-in",
    )
    auth_grant.add_argument("--email", required=True, help="Account email to bind")
    auth_grant.add_argument("--tenant", default=None, help="Tenant ID (default: platform default)")
    auth_grant.add_argument("--ttl", default="15m", help="Grant lifetime, e.g. 45s/15m/2h/1d")
    auth_grant.add_argument("--reason", default="", help="Audit reason")
    auth_grant.add_argument(
        "--issuer",
        default=None,
        help="Pin the grant to one IdP issuer URL (default: any allowlisted IdP)",
    )
    auth_grant.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    auth_grants = auth_sub.add_parser("grants", help="List SSO binding grants")
    auth_grants.add_argument("--tenant", default=None, help="Tenant ID (default: platform default)")
    auth_grants.add_argument(
        "--all",
        dest="include_inactive",
        action="store_true",
        help="Include used/revoked/expired grants",
    )
    auth_grants.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")
    auth_revoke = auth_sub.add_parser("revoke-binding", help="Revoke a pending binding grant")
    auth_revoke.add_argument("grant_id", help="Grant ID")
    auth_revoke.add_argument("--tenant", default=None, help="Tenant ID (default: any)")

    # user — closed-allowlist registration: link a human into the identity graph
    user_parser = subparsers.add_parser(
        "user", help="Register/link users into the identity graph (closed-allowlist onboarding)"
    )
    user_sub = user_parser.add_subparsers(dest="user_command")

    user_list = user_sub.add_parser("list", help="List tenant users")
    user_list.add_argument("--tenant", default=None, help="Filter by tenant (default: all tenants)")

    user_add = user_sub.add_parser("add", help="Register a new user with full identity linkage")
    user_add.add_argument(
        "--tenant", default=None, help="Tenant ID (default: ROBOTHOR_DEFAULT_TENANT)"
    )
    user_add.add_argument("--name", required=True, help="Display name")
    user_add.add_argument(
        "--role", required=True, help="Role: owner/admin/member/user/viewer/auditor"
    )
    user_add.add_argument("--telegram-id", default=None, help="Telegram user id")
    user_add.add_argument("--email", default=None, help="Email address")
    user_add_person = user_add.add_mutually_exclusive_group()
    user_add_person.add_argument(
        "--person-id", default=None, help="Link to an existing crm_people row"
    )
    user_add_person.add_argument(
        "--create-person", action="store_true", help="Force-create a new crm_people row"
    )

    user_link = user_sub.add_parser("link", help="Link a Telegram id to a person")
    user_link.add_argument("--telegram-id", required=True, help="Telegram user id")
    user_link.add_argument(
        "--tenant", default=None, help="Tenant ID (default: ROBOTHOR_DEFAULT_TENANT)"
    )
    user_link_person = user_link.add_mutually_exclusive_group(required=True)
    user_link_person.add_argument("--person-id", default=None, help="Existing crm_people id")
    user_link_person.add_argument(
        "--email", default=None, help="Look up the existing person by email"
    )

    user_link_face = user_sub.add_parser("link-face", help="Upsert a face label -> person binding")
    user_link_face.add_argument("--label", required=True, help="Face label")
    user_link_face.add_argument("--person-id", required=True, help="crm_people id")
    user_link_face.add_argument(
        "--display-name",
        default=None,
        help="Display name to store (default: derived from the person's CRM first+last name)",
    )
    user_link_face.add_argument(
        "--tenant", default=None, help="Tenant ID (default: ROBOTHOR_DEFAULT_TENANT)"
    )

    # export — portable configuration bundle (agents, skills, opt-in memory)
    export_parser = subparsers.add_parser(
        "export", help="Export configuration as a portable bundle"
    )
    export_parser.add_argument(
        "--tenant", default=None, help="Tenant ID (default: ROBOTHOR_DEFAULT_TENANT)"
    )
    export_parser.add_argument(
        "--output", default=None, help="Output directory (default: ./robothor-export-<tenant>)"
    )
    export_parser.add_argument(
        "--include-memory",
        action="store_true",
        help="Include memory block contents (opt-in — may contain PII)",
    )

    # import — migrate configuration from another agent platform
    platform_import_parser = subparsers.add_parser(
        "import", help="Import configuration from another agent platform"
    )
    platform_import_parser.add_argument(
        "platform",
        nargs="?",
        default="auto",
        choices=["auto", "hermes", "generic"],
        help="Source platform (default: auto-detect)",
    )
    platform_import_parser.add_argument(
        "--source", default=None, help="Source path (file or directory)"
    )
    platform_import_parser.add_argument(
        "--tenant", default=None, help="Target tenant ID (default: ROBOTHOR_DEFAULT_TENANT)"
    )

    # tenant — multi-tenant administration
    tenant_parser = subparsers.add_parser("tenant", help="Create, list, and inspect tenants")
    tenant_sub = tenant_parser.add_subparsers(dest="tenant_command")
    tenant_create = tenant_sub.add_parser("create", help="Create a new tenant")
    tenant_create.add_argument("id", help="Tenant ID (e.g. acme)")
    tenant_create.add_argument("--name", default=None, help="Display name (default: tenant ID)")
    tenant_create.add_argument(
        "--telegram-user-id", default=None, help="Bind a Telegram user id to the new tenant"
    )
    tenant_create.add_argument("--parent", default=None, help="Parent tenant ID")
    tenant_sub.add_parser("list", help="List all tenants")
    tenant_status = tenant_sub.add_parser(
        "status", help="Show tenant details, memory stats, and recent run counts"
    )
    tenant_status.add_argument("tenant_id", help="Tenant ID")

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

    # codex -- subscription-backed provider auth/status
    codex_parser = subparsers.add_parser("codex", help="Manage Codex subscription provider")
    codex_sub = codex_parser.add_subparsers(dest="codex_command")
    codex_login = codex_sub.add_parser("login", help="Log in to Codex with ChatGPT")
    codex_login.add_argument(
        "--with-access-token",
        action="store_true",
        help="Read CODEX_ACCESS_TOKEN from stdin via codex login",
    )
    codex_sub.add_parser("status", help="Show Codex login status")
    codex_sub.add_parser("doctor", help="Validate ChatGPT subscription auth for codex/* models")
    codex_test = codex_sub.add_parser("test", help="Run a small codex/* provider call")
    codex_test.add_argument("prompt", nargs="?", default="Reply with: codex provider ok")
    codex_test.add_argument("--model", default="codex/gpt-5.5")
    codex_test.add_argument("--timeout", type=int, default=120)

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
    eng_wf_sub.add_parser("pending", help="List workflow runs awaiting approval")
    for _verb in ("approve", "reject"):
        _p = eng_wf_sub.add_parser(_verb, help=f"{_verb.capitalize()} a waiting workflow step")
        _p.add_argument("run_id", help="Workflow run ID (from `workflow pending`)")
        _p.add_argument("--step", default="", help="Step ID (only needed if several are waiting)")
        _p.add_argument("--note", default="", help="Why — recorded with the decision")

    args = parser.parse_args(argv)

    if args.version or args.command == "version":
        from robothor import __version__

        print(f"robothor {__version__}")
        return 0

    # Dispatch to submodules (lazy imports inside branches for fast startup)
    if args.command == "init":
        from robothor.cli.admin import cmd_init

        return cmd_init(args)
    if args.command == "upgrade":
        from robothor.cli.upgrade import cmd_upgrade

        return cmd_upgrade(args)
    if args.command == "migrate":
        from robothor.cli.admin import cmd_migrate

        return cmd_migrate(args)
    if args.command == "snapshot":
        from robothor.cli.snapshot import cmd_snapshot

        return cmd_snapshot(args)
    if args.command == "memory-eval":
        from robothor.cli.memory_eval import cmd_memory_eval

        return cmd_memory_eval(args)
    if args.command == "serve":
        from robothor.cli.admin import cmd_serve

        return cmd_serve(args)
    if args.command == "mcp":
        from robothor.cli.admin import cmd_mcp

        return cmd_mcp()
    if args.command == "goal":
        from robothor.cli.goal import cmd_goal

        return cmd_goal(args)
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
    if args.command == "skills":
        from robothor.cli.skills import cmd_skills

        return cmd_skills(args)
    if args.command == "federation":
        from robothor.cli.federation import cmd_federation

        return cmd_federation(args)
    if args.command == "auth":
        from robothor.cli.auth import cmd_auth

        return cmd_auth(args)
    if args.command == "user":
        from robothor.cli.user import cmd_user

        return cmd_user(args)
    if args.command == "export":
        from robothor.cli.exporter import cmd_export

        return cmd_export(args)
    if args.command == "import":
        from robothor.cli.importer import cmd_import

        return cmd_import(args)
    if args.command == "tenant":
        from robothor.cli.tenant import cmd_tenant

        return cmd_tenant(args)
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
    if args.command == "codex":
        from robothor.cli.codex import cmd_codex

        return cmd_codex(args)
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

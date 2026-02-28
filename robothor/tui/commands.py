"""
Slash command registry for the Robothor TUI.

Handles local commands like /status, /agents, /costs, /help, etc.
Returns (handled: bool, output: str | None).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.tui.app import RobothorApp

# Command registry: name → (handler_name, description)
COMMANDS: dict[str, tuple[str, str]] = {
    "/status": ("cmd_status", "Engine health and agent summary"),
    "/agents": ("cmd_agents", "List agents with status"),
    "/costs": ("cmd_costs", "Cost breakdown (default 24h)"),
    "/history": ("cmd_history", "Session message count"),
    "/model": ("cmd_model", "Show current model"),
    "/clear": ("cmd_clear", "Reset session history"),
    "/abort": ("cmd_abort", "Cancel running response"),
    "/help": ("cmd_help", "Show available commands"),
    "/quit": ("cmd_quit", "Exit the TUI"),
    "/exit": ("cmd_quit", "Exit the TUI"),
}


async def handle_command(app: RobothorApp, text: str) -> tuple[bool, str | None]:
    """Try to handle text as a slash command.

    Returns (True, output) if handled, (False, None) if not a command.
    """
    parts = text.strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return False, None

    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd not in COMMANDS:
        return True, f"Unknown command: {cmd}. Type /help for available commands."

    handler_name, _ = COMMANDS[cmd]
    handler = globals().get(handler_name)
    if handler is None:
        return True, f"Command {cmd} not implemented."

    output = await handler(app, args)
    return True, output


async def cmd_status(app: RobothorApp, args: str) -> str:
    """Show engine health."""
    health = await app.client.check_health()
    if health is None:
        return "[red]Engine unreachable[/red]"

    agents = health.get("agents", {})
    lines = [
        f"[bold]Engine Status[/bold]: {health.get('status', 'unknown')}",
        f"Version: {health.get('engine_version', '?')}",
        f"Tenant: {health.get('tenant_id', '?')}",
        f"Bot: {'configured' if health.get('bot_configured') else 'disabled'}",
        f"Agents: {len(agents)}",
    ]
    return "\n".join(lines)


async def cmd_agents(app: RobothorApp, args: str) -> str:
    """List agents with status table."""
    health = await app.client.check_health()
    if health is None:
        return "[red]Engine unreachable[/red]"

    agents = health.get("agents", {})
    if not agents:
        return "No agents configured."

    lines = [f"{'Agent':<25} {'Status':<12} {'Errors':<8} {'Last Run'}"]
    lines.append("\u2500" * 70)
    for aid, info in agents.items():
        lines.append(
            f"{aid:<25} {info.get('last_status', '-'):<12} "
            f"{info.get('consecutive_errors', 0):<8} "
            f"{info.get('last_run_at', '-')}"
        )
    return "\n".join(lines)


async def cmd_costs(app: RobothorApp, args: str) -> str:
    """Show cost breakdown."""
    try:
        hours = int(args) if args.strip() else 24
    except ValueError:
        hours = 24

    data = await app.client.get_costs(hours)
    if not data:
        return "[red]Could not fetch costs[/red]"

    lines = [
        f"[bold]Costs ({hours}h)[/bold]",
        f"Total runs: {data.get('total_runs', 0)}",
        f"Total cost: ${data.get('total_cost_usd', 0):.4f}",
    ]

    breakdown = data.get("agents", {})
    if breakdown:
        lines.append("")
        lines.append(f"{'Agent':<25} {'Runs':<8} {'Cost':<12} {'Tokens'}")
        lines.append("\u2500" * 65)
        for aid, info in breakdown.items():
            tokens = f"{info.get('total_input_tokens', 0)}+{info.get('total_output_tokens', 0)}"
            lines.append(
                f"{aid:<25} {info.get('runs', 0):<8} "
                f"${info.get('total_cost_usd', 0):<11.4f} {tokens}"
            )

    return "\n".join(lines)


async def cmd_history(app: RobothorApp, args: str) -> str:
    """Show session message count."""
    history = await app.client.get_history()
    return f"Session has {len(history)} messages."


async def cmd_model(app: RobothorApp, args: str) -> str:
    """Show current model."""
    model = app.status_bar._model if app.status_bar._model else "default"
    return f"Current model: {model}"


async def cmd_clear(app: RobothorApp, args: str) -> str:
    """Reset session."""
    ok = await app.client.clear()
    if ok:
        # Clear the chat display
        chat = app.query_one("#chat-scroll")
        await chat.remove_children()
        return "Session cleared."
    return "[red]Failed to clear session[/red]"


async def cmd_abort(app: RobothorApp, args: str) -> str:
    """Cancel running response."""
    aborted = await app.client.abort()
    return "Response aborted." if aborted else "No active response to abort."


async def cmd_help(app: RobothorApp, args: str) -> str:
    """Show available commands."""
    lines = ["[bold]Available Commands[/bold]", ""]
    for cmd, (_, desc) in sorted(COMMANDS.items()):
        if cmd == "/exit":
            continue  # Skip alias
        lines.append(f"  {cmd:<12} {desc}")
    lines.append("")
    lines.append("Press Escape to abort a streaming response.")
    return "\n".join(lines)


async def cmd_quit(app: RobothorApp, args: str) -> str:
    """Exit the TUI."""
    app.exit()
    return ""

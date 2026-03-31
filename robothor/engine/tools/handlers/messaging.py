"""Agent messaging and team tool handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}


async def _handle_send_agent_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Send a message to another agent."""
    from robothor.engine.messaging import get_messenger

    messenger = get_messenger()
    if messenger is None:
        return {"error": "Messaging not initialized"}

    to_agent = args.get("to_agent", "")
    content = args.get("content", "")
    if not to_agent or not content:
        return {"error": "to_agent and content are required"}

    metadata = args.get("metadata")
    ok = messenger.send(
        from_agent=ctx.agent_id,
        to_agent=to_agent,
        content=content,
        metadata=metadata or {},
    )
    return {"sent": ok, "to_agent": to_agent}


async def _handle_receive_agent_messages(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Receive messages from agent's inbox."""
    from robothor.engine.messaging import get_messenger

    messenger = get_messenger()
    if messenger is None:
        return {"error": "Messaging not initialized"}

    limit = int(args.get("limit", 10))
    messages = messenger.receive(ctx.agent_id, limit=limit)
    return {
        "messages": [
            {
                "from_agent": m.from_agent,
                "content": m.content,
                "team_id": m.team_id,
                "timestamp": m.timestamp,
                "metadata": m.metadata,
            }
            for m in messages
        ],
        "count": len(messages),
    }


async def _handle_create_team(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a new agent team."""
    from robothor.engine.teams import get_team_manager

    tm = get_team_manager()
    if tm is None:
        return {"error": "Team manager not initialized"}

    team_id = args.get("team_id", "")
    members = args.get("members", [])
    objective = args.get("objective", "")

    if not team_id or not members:
        return {"error": "team_id and members are required"}

    # Ensure the calling agent is a member
    if ctx.agent_id and ctx.agent_id not in members:
        members.append(ctx.agent_id)

    team = tm.create_team(team_id, members, objective)
    if team is None:
        return {"error": "Failed to create team"}

    return {
        "team_id": team.team_id,
        "members": team.member_ids,
        "objective": team.objective,
    }


async def _handle_team_scratchpad_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Write to a team's shared scratchpad."""
    from robothor.engine.teams import get_team_manager

    tm = get_team_manager()
    if tm is None:
        return {"error": "Team manager not initialized"}

    team_id = args.get("team_id", "")
    key = args.get("key", "")
    value = args.get("value", "")

    if not team_id or not key:
        return {"error": "team_id and key are required"}

    ok = tm.scratchpad_write(team_id, key, value, agent_id=ctx.agent_id)
    return {"written": ok, "team_id": team_id, "key": key}


async def _handle_team_scratchpad_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Read from a team's shared scratchpad."""
    from robothor.engine.teams import get_team_manager

    tm = get_team_manager()
    if tm is None:
        return {"error": "Team manager not initialized"}

    team_id = args.get("team_id", "")
    if not team_id:
        return {"error": "team_id is required"}

    key = args.get("key")  # None = read all
    data = tm.scratchpad_read(team_id, key)
    return {"team_id": team_id, "data": data}


HANDLERS["send_agent_message"] = _handle_send_agent_message
HANDLERS["receive_agent_messages"] = _handle_receive_agent_messages
HANDLERS["create_team"] = _handle_create_team
HANDLERS["team_scratchpad_write"] = _handle_team_scratchpad_write
HANDLERS["team_scratchpad_read"] = _handle_team_scratchpad_read

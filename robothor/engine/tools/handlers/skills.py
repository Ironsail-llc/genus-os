"""Skill tool handlers — invoke and list available skills."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("invoke_skill")
async def _invoke_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the full content of a skill for the LLM to follow."""
    from robothor.engine.skills import get_skill_content

    name = args.get("name", "")
    if not name:
        return {"error": "name is required"}

    content = get_skill_content(name)
    if content is None:
        from robothor.engine.skills import load_skills

        available = sorted(load_skills().keys())
        return {
            "error": f"Skill '{name}' not found",
            "available_skills": available,
        }

    return {"skill": name, "content": content}


@_handler("list_skills")
async def _list_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return catalog of available skills."""
    from robothor.engine.skills import load_skills

    skills = load_skills()
    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "tags": list(s.tags),
            }
            for s in skills.values()
        ]
    }

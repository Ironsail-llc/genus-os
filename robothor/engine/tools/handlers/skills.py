"""Skill tool handlers — invoke and list available skills."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _resolve_content(
    skill_name: str,
    skill_args: dict[str, Any],
) -> dict[str, Any] | str:
    """Load, validate params, substitute placeholders, resolve dependencies.

    Returns the final content string on success, or an error dict on failure.
    """
    from robothor.engine.skills import load_skills

    skills = load_skills()
    defn = skills.get(skill_name)
    if defn is None:
        available = sorted(skills.keys())
        return {"error": f"Skill '{skill_name}' not found", "available_skills": available}

    # --- Validate required parameters -----------------------------------------
    missing = [
        param.name for param in defn.parameters if param.required and param.name not in skill_args
    ]
    if missing:
        return {
            "error": f"Missing required parameters: {', '.join(missing)}",
            "skill": skill_name,
            "required": [p.name for p in defn.parameters if p.required],
        }

    # --- Build substitution map (provided args + defaults) --------------------
    subs: dict[str, str] = {}
    for param in defn.parameters:
        value = skill_args.get(param.name, param.default)
        if value is not None:
            subs[param.name] = str(value)

    # --- Resolve depends_on (prepend dependent skill content) -----------------
    parts: list[str] = []
    for dep_name in defn.depends_on:
        dep = skills.get(dep_name)
        if dep:
            parts.append(f"<!-- prerequisite: {dep_name} -->\n{dep.content}\n")
        else:
            logger.warning("Skill '%s' depends on unknown skill '%s'", skill_name, dep_name)

    # --- Template-substitute placeholders in body -----------------------------
    content = defn.content
    for key, val in subs.items():
        content = content.replace(f"{{{key}}}", val)

    parts.append(content)

    # --- JSON output format instruction ---------------------------------------
    if defn.output_format == "json":
        parts.append(
            "\n\n---\n**Output format**: Respond with a valid JSON object containing "
            "your results. Do not wrap it in markdown code fences."
        )

    return "\n".join(parts)


@_handler("invoke_skill")
async def _invoke_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the full content of a skill for the LLM to follow."""
    name = args.get("name", "")
    if not name:
        return {"error": "name is required"}

    skill_args: dict[str, Any] = args.get("args") or {}

    result = _resolve_content(name, skill_args)
    if isinstance(result, dict):
        return result  # error dict

    return {"skill": name, "content": result}


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
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        **({"default": p.default} if p.default is not None else {}),
                    }
                    for p in s.parameters
                ],
                "output_format": s.output_format,
            }
            for s in skills.values()
        ]
    }

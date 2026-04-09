"""Skill tool handlers — invoke, list, create, and update skills."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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

    # Track usage for auto-generated skills
    from robothor.engine.skills import increment_usage

    increment_usage(name)

    return {"skill": name, "content": result}


@_handler("list_skills")
async def _list_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return catalog of available skills."""
    from robothor.engine.skills import load_skills, read_skill_meta

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
                **(
                    {"usage_count": meta.get("usage_count", 0), "auto_generated": True}
                    if (meta := read_skill_meta(s.name)) is not None
                    else {}
                ),
            }
            for s in skills.values()
        ]
    }


@_handler("create_skill")
async def _create_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a new reusable skill from a procedure."""
    from robothor.engine.skills import (
        _MAX_CONTENT_LEN,
        _content_hash,
        create_skill_meta,
        load_skills,
        read_skill_meta,
        validate_skill_name,
        write_skill_file,
        write_skill_meta,
    )

    name = args.get("name", "").strip()
    err = validate_skill_name(name)
    if err:
        return {"error": err}

    description = args.get("description", "").strip()
    if not description:
        return {"error": "description is required"}

    content = args.get("content", "").strip()
    if not content:
        return {"error": "content (markdown body) is required"}
    if len(content) > _MAX_CONTENT_LEN:
        return {"error": f"content exceeds {_MAX_CONTENT_LEN} char limit ({len(content)} chars)"}

    overwrite = args.get("overwrite", False)

    # Check for collisions with hand-authored skills
    existing = load_skills()
    if name in existing:
        existing_meta = read_skill_meta(name)
        is_auto = existing_meta and existing_meta.get("auto_generated")
        if not is_auto and not overwrite:
            return {
                "error": (
                    f"Skill '{name}' already exists and was hand-authored. "
                    "Set overwrite=true to replace it."
                ),
            }
        if is_auto and not overwrite:
            return {
                "error": (
                    f"Skill '{name}' already exists (auto-generated). "
                    "Use update_skill to revise it, or set overwrite=true to replace."
                ),
            }

    # Build frontmatter
    tags = args.get("tags") or []
    parameters = args.get("parameters") or []
    tools_required = args.get("tools_required") or []
    output_format = args.get("output_format", "text")

    frontmatter: dict[str, Any] = {
        "name": name,
        "description": description,
    }
    if tags:
        frontmatter["tags"] = tags
    if parameters:
        frontmatter["parameters"] = parameters
    if tools_required:
        frontmatter["tools_required"] = tools_required
    if output_format != "text":
        frontmatter["output_format"] = output_format

    path = write_skill_file(name, frontmatter, content)

    # Create meta.json sidecar
    meta = create_skill_meta(
        created_by=ctx.agent_id,
        created_from_run=getattr(ctx, "run_id", ""),
    )
    meta["content_hash"] = _content_hash(content)
    write_skill_meta(name, meta)

    logger.info("Skill '%s' created by agent '%s' at %s", name, ctx.agent_id, path)
    return {"created": True, "name": name, "path": str(path)}


@_handler("update_skill")
async def _update_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Update an existing skill with an improved version."""
    from robothor.engine.skills import (
        _MAX_CONTENT_LEN,
        _content_hash,
        load_skills,
        read_skill_meta,
        validate_skill_name,
        write_skill_file,
        write_skill_meta,
    )

    name = args.get("name", "").strip()
    err = validate_skill_name(name)
    if err:
        return {"error": err}

    # Must exist
    existing = load_skills()
    if name not in existing:
        return {"error": f"Skill '{name}' not found. Use create_skill to create it."}

    content = args.get("content", "").strip()
    if not content:
        return {"error": "content (new markdown body) is required"}
    if len(content) > _MAX_CONTENT_LEN:
        return {"error": f"content exceeds {_MAX_CONTENT_LEN} char limit ({len(content)} chars)"}

    reason = args.get("reason", "")
    new_description = args.get("description")

    # Load existing definition to preserve frontmatter fields
    old_defn = existing[name]
    frontmatter: dict[str, Any] = {
        "name": name,
        "description": new_description or old_defn.description,
    }
    if old_defn.tags:
        frontmatter["tags"] = list(old_defn.tags)
    if old_defn.parameters:
        frontmatter["parameters"] = [
            {
                "name": p.name,
                "type": p.type,
                "description": p.description,
                "required": p.required,
                **({"default": p.default} if p.default is not None else {}),
            }
            for p in old_defn.parameters
        ]
    if old_defn.tools_required:
        frontmatter["tools_required"] = list(old_defn.tools_required)
    if old_defn.output_format != "text":
        frontmatter["output_format"] = old_defn.output_format

    # Archive previous version hash in meta
    meta = read_skill_meta(name) or {}
    old_hash = meta.get("content_hash", "")
    revision = meta.get("revision", 1) + 1

    history = meta.get("revision_history", [])
    history.append(
        {
            "revision": meta.get("revision", 1),
            "date": datetime.now(UTC).isoformat(),
            "agent": ctx.agent_id,
            "content_hash": old_hash,
            "reason": reason,
        }
    )

    path = write_skill_file(name, frontmatter, content)

    meta.update(
        {
            "revision": revision,
            "content_hash": _content_hash(content),
            "last_revised_by": ctx.agent_id,
            "last_revised_at": datetime.now(UTC).isoformat(),
            "revision_history": history,
        }
    )
    write_skill_meta(name, meta)

    logger.info("Skill '%s' updated to revision %d by '%s'", name, revision, ctx.agent_id)
    return {"updated": True, "name": name, "revision": revision, "path": str(path)}

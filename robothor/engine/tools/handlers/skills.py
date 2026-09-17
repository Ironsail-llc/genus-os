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


def _shadow_refusal(name: str, args: dict[str, Any], verb: str) -> dict[str, Any] | None:
    """Refuse to start shadowing a bundled skill unless asked to, by name.

    Writing ``<name>`` into the instance tree while the platform ships a skill
    of the same name does not change the platform's file — it stands in front
    of it. Every agent then reads the instance's procedure while the tracked
    one still looks live to anyone reading the repository. That is a decision
    an operator should be able to find, so it is taken explicitly:
    ``shadow_bundled=true``. The same shape as the archive refusal.

    Returns an error dict to hand straight back, or None when the write may
    proceed (the name is not bundled, an overlay already exists, or the caller
    asked for it).
    """
    from robothor.engine.skills import bundled_skill_exists, instance_skill_exists

    if args.get("shadow_bundled") is True:
        return None
    if not bundled_skill_exists(name) or instance_skill_exists(name):
        return None
    return {
        "error": (
            f"'{name}' is a skill the platform ships. {verb} it here would write an "
            f"instance copy that shadows the bundled one for every agent, while the "
            f"bundled file stays untouched on disk. Pass shadow_bundled=true to do "
            f"that deliberately, or choose a different name."
        ),
        "refused_by": "shadow_gate",
        "bundled_skill": name,
    }


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
    """Return catalog of available skills.

    Every row says which layer it came from. ``origin`` is ``instance`` for a
    skill this instance wrote and ``platform`` for one the platform ships;
    ``shadows_bundled`` marks the rows where the instance's copy is being read
    INSTEAD of a bundled skill of the same name, which is otherwise invisible —
    the platform's file is untouched on disk.
    """
    from robothor.engine.skills import (
        INSTANCE_ORIGIN,
        load_skills,
        read_skill_view,
        shadows_bundled,
        skill_origin,
    )

    skills = load_skills()
    rows: list[dict[str, Any]] = []
    for s in skills.values():
        view = read_skill_view(s.name)
        origin = skill_origin(view)
        row: dict[str, Any] = {
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
            "origin": origin,
            "shadows_bundled": shadows_bundled(s.name),
            # Kept for callers that read it, but derived from the origin marker
            # rather than the legacy flag: an overlay written by update_skill
            # carries no `auto_generated` and is still the instance's.
            "auto_generated": origin == INSTANCE_ORIGIN,
        }
        if view is not None:
            row["usage_count"] = view.get("usage_count", 0)
        rows.append(row)
    return {"skills": rows}


@_handler("skill_view")
async def _skill_view(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the full body of one skill on demand (Rip 3).

    Pair with the lean ``build_skill_catalog`` that emits only
    name + truncated description in the system prompt. The agent
    sees the catalog cheaply, then calls ``skill_view(name=...)``
    when it actually needs the procedure body. Bumps the usage
    counter so the curator (Rip 5) can rank stale skills.
    """
    from robothor.engine.skills import (
        get_skill_content,
        increment_usage,
        load_skills,
        read_skill_view,
        shadows_bundled,
        skill_origin,
    )

    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}

    content = get_skill_content(name)
    if content is None:
        return {"error": f"Skill '{name}' not found"}

    skills = load_skills()
    defn = skills[name]
    view = read_skill_view(name) or {}

    # Side effect: bump the usage counter so the curator can
    # distinguish hot skills (don't archive) from cold ones
    # (candidate for consolidation).
    try:
        increment_usage(name)
    except Exception as exc:  # noqa: BLE001 — counter is best-effort
        logger.debug("skill_view increment_usage failed for %s: %s", name, exc)

    return {
        "name": defn.name,
        "description": defn.description,
        "content": content,
        "tags": list(defn.tags),
        "parameters": [
            {
                "name": p.name,
                "type": p.type,
                "description": p.description,
                "required": p.required,
                **({"default": p.default} if p.default is not None else {}),
            }
            for p in defn.parameters
        ],
        "trigger_phrases": list(defn.trigger_phrases),
        "tools_required": list(defn.tools_required),
        "output_format": defn.output_format,
        # Which layer this body came from, and whether it is standing in front
        # of a bundled skill of the same name.
        "origin": skill_origin(view),
        "shadows_bundled": shadows_bundled(name),
        "write_origin": view.get("write_origin", "foreground"),
        "is_agent_created": view.get("is_agent_created", False),
        "usage_count": view.get("usage_count", 0),
    }


@_handler("create_skill")
async def _create_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a new reusable skill from a procedure."""
    from robothor.engine.skills import (
        _MAX_CONTENT_LEN,
        _content_hash,
        create_skill_meta,
        create_skill_state,
        instance_skill_exists,
        is_instance_skill_meta,
        read_skill_meta,
        shadows_bundled,
        validate_skill_name,
        write_skill_file,
        write_skill_meta,
        write_skill_state,
    )

    name = args.get("name", "").strip()
    err = validate_skill_name(name)
    if err:
        return {"error": err}

    description = args.get("description", "").strip()
    if not description:
        return {"error": "description is required"}

    # Rip 2: class-level umbrella guardrail. Off by default; flipped on
    # via ROBOTHOR_RIP_2_ENABLED. Rejects names that look like one-off
    # session artifacts (PR numbers, fix-/debug-/audit- prefixes, single
    # library names, dates, error strings) — the failure mode that
    # produced Nightwatch's duplicate alerts.py PR spam.
    from robothor.engine.feature_flags import is_rip_enabled
    from robothor.engine.skills import class_level_check

    if is_rip_enabled(2):
        reason = class_level_check(name, description)
        if reason:
            return {
                "error": (
                    f"Skill name '{name}' rejected: {reason}. "
                    "Re-target as a CLASS-LEVEL umbrella (broader category, durable "
                    "across sessions) or PATCH an existing skill via update_skill."
                ),
                "rejected_by": "class_level_check",
            }

    content = args.get("content", "").strip()
    if not content:
        return {"error": "content (markdown body) is required"}
    if len(content) > _MAX_CONTENT_LEN:
        return {"error": f"content exceeds {_MAX_CONTENT_LEN} char limit ({len(content)} chars)"}

    overwrite = args.get("overwrite", False)

    # A name the platform ships is not a collision to overwrite — the write
    # would shadow it. That decision goes through the one gate.
    refusal = _shadow_refusal(name, args, "Creating")
    if refusal is not None:
        return refusal

    # Check for collisions with what is already AT THE WRITE TARGET. A bundled
    # skill of the same name is not a collision to overwrite — the write would
    # shadow it, which the gate above has already settled. Whose the existing
    # skill is comes from the origin marker, not the legacy `auto_generated`
    # flag: an overlay written by update_skill carries no such flag and is
    # still the instance's own.
    if instance_skill_exists(name) and not overwrite:
        existing_meta = read_skill_meta(name)
        if is_instance_skill_meta(existing_meta):
            return {
                "error": (
                    f"Skill '{name}' already exists and belongs to this instance. "
                    "Use update_skill to revise it, or set overwrite=true to replace."
                ),
            }
        return {
            "error": (
                f"Skill '{name}' already exists and was hand-authored. "
                "Set overwrite=true to replace it."
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
    )
    meta["content_hash"] = _content_hash(content)

    # Rip 4: stamp the write origin so the curator (Rip 5) can tell
    # autonomous-fork creates from user-directed creates. Foreground
    # writes (default origin) stay user-owned and are never auto-
    # consolidated; background-review-fork writes are explicitly
    # opt-in to curator management.
    from robothor.engine.skill_provenance import (
        get_current_write_origin,
        is_agent_authored_origin,
    )

    origin = get_current_write_origin()
    meta["write_origin"] = origin
    # Both the review fork and the curator are agent-authored (curator-eligible).
    meta["is_agent_created"] = is_agent_authored_origin(origin)

    write_skill_meta(name, meta)
    # Runtime telemetry starts fresh in the gitignored state.json sidecar.
    write_skill_state(name, create_skill_state())

    logger.info(
        "Skill '%s' created by agent '%s' (origin=%s) at %s",
        name,
        ctx.agent_id,
        origin,
        path,
    )
    return {
        "created": True,
        "name": name,
        "path": str(path),
        "write_origin": origin,
        "is_agent_created": meta["is_agent_created"],
        "shadows_bundled": shadows_bundled(name),
    }


@_handler("skill_archive")
async def _skill_archive(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Move an agent-created skill to the instance's ``.archive/`` — reversible.

    The curator's only destructive action (Rip 5). Refuses pinned skills and
    anything the platform ships, so retirement can never delete a tracked
    file. Content-preserving (a move, not a delete), so a wrongly archived
    skill is recovered by moving it back. A stray still sitting in the
    platform tree from before the instance split is archived OUT of it.
    """
    import shutil

    import robothor.engine.skills as _skills_mod
    from robothor.engine.skills import (
        instance_skills_dir,
        is_instance_skill_meta,
        read_skill_view,
        resolve_skill_dir,
    )

    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}

    try:
        src = resolve_skill_dir(name)
    except ValueError:
        return {"error": f"Skill '{name}' not found"}
    if src is None:
        return {"error": f"Skill '{name}' not found"}

    meta = read_skill_view(name) or {}
    if meta.get("pinned"):
        return {"error": f"Skill '{name}' is pinned — archive refused"}
    if not is_instance_skill_meta(meta):
        return {
            "error": (f"Skill '{name}' is operator-authored or platform-bundled — archive refused")
        }

    unshadowing = _skills_mod.shadows_bundled(name)

    archive_dir = instance_skills_dir() / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(src), str(dest))
    _skills_mod._skills_cache = None  # hot-reload picks up the removal
    if unshadowing:
        # Nothing was retired: the bundled skill of the same name is simply
        # live again, and saying "archived" would read as one fewer skill.
        logger.info(
            "Skill '%s' un-shadowed by '%s'; the bundled skill is live again (overlay at %s)",
            name,
            ctx.agent_id,
            dest,
        )
        return {
            "unshadowed_bundled": name,
            "path": str(dest),
            "note": (
                f"The instance's copy of '{name}' was moved aside, so agents now read the "
                f"skill the platform ships. Nothing was retired."
            ),
        }
    logger.info("Skill '%s' archived to %s by '%s'", name, dest, ctx.agent_id)
    return {"archived": name, "path": str(dest)}


@_handler("update_skill")
async def _update_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Update an existing skill with an improved version."""
    from robothor.engine.skills import (
        _MAX_CONTENT_LEN,
        INSTANCE_ORIGIN,
        RUNTIME_STATE_KEYS,
        _content_hash,
        load_skills,
        read_skill_meta,
        shadows_bundled,
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

    # Revising a bundled skill writes an instance copy that shadows it; the
    # platform's file is never rewritten. That is a decision, not a side effect.
    refusal = _shadow_refusal(name, args, "Updating")
    if refusal is not None:
        return refusal

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

    # Archive previous version hash in meta. Strip any legacy runtime keys
    # (pre-migration meta.json) so they are never re-persisted — runtime
    # state lives in the state.json sidecar.
    meta = read_skill_meta(name) or {}
    for key in RUNTIME_STATE_KEYS:
        meta.pop(key, None)
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

    # The write lands in the instance tree — a bundled skill is shadowed by an
    # overlay, never rewritten in place — so the copy is instance data whatever
    # the platform's own meta.json said.
    meta.update(
        {
            "origin": INSTANCE_ORIGIN,
            "revision": revision,
            "content_hash": _content_hash(content),
            "last_revised_by": ctx.agent_id,
            "last_revised_at": datetime.now(UTC).isoformat(),
            "revision_history": history,
        }
    )
    write_skill_meta(name, meta)

    shadowing = shadows_bundled(name)
    logger.info(
        "Skill '%s' updated to revision %d by '%s'%s",
        name,
        revision,
        ctx.agent_id,
        " (shadowing the bundled skill of the same name)" if shadowing else "",
    )
    return {
        "updated": True,
        "name": name,
        "revision": revision,
        "path": str(path),
        "shadows_bundled": shadowing,
    }


@_handler("get_accretion_ledger")
async def _get_accretion_ledger(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Audit what the fleet has learned: agent-authored skills + git history + usage."""
    import asyncio

    from robothor.engine.accretion import get_accretion_ledger

    return await asyncio.to_thread(get_accretion_ledger, int(args.get("limit", 30)))

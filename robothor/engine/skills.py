"""Skill system — YAML/Markdown-defined higher-level operations.

Skills are structured prompts that agents can invoke via the `invoke_skill` tool.
Each skill is a SKILL.md file with YAML frontmatter (name, description) and a
markdown body containing step-by-step instructions.

The LLM is the orchestrator — skills are just instructions, not automated pipelines.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_skills_cache: tuple[float, dict[str, SkillDefinition]] | None = None

_KEBAB_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])?$")
_MAX_CONTENT_LEN = 10_000


@dataclass(frozen=True)
class SkillParameter:
    """A typed parameter for a skill."""

    name: str
    type: str = "string"  # string, integer, float, boolean, file_glob
    description: str = ""
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class SkillDefinition:
    """A single skill parsed from a SKILL.md file."""

    name: str
    description: str
    content: str  # Full markdown body (without frontmatter)
    path: str  # Relative path to the SKILL.md file
    tags: tuple[str, ...] = ()
    tools_required: tuple[str, ...] = ()
    trigger_phrases: tuple[str, ...] = ()
    parameters: tuple[SkillParameter, ...] = ()
    output_format: str = "text"  # "text" or "json"
    composable: bool = False  # can invoke other skills mid-execution
    depends_on: tuple[str, ...] = ()  # prerequisite skills


def _parse_skill_file(path: Path) -> SkillDefinition | None:
    """Parse a SKILL.md file with YAML frontmatter."""
    try:
        text = path.read_text()
    except Exception as e:
        logger.debug("Failed to read skill file %s: %s", path, e)
        return None

    # Parse YAML frontmatter (--- delimited)
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not match:
        logger.debug("No YAML frontmatter in %s", path)
        return None

    frontmatter_text = match.group(1)
    body = match.group(2).strip()

    # Parse YAML frontmatter — use PyYAML for full nested structure support,
    # fall back to simple line parser if unavailable or parse fails.
    meta: dict[str, Any] = {}
    try:
        import yaml

        parsed = yaml.safe_load(frontmatter_text)
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:
        # Fallback: simple line-by-line parser (key: value, inline lists)
        for line in frontmatter_text.strip().split("\n"):
            line = line.strip()
            if ":" in line:
                key, _, value = line.partition(":")
                value = value.strip()
                if value.startswith("[") and value.endswith("]"):
                    items = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
                    meta[key.strip()] = items
                else:
                    meta[key.strip()] = value

    name = meta.get("name", "")
    description = meta.get("description", "")
    if not name:
        logger.debug("Skill file %s missing name", path)
        return None

    # Parse parameters list (each item is a dict or simple key: value)
    raw_params = meta.get("parameters", [])
    params: list[SkillParameter] = []
    if isinstance(raw_params, list):
        for p in raw_params:
            if isinstance(p, dict):
                params.append(
                    SkillParameter(
                        name=p.get("name", ""),
                        type=p.get("type", "string"),
                        description=p.get("description", ""),
                        required=p.get("required", False),
                        default=p.get("default"),
                    )
                )
            elif isinstance(p, str):
                params.append(SkillParameter(name=p))

    return SkillDefinition(
        name=name,
        description=description,
        content=body,
        path=str(path),
        tags=tuple(meta.get("tags", [])),
        tools_required=tuple(meta.get("tools_required", [])),
        trigger_phrases=tuple(meta.get("trigger_phrases", [])),
        parameters=tuple(params),
        output_format=meta.get("output_format", "text"),
        composable=meta.get("composable", "false").lower() in ("true", "yes", "1")
        if isinstance(meta.get("composable"), str)
        else bool(meta.get("composable", False)),
        depends_on=tuple(meta.get("depends_on", [])),
    )


def load_skills(skills_dir: Path | None = None) -> dict[str, SkillDefinition]:
    """Load all skills from agents/skills/*/SKILL.md, cached by mtime."""
    global _skills_cache

    if skills_dir is None:
        skills_dir = _skills_dir()

    if not skills_dir.exists():
        return {}

    # Check mtimes for cache invalidation
    max_mtime = 0.0
    skill_files = list(skills_dir.glob("*/SKILL.md"))
    for fp in skill_files:
        with contextlib.suppress(OSError):
            max_mtime = max(max_mtime, fp.stat().st_mtime)

    if _skills_cache and _skills_cache[0] == max_mtime:
        return _skills_cache[1]

    skills: dict[str, SkillDefinition] = {}
    for fp in sorted(skill_files):
        defn = _parse_skill_file(fp)
        if defn:
            skills[defn.name] = defn

    _skills_cache = (max_mtime, skills)
    logger.debug("Loaded %d skills from %s", len(skills), skills_dir)
    return skills


def get_skill_content(name: str) -> str | None:
    """Return the full content of a skill by name, or None if not found."""
    skills = load_skills()
    defn = skills.get(name)
    return defn.content if defn else None


def build_skill_catalog(skills: dict[str, SkillDefinition] | None = None) -> str:
    """Build a system prompt section listing available skills."""
    if skills is None:
        skills = load_skills()

    if not skills:
        return ""

    lines = ["## Available Skills", ""]
    lines.append("Use `invoke_skill` with `name` and optional `args` dict.")
    lines.append("")
    for defn in skills.values():
        if defn.parameters:
            sig_parts = []
            for p in defn.parameters:
                if p.default is not None:
                    sig_parts.append(f"{p.name}={p.default}")
                elif not p.required:
                    sig_parts.append(f"{p.name}=None")
                else:
                    sig_parts.append(p.name)
            sig = f"({', '.join(sig_parts)})"
        else:
            sig = ""
        trigger = f" (triggers: {', '.join(defn.trigger_phrases)})" if defn.trigger_phrases else ""
        lines.append(f"- **{defn.name}**{sig}: {defn.description}{trigger}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill authoring helpers (used by create_skill / update_skill tools)
# ---------------------------------------------------------------------------


def _skills_dir() -> Path:
    """Return canonical skills directory path."""
    return (
        Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
        / "agents"
        / "skills"
    )


def _meta_path(skill_name: str, base: Path | None = None) -> Path:
    base = base or _skills_dir()
    result = (base / skill_name / "meta.json").resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError(f"Skill name {skill_name!r} resolves outside skills directory")
    return result


def _skill_path(skill_name: str, base: Path | None = None) -> Path:
    base = base or _skills_dir()
    result = (base / skill_name / "SKILL.md").resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError(f"Skill name {skill_name!r} resolves outside skills directory")
    return result


def validate_skill_name(name: str) -> str | None:
    """Return an error message if *name* is invalid, else None."""
    if not name:
        return "name is required"
    if not _KEBAB_RE.match(name):
        return (
            f"name must be kebab-case (lowercase letters, digits, hyphens), "
            f"3-60 chars, got: {name!r}"
        )
    return None


def read_skill_meta(name: str, base: Path | None = None) -> dict[str, Any] | None:
    """Read meta.json sidecar for a skill, or None if missing."""
    path = _meta_path(name, base)
    if not path.exists():
        return None
    try:
        result: dict[str, Any] = json.loads(path.read_text())
        return result
    except Exception as e:
        logger.warning("Failed to read skill meta %s: %s", path, e)
        return None


def write_skill_meta(name: str, meta: dict[str, Any], base: Path | None = None) -> None:
    """Write meta.json sidecar for a skill."""
    path = _meta_path(name, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, default=str) + "\n")


def write_skill_file(
    name: str,
    frontmatter: dict[str, Any],
    body: str,
    base: Path | None = None,
) -> Path:
    """Write a SKILL.md file with YAML frontmatter and markdown body.

    Returns the path to the written file.
    """
    import yaml

    path = _skill_path(name, base)
    path.parent.mkdir(parents=True, exist_ok=True)

    fm_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
    content = f"---\n{fm_text}\n---\n\n{body.strip()}\n"
    path.write_text(content)

    # Invalidate cache so hot-reload picks up the new file
    global _skills_cache
    _skills_cache = None

    return path


def increment_usage(name: str, base: Path | None = None) -> None:
    """Increment usage_count in a skill's meta.json (if it exists)."""
    meta = read_skill_meta(name, base)
    if meta is None:
        return
    meta["usage_count"] = meta.get("usage_count", 0) + 1
    meta["last_used"] = datetime.now(UTC).isoformat()
    write_skill_meta(name, meta, base)


def create_skill_meta(
    *,
    created_by: str = "",
) -> dict[str, Any]:
    """Build initial meta.json for a newly created skill."""
    return {
        "auto_generated": True,
        "created_by": created_by,
        "created_at": datetime.now(UTC).isoformat(),
        "revision": 1,
        "usage_count": 0,
        "last_used": None,
        "revision_history": [],
    }


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

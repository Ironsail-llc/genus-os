"""Skill system — YAML/Markdown-defined higher-level operations.

Skills are structured prompts that agents can invoke via the `invoke_skill` tool.
Each skill is a SKILL.md file with YAML frontmatter (name, description) and a
markdown body containing step-by-step instructions.

The LLM is the orchestrator — skills are just instructions, not automated pipelines.
"""

from __future__ import annotations

import contextlib
import copy
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
    """Build a system prompt section listing available skills.

    Two modes:

    * **Rip 3 lean catalog** (``ROBOTHOR_RIP_3_ENABLED=1``) — emits
      one line per skill: ``- /name — description``. Description is
      truncated to 100 chars (agentskills.io frontmatter convention).
      No signatures, no triggers, no per-skill bullet structure. The
      agent loads bodies on demand via the ``skill_view`` tool. This
      keeps the per-turn system prompt small and scales to hundreds
      of skills.

    * **Legacy catalog** (default, when Rip 3 off) — full per-skill
      description plus parameter signature plus trigger phrases.
      Preserves backwards compatibility while Rip 3 rolls out.
    """
    if skills is None:
        skills = load_skills()

    # Anti-bloat (Phase 3): never surface archived agent-skills in the prompt.
    # compute_skill_state is time-derived, so this holds even if no curator pass
    # has run. Pinned / operator-authored skills are never archived.
    _now = datetime.now(UTC)
    skills = {n: d for n, d in skills.items() if not _skill_is_archived(n, _now)}

    if not skills:
        return ""

    from robothor.engine.feature_flags import is_rip_enabled

    if is_rip_enabled(3):
        lines = ["## Available Skills"]
        lines.append(
            "Call `skill_view(name=...)` to load any skill's full body before "
            "invoking it. Call `invoke_skill(name=..., args=...)` to run."
        )
        lines.append("")
        for defn in skills.values():
            desc = (defn.description or "").strip()
            if len(desc) > 100:
                desc = desc[:97] + "..."
            lines.append(f"- /{defn.name} — {desc}")
        return "\n".join(lines)

    # Legacy verbose catalog (pre-Rip 3 behaviour).
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


def _state_path(skill_name: str, base: Path | None = None) -> Path:
    base = base or _skills_dir()
    result = (base / skill_name / "state.json").resolve()
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


# ── Rip 2: class-level umbrella naming guardrail ─────────────────────
# Ported from the Hermes background-review prompt rules
# (/tmp/research/hermes-agent/agent/background_review.py:100-105).
# These patterns are exactly the failure mode Nightwatch hit:
# the same `test: add unit tests for robothor/engine/alerts.py` PR
# (#109/110/112/113) recreated daily because the skill name was
# scoped to a one-off task instead of the class of work.
#
# The check fires from _create_skill / _update_skill before any
# write happens; on rejection, the agent gets a structured error
# explaining which class-level pattern to use instead.

_CLASS_LEVEL_REJECTIONS: tuple[tuple[str, str], ...] = (
    # Each tuple is (regex_pattern, human_reason).
    (r"-pr-\d+", "skill names must not embed PR numbers"),
    (r"-#\d+", "skill names must not embed issue numbers"),
    (r"^fix-", "names starting with 'fix-' describe a one-off bug, not a class of work"),
    (r"^debug-", "names starting with 'debug-' describe a one-off session"),
    (r"^audit-", "names starting with 'audit-' describe a one-off review"),
    (
        r"-(today|yesterday|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)$",
        "skill names must not embed dates or day-of-week — they should describe a "
        "durable class of work, not a session timestamp",
    ),
    (
        r"-(error|exception|traceback|crash|stacktrace)\b",
        "skill names must not embed error keywords — capture the FIX as a class, not "
        "the error string",
    ),
)


_KNOWN_SINGLE_LIBRARY_NAMES: frozenset[str] = frozenset(
    {
        "requests",
        "pandas",
        "numpy",
        "pydantic",
        "fastapi",
        "django",
        "flask",
        "sqlalchemy",
        "asyncio",
        "psycopg2",
        "celery",
        "redis",
    }
)


def class_level_check(name: str, description: str = "") -> str | None:
    """Return rejection reason if *name* looks like a one-off session
    artifact rather than a class-level umbrella.

    Returns ``None`` when the name passes. Reasons match the
    background-review prompt's "do not capture" list so the
    autonomous review fork that creates skills can produce
    consistent, durable names instead of the spam Nightwatch
    generated.

    Called from ``_create_skill`` and ``_update_skill`` only when
    Rip 2 is enabled (``ROBOTHOR_RIP_2_ENABLED=1``); off by
    default to keep existing operator-authored creates unblocked
    while the rip is rolling out.
    """
    import re

    lowered = name.lower()

    for pattern, reason in _CLASS_LEVEL_REJECTIONS:
        if re.search(pattern, lowered):
            return reason

    # Bare single-word library names are valid Python identifiers but
    # are too narrow for a class-level skill — there's no "shape" of
    # task they describe, just the dependency.
    if lowered in _KNOWN_SINGLE_LIBRARY_NAMES:
        return (
            f"'{name}' is a single library name — skill names should describe a "
            "class of WORK ('database-migrations', 'api-client-debugging') not a "
            "single dependency"
        )

    # All-digit names (e.g. raw error codes copied verbatim) are
    # always one-off artifacts.
    if re.fullmatch(r"[\d\-]+", lowered):
        return "skill names must not be all digits / dashes — that's an error code, not a class"

    return None


# BUG-7: mtime-keyed cache so build_skill_catalog's per-skill meta read (one
# per skill, every prompt) doesn't re-parse JSON from disk each time. Returns a
# deep copy so callers that mutate-then-write (increment_usage, apply_skill_
# lifecycle) can't corrupt the cached object.
_meta_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def read_skill_meta(name: str, base: Path | None = None) -> dict[str, Any] | None:
    """Read meta.json sidecar for a skill, or None if missing."""
    path = _meta_path(name, base)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(path)
    cached = _meta_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return copy.deepcopy(cached[1])
    try:
        result: dict[str, Any] = json.loads(path.read_text())
    except Exception as e:
        logger.warning("Failed to read skill meta %s: %s", path, e)
        return None
    _meta_cache[key] = (mtime, result)
    return copy.deepcopy(result)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write *payload* as JSON via tmp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    tmp.replace(path)


def write_skill_meta(name: str, meta: dict[str, Any], base: Path | None = None) -> None:
    """Write meta.json (static, tracked metadata) for a skill."""
    path = _meta_path(name, base)
    _atomic_write_json(path, meta)
    with contextlib.suppress(OSError):
        _meta_cache[str(path)] = (path.stat().st_mtime, copy.deepcopy(meta))


# ── Runtime state sidecar (state.json) ───────────────────────────────
# meta.json is tracked in git and must stay byte-stable at runtime. All
# mutable telemetry (usage_count, last_used) lives in a gitignored
# state.json sidecar next to it; lifecycle "state" is never persisted at
# all — it is pure-derived via compute_skill_state. Callers should read
# skills through read_skill_view so they never learn about the split.

#: Runtime keys that must never be (re-)persisted into meta.json.
RUNTIME_STATE_KEYS = ("usage_count", "last_used", "state")

_state_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def read_skill_state(name: str, base: Path | None = None) -> dict[str, Any] | None:
    """Read state.json runtime sidecar for a skill, or None if missing."""
    path = _state_path(name, base)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(path)
    cached = _state_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return copy.deepcopy(cached[1])
    try:
        result: dict[str, Any] = json.loads(path.read_text())
    except Exception as e:
        logger.warning("Failed to read skill state %s: %s", path, e)
        return None
    _state_cache[key] = (mtime, result)
    return copy.deepcopy(result)


def write_skill_state(name: str, state: dict[str, Any], base: Path | None = None) -> None:
    """Atomically write state.json runtime sidecar for a skill."""
    path = _state_path(name, base)
    _atomic_write_json(path, state)
    with contextlib.suppress(OSError):
        _state_cache[str(path)] = (path.stat().st_mtime, copy.deepcopy(state))


def create_skill_state() -> dict[str, Any]:
    """Build a fresh state.json payload for a newly created skill."""
    return {"usage_count": 0, "last_used": None}


def read_skill_view(
    name: str, base: Path | None = None, now: datetime | None = None
) -> dict[str, Any] | None:
    """Merged skill record: meta.json static fields + state.json runtime
    fields + derived lifecycle ``state``.

    The one accessor callers should use — it hides the meta/state split.
    Back-compat: legacy runtime keys still living in meta.json (pre-
    migration) are used as fallback; the sidecar wins when both exist.
    Returns None when the skill has neither file.
    """
    meta = read_skill_meta(name, base)
    state = read_skill_state(name, base)
    if meta is None and state is None:
        return None
    view: dict[str, Any] = dict(meta or {})
    if state:
        for key in ("usage_count", "last_used"):
            if key in state:
                view[key] = state[key]
    view.setdefault("usage_count", 0)
    view.setdefault("last_used", None)
    view["state"] = compute_skill_state(view, now)
    return view


def migrate_skill_runtime_state(base: Path | None = None) -> dict[str, list[str]]:
    """One-shot, idempotent migration: move runtime keys out of meta.json.

    For every ``<skill>/meta.json`` still carrying runtime keys
    (usage_count, last_used, state): seed ``state.json`` with the runtime
    values (an existing sidecar wins) and rewrite meta.json without them,
    preserving key order. Safe to re-run — a second pass finds nothing to
    move and touches no files.

    Returns {"migrated": [...], "unchanged": [...], "errors": [...]}.
    """
    root = base or _skills_dir()
    result: dict[str, list[str]] = {"migrated": [], "unchanged": [], "errors": []}
    for meta_path in sorted(root.glob("*/meta.json")):
        name = meta_path.parent.name
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as e:
            logger.warning("migrate-state: unreadable meta.json for %s: %s", name, e)
            result["errors"].append(name)
            continue
        present = [k for k in RUNTIME_STATE_KEYS if k in meta]
        if not present:
            result["unchanged"].append(name)
            continue
        existing = read_skill_state(name, root) or {}
        new_state = {
            "usage_count": existing.get("usage_count", meta.get("usage_count", 0) or 0),
            "last_used": existing.get("last_used", meta.get("last_used")),
        }
        write_skill_state(name, new_state, root)
        for key in present:
            meta.pop(key, None)
        write_skill_meta(name, meta, root)
        result["migrated"].append(name)
    return result


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
    """Increment usage_count in a skill's state.json sidecar.

    No-op when the skill directory doesn't exist. Legacy runtime keys
    still living in meta.json (pre-migration) seed the counter so no
    history is lost the first time the sidecar is written.
    """
    try:
        skill_dir = _state_path(name, base).parent
    except ValueError:
        return
    if not skill_dir.is_dir():
        return
    state = read_skill_state(name, base)
    if state is None:
        legacy = read_skill_meta(name, base) or {}
        state = {
            "usage_count": legacy.get("usage_count", 0) or 0,
            "last_used": legacy.get("last_used"),
        }
    state["usage_count"] = int(state.get("usage_count", 0) or 0) + 1
    state["last_used"] = datetime.now(UTC).isoformat()
    write_skill_state(name, state, base)


def create_skill_meta(
    *,
    created_by: str = "",
) -> dict[str, Any]:
    """Build initial meta.json (static fields only) for a newly created skill.

    Runtime telemetry lives in the state.json sidecar — see create_skill_state.
    """
    return {
        "auto_generated": True,
        "created_by": created_by,
        "created_at": datetime.now(UTC).isoformat(),
        "revision": 1,
        "revision_history": [],
    }


# ── Skill lifecycle / time-retirement (self-improvement Phase 3) ─────────
# Without this, autonomously-accreted skills pile up forever and bloat every
# agent's system prompt — the one guardrail whose absence makes accretion
# itself a degradation. State is derived purely from age + last use, so the
# catalog filter is self-sufficient (it never surfaces an archived agent-skill
# even if no curator pass has run). Pinned and operator-authored skills are
# never retired.
SKILL_STALE_AFTER_DAYS = 30
SKILL_ARCHIVE_AFTER_DAYS = 90


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def compute_skill_state(meta: dict[str, Any] | None, now: datetime | None = None) -> str:
    """Effective lifecycle state of a skill from its meta — pure & idempotent.

    'active' → 'stale' (unused > 30d) → 'archived' (unused > 90d). Re-use resets
    the clock automatically (last_used is the anchor). NEVER retires a pinned or
    operator-authored (is_agent_created=False) skill, nor one with no meta.
    """
    if not meta:
        return "active"
    # BUG-2: the existing corpus stamps `auto_generated`; only the RIP_1 fork
    # path stamps `is_agent_created`. Treat either as agent-made, else the whole
    # anti-bloat guardrail is inert for every skill on disk today.
    agent_made = meta.get("is_agent_created") or meta.get("auto_generated")
    if meta.get("pinned") or not agent_made:
        return "active"
    now = now or datetime.now(UTC)
    anchor = _parse_iso(meta.get("last_used")) or _parse_iso(meta.get("created_at"))
    if anchor is None:
        return "active"
    age_days = (now - anchor).total_seconds() / 86400.0
    if age_days >= SKILL_ARCHIVE_AFTER_DAYS:
        return "archived"
    if age_days >= SKILL_STALE_AFTER_DAYS:
        return "stale"
    return "active"


def apply_skill_lifecycle(
    base: Path | None = None, now: datetime | None = None
) -> dict[str, list[str]]:
    """Report each agent-skill's derived lifecycle state — read-only.

    Lifecycle state is pure-derived (compute_skill_state) and never
    persisted: meta.json is static tracked metadata and state.json holds
    only usage telemetry. Returns {"stale": [...], "archived": [...]} for
    observability (daemon curator loop, dashboards).
    """
    now = now or datetime.now(UTC)
    report: dict[str, list[str]] = {"stale": [], "archived": []}
    for fp in _skills_dir().glob("*/SKILL.md") if base is None else base.glob("*/SKILL.md"):
        name = fp.parent.name
        view = read_skill_view(name, base, now=now)
        if view is None:
            continue
        state = view["state"]
        if state in report:
            report[state].append(name)
    return report


def _skill_is_archived(name: str, now: datetime | None = None) -> bool:
    """True if the skill should be hidden from the prompt catalog (anti-bloat)."""
    return (read_skill_view(name, now=now) or {}).get("state") == "archived"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

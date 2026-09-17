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
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: (cache key, skills). The key is the max mtime plus the file list across
#: every directory that was read -- see load_skills.
_skills_cache: tuple[tuple[float, tuple[str, ...]], dict[str, SkillDefinition]] | None = None

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
    """Load every skill both trees offer, cached by mtime.

    Without an explicit directory this reads the platform-bundled skills
    first and this instance's own second, so an instance skill of the
    same name shadows the bundled one instead of colliding with it.
    """
    global _skills_cache

    dirs = (skills_dir,) if skills_dir is not None else skill_search_paths()
    present = [d for d in dirs if d.exists()]
    if not present:
        return {}

    # Check mtimes for cache invalidation
    max_mtime = 0.0
    skill_files: list[Path] = []
    for directory in present:
        skill_files.extend(sorted(directory.glob("*/SKILL.md")))
    for fp in skill_files:
        with contextlib.suppress(OSError):
            max_mtime = max(max_mtime, fp.stat().st_mtime)

    # The file list is part of the key: a removed skill leaves the surviving
    # mtimes untouched, so mtime alone would keep serving a deleted skill.
    cache_key = (max_mtime, tuple(str(fp) for fp in skill_files))
    if _skills_cache and _skills_cache[0] == cache_key:
        return _skills_cache[1]

    skills: dict[str, SkillDefinition] = {}
    for fp in skill_files:
        defn = _parse_skill_file(fp)
        if not defn:
            continue
        shadowed = skills.get(defn.name)
        if shadowed is not None:
            # INFO, not debug: from here on every agent that invokes this name
            # reads the instance's procedure, and the platform's copy is
            # untouched on disk, so nothing else on the box would say so.
            logger.info(
                "Skill %r: %s shadows the bundled %s",
                defn.name,
                defn.path,
                shadowed.path,
            )
        skills[defn.name] = defn

    _skills_cache = (cache_key, skills)
    logger.debug("Loaded %d skills from %s", len(skills), [str(d) for d in present])
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


# ── Two trees: the platform's skills and the instance's ──────────────
#
# ``<workspace>/agents/skills`` is PLATFORM code -- tracked in git, the
# bundled skills every instance gets. A skill an agent writes there is
# instance data sitting inside the platform tree: one ``git add -A``
# commits it into the public repo, and a clean checkout deletes it.
#
# So every runtime WRITE goes to the instance directory
# (``<workspace>/brain/skills`` by default -- gitignored, exactly like
# the rest of ``brain/``, and already inside the snapshot's workspace
# roots), and every READ walks the bundled directory first and the
# instance directory second so an instance skill wins a name collision.
# Updating a bundled skill is therefore copy-on-write: the platform file
# is never rewritten, the instance gets an overlay that shadows it.

#: ``meta.json`` origin values. The marker is what the migration and the
#: boundary guard read; it is stamped on every create from this release on.
INSTANCE_ORIGIN = "instance"
PLATFORM_ORIGIN = "platform"

#: Markers on a ``meta.json`` written before ``origin`` existed that still
#: mean "the engine wrote this at runtime". ``auto_generated`` is stamped by
#: every create; ``write_origin`` / ``is_agent_created`` by the Rip 4 fork.
#: An explicit ``origin`` always wins over these -- that is how a skill the
#: platform deliberately adopted (several bundled ones were agent-written
#: first) stays platform after being committed.
_LEGACY_INSTANCE_MARKERS = ("auto_generated", "write_origin", "is_agent_created")


def _workspace_root() -> Path:
    """The instance workspace -- never a hardcoded home (CLAUDE.md rule 2)."""
    return Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))


def _skills_dir() -> Path:
    """The platform-bundled skills directory (``<workspace>/agents/skills``).

    Tracked in git. The engine reads it and never writes to it.
    """
    return _workspace_root() / "agents" / "skills"


def bundled_skills_dir() -> Path:
    """Public name for :func:`_skills_dir` -- the platform's own skills."""
    return _skills_dir()


def instance_skills_dir() -> Path:
    """Where this instance's own skills live -- every runtime write's target.

    ``ROBOTHOR_INSTANCE_SKILLS_DIR`` overrides it; empty (the default)
    means ``<workspace>/brain/skills``. Read through the settings model
    rather than the environment so the name stays declared in one place.
    """
    configured = ""
    try:
        from robothor.settings import get_settings

        configured = str(get_settings().paths.instance_skills_dir or "")
    except Exception:  # noqa: BLE001 - settings must never break a skill write
        logger.debug("settings unavailable while resolving the instance skills dir")
    if configured:
        return Path(configured).expanduser()
    return _skills_dir().parent.parent / "brain" / "skills"


def skill_search_paths() -> tuple[Path, ...]:
    """Every directory a skill can be read from, lowest precedence first."""
    return (_skills_dir(), instance_skills_dir())


def skill_origin(meta: dict[str, Any] | None) -> str:
    """Which layer a skill belongs to, read from its ``meta.json``."""
    if not meta:
        return PLATFORM_ORIGIN
    declared = meta.get("origin")
    if declared in (INSTANCE_ORIGIN, PLATFORM_ORIGIN):
        return str(declared)
    if any(meta.get(key) for key in _LEGACY_INSTANCE_MARKERS):
        return INSTANCE_ORIGIN
    if "write_origin" in meta:
        # Stamped foreground -- falsy, but still an engine write.
        return INSTANCE_ORIGIN
    return PLATFORM_ORIGIN


def is_instance_skill_meta(meta: dict[str, Any] | None) -> bool:
    """True when this skill was created by the engine, not shipped by it."""
    return skill_origin(meta) == INSTANCE_ORIGIN


def _contained(base: Path, skill_name: str, filename: str) -> Path:
    result = (base / skill_name / filename).resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError(f"Skill name {skill_name!r} resolves outside skills directory")
    return result


def _read_file_path(skill_name: str, filename: str, base: Path | None = None) -> Path:
    """Resolve one of a skill's files for READING -- instance first.

    Resolution is per FILE, not per directory: a bundled skill whose usage
    sidecar has been written into the instance tree must still read its
    ``meta.json`` from the platform tree. Returns the instance path when
    neither exists, so callers testing ``.exists()`` behave as before.
    """
    if base is not None:
        return _contained(base, skill_name, filename)
    instance = _contained(instance_skills_dir(), skill_name, filename)
    if instance.exists():
        return instance
    bundled = _contained(_skills_dir(), skill_name, filename)
    if bundled.exists():
        return bundled
    return instance


def _write_file_path(skill_name: str, filename: str, base: Path | None = None) -> Path:
    """Resolve one of a skill's files for WRITING -- always the instance."""
    return _contained(base if base is not None else instance_skills_dir(), skill_name, filename)


def resolve_skill_dir(skill_name: str, base: Path | None = None) -> Path | None:
    """The directory a skill currently lives in, or None if it lives nowhere.

    A directory is only a skill when it holds a ``SKILL.md``. Testing the
    directory instead would let a bare telemetry sidecar -- ``state.json``
    written into the instance tree for a bundled skill -- answer as if the
    instance owned the skill.
    """
    bases = (base,) if base is not None else tuple(reversed(skill_search_paths()))
    for candidate in bases:
        if candidate is None:
            continue
        try:
            skill_file = _contained(candidate, skill_name, "SKILL.md")
        except ValueError:
            return None
        if skill_file.is_file():
            return skill_file.parent
    return None


def bundled_skill_exists(name: str) -> bool:
    """True when the platform ships a skill under this name."""
    try:
        return _contained(_skills_dir(), name, "SKILL.md").is_file()
    except ValueError:
        return False


def instance_skill_exists(name: str) -> bool:
    """True when this instance has its own skill under this name."""
    try:
        return _contained(instance_skills_dir(), name, "SKILL.md").is_file()
    except ValueError:
        return False


def shadows_bundled(name: str) -> bool:
    """True when an instance skill is standing in front of a bundled one.

    Agents read the instance's copy; the platform's file is untouched on
    disk, so nothing in a checkout reveals the substitution. Every surface
    that reports a skill reports this.
    """
    return instance_skill_exists(name) and bundled_skill_exists(name)


def shadowed_skill_names() -> tuple[str, ...]:
    """Every bundled skill this instance has replaced, sorted."""
    instance_dir = instance_skills_dir()
    if not instance_dir.is_dir():
        return ()
    names = {fp.parent.name for fp in instance_dir.glob("*/SKILL.md")}
    return tuple(sorted(name for name in names if bundled_skill_exists(name)))


def _meta_path(skill_name: str, base: Path | None = None) -> Path:
    return _read_file_path(skill_name, "meta.json", base)


def _state_path(skill_name: str, base: Path | None = None) -> Path:
    return _read_file_path(skill_name, "state.json", base)


def _skill_path(skill_name: str, base: Path | None = None) -> Path:
    return _read_file_path(skill_name, "SKILL.md", base)


def _meta_write_path(skill_name: str, base: Path | None = None) -> Path:
    return _write_file_path(skill_name, "meta.json", base)


def _state_write_path(skill_name: str, base: Path | None = None) -> Path:
    return _write_file_path(skill_name, "state.json", base)


def _skill_write_path(skill_name: str, base: Path | None = None) -> Path:
    return _write_file_path(skill_name, "SKILL.md", base)


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
    if not isinstance(result, dict):
        logger.warning("Skill meta %s is not a JSON object — ignoring", path)
        return None
    _meta_cache[key] = (mtime, result)
    return copy.deepcopy(result)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write *payload* as JSON via tmp-file + rename.

    The tmp file lives in the target's own directory (same filesystem, so
    the rename is atomic — never cross-device) and carries a unique name so
    concurrent writers in separate processes can't rename each other's tmp
    file out from under themselves. Concurrent writes are last-write-wins.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json.tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=2, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; restore normal umask-style perms so other
        # readers (dashboards, backup jobs) aren't locked out.
        tmp.chmod(0o644)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_skill_meta(name: str, meta: dict[str, Any], base: Path | None = None) -> None:
    """Write meta.json (static metadata) for a skill, into the instance tree."""
    path = _meta_write_path(name, base)
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
    if not isinstance(result, dict):
        logger.warning("Skill state %s is not a JSON object — ignoring", path)
        return None
    _state_cache[key] = (mtime, result)
    return copy.deepcopy(result)


def write_skill_state(name: str, state: dict[str, Any], base: Path | None = None) -> None:
    """Atomically write state.json runtime sidecar into the instance tree."""
    path = _state_write_path(name, base)
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

    With no explicit base it walks BOTH trees: the platform's meta.json files
    are the ones this was written for, but a skill written by an engine that
    predates the state sidecar can equally be sitting in the instance tree.
    """
    roots = (base,) if base is not None else skill_search_paths()
    result: dict[str, list[str]] = {"migrated": [], "unchanged": [], "errors": []}
    for root in roots:
        _migrate_runtime_state_in(root, result)
    return result


def _migrate_runtime_state_in(root: Path, result: dict[str, list[str]]) -> None:
    """One tree's worth of :func:`migrate_skill_runtime_state`."""
    if not root.is_dir():
        return
    for meta_path in sorted(root.glob("*/meta.json")):
        name = meta_path.parent.name
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as e:
            logger.warning("migrate-state: unreadable meta.json for %s: %s", name, e)
            result["errors"].append(name)
            continue
        if not isinstance(meta, dict):
            logger.warning("migrate-state: meta.json for %s is not a JSON object", name)
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


def migrate_instance_skills(
    bundled: Path | None = None,
    instance: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Move skills the engine created out of the platform tree. Idempotent.

    Every directory under the bundled tree whose ``meta.json`` marks it as
    instance-origin (see :func:`skill_origin`) is moved whole -- SKILL.md,
    meta.json, the state.json sidecar, any ``references/`` -- into the
    instance tree, ``.archive/`` included. A skill the platform ships is
    left where it is, and a name already present in the instance tree is
    reported as a conflict rather than overwritten: the two bodies may have
    diverged and only the operator can say which one wins.

    The walk is over ``SKILL.md``, not ``meta.json``: a stray with no sidecar
    at all is exactly the case a meta glob cannot see, and leaving it
    uncounted is how it stays in the platform tree forever. It lands in
    ``needs-review`` instead -- nothing is moved on a guess about provenance.

    Returns {"moved": [...], "skipped": [...], "unmarked": [...],
    "needs-review": [...], "conflicts": [...], "errors": [...]}:

    * ``skipped`` -- ``meta.json`` says ``origin: platform``. The platform's.
    * ``unmarked`` -- a ``meta.json`` with no origin and no legacy marker.
      Left in place; it predates the marker and nothing says whose it is.
    * ``needs-review`` -- a ``SKILL.md`` with no ``meta.json`` at all.

    With ``dry_run`` the same report is produced and nothing on disk is
    touched.
    """
    import shutil

    global _skills_cache

    src_root = bundled or _skills_dir()
    dest_root = instance or instance_skills_dir()
    result: dict[str, list[str]] = {
        "moved": [],
        "skipped": [],
        "unmarked": [],
        "needs-review": [],
        "conflicts": [],
        "errors": [],
    }
    if not src_root.is_dir():
        return result

    candidates = sorted(src_root.glob("*/SKILL.md"))
    candidates.extend(sorted(src_root.glob(".archive/*/SKILL.md")))
    for skill_file in candidates:
        skill_dir = skill_file.parent
        label = str(skill_dir.relative_to(src_root))
        meta_path = skill_dir / "meta.json"
        if not meta_path.is_file():
            result["needs-review"].append(label)
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the pass
            logger.warning("migrate-instance: unreadable meta.json for %s: %s", label, e)
            result["errors"].append(label)
            continue
        if not isinstance(meta, dict):
            logger.warning("migrate-instance: meta.json for %s is not a JSON object", label)
            result["errors"].append(label)
            continue
        if not is_instance_skill_meta(meta):
            bucket = "skipped" if meta.get("origin") == PLATFORM_ORIGIN else "unmarked"
            result[bucket].append(label)
            continue
        target = dest_root / label
        if target.exists():
            result["conflicts"].append(label)
            continue
        if not dry_run:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(skill_dir), str(target))
            except OSError as e:
                logger.warning("migrate-instance: could not move %s: %s", label, e)
                result["errors"].append(label)
                continue
        result["moved"].append(label)

    if result["moved"] and not dry_run:
        _skills_cache = None
        _meta_cache.clear()
        _state_cache.clear()
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

    path = _skill_write_path(name, base)
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

    Best-effort telemetry: the read-modify-write is not locked, so
    concurrent invokes are last-write-wins (a racing bump can be lost),
    and an I/O failure is logged rather than raised — the counter must
    never break a skill invocation.
    """
    try:
        if resolve_skill_dir(name, base) is None:
            return
    except ValueError:
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
    try:
        write_skill_state(name, state, base)
    except OSError as e:
        logger.warning("increment_usage: could not write state.json for %s: %s", name, e)


def create_skill_meta(
    *,
    created_by: str = "",
) -> dict[str, Any]:
    """Build initial meta.json (static fields only) for a newly created skill.

    Runtime telemetry lives in the state.json sidecar — see create_skill_state.
    The ``origin`` marker is what makes this skill instance data: the boundary
    guard fails if one carrying it is ever tracked under ``agents/skills/``,
    and ``migrate_instance_skills`` moves strays out of the platform tree.
    """
    return {
        "origin": INSTANCE_ORIGIN,
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
    # path stamps `is_agent_created`; creates from this release on stamp
    # `origin`. skill_origin reads all three, else the whole anti-bloat
    # guardrail is inert for some generation of skills on disk today.
    if meta.get("pinned") or not is_instance_skill_meta(meta):
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
    roots = (base,) if base is not None else skill_search_paths()
    seen: set[str] = set()
    for fp in sorted(fp for root in roots for fp in root.glob("*/SKILL.md")):
        name = fp.parent.name
        if name in seen:
            continue
        seen.add(name)
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

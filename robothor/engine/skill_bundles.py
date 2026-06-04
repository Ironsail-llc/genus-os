"""Skill bundles — load N skills atomically via a single slash (Rip 11).

Adapted from Hermes Agent ``agent/skill_bundles.py``. A bundle is a
named YAML alias that names a list of skills (and an optional
preamble instruction). When the agent invokes a bundle, the engine
renders all referenced skill bodies as one composed message — useful
when "doing a release" means following code-review + run-tests +
update-changelog + open-pr together.

Bundles live at ``agents/bundles/*.yaml``::

    name: release
    description: ship a release end-to-end
    instruction: |
      Follow each linked skill in order; do not skip safety steps.
    skills:
      - code-review
      - run-tests
      - update-changelog
      - open-pr

Lookup precedence (telegram / chat slash dispatch):

1. Bundle of the same name (rip 11; matches Hermes precedence)
2. Single skill of the same name (existing /skill-name path)
3. Unknown command → fall through to normal text handling
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_BUNDLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,59}$")


@dataclass(frozen=True)
class BundleDefinition:
    """A skill bundle parsed from a YAML file."""

    name: str
    description: str
    skills: tuple[str, ...]
    instruction: str = ""
    path: Path = field(default_factory=lambda: Path("/tmp/empty"))


def _bundles_dir() -> Path:
    """Canonical bundles directory."""
    return (
        Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
        / "agents"
        / "bundles"
    )


def _parse_bundle_file(path: Path) -> BundleDefinition | None:
    """Parse one bundle YAML. Returns None on malformed input (logged)."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bundle %s: failed to parse YAML: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Bundle %s: top-level must be a mapping", path)
        return None

    name = str(raw.get("name", "")).strip().lower()
    if not _BUNDLE_NAME_RE.match(name):
        logger.warning("Bundle %s: name '%s' must be kebab-case (3-60 chars)", path, name)
        return None
    desc = str(raw.get("description", "")).strip()
    instruction = str(raw.get("instruction", "")).strip()
    skills_raw = raw.get("skills") or []
    if not isinstance(skills_raw, list) or not skills_raw:
        logger.warning("Bundle %s: 'skills' must be a non-empty list", path)
        return None
    skills = tuple(str(s).strip() for s in skills_raw if str(s).strip())
    if not skills:
        return None

    return BundleDefinition(
        name=name, description=desc, skills=skills, instruction=instruction, path=path
    )


def load_bundles(bundles_dir: Path | None = None) -> dict[str, BundleDefinition]:
    """Load every bundle YAML under ``agents/bundles/``.

    Returns ``{name: BundleDefinition}``. Malformed files are skipped
    with a warning (mirrors load_skills' lenient parsing).
    """
    base = bundles_dir or _bundles_dir()
    if not base.is_dir():
        return {}
    result: dict[str, BundleDefinition] = {}
    for path in sorted(base.glob("*.yaml")):
        if path.name.startswith("_") or path.name == "README.yaml":
            continue
        bundle = _parse_bundle_file(path)
        if bundle is None:
            continue
        if bundle.name in result:
            logger.warning(
                "Bundle %s: duplicate name '%s' (also at %s) — keeping first",
                path,
                bundle.name,
                result[bundle.name].path,
            )
            continue
        result[bundle.name] = bundle
    return result


def get_bundle(name: str, bundles_dir: Path | None = None) -> BundleDefinition | None:
    """Look up one bundle by name."""
    return load_bundles(bundles_dir).get(name.strip().lower())


def build_bundle_invocation_message(
    bundle: BundleDefinition,
    *,
    skill_bodies: dict[str, str],
) -> str:
    """Render a single message that includes every referenced skill body.

    ``skill_bodies`` maps skill name → SKILL.md body content. The
    caller is responsible for resolving skill names to bodies (via
    ``robothor.engine.skills.get_skill_content``) so this module
    stays free of the skills DAL import cycle.
    """
    parts: list[str] = []
    parts.append(f"# Bundle: {bundle.name}")
    if bundle.description:
        parts.append(f"\n_{bundle.description}_\n")
    if bundle.instruction:
        parts.append(f"\n{bundle.instruction}\n")

    for skill_name in bundle.skills:
        body = skill_bodies.get(skill_name)
        parts.append(f"\n---\n\n## Skill: {skill_name}\n")
        if body is None:
            parts.append(f"_(skill '{skill_name}' not found — skipping)_\n")
        else:
            parts.append(body)

    return "\n".join(parts)


def resolve_slash_command(
    command: str,
    *,
    bundles_dir: Path | None = None,
    skills: dict[str, object] | None = None,
) -> tuple[str, BundleDefinition | None]:
    """Classify a ``/command`` token as bundle, skill, or unknown.

    Returns ``("bundle", definition)`` when a bundle matches the
    name, ``("skill", None)`` when a skill of that name exists
    (caller dispatches via the existing skills handler), and
    ``("unknown", None)`` otherwise. Bundle wins over skill on
    name collision — matches Hermes precedence so an operator can
    override a single skill with a multi-step bundle of the same
    name.
    """
    token = command.lstrip("/").strip().lower()
    if not token:
        return ("unknown", None)
    bundle = get_bundle(token, bundles_dir)
    if bundle is not None:
        return ("bundle", bundle)
    if skills is None:
        from robothor.engine.skills import load_skills

        loaded: dict[str, object] = dict(load_skills())
    else:
        loaded = skills
    if token in loaded:
        return ("skill", None)
    return ("unknown", None)

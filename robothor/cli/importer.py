"""Platform import framework — migrate data from other AI agent platforms.

Supports importing configuration, skills, and memory from:
- Hermes Agent (Nous Research): ~/.hermes/
- Generic bundle: robothor-import.yaml

Usage:
    robothor import hermes [--source ~/.hermes]
    robothor import generic --source /path/to/robothor-import.yaml
    robothor import auto [--source /path]  # auto-detect platform
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
import json
import logging
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Result of an import operation."""

    platform: str
    tenant_id: str
    skills_imported: int = 0
    memory_blocks_set: int = 0
    agents_imported: int = 0
    secrets_imported: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class PlatformImporter(ABC):
    """Base class for platform-specific importers."""

    platform_name: str = ""

    @abstractmethod
    def detect(self, source_path: Path) -> bool:
        """Return True if source_path contains data from this platform."""

    @abstractmethod
    def run_import(self, source: Path, tenant_id: str) -> ImportResult:
        """Import configuration, skills, and memory from the source."""


class HermesImporter(PlatformImporter):
    """Import from Hermes Agent (~/.hermes/)."""

    platform_name = "hermes"

    def detect(self, source_path: Path) -> bool:
        return (source_path / "config.yaml").exists() or (source_path / "skills").is_dir()

    def run_import(self, source: Path, tenant_id: str) -> ImportResult:
        result = ImportResult(platform="hermes", tenant_id=tenant_id)

        # Import skills (Hermes uses agentskills.io format — same as ours)
        skills_dir = source / "skills"
        if skills_dir.is_dir():
            result.skills_imported = self._import_skills(skills_dir, result)

        # Import memory (MEMORY.md, USER.md)
        result.memory_blocks_set = self._import_memory(source, tenant_id, result)

        # Import config (model preferences, etc.)
        self._import_config(source, tenant_id, result)

        return result

    def _import_skills(self, skills_dir: Path, result: ImportResult) -> int:
        from robothor.engine.skills import (
            _skills_dir,
            create_skill_meta,
            write_skill_meta,
        )

        target = _skills_dir()
        count = 0
        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            name = skill_dir.name
            target_dir = target / name

            if target_dir.exists():
                result.warnings.append(f"Skill '{name}' already exists, skipping")
                continue

            # Copy the entire skill directory (SKILL.md + references/)
            shutil.copytree(skill_dir, target_dir)

            # Create meta.json marking as imported
            meta = create_skill_meta(created_by="import:hermes")
            meta["imported_from"] = "hermes"
            meta["import_source"] = str(skill_dir)
            write_skill_meta(name, meta)

            count += 1
            logger.info("Imported skill: %s", name)

        return count

    def _import_memory(self, source: Path, tenant_id: str, result: ImportResult) -> int:
        from robothor.memory.blocks import write_block

        count = 0

        # MEMORY.md → operational_findings
        memory_md = source / "MEMORY.md"
        if memory_md.exists():
            content = memory_md.read_text().strip()
            if content:
                write_block("operational_findings", content, tenant_id=tenant_id)
                count += 1
                logger.info("Imported MEMORY.md → operational_findings")

        # USER.md → user_profile
        user_md = source / "USER.md"
        if user_md.exists():
            content = user_md.read_text().strip()
            if content:
                write_block("user_profile", content, tenant_id=tenant_id)
                count += 1
                logger.info("Imported USER.md → user_profile")

        return count

    def _import_config(self, source: Path, tenant_id: str, result: ImportResult) -> None:
        config_path = source / "config.yaml"
        if not config_path.exists():
            return

        try:
            import yaml

            config = yaml.safe_load(config_path.read_text()) or {}
        except Exception as e:
            result.warnings.append(f"Could not parse config.yaml: {e}")
            return

        # Extract model preferences into a note
        model = config.get("model") or config.get("default_model")
        if model:
            result.warnings.append(
                f"Hermes model preference: {model} — "
                "configure in agent manifests (docs/agents/*.yaml)"
            )


class GenericImporter(PlatformImporter):
    """Import from a standardized robothor-import.yaml bundle."""

    platform_name = "generic"

    def detect(self, source_path: Path) -> bool:
        if source_path.is_file():
            return source_path.suffix in (".yaml", ".yml", ".json")
        return (source_path / "robothor-import.yaml").exists()

    def run_import(self, source: Path, tenant_id: str) -> ImportResult:
        result = ImportResult(platform="generic", tenant_id=tenant_id)

        # Find the bundle file
        if source.is_file():
            bundle_path = source
        elif (source / "robothor-import.yaml").exists():
            bundle_path = source / "robothor-import.yaml"
        elif (source / "robothor-import.json").exists():
            bundle_path = source / "robothor-import.json"
        else:
            result.errors.append(f"No robothor-import.yaml or .json found at {source}")
            return result

        try:
            text = bundle_path.read_text()
            if bundle_path.suffix == ".json":
                bundle = json.loads(text)
            else:
                import yaml

                bundle = yaml.safe_load(text) or {}
        except Exception as e:
            result.errors.append(f"Failed to parse bundle: {e}")
            return result

        # Import memory blocks
        memory = bundle.get("memory", {})
        if memory:
            from robothor.memory.blocks import write_block

            for block_name, content in memory.items():
                if isinstance(content, str) and content.strip():
                    write_block(block_name, content.strip(), tenant_id=tenant_id)
                    result.memory_blocks_set += 1

        # Import secrets (if present, warn about security)
        secrets = bundle.get("secrets", {})
        if secrets:
            result.warnings.append(
                f"Bundle contains {len(secrets)} secret(s). "
                "Use 'robothor vault set' to import them securely."
            )

        # Import skills (inline definitions)
        skills = bundle.get("skills", [])
        if skills:
            result.warnings.append(
                f"Bundle contains {len(skills)} skill definition(s). "
                "Skill import from bundles is not yet supported — "
                "copy SKILL.md files to agents/skills/ manually."
            )

        return result


# Registry of available importers
_IMPORTERS: dict[str, PlatformImporter] = {
    "hermes": HermesImporter(),
    "generic": GenericImporter(),
}


def auto_detect_platform(source: Path) -> PlatformImporter | None:
    """Try each importer's detect() to find the right one."""
    for importer in _IMPORTERS.values():
        if importer.detect(source):
            return importer
    return None


def cmd_import(args: argparse.Namespace) -> int:
    """Run a platform import."""
    platform = getattr(args, "platform", "auto")
    source_str = getattr(args, "source", None)
    tenant_id = getattr(args, "tenant", "robothor-primary")

    # Default source paths per platform
    if source_str:
        source = Path(source_str).expanduser()
    elif platform == "hermes":
        source = Path.home() / ".hermes"
    else:
        print("Error: --source is required for this platform.", file=sys.stderr)
        return 1

    if not source.exists():
        print(f"Error: Source path does not exist: {source}", file=sys.stderr)
        return 1

    # Resolve importer
    if platform == "auto":
        importer = auto_detect_platform(source)
        if not importer:
            print(
                f"Error: Could not auto-detect platform at {source}. "
                "Specify --platform explicitly.",
                file=sys.stderr,
            )
            return 1
        print(f"Auto-detected platform: {importer.platform_name}")
    else:
        importer = _IMPORTERS.get(platform)
        if not importer:
            print(
                f"Error: Unknown platform '{platform}'. Available: {', '.join(_IMPORTERS.keys())}",
                file=sys.stderr,
            )
            return 1

    print(f"Importing from {importer.platform_name} ({source}) → tenant '{tenant_id}'...")
    result = importer.run_import(source, tenant_id)

    # Report
    print("\nImport complete:")
    print(f"  Skills imported:       {result.skills_imported}")
    print(f"  Memory blocks set:     {result.memory_blocks_set}")
    print(f"  Agents imported:       {result.agents_imported}")

    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  - {w}")

    if result.errors:
        print("\nErrors:", file=sys.stderr)
        for e in result.errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    return 0

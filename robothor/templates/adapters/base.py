"""
Format adapter interface — base class for all agent runtime format adapters.

Each adapter knows how to:
  1. Check if it can handle a given SKILL.md format
  2. Generate the runtime files from a template bundle + variables
  3. Validate the generated output
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class FormatAdapter:
    """Base class for format adapters."""

    format_id: str = ""

    def can_install(self, skill_md: dict[str, Any]) -> bool:
        """Check if this adapter can handle the given SKILL.md frontmatter."""
        return str(skill_md.get("format", "")).startswith(self.format_id)

    def generate_files(self, bundle_path: Path, variables: dict[str, Any]) -> dict[str, str]:
        """Generate runtime files from a template bundle.

        Returns dict mapping relative output paths to file contents.
        """
        raise NotImplementedError

    def validate(self, bundle_path: Path) -> list[str]:
        """Validate a template bundle for this format.

        Returns list of error/warning messages (empty = valid).
        """
        raise NotImplementedError

    def get_output_paths(self, manifest: dict[str, Any]) -> dict[str, str]:
        """Determine output file paths from a resolved manifest.

        Returns dict like {"manifest": "docs/agents/foo.yaml", "instruction": "brain/FOO.md"}.
        """
        raise NotImplementedError

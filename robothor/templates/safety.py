"""Filesystem and identifier safety primitives for agent bundle operations.

Template bundles are code-like inputs: installing one changes the agent fleet's
runtime instructions.  Keep the path boundary in one small module so install,
update, removal, archiving, and hub downloads all enforce the same rules.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_SAFE_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


class TemplateSecurityError(ValueError):
    """An agent bundle requested an unsafe identifier or filesystem path."""


def validate_identifier(value: object, *, label: str = "identifier") -> str:
    """Return a strict, portable kebab-case identifier or fail closed."""

    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise TemplateSecurityError(
            f"Invalid {label}: expected 1-64 lowercase letters, digits, or hyphens"
        )
    return value


def validate_sha256(value: object, *, label: str = "SHA-256") -> str:
    """Return a canonical lowercase SHA-256 digest or fail closed."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise TemplateSecurityError(f"Invalid {label}: expected 64 lowercase hexadecimal digits")
    return value


def safe_relative_path(value: object, *, label: str = "path") -> PurePosixPath:
    """Parse an untrusted workspace-relative POSIX path.

    Backslashes and colons are rejected as well as normal traversal components
    so a value cannot change meaning when a workspace is moved between POSIX
    and Windows tooling.
    """

    if not isinstance(value, str) or not value or "\x00" in value:
        raise TemplateSecurityError(f"Invalid {label}: expected a non-empty relative path")
    if "\\" in value or ":" in value:
        raise TemplateSecurityError(f"Invalid {label}: platform-specific paths are not allowed")

    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise TemplateSecurityError(
            f"Invalid {label}: absolute and traversal paths are not allowed"
        )
    return relative


def contained_path(root: Path, relative: object, *, label: str = "path") -> Path:
    """Resolve *relative* under the explicit allowed *root*.

    Existing symlinks are resolved before containment is checked.  Returning the
    resolved path also avoids subsequently traversing a validated symlink path.
    """

    root_resolved = root.resolve(strict=True)
    if not root_resolved.is_dir():
        raise TemplateSecurityError(f"Allowed root is not a directory: {root}")

    relative_path = safe_relative_path(relative, label=label)
    raw_candidate = root_resolved
    for part in relative_path.parts:
        raw_candidate /= part
        if raw_candidate.is_symlink():
            raise TemplateSecurityError(f"Invalid {label}: symlinks are not allowed")
    candidate = raw_candidate.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as error:
        raise TemplateSecurityError(f"Invalid {label}: path escapes its allowed root") from error
    return candidate


def workspace_path(
    workspace: Path,
    relative: object,
    *,
    allowed_prefix: str,
    label: str = "path",
) -> Path:
    """Resolve a path within one explicit workspace sub-root.

    The allowed sub-root itself must remain inside the resolved workspace.  This
    catches deployments where, for example, ``brain`` is a symlink to a path
    outside the appliance-owned workspace.
    """

    workspace_resolved = workspace.resolve(strict=True)
    if not workspace_resolved.is_dir():
        raise TemplateSecurityError(f"Workspace root is not a directory: {workspace}")

    relative_path = safe_relative_path(relative, label=label)
    prefix = safe_relative_path(allowed_prefix, label="allowed path root")
    if relative_path.parts[: len(prefix.parts)] != prefix.parts:
        raise TemplateSecurityError(f"Invalid {label}: must be under {prefix.as_posix()}/")

    raw_target = workspace_resolved
    for part in relative_path.parts:
        raw_target /= part
        if raw_target.is_symlink():
            raise TemplateSecurityError(f"Invalid {label}: symlinks are not allowed")

    target = raw_target.resolve(strict=False)
    allowed_root = workspace_resolved.joinpath(*prefix.parts).resolve(strict=False)
    try:
        allowed_root.relative_to(workspace_resolved)
        target.relative_to(allowed_root)
    except ValueError as error:
        raise TemplateSecurityError(
            f"Invalid {label}: path escapes its allowed workspace root"
        ) from error
    return target

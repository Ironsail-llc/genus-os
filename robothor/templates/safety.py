"""Filesystem and identifier safety primitives for agent bundle operations.

Template bundles are code-like inputs: installing one changes the agent fleet's
runtime instructions.  Keep the path boundary in one small module so install,
update, removal, archiving, and hub downloads all enforce the same rules.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

_SAFE_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


class TemplateSecurityError(ValueError):
    """An agent bundle requested an unsafe identifier or filesystem path."""


def trusted_directory(root: str | Path, *, label: str = "directory") -> Path:
    """Return an existing directory after resolving and rejecting symlinks.

    The caller supplies the authorization boundary (for example, a downloaded
    bundle root or the appliance workspace).  We canonicalize that boundary
    before any child path is derived from it and reject a boundary reached via
    a symlink, so later containment checks cannot be redirected by an alias.
    """

    raw_root = os.fspath(Path(root).expanduser().absolute())
    resolved_root = os.path.realpath(raw_root)
    if os.path.normcase(raw_root) != os.path.normcase(resolved_root):
        raise TemplateSecurityError(f"Invalid {label}: symlinks are not allowed")
    if not Path(resolved_root).is_dir():
        raise TemplateSecurityError(f"Invalid {label}: expected an existing directory")
    return Path(resolved_root)


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

    root_resolved = trusted_directory(root, label="allowed root")
    relative_path = safe_relative_path(relative, label=label)
    raw_candidate = os.fspath(root_resolved.joinpath(*relative_path.parts).absolute())
    candidate = os.path.realpath(raw_candidate)
    if os.path.normcase(raw_candidate) != os.path.normcase(candidate):
        raise TemplateSecurityError(f"Invalid {label}: symlinks are not allowed")
    try:
        within_root = os.path.commonpath((os.fspath(root_resolved), candidate))
    except ValueError as error:
        raise TemplateSecurityError(f"Invalid {label}: path escapes its allowed root") from error
    if os.path.normcase(within_root) != os.path.normcase(os.fspath(root_resolved)):
        raise TemplateSecurityError(f"Invalid {label}: path escapes its allowed root")
    return Path(candidate)


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

    workspace_resolved = trusted_directory(workspace, label="workspace root")
    relative_path = safe_relative_path(relative, label=label)
    prefix = safe_relative_path(allowed_prefix, label="allowed path root")
    if relative_path.parts[: len(prefix.parts)] != prefix.parts:
        raise TemplateSecurityError(f"Invalid {label}: must be under {prefix.as_posix()}/")

    raw_allowed_root = os.fspath(workspace_resolved.joinpath(*prefix.parts).absolute())
    allowed_root = os.path.realpath(raw_allowed_root)
    raw_target = os.fspath(workspace_resolved.joinpath(*relative_path.parts).absolute())
    target = os.path.realpath(raw_target)
    if os.path.normcase(raw_allowed_root) != os.path.normcase(allowed_root) or os.path.normcase(
        raw_target
    ) != os.path.normcase(target):
        raise TemplateSecurityError(f"Invalid {label}: symlinks are not allowed")
    try:
        allowed_within_workspace = os.path.commonpath((os.fspath(workspace_resolved), allowed_root))
        target_within_allowed = os.path.commonpath((allowed_root, target))
    except ValueError as error:
        raise TemplateSecurityError(
            f"Invalid {label}: path escapes its allowed workspace root"
        ) from error
    if os.path.normcase(allowed_within_workspace) != os.path.normcase(
        os.fspath(workspace_resolved)
    ) or os.path.normcase(target_within_allowed) != os.path.normcase(allowed_root):
        raise TemplateSecurityError(f"Invalid {label}: path escapes its allowed workspace root")
    return Path(target)

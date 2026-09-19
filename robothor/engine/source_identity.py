"""Measured identity for a source-checkout runtime; no version-label inference.

Capture at runtime generation startup and retain it. Installed wheels/containers
without a Git source checkout need a separate build-provenance mechanism; this
module refuses to invent an identity for them.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from robothor.templates.safety import contained_path, trusted_directory


def _git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=10, check=False
    )
    if result.returncode:
        raise ValueError("Runtime source identity is unavailable")
    return result.stdout


def _probe(root):
    if Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("Runtime source must be its repository root")
    revision = _git(root, "rev-parse", "HEAD").decode().strip()
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Runtime source has uncommitted changes")
    if _git(
        root, "ls-files", "--others", "--exclude-standard", "--", "robothor", "crm/bridge"
    ).strip():
        raise ValueError("Runtime source has untracked executable files")
    inventory = []
    for raw in _git(root, "ls-files", "-z").split(b"\0"):
        if not raw:
            continue
        relative = raw.decode()
        path = contained_path(root, relative)
        stat = path.stat()
        if not path.is_file():
            raise ValueError("Unsupported runtime source member")
        inventory.append((relative, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    if not inventory or _git(root, "rev-parse", "HEAD").decode().strip() != revision:
        raise ValueError("Runtime source changed during measurement")
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Runtime source changed during measurement")
    return revision, hashlib.sha256(json.dumps(inventory).encode()).hexdigest()


@dataclass(frozen=True)
class SourceIdentity:
    revision: str
    root: Path = field(repr=False)
    inventory: str = field(repr=False)

    @classmethod
    def capture(cls, root: Path):
        try:
            root = trusted_directory(root, label="runtime source")
            revision, inventory = _probe(root)
            return cls(revision, root, inventory)
        except Exception:
            raise ValueError("Clean source-checkout runtime identity is required") from None

    @classmethod
    def capture_current(cls):
        return cls.capture(Path(__file__).resolve().parents[2])

    def verify(self, expected_revision: str):
        try:
            if expected_revision != self.revision or _probe(self.root) != (
                self.revision,
                self.inventory,
            ):
                raise ValueError("Runtime generation source changed")
        except Exception:
            raise ValueError(
                "Runtime source no longer matches the selected release; restart required"
            ) from None

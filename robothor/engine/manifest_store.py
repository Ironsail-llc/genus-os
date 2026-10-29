"""The agent manifest files of ONE engine: read, write, snapshot, retire.

Two processes write manifests. The bridge writes the platform tenant's, on the
host it shares with the root engine; each child tenant's engine writes its own,
on a volume the bridge cannot reach (``admin_manifests``). Both use this module,
so "an atomic write", "the history ring" and "retire moves, never unlinks" mean
one thing however the write arrived.

Every path is resolved through ``robothor.templates.safety``: the manifest root
must not be reached through a symlink, a manifest name is ``<kebab-id>.yaml``
inside it, and an instruction file lives under the workspace's ``brain/``.
Those refusals raise :class:`~robothor.templates.safety.TemplateSecurityError`
with a message that names the rule, never the path; callers turn it into a 422.

Nothing here logs a path or a manifest value (CLAUDE.md rules 1 and 2).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robothor.templates.safety import (
    contained_path,
    trusted_directory,
    validate_identifier,
    workspace_path,
)

logger = logging.getLogger(__name__)

#: How many previous versions of a manifest are kept per agent.
HISTORY_DEPTH = 5

#: Sub-directories of the manifest dir that the engine's loader cannot see:
#: ``load_manifest_dir`` globs ``*.yaml`` one level deep, non-recursively.
HISTORY_DIR = ".history"
RETIRED_DIR = "retired"

#: Where ``manifest_checks`` reads the canonical schema from, under the workspace.
SCHEMA_RELATIVE = "docs/agents/schema.yaml"


def write_atomically(path: Path, text: str, *, exclusive: bool = False) -> None:
    """Replace one file in a single step, leaving no residue on failure.

    Temp file in the SAME directory so ``os.replace`` stays atomic, fsync
    before the rename so a power loss cannot leave a zero-length manifest, and
    ``BaseException`` on the cleanup so a ``KeyboardInterrupt`` mid-write does
    not strand a ``.tmp`` in the directory the engine globs.

    ``exclusive=True`` makes the create path a create: ``os.replace`` happily
    overwrites, so ``.exists()`` followed by a write is a check-then-act race in
    which two concurrent creates both clear the 409 and the second silently
    destroys the first. ``O_EXCL`` moves the decision into the kernel, where it
    is the only place it can actually be atomic. Raises ``FileExistsError``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            # link() fails with EEXIST if the target exists — the atomic
            # "create only" primitive os.replace does not offer.
            os.link(temp_path, path)
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class ManifestStore:
    """One engine's manifest directory and the workspace its paths resolve in."""

    manifest_dir: Path
    workspace: Path

    @classmethod
    def from_env(cls) -> ManifestStore:
        """The directories THIS process's engine reads — ``EngineConfig``'s answer."""
        from robothor.engine.config import EngineConfig

        config = EngineConfig.from_env()
        return cls(manifest_dir=config.manifest_dir, workspace=config.workspace)

    # ─── Paths ───────────────────────────────────────────────────────

    def root(self) -> Path:
        """The manifest directory, created and canonicalized (no symlinked root)."""
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        return trusted_directory(self.manifest_dir, label="agent manifest root")

    def manifest_path(self, agent_id: str) -> Path:
        agent_id = validate_identifier(agent_id, label="agent id")
        return contained_path(self.root(), f"{agent_id}.yaml", label="agent manifest")

    def instruction_path(self, relative: object) -> Path:
        """``brain/<FILE>.md`` inside the workspace, or a refusal."""
        return workspace_path(
            self.workspace, relative, allowed_prefix="brain", label="agent instruction file"
        )

    # ─── Reads ───────────────────────────────────────────────────────

    def read_manifest(self, agent_id: str) -> str | None:
        path = self.manifest_path(agent_id)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def read_instructions(self, relative: object) -> str:
        """The instruction file's text, or ``""`` if it has none or it is outside."""
        if not relative:
            return ""
        try:
            path = self.instruction_path(relative)
        except Exception:  # noqa: BLE001 — a READ of the fleet must not fail on one agent
            logger.warning("Refusing to read an instruction file outside the workspace")
            return ""
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def scan(self) -> Any:
        from robothor.engine.config import load_manifest_dir

        return load_manifest_dir(self.manifest_dir)

    def defaults_text(self) -> str:
        path = self.manifest_dir / "_defaults.yaml"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def schema_text(self) -> str:
        path = self.workspace / SCHEMA_RELATIVE
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def existing(self, relatives: list[str]) -> list[str]:
        """Which of these workspace-relative paths exist INSIDE the workspace."""
        found = []
        root = self.workspace.resolve()
        for relative in relatives:
            if not isinstance(relative, str) or not relative or relative.startswith("/"):
                continue
            candidate = (self.workspace / relative).resolve()
            if candidate.is_relative_to(root) and candidate.exists():
                found.append(relative)
        return found

    # ─── Writes ──────────────────────────────────────────────────────

    def create(
        self,
        agent_id: str,
        text: str,
        instruction_relative: str | None = None,
        instructions: str | None = None,
    ) -> None:
        """Manifest and instructions, each replaced atomically.

        Not one transaction: two files on a POSIX filesystem cannot be. The
        order is deliberate — instructions first, manifest second — so the
        window between them is "an instruction file nothing references" rather
        than "an agent whose instructions do not exist". An existing
        instruction file is the operator's writing and is never overwritten.
        Raises ``FileExistsError`` if the manifest already exists.
        """
        manifest = self.manifest_path(agent_id)
        if instruction_relative:
            instruction = self.instruction_path(instruction_relative)
            if not instruction.exists():
                write_atomically(instruction, instructions or "")
        write_atomically(manifest, text, exclusive=True)

    def update(self, agent_id: str, text: str) -> None:
        """Snapshot the current manifest, then replace it. ``FileNotFoundError`` if absent."""
        path = self.manifest_path(agent_id)
        if not path.is_file():
            raise FileNotFoundError(agent_id)
        self.snapshot(agent_id)
        write_atomically(path, text)

    def snapshot(self, agent_id: str) -> None:
        """Copy the current manifest into the agent's history ring.

        Best-effort by design: losing a snapshot is a smaller harm than refusing
        an edit the operator asked for, and the ring is a convenience over git,
        not the backup story.
        """
        source = self.manifest_path(agent_id)
        if not source.is_file():
            return
        # Microseconds, not whole seconds: two edits inside one second are a
        # normal thing for a form, and a second-resolution name silently
        # overwrote the earlier snapshot. Every name shares this shape, so a
        # plain sort is chronological.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        try:
            target = contained_path(
                self.root(),
                f"{HISTORY_DIR}/{agent_id}/{stamp}.yaml",
                label="manifest history entry",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            for stale in sorted(target.parent.glob("*.yaml"))[:-HISTORY_DEPTH]:
                stale.unlink(missing_ok=True)
        except Exception as error:  # noqa: BLE001 — a snapshot must not block an edit
            # The TYPE only: an OSError's str() carries the absolute path.
            logger.warning("Could not snapshot a manifest: %s", type(error).__name__)

    def retire(self, agent_id: str) -> None:
        """Snapshot, then move the manifest to ``retired/``. Nothing is unlinked.

        ``FileNotFoundError`` if there is no such manifest.
        """
        path = self.manifest_path(agent_id)
        if not path.is_file():
            raise FileNotFoundError(agent_id)
        self.snapshot(agent_id)
        root = self.root()
        (root / RETIRED_DIR).mkdir(parents=True, exist_ok=True)
        target = contained_path(root, f"{RETIRED_DIR}/{agent_id}.yaml", label="retired manifest")
        path.replace(target)


def main_agent_id() -> str:
    """The agent whose conversation a chat request reaches by sending NO key.

    Read out of ``EngineConfig.main_session_key``, not ``default_chat_agent``.
    They are two independent environment variables and they are free to
    disagree, and only one of them answers the question a browser is asking.
    ``chat.py::_effective_session_key`` substitutes ``main_session_key`` for an
    empty key and then derives the agent as ``parts[1]``; this derives it the
    same way. A key not shaped ``agent:<id>:…`` leaves nothing to derive, and
    the honest fallback is the other configured value rather than a guess.
    """
    from robothor.engine.config import EngineConfig

    config = EngineConfig.from_env()
    parts = config.main_session_key.split(":")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return config.default_chat_agent


def default_model(manifest_dir: Path) -> str:
    """The fleet default ``model.primary`` from ``_defaults.yaml``, or ``""``."""
    from robothor.engine import config as engine_config

    block = engine_config._load_defaults(manifest_dir).get("model") or {}
    return str(block.get("primary") or "")


def failure_rows(scan: Any) -> list[dict[str, str]]:
    """A scan's failure bucket as ids and error TYPES — never the parser detail.

    ``ManifestFailure.detail`` routinely carries the absolute path of the file.
    """
    return [
        {
            "agent_id": failure.agent_id or Path(failure.filename).stem,
            "filename": Path(failure.filename).name,
            "error_type": failure.error_type,
        }
        for failure in scan.failures
    ]

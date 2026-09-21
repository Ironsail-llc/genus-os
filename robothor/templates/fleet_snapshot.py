"""Verified immutable fleet bytes for one admitted unit of native runtime work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

from robothor.templates.fleet_release import ReleaseError, _read, verify_release

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class FleetSnapshot:
    release_id: str
    source_revision: str
    platform_revision: str
    _files: tuple[tuple[str, bytes], ...] = field(repr=False)
    _agents: tuple[str, ...] = field(repr=False)
    _knowledge: tuple[str, ...] = field(repr=False)
    _metadata: bytes = field(repr=False)

    def metadata(self) -> dict[str, Any]:
        """A fresh metadata document, independent of the captured snapshot."""
        document: dict[str, Any] = json.loads(self._metadata)
        return document

    def document(self, path: str | None) -> Any:
        """Parse a captured YAML member without returning a mutable shared object."""
        if not path or path not in dict(self._files) or not path.endswith(".yaml"):
            raise ReleaseError("Document is not part of the verified fleet snapshot")
        return yaml.safe_load(dict(self._files)[path])

    def agent(self, agent_id: str) -> Any:
        """Fresh native config with captured knowledge; no ambient config merge."""
        from robothor.engine.config import manifest_to_agent_config

        files = dict(self._files)
        path = f"docs/agents/{agent_id}.yaml"
        if path not in self._agents:
            raise ReleaseError("Agent is not part of the verified fleet snapshot")
        config = manifest_to_agent_config(yaml.safe_load(files[path]))
        config.fleet_release_id = self.release_id
        config.knowledge_snapshot = tuple((p, files[p].decode("utf-8")) for p in self._knowledge)
        return config


def load_snapshot(root: Path, *, expected_digest: str) -> FleetSnapshot:
    """Capture reviewed bytes; refuse drift between verification and capture too."""
    document = verify_release(root, expected_digest=expected_digest)
    files = []
    for path, expected in document["files"].items():
        content = _read(root, path)
        if expected != {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}:
            raise ReleaseError("Release changed while capturing the runtime snapshot")
        files.append((path, content))
    return FleetSnapshot(
        release_id=expected_digest,
        source_revision=document["source_revision"],
        platform_revision=document["platform_revision"],
        _files=tuple(files),
        _agents=tuple(document["agents"]),
        _knowledge=tuple(document["knowledge"]),
        _metadata=json.dumps(document, sort_keys=True).encode(),
    )

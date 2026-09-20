"""Stage complete verified fleet artifacts at the native runtime lookup path.

Staging does not select a release, install executable plugins or register jobs.
The immutable artifact metadata is preserved; runtime activation is separate.
"""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from robothor.templates.fleet_release import ReleaseError, _read, verify_release
from robothor.templates.safety import contained_path, trusted_directory, validate_sha256


@dataclass(frozen=True)
class StagedRelease:
    path: Path
    release_id: str
    already_present: bool


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def staged_release_path(workspace: Path, release_id: str) -> Path:
    """Resolve the native lookup path, rejecting redirects and path traversal."""
    validate_sha256(release_id)
    root = trusted_directory(workspace, label="runtime workspace")
    return contained_path(root, f".robothor/fleet-releases/{release_id}")


def stage_release(source: Path, workspace: Path, *, expected_digest: str) -> StagedRelease:
    """Verify, privately stage and publish once; existing content is reverified."""
    try:
        validate_sha256(expected_digest)
        source = trusted_directory(source, label="release source")
        document = verify_release(source, expected_digest=expected_digest)
        workspace = trusted_directory(workspace, label="runtime workspace")
        store = contained_path(workspace, ".robothor/fleet-releases")
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        store = trusted_directory(store, label="fleet store")
        _sync_directory(store.parent)
        _sync_directory(workspace)
        target = staged_release_path(workspace, expected_digest)
        lock = contained_path(store, ".stage.lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReleaseError("Invalid fleet store lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if target.exists() or target.is_symlink():
                verify_release(target, expected_digest=expected_digest)
                _sync_directory(store)
                return StagedRelease(target, expected_digest, True)
            with tempfile.TemporaryDirectory(prefix=".staging-", dir=store) as temporary:
                staged = Path(temporary) / "artifact"
                staged.mkdir(mode=0o700)
                for path in [*sorted(document["files"]), "release.json"]:
                    data = _read(source, path)
                    output = contained_path(staged, path)
                    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with output.open("xb") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                verify_release(staged, expected_digest=expected_digest)
                directories = [staged, *(p for p in staged.rglob("*") if p.is_dir())]
                for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                    _sync_directory(directory)
                staged.rename(target)
                _sync_directory(store)
                return StagedRelease(target, expected_digest, False)
        finally:
            os.close(descriptor)
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError(f"Fleet staging refused ({type(exc).__name__})") from None

"""Compare an installed plugin's payload to the exact governed wheel, without import."""

from __future__ import annotations

import hashlib
import re
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from robothor.plugins.lockfile import read_lockfile
from robothor.plugins.wheel import MAX_WHEEL_BYTES, open_wheel
from robothor.templates.safety import contained_path, trusted_directory, validate_sha256


def _generated(path: str, expected: dict[str, Path], dist_info: str) -> bool:
    if path in {
        f"{dist_info}/{name}" for name in ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json")
    }:
        return True
    relative = Path(path)
    if relative.parent.name == "__pycache__":
        match = re.fullmatch(r"(.+?)\.[a-zA-Z0-9_-]+(?:\.opt-[0-2])?\.pyc", relative.name)
        if match:
            return (relative.parent.parent / (match[1] + ".py")).as_posix() in expected
    return False


def verify_installed_wheel(
    wheel_path: Path,
    *,
    expected_digest: str,
    lock_path: Path,
    distribution: metadata.Distribution | None = None,
) -> dict[str, Any]:
    """Verify governed payload bytes and package membership; never import code.

    Generated installer metadata and caches for declared Python sources are not
    wheel payloads. This check does not attest already loaded Python objects.
    Relocated .data layouts and generated scripts require separate support.
    """
    try:
        validate_sha256(expected_digest)
        wheel_path = contained_path(trusted_directory(wheel_path.parent), wheel_path.name)
        if wheel_path.stat().st_size > MAX_WHEEL_BYTES:
            raise ValueError("Oversized plugin wheel")
        wheel_bytes = wheel_path.read_bytes()
        if (
            len(wheel_bytes) > MAX_WHEEL_BYTES
            or hashlib.sha256(wheel_bytes).hexdigest() != expected_digest
        ):
            raise ValueError("Plugin wheel differs from reviewed artifact")
        with tempfile.TemporaryDirectory(prefix="genus-installed-check-") as folder:
            captured = Path(folder) / wheel_path.name
            captured.write_bytes(wheel_bytes)
            wheel = open_wheel(captured, Path(folder) / "contents")
            lock = read_lockfile(lock_path)
            rows = [
                row
                for row in lock.rows.values()
                if canonicalize_name(row.name) == canonicalize_name(wheel.name)
            ]
            if not lock.present or lock.malformed or len(rows) != 1:
                raise ValueError("Plugin governance record is missing or ambiguous")
            row = rows[0]
            if (
                not row.enabled
                or row.version != wheel.version
                or row.dist_sha256 != expected_digest
                or row.manifest_sha256 != wheel.manifest_sha256
                or row.verdict not in {"safe", "review"}
                or row.source is None
                or row.source.origin not in {"wheel", "registry"}
            ):
                raise ValueError("Plugin is not governed at the reviewed version")
            distribution = distribution or metadata.distribution(wheel.name)
            if distribution.version != wheel.version or canonicalize_name(
                distribution.metadata["Name"]
            ) != canonicalize_name(wheel.name):
                raise ValueError("Installed plugin version differs")
            root = trusted_directory(Path(str(distribution.locate_file("."))).resolve())
            expected = {
                path.relative_to(wheel.root).as_posix(): path
                for path in wheel.root.rglob("*")
                if path.is_file()
            }
            if any(Path(path).parts[0].endswith(".data") for path in expected):
                raise ValueError("Relocated plugin layouts need explicit verification support")
            recorded = {str(path) for path in distribution.files or ()}
            verified = 0
            for relative, source in expected.items():
                if relative.endswith(".dist-info/RECORD"):
                    continue
                target = contained_path(root, relative)
                expected_bytes = source.read_bytes()
                if (
                    relative not in recorded
                    or Path(str(distribution.locate_file(relative))) != target
                    or not target.is_file()
                    or target.stat().st_size != len(expected_bytes)
                ):
                    raise ValueError("Installed plugin payload differs from the reviewed wheel")
                with target.open("rb") as stream:
                    if stream.read(len(expected_bytes) + 1) != expected_bytes:
                        raise ValueError("Installed plugin payload differs from the reviewed wheel")
                verified += 1
            roots = {Path(path).parts[0] for path in expected}
            observed = set()
            for name in roots:
                member = contained_path(root, name)
                paths = [member, *member.rglob("*")] if member.is_dir() else [member]
                for path in paths:
                    if path.is_symlink():
                        raise ValueError("Installed plugin contains a symlink")
                    if path.is_file():
                        observed.add(path.relative_to(root).as_posix())
                    elif not path.is_dir():
                        raise ValueError("Installed plugin contains a special file")
            if any(
                path not in expected and not _generated(path, expected, wheel.dist_info)
                for path in observed | recorded
            ):
                raise ValueError("Installed plugin contains unreviewed members")
            return {
                "name": wheel.name,
                "version": wheel.version,
                "wheel_sha256": expected_digest,
                "manifest_sha256": wheel.manifest_sha256,
                "payload_files_verified": verified,
            }
    except Exception:
        raise ValueError(
            "Installed plugin does not match its reviewed wheel and governance record"
        ) from None

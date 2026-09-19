"""Cold process identity for managed source-checkout fleets and service plugins."""

from __future__ import annotations

import hashlib
import inspect
import tempfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import yaml
from packaging.utils import canonicalize_name

from robothor import plugins
from robothor.engine.source_identity import SourceIdentity
from robothor.plugins.installed import verify_installed_wheel
from robothor.plugins.lockfile import lockfile_path, read_lockfile
from robothor.plugins.wheel import open_wheel
from robothor.templates.fleet_release import _read
from robothor.templates.safety import contained_path, trusted_directory


def _lock_digest(path):
    if path is None or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Plugin governance is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(name):
    dist = metadata.distribution(name)
    root = trusted_directory(Path(dist.locate_file(".")).resolve())
    files = []
    for relative in sorted(str(path) for path in dist.files or ()):
        if "__pycache__" in Path(relative).parts:
            continue
        path = contained_path(root, relative)
        stat = path.stat()
        files.append((relative, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    if not files:
        raise ValueError("Installed distribution inventory is unavailable")
    return str(root), dist.version, tuple(files)


@dataclass(frozen=True)
class PluginBootIdentity:
    path: Path | None = field(repr=False)
    digest: str | None = field(repr=False)
    generation: int
    signatures: dict = field(repr=False)

    @classmethod
    def capture(cls, path=None):
        path = path if path is not None else lockfile_path()
        signatures = {}
        digest = None
        try:
            digest = _lock_digest(path)
            lock = read_lockfile(path)
            if not lock.present or lock.malformed:
                raise ValueError("Invalid governance record")
            for row in lock.rows.values():
                if row.enabled:
                    try:
                        signatures[canonicalize_name(row.name)] = _signature(row.name)
                    except Exception:
                        continue  # This distribution cannot pass later verification.
        except Exception:
            digest = None
        return cls(path, digest, plugins.generation(), signatures)

    def verify(self, name):
        try:
            key = canonicalize_name(name)
            if (
                not self.digest
                or plugins.generation() != self.generation
                or _lock_digest(self.path) != self.digest
                or key not in self.signatures
                or _signature(name) != self.signatures[key]
            ):
                raise ValueError("Plugin process identity changed")
        except Exception:
            raise ValueError("Plugin changed since engine startup; restart required") from None


@dataclass(frozen=True)
class RuntimeAssets:
    source: SourceIdentity | None
    plugin_boot: PluginBootIdentity | None

    @classmethod
    def capture(cls):
        # Ordinary unmanaged engines can run without Git. Managed deployment
        # verification remains unavailable until build provenance is supported.
        try:
            source = SourceIdentity.capture_current()
        except ValueError:
            source = None
        return cls(source, PluginBootIdentity.capture())

    def verify(self, snapshot, root):
        if self.source is None:
            raise ValueError("Managed fleet requires verified runtime source provenance")
        self.source.verify(snapshot.platform_revision)
        from robothor.engine.services import get_service

        document = snapshot.metadata()
        receipts = []
        for contract in document["contracts"]["plugins"]:
            if self.plugin_boot is None:
                raise ValueError("Plugin startup identity is unavailable")
            name = contract["name"]
            self.plugin_boot.verify(name)
            wheel_bytes = _read(root, contract["path"])
            digest = document["files"][contract["path"]]["sha256"]
            if hashlib.sha256(wheel_bytes).hexdigest() != digest:
                raise ValueError("Runtime plugin artifact changed")
            dist = metadata.distribution(name)
            installed_root = Path(dist.locate_file(".")).resolve()
            with tempfile.TemporaryDirectory(prefix="genus-runtime-plugin-") as folder:
                wheel_path = Path(folder) / Path(contract["path"]).name
                wheel_path.write_bytes(wheel_bytes)
                receipt = verify_installed_wheel(
                    wheel_path,
                    expected_digest=digest,
                    lock_path=self.plugin_boot.path,
                )
                wheel = open_wheel(wheel_path, Path(folder) / "wheel")
                manifest = yaml.safe_load(wheel.manifest_text)
                if set(manifest.get("entry_points", {})) != {"genus.services"}:
                    raise ValueError("Managed fleet currently supports service-only plugins")
                expected_services = set(manifest.get("services", []))
                observed = set()
                for entry in dist.entry_points:
                    if entry.group != "genus.services":
                        continue
                    payload = entry.load()
                    for service_name, factory in payload.get("services", {}).items():
                        origin = Path(inspect.getfile(factory)).resolve()
                        relative = origin.relative_to(installed_root).as_posix()
                        if (
                            get_service(service_name) is not factory
                            or relative not in {str(path) for path in dist.files or ()}
                            or not contained_path(wheel.root, relative).is_file()
                        ):
                            raise ValueError("Running service differs from its installed plugin")
                        observed.add(service_name)
                if not expected_services or observed != expected_services:
                    raise ValueError("Managed services are not registered exactly")
            self.plugin_boot.verify(name)
            receipts.append(receipt)
        self.source.verify(snapshot.platform_revision)
        return {"platform_revision": snapshot.platform_revision, "plugins": receipts}

"""Installing an agent bundle that came from somewhere else.

``genus agent install <slug>`` already existed, and everything it installed came
from one place the platform vouched for: the hub, pinned by a SHA-256 the hub's
own metadata supplied. This module is the other door — a path, a directory, or
a URL an operator names themselves — and the whole of it is about the fact that
nothing on the other side of that door is trusted.

**The plan is the product.** An operator is shown what would be written, which
requirements this instance does and does not satisfy, and whether the id
collides, and then nothing at all happens until they say ``--yes``. A tool that
installs first and reports afterwards has already made the decision.

**Verify, then copy, then rewrite.** The bundle is verified where it lies, then
copied into a staging directory, and only the copy is ever modified. Renaming
an agent with ``--id`` edits the manifest, the setup file and the instruction
path together — which means editing files whose hashes ``bundle.yaml`` pins, so
doing it in place would either corrupt the operator's own directory or leave a
bundle that no longer verifies.

**The write goes through ``installer.install``.** Not a second copy of it. That
function owns the atomic temp-and-rename, the post-install validation and the
path guard that keeps a manifest inside ``docs/agents/``; a bundle path that
wrote files itself would be a second set of rules for the same destination, and
the second set is always the one that is wrong.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from robothor.templates.bundle import (
    BUNDLE_FILENAME,
    BundleError,
    BundleManifest,
    read_bundle,
    verify_bundle_files,
)
from robothor.templates.safety import (
    TemplateSecurityError,
    default_workspace_root,
    safe_relative_path,
    trusted_directory,
    validate_identifier,
    validate_sha256,
    workspace_path,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping

    import httpx

__all__ = [
    "BundleInstallError",
    "InstallPlan",
    "RequireStatus",
    "install_bundle",
    "plan_install",
]

#: Suffixes an agent bundle archive may have.
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")

#: Seconds a bundle fetch may take. Short on purpose: this sits in front of an
#: operator at a terminal, and a hung mirror must not own the session.
FETCH_TIMEOUT_SECONDS = 30.0


class BundleInstallError(Exception):
    """A refusal, in the sentence the operator gets told."""


@dataclass(frozen=True)
class RequireStatus:
    """One requirement, and whether this instance meets it."""

    kind: str
    name: str
    satisfied: bool
    detail: str = ""

    def describe(self) -> str:
        mark = "ok" if self.satisfied else "MISSING"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{mark:>8}  {self.kind[:-1]} {self.name}{suffix}"


@dataclass(frozen=True)
class InstallPlan:
    """What an install WOULD do, shown before it does any of it."""

    manifest: BundleManifest
    target_id: str
    source: str
    sha256: str = ""
    writes: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    kept_skills: tuple[str, ...] = ()
    requires: tuple[RequireStatus, ...] = ()
    collision: bool = False

    def unsatisfied(self) -> tuple[RequireStatus, ...]:
        return tuple(status for status in self.requires if not status.satisfied)

    def describe(self) -> str:
        lines = [
            f"Agent:    {self.manifest.name} ({self.target_id}) v{self.manifest.version}",
            f"Source:   {self.source}",
        ]
        if self.sha256:
            lines.append(f"SHA-256:  {self.sha256}")
        if self.manifest.exported_at:
            lines.append(
                f"Exported: {self.manifest.exported_at} "
                f"by Genus {self.manifest.platform_version or 'unknown'}"
            )
        lines.append("")
        lines.append("Files this would write:")
        lines.extend(f"  {path}" for path in self.writes)
        lines.extend(f"  agents/skills/{skill}/" for skill in self.skills)
        lines.extend(
            f"  agents/skills/{skill}/ (already present — kept)" for skill in self.kept_skills
        )
        if self.requires:
            lines.append("")
            lines.append("Requirements:")
            lines.extend(f"  {status.describe()}" for status in self.requires)
        if self.collision:
            lines.append("")
            lines.append(
                f"REFUSED: {self.target_id} is already installed. Pass --id <new-id> to "
                "install it alongside; nothing is ever overwritten."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# getting the bytes
# ---------------------------------------------------------------------------


def _read_archive_bytes(path: Path) -> bytes:
    from robothor.templates.hub_client import MAX_DOWNLOAD_BYTES

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BundleInstallError(f"{path.name} could not be read ({type(exc).__name__}).") from exc
    if size > MAX_DOWNLOAD_BYTES:
        raise BundleInstallError(
            f"{path.name} is {size} bytes, over the {MAX_DOWNLOAD_BYTES}-byte limit for "
            "an agent bundle."
        )
    return path.read_bytes()


def _fetch(url: str, *, client: httpx.Client | None) -> bytes:
    import httpx as _httpx

    from robothor.plugins.registry import bounded_get
    from robothor.templates.hub_client import MAX_DOWNLOAD_BYTES

    owned = client is None
    active = client or _httpx.Client(follow_redirects=False, timeout=FETCH_TIMEOUT_SECONDS)
    try:
        return bounded_get(
            active,
            url,
            label="agent bundle",
            cap=MAX_DOWNLOAD_BYTES,
            timeout=FETCH_TIMEOUT_SECONDS,
            error=BundleInstallError,
        )
    finally:
        if owned:
            active.close()


def _extract(content: bytes, into: Path) -> Path:
    """Extract *content* under *into* and return the directory holding ``bundle.yaml``.

    The extraction itself is :meth:`HubClient._extract_archive` — the bounded,
    traversal-refusing, symlink-refusing one that already exists. A second
    extractor would be a second set of limits, and the hostile tarballs this
    platform tests against are aimed at that one.
    """
    from robothor.templates.hub_client import HubClient, HubError

    try:
        HubClient._extract_archive(content, into)  # noqa: SLF001 - one extractor, shared
    except HubError as exc:
        raise BundleInstallError(str(exc)) from exc

    if (into / BUNDLE_FILENAME).is_file():
        return into
    candidates = [
        child
        for child in sorted(into.iterdir())
        if child.is_dir() and not child.is_symlink() and (child / BUNDLE_FILENAME).is_file()
    ]
    if len(candidates) != 1:
        raise BundleInstallError(
            f"The archive must contain exactly one {BUNDLE_FILENAME} root. This one has "
            f"{len(candidates)}, so it is not an agent bundle — a plugin wheel installs "
            "with 'genus plugin install'."
        )
    return candidates[0]


def _materialize(
    source: str | Path,
    *,
    sha256: str | None,
    client: httpx.Client | None,
    scratch: Path,
) -> tuple[Path, str]:
    """Return (a directory holding ``bundle.yaml``, the archive digest or "")."""
    text = str(source)

    if text.startswith("http://"):
        raise BundleInstallError(
            "An agent bundle must be fetched over https. The SHA-256 protects the "
            "contents, but a plaintext fetch still publishes which agent you are "
            "installing."
        )
    if text.startswith("https://"):
        if not sha256:
            raise BundleInstallError(
                "Installing from a URL needs --sha256 <hex>. No signed index vouches for "
                "a file you name yourself, so the hash is the only thing that says it is "
                "the file you meant."
            )
        expected = _checked_digest(sha256)
        content = _fetch(text, client=client)
        digest = _verify_digest(content, expected)
        return _extract(content, scratch), digest

    path = Path(text).expanduser()
    if path.is_dir():
        if sha256:
            raise BundleInstallError(
                "--sha256 pins an archive's bytes; a directory has none. Point at the "
                "tarball, or drop the flag."
            )
        try:
            return trusted_directory(path, label="bundle directory"), ""
        except TemplateSecurityError as exc:
            raise BundleInstallError(str(exc)) from exc
    if not path.is_file():
        raise BundleInstallError(f"There is no bundle at {text!r}.")
    if path.name.endswith(".whl"):
        raise BundleInstallError(
            f"{path.name} is a plugin wheel, not an agent bundle. Install it with "
            "'genus plugin install', which verifies a wheel the way a wheel has to be "
            "verified."
        )
    if not path.name.endswith(ARCHIVE_SUFFIXES):
        raise BundleInstallError(
            f"{path.name} is not an agent bundle. Point at a directory, or at a "
            f"{' / '.join(ARCHIVE_SUFFIXES)} archive produced by 'genus agent export'."
        )
    content = _read_archive_bytes(path)
    digest = hashlib.sha256(content).hexdigest()
    if sha256:
        digest = _verify_digest(content, _checked_digest(sha256))
    return _extract(content, scratch), digest


def _checked_digest(value: str) -> str:
    try:
        return validate_sha256(value, label="--sha256")
    except TemplateSecurityError as exc:
        raise BundleInstallError(str(exc)) from exc


def _verify_digest(content: bytes, expected: str) -> str:
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise BundleInstallError(
            "The bundle's SHA-256 is not the one you pinned. The file is not what the "
            "person who gave you that hash exported."
        )
    return actual


# ---------------------------------------------------------------------------
# requirements
# ---------------------------------------------------------------------------


def _plugin_rows(instance_dir: Path | None) -> dict[str, Any]:
    from robothor.plugins.lockfile import LOCKFILE_NAME, read_lockfile

    try:
        path = None if instance_dir is None else Path(instance_dir) / LOCKFILE_NAME
        return dict(read_lockfile(path).rows)
    except Exception:  # noqa: BLE001 - a lockfile that will not read satisfies nothing
        return {}


def _default_secret_lookup(name: str) -> str:
    try:
        from robothor.secrets import secret_source

        return str(secret_source(name))
    except Exception:  # noqa: BLE001 - an unreachable vault is "nobody knows", not "absent"
        return "unavailable"


def _requirement_statuses(
    manifest: BundleManifest,
    *,
    repo_root: Path,
    instance_dir: Path | None,
    adapter_dir: Path | None,
    environ: Mapping[str, str],
    secret_lookup: Callable[[str], str],
) -> tuple[RequireStatus, ...]:
    """ "Present or not" for every requirement. Nothing here installs anything."""
    statuses: list[RequireStatus] = []

    rows = _plugin_rows(instance_dir)
    for name in manifest.requires.plugins:
        row = rows.get(name)
        enabled = bool(getattr(row, "enabled", False)) if row is not None else False
        statuses.append(
            RequireStatus(
                "plugins",
                name,
                enabled,
                "" if enabled else "not installed; 'genus plugin install " + name + "'",
            )
        )

    carried = set(manifest.file_paths())
    for name in manifest.requires.adapters:
        in_bundle = f"adapters/{name}.yaml" in carried
        on_disk = adapter_dir is not None and (Path(adapter_dir) / f"{name}.yaml").is_file()
        detail = (
            "carried in the bundle — review it before copying it into your adapter "
            "directory; an adapter names a command to run"
            if in_bundle and not on_disk
            else ("" if on_disk else "no adapter by that name on this instance")
        )
        statuses.append(RequireStatus("adapters", name, in_bundle or on_disk, detail))

    for name in manifest.requires.secrets:
        if name in environ:
            statuses.append(RequireStatus("secrets", name, True))
            continue
        source = secret_lookup(name)
        satisfied = source in {"env", "vault"}
        detail = "" if satisfied else ("not set" if source == "missing" else f"vault {source}")
        statuses.append(RequireStatus("secrets", name, satisfied, detail))

    for name in manifest.requires.skills:
        in_bundle = any(path.startswith(f"skills/{name}/") for path in carried)
        on_disk = (repo_root / "agents" / "skills" / name).is_dir()
        statuses.append(
            RequireStatus(
                "skills",
                name,
                in_bundle or on_disk,
                "" if (in_bundle or on_disk) else "not in the bundle and not installed here",
            )
        )

    return tuple(statuses)


# ---------------------------------------------------------------------------
# renaming
# ---------------------------------------------------------------------------


def _renamed_instruction(path: str, old_id: str, new_id: str) -> str:
    """The instruction path an agent renamed *old_id* -> *new_id* should use.

    Both spellings an instance uses are honoured: ``brain/agents/<id>.md`` and
    the shouty ``brain/<ID>.md``. Anything else keeps its directory and takes
    the new id as its stem — a renamed agent whose instruction file still
    carried the old name is how two agents end up sharing one brain file.
    """
    relative = safe_relative_path(path, label="instruction path")
    stem = Path(relative.name).stem
    suffix = Path(relative.name).suffix or ".md"
    shouty = old_id.upper().replace("-", "_")
    if stem == shouty:
        leaf = new_id.upper().replace("-", "_") + suffix
    else:
        leaf = new_id + suffix
    parent = relative.parent.as_posix()
    return f"{parent}/{leaf}" if parent not in ("", ".") else leaf


def _rewrite_identity(staging: Path, old_id: str, new_id: str) -> None:
    """Rename the agent inside the STAGED copy: setup, manifest, instruction path."""
    setup_path = staging / "setup.yaml"
    setup = yaml.safe_load(setup_path.read_text(encoding="utf-8")) or {}
    if not isinstance(setup, dict):
        raise BundleInstallError("The bundle's setup.yaml is not a mapping.")
    old_instruction = str(setup.get("instruction_file_path") or "")
    new_instruction = (
        _renamed_instruction(old_instruction, old_id, new_id) if old_instruction else ""
    )
    setup["agent_id"] = new_id
    if new_instruction:
        setup["instruction_file_path"] = new_instruction
    setup_path.write_text(yaml.dump(setup, sort_keys=False, default_flow_style=False))

    manifest_path = staging / "manifest.template.yaml"
    text = manifest_path.read_text(encoding="utf-8")
    # Textual, not a YAML round-trip: the template is full of ``{{ var }}``
    # placeholders that are not valid YAML scalars, so parsing it would either
    # fail or silently rewrite them.
    text = re.sub(rf"(?m)^id:\s*{re.escape(old_id)}\s*$", f"id: {new_id}", text)
    if old_instruction and new_instruction:
        text = text.replace(old_instruction, new_instruction)
    manifest_path.write_text(text)


# ---------------------------------------------------------------------------
# the install
# ---------------------------------------------------------------------------


def _instruction_destination(staging: Path) -> str:
    setup = yaml.safe_load((staging / "setup.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(setup, dict):
        raise BundleInstallError("The bundle's setup.yaml is not a mapping.")
    return str(setup.get("instruction_file_path") or "")


def _carried_skills(manifest: BundleManifest) -> tuple[str, ...]:
    names: set[str] = set()
    for path in manifest.file_paths():
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "skills":
            names.add(parts[1])
    return tuple(sorted(names))


def _write_skills(
    staging: Path, repo_root: Path, names: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Copy skills the bundle carries. Returns (written, kept).

    A skill already on this instance is KEPT, never replaced. Skills are a
    shared library — the operator's own ``triage`` may be three months of
    tuning, and an agent install is not the operator asking for it to be
    replaced by a stranger's.
    """
    written: list[str] = []
    kept: list[str] = []
    for name in names:
        destination = workspace_path(
            repo_root,
            f"agents/skills/{name}",
            allowed_prefix="agents/skills",
            label="skill destination",
        )
        if destination.exists():
            kept.append(name)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging / "skills" / name, destination, symlinks=False)
        written.append(name)
    return tuple(written), tuple(kept)


def install_bundle(
    source: str | Path,
    *,
    sha256: str | None = None,
    new_id: str | None = None,
    yes: bool = False,
    strict: bool = False,
    repo_root: Path | None = None,
    instance_dir: Path | None = None,
    adapter_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    secret_lookup: Callable[[str], str] | None = None,
    client: httpx.Client | None = None,
    overrides: dict[str, Any] | None = None,
) -> tuple[InstallPlan, dict[str, Any] | None]:
    """Plan, and (with ``yes``) perform, an install from a path, directory or URL.

    Returns ``(plan, result)``; ``result`` is ``None`` whenever nothing was
    written, which is every call without ``yes`` and every refusal.
    """
    import os

    if repo_root is None:
        repo_root = default_workspace_root()
    repo_root = Path(repo_root).resolve(strict=True)
    environ = os.environ if environ is None else environ
    secret_lookup = secret_lookup or _default_secret_lookup
    adapter_path = Path(adapter_dir) if adapter_dir is not None else None

    scratch = Path(tempfile.mkdtemp(prefix="genus-bundle-")).resolve()
    try:
        download_root = scratch / "download"
        download_root.mkdir()
        verified, digest = _materialize(source, sha256=sha256, client=client, scratch=download_root)

        try:
            manifest = read_bundle(verified)
            verify_bundle_files(verified, manifest)
        except BundleError as exc:
            raise BundleInstallError(str(exc)) from exc

        try:
            target_id = validate_identifier(new_id, label="--id") if new_id else manifest.id
        except TemplateSecurityError as exc:
            raise BundleInstallError(str(exc)) from exc

        staging = scratch / "staging"
        shutil.copytree(verified, staging, symlinks=False)
        if target_id != manifest.id:
            _rewrite_identity(staging, manifest.id, target_id)

        instruction = _instruction_destination(staging)
        writes = [f"docs/agents/{target_id}.yaml"]
        if instruction:
            try:
                safe_relative_path(instruction, label="instruction path")
            except TemplateSecurityError as exc:
                raise BundleInstallError(str(exc)) from exc
            writes.append(instruction)

        carried = _carried_skills(manifest)
        already = tuple(
            name for name in carried if (repo_root / "agents" / "skills" / name).exists()
        )
        collision = (repo_root / "docs" / "agents" / f"{target_id}.yaml").exists()

        plan = InstallPlan(
            manifest=manifest,
            target_id=target_id,
            source=str(source),
            sha256=digest,
            writes=tuple(writes),
            skills=tuple(name for name in carried if name not in already),
            kept_skills=already,
            requires=_requirement_statuses(
                manifest,
                repo_root=repo_root,
                instance_dir=instance_dir,
                adapter_dir=adapter_path,
                environ=environ,
                secret_lookup=secret_lookup,
            ),
            collision=collision,
        )

        # A refusal fires on the WRITE, never on the preview. An operator whose
        # id collides needs to SEE the plan — that is how they learn what --id
        # would install and what the collision is with; a preview that raised
        # would tell them only that something was wrong.
        if not yes:
            return plan, None
        if collision:
            raise BundleInstallError(
                f"{target_id} is already installed on this instance. An install never "
                "overwrites an agent — pass --id <new-id> to install this one alongside it."
            )
        if strict and plan.unsatisfied():
            missing = ", ".join(f"{s.kind[:-1]} {s.name}" for s in plan.unsatisfied())
            raise BundleInstallError(
                f"--strict: this instance does not satisfy {missing}. Install what is "
                "missing, or drop --strict to install the agent anyway."
            )

        from robothor.templates.installer import install

        result = install(
            staging,
            overrides=overrides or {},
            auto_yes=True,
            instance_dir=instance_dir,
            repo_root=repo_root,
            source="bundle",
            source_ref=target_id,
            source_sha256=digest or None,
        )
        _write_skills(staging, repo_root, plan.skills)
        return plan, result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def plan_install(source: str | Path, **kwargs: Any) -> InstallPlan:
    """The plan alone. Writes nothing, whatever else is passed."""
    kwargs.pop("yes", None)
    plan, _ = install_bundle(source, yes=False, **kwargs)
    return plan

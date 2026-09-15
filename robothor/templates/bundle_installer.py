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
    from collections.abc import Callable, Mapping, Sequence

    import httpx

__all__ = [
    "BundleInstallError",
    "BundleNotPublishedError",
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


class BundleNotPublishedError(BundleInstallError):
    """No configured index publishes this slug.

    Separate from every other refusal so the CLI may fall through to the hub on
    this one alone. Falling through on a SIGNATURE failure would hand the
    operator an unsigned copy of the agent their tampered index refused, and
    say nothing.
    """


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
    #: ``(workspace-relative path, the agent that owns it or "")`` for every
    #: destination that already exists. A tuple rather than a bool because
    #: "which file, and whose" is the whole of what the operator needs.
    collisions: tuple[tuple[str, str], ...] = ()

    @property
    def collision(self) -> bool:
        return bool(self.collisions)

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
        existing = dict(self.collisions)
        for path in self.writes:
            owner = existing.get(path)
            if owner is None:
                lines.append(f"  {path}")
            elif owner:
                lines.append(f"  {path}  EXISTS — owned by {owner}")
            else:
                lines.append(f"  {path}  EXISTS")
        lines.extend(f"  agents/skills/{skill}/" for skill in self.skills)
        lines.extend(
            f"  agents/skills/{skill}/ (already present — kept)" for skill in self.kept_skills
        )
        if self.requires:
            lines.append("")
            lines.append("Requirements:")
            lines.extend(f"  {status.describe()}" for status in self.requires)
        if self.collisions:
            lines.append("")
            lines.append(
                "REFUSED: an install never overwrites a file. Pass --id <new-id> to "
                "install this bundle alongside what is already there."
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


def _from_index(
    slug: str,
    *,
    version: str | None,
    index: str | None,
    indexes: Sequence[str] | None,
    keys: dict[str, str] | None,
    client: httpx.Client | None,
    scratch: Path,
) -> tuple[Path, str, str]:
    """Resolve a slug through the SIGNED index and fetch what it pins.

    The same verification the plugin installer gets, because it is the same
    function: signature over canonical bytes, a pinned key id, a freshness
    window, one publisher per name. What is different is only the ``kind`` —
    and asking for the wrong one is refused with the verb that takes it, not
    with "not found".
    """
    from robothor.plugins import registry
    from robothor.templates.hub_client import MAX_DOWNLOAD_BYTES

    urls: Sequence[str]
    if index:
        urls = (index,)
    elif indexes is not None:
        urls = tuple(indexes)
    else:
        urls = registry.configured_indexes()

    try:
        loaded = registry.load_indexes(urls, keys=keys, client=client)
        entry, _source = registry.select(slug, version, indexes=loaded, kind=registry.BUNDLE_KIND)
    except registry.NotPublishedError as exc:
        raise BundleNotPublishedError(str(exc)) from exc
    except registry.RegistryError as exc:
        raise BundleInstallError(str(exc)) from exc

    artifact = entry.bundle()
    assert artifact is not None  # select() refuses an entry with no bundle artifact
    if artifact.size > MAX_DOWNLOAD_BYTES:
        # Refused from the SIGNED size, before a byte is fetched — the only
        # place an oversized artifact costs nothing.
        raise BundleInstallError(
            f"{entry.name} {entry.version} is too large: the index declares "
            f"{artifact.size} bytes, over the {MAX_DOWNLOAD_BYTES}-byte limit."
        )
    content = _fetch(artifact.url, client=client)
    digest = _verify_digest(content, artifact.sha256)
    return _extract(content, scratch), digest, entry.name


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


def _env_is_set(name: str, environment: Mapping[str, str] | None) -> bool:
    """Whether the variable *name* is set, without reading its value.

    Through :mod:`robothor.settings.env` rather than ``os.environ`` directly.
    A bundle's ``requires.secrets`` names arrive at runtime from a document
    somebody else wrote, so they are exactly the dynamically-named case that
    module exists for — there is no settings field to declare for a variable
    this instance learns about when the tarball is opened.

    *environment* overrides it outright, which is how a test states the environment
    it is describing instead of describing the machine it happens to run on.
    """
    if environment is not None:
        return name in environment
    from robothor.settings.env import process_env_get

    return process_env_get(name, None) is not None


def _requirement_statuses(
    manifest: BundleManifest,
    *,
    repo_root: Path,
    instance_dir: Path | None,
    adapter_dir: Path | None,
    environment: Mapping[str, str] | None,
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
        if _env_is_set(name, environment):
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


#: ``{{ variable }}`` — not a YAML scalar. ``timezone: {{ tz }}`` parses as a
#: flow mapping whose key is a mapping, so a template has to be stood in for
#: before it can be read at all.
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


def read_manifest_template(text: str) -> dict[str, Any]:
    """The staged manifest template, read as YAML with placeholders stood in for.

    Every refusal here is a sentence. The first cut of the rename was a
    line-anchored regex over this file, and a manifest spelling its id as
    ``id: "gamma"`` or ``id: gamma  # the agent`` produced a Python traceback
    out of ``genus agent install --id`` instead of an exit code — from the very
    flag the collision refusal tells an operator to reach for.
    """
    stood_in = _TEMPLATE_PLACEHOLDER.sub("TEMPLATE_VALUE", text)
    try:
        data = yaml.safe_load(stood_in)
    except yaml.YAMLError as exc:
        raise BundleInstallError(
            "The bundle's manifest.template.yaml is not readable as YAML "
            f"({type(exc).__name__}). The bundle is malformed; ask whoever exported "
            "it to re-export."
        ) from exc
    if not isinstance(data, dict):
        raise BundleInstallError("The bundle's manifest.template.yaml is not a YAML mapping.")
    return data


def _set_top_level_scalar(text: str, key: str, value: str) -> str:
    """Replace one top-level ``key: <scalar>`` line, keeping any trailing comment.

    A targeted edit rather than a YAML round-trip, because dumping the parsed
    document back would replace every ``{{ variable }}`` with the stand-in and
    destroy the template. The caller re-parses afterwards and refuses if the
    value did not actually change — an edit that silently did nothing is how a
    renamed agent keeps the name it was renamed away from.
    """
    pattern = re.compile(rf"(?m)^{re.escape(key)}[ \t]*:[ \t]*(?P<rest>[^\n]*)$")

    def replace(match: re.Match[str]) -> str:
        comment = re.search(r"\s+#[^\n]*$", match.group("rest"))
        return f"{key}: {value}{comment.group(0) if comment else ''}"

    return pattern.sub(replace, text, count=1)


def canonical_instruction(path: str, agent_id: str) -> str:
    """The only instruction path an agent with this id may own.

    **Derived, never taken verbatim.** A bundle declaring
    ``instruction_file: brain/agents/main.md`` for an agent called
    ``helpful-bot`` was, until this existed, an install that replaced the
    operator's main agent's instructions while leaving ``main.yaml`` untouched —
    so nothing in ``genus agent list`` or the Helm looked wrong and the most
    privileged agent on the appliance was running a stranger's prompt.

    The bundle still chooses its DIRECTORY (both conventions this platform uses
    live in different ones), and the shouty ``brain/<ID>.md`` spelling is
    preserved when that is what the bundle used. Only the leaf is pinned.
    """
    relative = safe_relative_path(path, label="instruction path")
    stem = Path(relative.name).stem
    suffix = Path(relative.name).suffix or ".md"
    shouty = agent_id.upper().replace("-", "_")
    leaf = (shouty if stem == shouty else agent_id) + suffix
    parent = relative.parent.as_posix()
    return f"{parent}/{leaf}" if parent not in ("", ".") else leaf


def _rewrite_identity(staging: Path, target_id: str) -> None:
    """Pin the STAGED copy to *target_id*: setup, manifest, instruction path.

    Runs on every install, not only under ``--id``. Canonicalising the
    instruction path is what stops a bundle aiming its instructions at somebody
    else's agent, and a rewrite that only ran when the operator renamed the
    agent would leave the default path — the one nobody looks at — unprotected.
    """
    setup_path = staging / "setup.yaml"
    try:
        setup = yaml.safe_load(setup_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BundleInstallError(
            f"The bundle's setup.yaml could not be read ({type(exc).__name__})."
        ) from exc
    if not isinstance(setup, dict):
        raise BundleInstallError("The bundle's setup.yaml is not a mapping.")

    manifest_path = staging / "manifest.template.yaml"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleInstallError(
            f"The bundle's manifest.template.yaml could not be read ({type(exc).__name__})."
        ) from exc
    declared = read_manifest_template(text)

    source = str(setup.get("instruction_file_path") or declared.get("instruction_file") or "")
    try:
        instruction = canonical_instruction(source, target_id) if source else ""
    except TemplateSecurityError as exc:
        raise BundleInstallError(str(exc)) from exc

    setup["agent_id"] = target_id
    if instruction:
        setup["instruction_file_path"] = instruction
    setup_path.write_text(yaml.dump(setup, sort_keys=False, default_flow_style=False))

    text = _set_top_level_scalar(text, "id", target_id)
    if instruction and "instruction_file" in declared:
        text = _set_top_level_scalar(text, "instruction_file", instruction)
    manifest_path.write_text(text)

    # Probe, do not trust the edit. A quoted or commented scalar the regex did
    # not reach would otherwise install under the name it was supposed to leave.
    rewritten = read_manifest_template(text)
    if str(rewritten.get("id") or "") != target_id:
        raise BundleInstallError(
            f"The bundle's manifest could not be renamed to {target_id!r} — its id is "
            "written in a form this installer cannot rewrite safely. Ask for a "
            "re-export, or install it under its own id."
        )
    if instruction and str(rewritten.get("instruction_file") or "") != instruction:
        raise BundleInstallError(
            "The bundle's manifest declares its instruction_file in a form this "
            "installer cannot rewrite safely. Ask for a re-export."
        )


def _instruction_owners(repo_root: Path) -> dict[str, str]:
    """``{workspace-relative instruction path: the agent that claims it}``.

    Read from the canonical manifest directory rather than from
    ``installed.yaml``: install records are mutable state, and the question
    being asked — "whose file is this?" — has to be answered by the files the
    engine actually reads.
    """
    owners: dict[str, str] = {}
    agents_dir = repo_root / "docs" / "agents"
    if not agents_dir.is_dir():
        return owners
    for path in sorted(agents_dir.glob("*.yaml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        instruction = data.get("instruction_file")
        if isinstance(instruction, str) and instruction:
            owners[instruction] = str(data.get("id") or path.stem)
    return owners


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
    from_index: bool = False,
    version: str | None = None,
    index: str | None = None,
    indexes: Sequence[str] | None = None,
    keys: dict[str, str] | None = None,
    repo_root: Path | None = None,
    instance_dir: Path | None = None,
    adapter_dir: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    secret_lookup: Callable[[str], str] | None = None,
    client: httpx.Client | None = None,
    overrides: dict[str, Any] | None = None,
) -> tuple[InstallPlan, dict[str, Any] | None]:
    """Plan, and (with ``yes``) perform, an install.

    *source* is a path, a directory, an ``https`` URL, or — with
    ``from_index`` — a slug published in a signed index.

    Returns ``(plan, result)``; ``result`` is ``None`` whenever nothing was
    written, which is every call without ``yes`` and every refusal.
    """
    if repo_root is None:
        repo_root = default_workspace_root()
    repo_root = Path(repo_root).resolve(strict=True)
    secret_lookup = secret_lookup or _default_secret_lookup
    adapter_path = Path(adapter_dir) if adapter_dir is not None else None

    scratch = Path(tempfile.mkdtemp(prefix="genus-bundle-")).resolve()
    try:
        download_root = scratch / "download"
        download_root.mkdir()
        published_as = ""
        if from_index:
            verified, digest, published_as = _from_index(
                str(source),
                version=version,
                index=index,
                indexes=indexes,
                keys=keys,
                client=client,
                scratch=download_root,
            )
        else:
            verified, digest = _materialize(
                source, sha256=sha256, client=client, scratch=download_root
            )

        try:
            manifest = read_bundle(verified)
            verify_bundle_files(verified, manifest)
        except BundleError as exc:
            raise BundleInstallError(str(exc)) from exc

        if published_as and manifest.id != published_as:
            # The SIGNED name is the authority. A tarball whose bundle.yaml
            # calls itself something else is the publisher's signature
            # vouching for one agent and the archive delivering another.
            raise BundleInstallError(
                f"The index publishes this as {published_as!r}, but the bundle inside "
                f"calls itself {manifest.id!r}. Refusing rather than picking one."
            )

        try:
            target_id = validate_identifier(new_id, label="--id") if new_id else manifest.id
        except TemplateSecurityError as exc:
            raise BundleInstallError(str(exc)) from exc

        staging = scratch / "staging"
        shutil.copytree(verified, staging, symlinks=False)
        _rewrite_identity(staging, target_id)

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

        # Collision covers EVERY file the install would write, not just the
        # manifest. Checking only ``docs/agents/<id>.yaml`` is what let a bundle
        # replace another agent's instruction file while reporting no collision
        # at all. A skill is not in this list because a skill already present is
        # kept rather than written.
        owners = _instruction_owners(repo_root)
        collisions = tuple(
            (relative, owners.get(relative, ""))
            for relative in writes
            if (repo_root / relative).exists()
        )

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
                environment=environment,
                secret_lookup=secret_lookup,
            ),
            collisions=collisions,
        )

        # A refusal fires on the WRITE, never on the preview. An operator whose
        # id collides needs to SEE the plan — that is how they learn what --id
        # would install and what the collision is with; a preview that raised
        # would tell them only that something was wrong.
        if not yes:
            return plan, None
        if collisions:
            detail = ", ".join(
                f"{relative} (owned by {owner})" if owner else relative
                for relative, owner in collisions
            )
            raise BundleInstallError(
                f"An install never overwrites a file. These already exist: {detail}. "
                "Pass --id <new-id> to install this bundle alongside what is there."
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

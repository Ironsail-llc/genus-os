"""Workspace files a plugin ships: agents, workflows, manifest patches, files.

A plugin's ``genus-plugin.yaml`` may declare, as paths relative to the
directory that holds it (package data inside the wheel)::

    agents:            # agent template bundles, laid out like templates/agents/core/main
      - agents/desk-monitor
    workflows:         # copied to <workspace>/docs/workflows/<basename>
      - workflows/example-pass.yaml
    patches:           # edit an EXISTING agent manifest the plugin does not own
      - agent: main
        file: patches/main.patch.yaml
    files:             # other workspace files, inside an allowlist
      - src: extra/webhooks.yaml
        dest: docs/webhooks.yaml
        mode: overwrite          # or if_missing

The declaration lives in the manifest -- which ``manifest_sha256`` already
covers -- rather than in code, so a registry can read it without importing
anything and the bytes the index signed are the bytes that decide what is
written.

Three operations, each called by ``genus plugin install`` / ``sync`` /
``remove``:

* :func:`plan` -- every refusal, before pip runs: unsafe paths, a destination
  outside the allowlist or reached through a symlink, a missing source, an
  agent somebody else installed, a file another plugin owns.
* :func:`apply` -- render agents through the agent-template installer (so
  ``.robothor/overrides/<id>.yaml`` and post-install validation apply exactly
  as they do for ``genus agent install``), copy workflows and files, apply each
  patch's ``install:`` section. Idempotent. The first time a plugin overwrites
  a file it did not create, the original is kept as ``<dst>.pre-<plugin>``.
* :func:`retire` -- move the plugin's agent manifests to
  ``docs/agents/retired/`` (the Helm's retire convention: nothing is unlinked
  and the instruction file stays), delete the workflows and files it wrote
  unless the operator has edited them since, put back what it overwrote, and
  apply each patch's ``remove:`` section.

What the plugin wrote is recorded in ``.robothor/plugin-assets.yaml`` -- the
ledger :func:`retire` works from, because by the time a plugin is removed pip
has already deleted the patch files that said how to undo themselves.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
import os
import pathlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from robothor.plugins.manifest import ASSET_KEYS, MANIFEST_NAME
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    default_workspace_root,
    safe_relative_path,
    trusted_directory,
    validate_identifier,
    workspace_path,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AssetError",
    "PluginAssets",
    "apply",
    "declaration_from_distribution",
    "declaration_from_wheel",
    "parse",
    "plan",
    "retire",
    "sync_installed",
]

#: The ledger, beside ``installed.yaml`` in the instance directory.
LEDGER_NAME = "plugin-assets.yaml"

#: Where a ``files:`` entry may write. A prefix entry admits everything under
#: it; ``docs/webhooks.yaml`` is the one exact file. ``docs/agents/`` is NOT
#: here: an agent arrives through ``agents:`` and the template installer, or
#: changes through ``patches:``, never as a raw file copy.
FILE_PREFIXES = ("brain", "docs/workflows", "docs/hooks")
FILE_EXACT = ("docs/webhooks.yaml",)

#: The modes a ``files:`` entry may ask for.
MODES = ("overwrite", "if_missing")

#: Top-level manifest keys a patch may not touch: renaming an agent by patch
#: would orphan every record that names it.
_PROTECTED_KEYS = frozenset({"id"})


class AssetError(ValueError):
    """A refusal, in the sentence the operator gets told."""


@dataclass(frozen=True)
class AgentDecl:
    src: str
    agent_id: str
    bundle: Path
    #: Workspace-relative instruction path the template declares, if any.
    instruction: str = ""


@dataclass(frozen=True)
class FileDecl:
    src: str
    dest: str
    mode: str
    source: Path
    kind: str = "file"  # "workflow" or "file"


@dataclass(frozen=True)
class PatchDecl:
    agent: str
    src: str
    install: dict[str, Any]
    remove: dict[str, Any]


@dataclass(frozen=True)
class PluginAssets:
    """One plugin's declaration, parsed and checked against its own package."""

    agents: tuple[AgentDecl, ...] = ()
    files: tuple[FileDecl, ...] = ()
    patches: tuple[PatchDecl, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.agents or self.files or self.patches)


@dataclass
class Report:
    """What one apply or retire did, as lines an operator reads."""

    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# names and paths
# ---------------------------------------------------------------------------


def canonical(name: str) -> str:
    """PEP 503 normalisation: the ledger key and the ``plugin:<dist>`` source."""
    from packaging.utils import canonicalize_name

    return str(canonicalize_name(name))


def _workspace(workspace: Path | None) -> Path:
    root = workspace if workspace is not None else default_workspace_root()
    try:
        return trusted_directory(root, label="workspace root")
    except TemplateSecurityError as exc:
        raise AssetError(
            "This plugin ships workspace files, but the workspace does not resolve to an "
            "existing directory (set ROBOTHOR_WORKSPACE)."
        ) from exc


def _instance_dir(workspace: Path, instance_dir: Path | None) -> Path:
    return instance_dir if instance_dir is not None else workspace / ".robothor"


def _dest(workspace: Path, relative: str) -> Path:
    """A ``files:``/``workflows:`` destination, inside the allowlist, no symlinks."""
    parsed = _check_dest_shape(relative)
    prefix = (
        str(pathlib.PurePosixPath(parsed).parent)
        if parsed in FILE_EXACT
        else next(p for p in FILE_PREFIXES if parsed.startswith(p + "/"))
    )
    try:
        return workspace_path(workspace, parsed, allowed_prefix=prefix, label="destination")
    except TemplateSecurityError as exc:
        raise AssetError(f"{relative!r}: {exc}") from exc


def _source(base: Path, relative: object, *, want_dir: bool = False) -> Path:
    """A package-relative path inside the plugin, regular and not a symlink."""
    try:
        path = contained_path(base, relative, label="plugin path")
    except TemplateSecurityError as exc:
        raise AssetError(f"{relative!r}: {exc}") from exc
    if want_dir and not path.is_dir():
        raise AssetError(f"{relative!r} is not a directory in the plugin package.")
    if not want_dir and not path.is_file():
        raise AssetError(f"{relative!r} is not a file in the plugin package.")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: The journal of the apply in progress, if any. Every write site calls
#: :func:`_touch` first, so a plugin whose apply fails part-way is put back
#: exactly as it was -- one plugin's broken agent never leaves half of its
#: files new and half old, and never stops the next plugin's sync.
_JOURNAL: contextvars.ContextVar[dict[Path, bytes | None] | None] = contextvars.ContextVar(
    "plugin_asset_journal", default=None
)


def _touch(path: Path) -> None:
    """Remember *path*'s bytes (or its absence) before the first write to it."""
    journal = _JOURNAL.get()
    if journal is None or path in journal:
        return
    journal[path] = path.read_bytes() if path.is_file() and not path.is_symlink() else None


def _rollback(journal: dict[Path, bytes | None]) -> None:
    for path, original in reversed(list(journal.items())):
        try:
            if original is None:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            else:
                _write_atomically(path, original)
        except OSError as exc:  # pragma: no cover - best effort, and said so
            logger.warning("Could not restore %s after a failed apply: %s", path.name, exc)


def _write_atomically(path: Path, data: bytes) -> None:
    _touch(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _backup_path(path: Path, dist: str) -> Path:
    return path.with_name(f"{path.name}.pre-{dist}")


def _backup_once(path: Path, dist: str, workspace: Path) -> str | None:
    """Copy *path* to ``<path>.pre-<dist>`` unless that copy already exists."""
    if not path.exists():
        return None
    backup = _backup_path(path, dist)
    if backup.is_symlink():
        raise AssetError(f"Refusing to back up over a symlink: {backup.name}")
    if not backup.exists():
        _touch(backup)
        shutil.copy2(path, backup)
    return backup.relative_to(workspace).as_posix()


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def _list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise AssetError(f"{MANIFEST_NAME}: `{key}` must be a list.")
    return value


def _agent_id(bundle: Path, src: str) -> tuple[str, str]:
    """The id the template installer will use, checked against the template."""
    from robothor.templates.bundle_installer import BundleInstallError, read_manifest_template

    setup: dict[str, Any] = {}
    setup_path = bundle / "setup.yaml"
    if setup_path.is_file() and not setup_path.is_symlink():
        try:
            loaded = yaml.safe_load(setup_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise AssetError(f"{src}/setup.yaml is not valid YAML.") from exc
        if not isinstance(loaded, dict):
            raise AssetError(f"{src}/setup.yaml is not a YAML mapping.")
        setup = loaded
    try:
        agent_id = validate_identifier(setup.get("agent_id", bundle.name), label="agent ID")
    except TemplateSecurityError as exc:
        raise AssetError(f"{src}: {exc}") from exc
    template = bundle / "manifest.template.yaml"
    if not template.is_file() or template.is_symlink():
        raise AssetError(f"{src} has no manifest.template.yaml.")
    defaults = {
        k: v.get("default") if isinstance(v, dict) else v
        for k, v in (setup.get("variables") or {}).items()
    }
    try:
        manifest = read_manifest_template(template.read_text(encoding="utf-8"), defaults)
    except BundleInstallError as exc:
        raise AssetError(f"{src}: {exc}") from exc
    declared = manifest.get("id")
    if declared != agent_id:
        raise AssetError(
            f"{src}: manifest.template.yaml declares id {declared!r}, but the bundle "
            f"installs as {agent_id!r}."
        )
    instruction = setup.get("instruction_file_path") or manifest.get("instruction_file") or ""
    instruction = str(instruction) if isinstance(instruction, str) else ""
    if instruction:
        # The agent's own brain path only -- the rule `files:` follows. Pointed
        # at a shared file (brain/TOOLS.md) or another agent's, an identical
        # copy would be taken over, and a later release would overwrite it.
        try:
            normal = safe_relative_path(instruction, label="instruction file").as_posix()
        except TemplateSecurityError as exc:
            raise AssetError(f"{src}: {exc}") from exc
        if normal != f"brain/agents/{agent_id}.md" and not normal.startswith(
            f"brain/agents/{agent_id}/"
        ):
            raise AssetError(
                f"{src}: instruction file {instruction} is not this agent's own. A plugin "
                f"agent's instruction file must be brain/agents/{agent_id}.md or under "
                f"brain/agents/{agent_id}/."
            )
        instruction = normal
    return agent_id, instruction


#: Agent ids no plugin may ship, whatever the workspace holds: the operator's
#: main agent always, plus the core department of the platform's template
#: catalog. A plugin changes one of these only through ``patches:``, which is
#: declared, reviewed (the scan grades it ``review``) and conflict-tracked.
ALWAYS_RESERVED = frozenset({"main"})


def reserved_agent_ids() -> frozenset[str]:
    """``main`` plus every agent in the template catalog's ``core`` department."""
    try:
        from robothor.templates.catalog import Catalog

        core = Catalog().get_department_agents("core")
    except Exception as exc:  # noqa: BLE001 - no catalog still reserves main
        logger.debug("Template catalog unavailable for reserved ids (%s)", type(exc).__name__)
        core = []
    return ALWAYS_RESERVED | {str(agent) for agent in core}


def _patch_section(raw: Any, where: str) -> dict[str, Any]:
    if raw is None:
        return {"set": {}, "delete": []}
    if not isinstance(raw, dict) or set(raw) - {"set", "delete"}:
        raise AssetError(f"{where} must be a mapping with only `set:` and `delete:`.")
    to_set = raw.get("set") or {}
    to_delete = raw.get("delete") or []
    if not isinstance(to_set, dict):
        raise AssetError(f"{where}.set must be a mapping.")
    if not isinstance(to_delete, list) or not all(
        isinstance(k, str) and k and all(k.split(".")) for k in to_delete
    ):
        raise AssetError(f"{where}.delete must be a list of keys (dotted for nested).")
    touched = {str(k) for k in to_set} | {k.split(".")[0] for k in to_delete}
    if touched & _PROTECTED_KEYS:
        raise AssetError(f"{where} may not change an agent's `id`.")
    return {"set": to_set, "delete": list(to_delete)}


def parse(manifest_text: str | None, base: Path) -> PluginAssets:
    """The declaration in *manifest_text*, every path checked against *base*.

    *base* is the directory holding ``genus-plugin.yaml``; every declared path
    is relative to it and must stay inside it.
    """
    if not manifest_text:
        return PluginAssets()
    try:
        data = yaml.safe_load(manifest_text) or {}
    except yaml.YAMLError as exc:
        raise AssetError(f"{MANIFEST_NAME} is not valid YAML ({type(exc).__name__}).") from exc
    if not isinstance(data, dict) or not any(data.get(k) for k in ASSET_KEYS):
        return PluginAssets()
    try:
        base = trusted_directory(base, label="plugin package")
    except TemplateSecurityError as exc:
        raise AssetError(str(exc)) from exc

    agents: list[AgentDecl] = []
    for src in _list(data, "agents"):
        bundle = _source(base, src, want_dir=True)
        agent_id, instruction = _agent_id(bundle, str(src))
        agents.append(
            AgentDecl(src=str(src), agent_id=agent_id, bundle=bundle, instruction=instruction)
        )
    ids = [a.agent_id for a in agents]
    reserved = sorted(set(ids) & reserved_agent_ids())
    if reserved:
        raise AssetError(
            f"Agent id {', '.join(map(repr, reserved))} is reserved for the platform, so a "
            "plugin may not ship it (or adopt an existing copy). Change it through "
            "`patches:` instead."
        )
    if len(ids) != len(set(ids)):
        raise AssetError(f"{MANIFEST_NAME} declares the same agent twice.")

    files: list[FileDecl] = []
    for src in _list(data, "workflows"):
        source = _source(base, src)
        files.append(
            FileDecl(
                src=str(src),
                dest=f"docs/workflows/{source.name}",
                mode="overwrite",
                source=source,
                kind="workflow",
            )
        )
    for entry in _list(data, "files"):
        if not isinstance(entry, dict) or set(entry) - {"src", "dest", "mode"}:
            raise AssetError("Each `files:` entry is a mapping of src, dest and mode.")
        mode = entry.get("mode", "overwrite")
        if mode not in MODES:
            raise AssetError(f"`files:` mode {mode!r} is not one of {', '.join(MODES)}.")
        dest = entry.get("dest")
        if not isinstance(dest, str):
            raise AssetError("Each `files:` entry needs a `dest`.")
        safe_dest = _check_dest_shape(dest)
        files.append(
            FileDecl(
                src=str(entry.get("src")),
                dest=safe_dest,
                mode=mode,
                source=_source(base, entry.get("src")),
            )
        )
    dests = [f.dest for f in files]
    if len(dests) != len(set(dests)):
        raise AssetError(f"{MANIFEST_NAME} writes the same destination twice.")

    patches: list[PatchDecl] = []
    for entry in _list(data, "patches"):
        if not isinstance(entry, dict) or set(entry) - {"agent", "file"}:
            raise AssetError("Each `patches:` entry is a mapping of agent and file.")
        try:
            agent = validate_identifier(entry.get("agent"), label="patched agent ID")
        except TemplateSecurityError as exc:
            raise AssetError(str(exc)) from exc
        if agent in ids:
            raise AssetError(
                f"A patch edits an agent the plugin does not own; {agent!r} is one of its own."
            )
        path = _source(base, entry.get("file"))
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise AssetError(f"{entry.get('file')} is not valid YAML.") from exc
        if not isinstance(body, dict) or set(body) - {"install", "remove"}:
            raise AssetError(
                f"{entry.get('file')} must be a mapping with `install:` and `remove:` sections."
            )
        patches.append(
            PatchDecl(
                agent=agent,
                src=str(entry.get("file")),
                install=_patch_section(body.get("install"), f"{entry.get('file')}: install"),
                remove=_patch_section(body.get("remove"), f"{entry.get('file')}: remove"),
            )
        )
    allowed = set(ids) | {p.agent for p in patches}
    for item in files:
        owner = _brain_agent(item.dest)
        if owner is not None and owner not in allowed:
            raise AssetError(
                f"{item.dest} belongs to agent {owner!r}. A plugin writes under "
                "brain/agents/<id> only for an agent it ships or declares a patch for."
            )
    return PluginAssets(agents=tuple(agents), files=tuple(files), patches=tuple(patches))


def _check_dest_shape(dest: str) -> str:
    """The allowlist check that needs no workspace, so ``parse`` can refuse early."""
    try:
        parsed = safe_relative_path(dest, label="destination").as_posix()
    except TemplateSecurityError as exc:
        raise AssetError(f"{dest!r}: {exc}") from exc
    if parsed not in FILE_EXACT and not any(parsed.startswith(p + "/") for p in FILE_PREFIXES):
        allowed = ", ".join([*(p + "/**" for p in FILE_PREFIXES), *FILE_EXACT])
        raise AssetError(f"{dest!r} is outside the paths a plugin may write ({allowed}).")
    return parsed


# ---------------------------------------------------------------------------
# where the declaration comes from
# ---------------------------------------------------------------------------


def declaration_from_wheel(contents: Any) -> PluginAssets:
    """The declaration in an extracted, reviewed wheel."""
    if not contents.manifest_text or not contents.manifest_candidates:
        return PluginAssets()
    base = contents.root / pathlib.PurePosixPath(contents.manifest_candidates[0]).parent
    return parse(contents.manifest_text, base)


def declaration_from_distribution(dist: Any) -> PluginAssets:
    """The declaration in an installed distribution, read without importing it.

    The manifest file is located by the loader's own rule (``.dist-info`` first,
    then the distribution's file list) and its text must equal what
    :func:`robothor.plugins.manifest.read_manifest_text` returns -- the bytes
    the lockfile hashed -- or nothing is applied.
    """
    from robothor.plugins.manifest import read_manifest_text

    text = read_manifest_text(dist)
    if not text:
        return PluginAssets()
    candidates = [
        f for f in (dist.files or ()) if pathlib.PurePosixPath(str(f)).name == MANIFEST_NAME
    ]
    candidates.sort(
        key=lambda f: not str(pathlib.PurePosixPath(str(f)).parent).endswith(".dist-info")
    )
    if candidates:
        located = Path(str(dist.locate_file(candidates[0])))
    else:
        from robothor.plugins.manifest import editable_manifest_path

        editable = editable_manifest_path(dist)
        if editable is None:
            return PluginAssets() if not _mentions_assets(text) else _unlocatable()
        located = editable
    try:
        if located.is_symlink() or located.read_text(encoding="utf-8") != text:
            raise AssetError(
                f"The installed {MANIFEST_NAME} could not be matched to the declaration "
                "the lockfile hashed; refusing to write its workspace files."
            )
    except OSError as exc:
        raise AssetError(f"The installed {MANIFEST_NAME} could not be read.") from exc
    return parse(text, located.parent)


def _mentions_assets(text: str) -> bool:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and any(data.get(k) for k in ASSET_KEYS)


def _unlocatable() -> PluginAssets:
    raise AssetError(
        f"The distribution's {MANIFEST_NAME} declares workspace files but cannot be "
        "located on disk, so its package-relative paths cannot be resolved."
    )


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def _ledger_path(instance_dir: Path) -> Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    return contained_path(instance_dir, LEDGER_NAME, label="plugin asset ledger")


def _ledger_document(instance_dir: Path) -> dict[str, Any]:
    path = _ledger_path(instance_dir)
    if not path.exists():
        return {"plugins": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise AssetError(
            f"The plugin asset ledger (.robothor/{LEDGER_NAME}) is not valid YAML; "
            "repair it or restore it from a backup."
        ) from exc
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        raise AssetError(f"The plugin asset ledger (.robothor/{LEDGER_NAME}) is not readable.")
    return data


def read_ledger(instance_dir: Path) -> dict[str, Any]:
    """Each plugin's record of what it wrote, keyed by canonical distribution."""
    plugins: dict[str, Any] = _ledger_document(instance_dir)["plugins"]
    return plugins


def read_retired(instance_dir: Path) -> dict[str, Any]:
    """Instruction files a retired plugin agent left behind, keyed by path.

    Retire leaves ``brain/agents/<id>.md`` in place (the Helm's convention), so
    a later install of the same plugin finds a file no manifest claims. This
    remembers whose it was and the sha256 the plugin last wrote, so that
    reinstall can take it back -- and keep it, if the operator has edited it.
    """
    retired = _ledger_document(instance_dir).get("retired")
    return retired if isinstance(retired, dict) else {}


def _write_ledger(
    instance_dir: Path, plugins: dict[str, Any], retired: dict[str, Any] | None = None
) -> None:
    if retired is None:
        retired = read_retired(instance_dir)
    document: dict[str, Any] = {"plugins": plugins}
    if retired:
        document["retired"] = retired
    text = yaml.safe_dump(document, default_flow_style=False, sort_keys=True)
    path = _ledger_path(instance_dir)
    if path.is_file() and path.read_bytes() == text.encode("utf-8"):
        return
    _write_atomically(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _manifest_fingerprint(data: dict[str, Any]) -> dict[str, str]:
    """A sha256 per top-level key of a parsed agent manifest.

    Per key, not one hash for the file, so that keys some plugin's patch
    manages can be left out of the comparison: another plugin patching this
    plugin's agent is not an operator edit.
    """
    return {
        str(key): hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for key, value in data.items()
    }


def _text_sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patched_keys(ledger: dict[str, Any], agent_id: str, patches: tuple[Any, ...]) -> set[str]:
    """The top-level manifest keys any plugin's patch manages on *agent_id*."""
    keys: set[str] = set()
    for entry in ledger.values():
        if not isinstance(entry, dict):
            continue
        for recorded in entry.get("patches") or []:
            if isinstance(recorded, dict) and recorded.get("agent") == agent_id:
                keys.update(str(k).split(".", 1)[0] for k in recorded.get("keys") or [])
    for patch in patches:
        if patch.agent == agent_id:
            keys.update(
                k.split(".", 1)[0] for k in patch_keys(patch.install) | patch_keys(patch.remove)
            )
    return keys


def _manifest_edited(path: Path, recorded: Any, ignore: set[str]) -> bool:
    """Whether the operator changed the manifest since the plugin last wrote it."""
    if not isinstance(recorded, dict) or not path.is_file() or path.is_symlink():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    current = _manifest_fingerprint(data)

    def kept(fingerprint: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in fingerprint.items() if k not in ignore}

    return kept(current) != kept(recorded)


def _reclaimable(
    retired: dict[str, Any], instruction: str, dist: str, agent_id: str
) -> dict[str, Any] | None:
    """The retired record that lets *dist* take back *instruction*, if any."""
    record = retired.get(instruction)
    if (
        isinstance(record, dict)
        and record.get("plugin") == dist
        and record.get("agent") == agent_id
    ):
        return record
    return None


def _brain_agent(dest: str) -> str | None:
    """The agent id a ``brain/agents/<id>.md`` or ``brain/agents/<id>/...`` path belongs to."""
    parts = pathlib.PurePosixPath(dest).parts
    if len(parts) < 3 or parts[:2] != ("brain", "agents"):
        return None
    return pathlib.PurePosixPath(parts[2]).stem if len(parts) == 3 else parts[2]


def patch_keys(section: dict[str, Any]) -> set[str]:
    """The dotted keys a patch section sets or deletes. A mapping under
    ``set`` is a deep merge, so its LEAVES are what it touches."""

    def leaves(mapping: dict[str, Any], prefix: str) -> set[str]:
        out: set[str] = set()
        for key, value in mapping.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict) and value:
                out |= leaves(value, dotted + ".")
            else:
                out.add(dotted)
        return out

    return leaves(section.get("set") or {}, "") | set(section.get("delete") or [])


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def _leftover_instruction(
    agent: AgentDecl,
    dist: str,
    version: str,
    root: Path,
    instance: Path,
    retired: dict[str, Any],
) -> str:
    """Judge an instruction file already on disk for an agent not yet installed.

    Retire leaves the instruction file where it was, so removing a plugin and
    installing it again finds one no manifest claims -- which the template
    installer refuses. It is this plugin's to take back when the retired record
    says this plugin wrote it for this agent, or when it holds exactly the bytes
    the plugin renders. Anything else is somebody else's file, refused here,
    before pip runs. Returns the plan line's suffix.
    """
    from robothor.templates.installer import _instruction_owner

    assert agent.instruction is not None
    try:
        path = workspace_path(
            root, agent.instruction, allowed_prefix="brain", label="instruction file"
        )
    except TemplateSecurityError as exc:
        raise AssetError(f"agent {agent.agent_id}: {exc}") from exc
    if not path.exists():
        return ""
    if _instruction_owner(root, path) is None:
        if _reclaimable(retired, agent.instruction, dist, agent.agent_id) is not None:
            return f" (takes back its retired {agent.instruction})"
        rendered = _render(agent, dist, version, root, instance).instruction
        if (
            rendered is not None
            and path.is_file()
            and not path.is_symlink()
            and path.read_bytes() == rendered.encode("utf-8")
        ):
            return f" (adopts the identical {agent.instruction})"
    raise AssetError(
        f"{agent.instruction} already exists and is not this plugin's: no record shows "
        "the plugin wrote it, and it differs from what the plugin renders. Agent "
        f"{agent.agent_id!r} will not be installed over it; move that file aside first."
    )


def plan(
    assets: PluginAssets,
    dist: str,
    *,
    workspace: Path | None = None,
    instance_dir: Path | None = None,
    version: str = "0.0.0",
) -> tuple[str, ...]:
    """What :func:`apply` would write, or an :class:`AssetError`. Writes nothing.

    The ownership rules, all checked here, before anything is written:

    * an agent id whose manifest already exists is refused unless
      ``installed.yaml`` records it as this plugin's -- or, unrecorded, its
      parsed data equals what this plugin's bundle renders here and its
      instruction file has the same bytes, in which case it is ADOPTED (a
      hand copy of exactly this agent is safe to hand over). Recorded
      elsewhere, or different, it is somebody else's and is never overwritten
      or later retired;
    * a destination another plugin's ledger owns is refused;
    * a ``brain/agents/<id>`` destination is refused at parse unless ``<id>``
      is one of this plugin's agents or an agent it declares a patch for;
    * any EXISTING ``docs/hooks/`` file this plugin did not write is refused
      in ``overwrite`` mode -- the engine executes hooks -- with no exception;
    * any other EXISTING ``brain/`` file this plugin did not write is refused in
      ``overwrite`` mode -- ``if_missing`` only -- so ``brain/TOOLS.md`` and
      the like cannot be replaced. The same spirit as the template installer's
      ``_instruction_refusal``: an agent only ever writes its own files. An
      existing file with identical bytes is not a replacement and is adopted;
    * a patch key (dotted, prefix-aware) another plugin already patches on the
      same agent is refused, naming both plugins.
    """
    if assets.empty:
        return ()
    from robothor.templates.installer import _instruction_owner
    from robothor.templates.instance import InstanceConfig

    dist = canonical(dist)
    root = _workspace(workspace)
    instance = _instance_dir(root, instance_dir)
    ledger = read_ledger(instance) if instance.is_dir() else {}
    retired = read_retired(instance) if instance.is_dir() else {}
    try:
        installed = InstanceConfig(instance).installed_agents if instance.is_dir() else {}
    except (ValueError, yaml.YAMLError) as exc:
        raise AssetError(f"installed.yaml is not readable ({type(exc).__name__}).") from exc
    mine = f"plugin:{dist}"
    own_ledger = ledger.get(dist) if isinstance(ledger.get(dist), dict) else {}
    # A file this plugin ADOPTED (it was already there with the same bytes) is
    # not one it wrote: it stays subject to the shared-file rule, so a later
    # release with different bytes is judged as replacing someone else's file.
    own_files = {
        dest
        for dest, recorded in ((own_ledger or {}).get("files") or {}).items()
        if not (isinstance(recorded, dict) and recorded.get("adopted"))
    }
    declared_for = {a.agent_id for a in assets.agents} | {p.agent for p in assets.patches}

    lines: list[str] = []
    for agent in assets.agents:
        record = installed.get(agent.agent_id)
        manifest = workspace_path(
            root,
            f"docs/agents/{agent.agent_id}.yaml",
            allowed_prefix="docs/agents",
            label="agent manifest",
        )
        if isinstance(record, dict) and record.get("source") != mine:
            raise AssetError(
                f"Agent {agent.agent_id!r} is already installed from "
                f"{record.get('source')!r}; this plugin will not take it over. Remove "
                "or rename that agent first."
            )
        if record is None and manifest.exists():
            if _matches(agent, _render(agent, dist, version, root, instance), root, exact=False):
                lines.append(
                    f"agent    {agent.agent_id} -> docs/agents/{agent.agent_id}.yaml "
                    "(adopts the existing copy: its data matches)"
                )
                continue
            raise AssetError(
                f"docs/agents/{agent.agent_id}.yaml already exists, is not this plugin's, "
                "and does not match what the plugin renders (or its instruction file "
                f"differs), so agent {agent.agent_id!r} will not be installed over it. "
                "Retire or rename the existing agent first"
                + (
                    f", and move its instruction file ({agent.instruction}) aside too: "
                    "once no manifest claims it, the template installer will not "
                    "write over it either."
                    if agent.instruction
                    else "."
                )
            )
        suffix = ""
        if record is None and agent.instruction:
            suffix = _leftover_instruction(agent, dist, version, root, instance, retired)
        lines.append(f"agent    {agent.agent_id} -> docs/agents/{agent.agent_id}.yaml{suffix}")

    others = {
        dest: owner
        for owner, entry in ledger.items()
        if owner != dist and isinstance(entry, dict)
        for dest in (entry.get("files") or {})
    }
    for item in assets.files:
        target = _dest(root, item.dest)  # allowlist and symlink refusals
        if item.dest in others:
            raise AssetError(f"{item.dest} is owned by the plugin {others[item.dest]!r}.")
        replaces = (
            target.is_file()
            and item.mode == "overwrite"
            and item.dest not in own_files
            and target.read_bytes() != item.source.read_bytes()
        )
        if replaces and item.dest.startswith("brain/"):
            agent_id = _brain_agent(item.dest)
            owner = _instruction_owner(root, target)
            if agent_id is None and owner not in declared_for:
                raise AssetError(
                    f"{item.dest} already exists and this plugin did not write it. A "
                    "plugin replaces a brain/ file only under brain/agents/<id> for an "
                    "agent it ships or patches; declare it `mode: if_missing` to seed "
                    "it only where it is absent."
                )
        if replaces and item.dest.startswith("docs/hooks/"):
            # The engine EXECUTES hooks (a `command` hook runs shell), and
            # global.yaml carries the platform's own: the shared-file rule.
            raise AssetError(
                f"{item.dest} already exists and this plugin did not write it. A plugin "
                "never replaces an existing docs/hooks/ file; declare it `mode: "
                "if_missing` to seed it only where it is absent."
            )
        label = "workflow" if item.kind == "workflow" else "file    "
        suffix = " (if missing)" if item.mode == "if_missing" else ""
        if replaces:
            suffix = f" (replaces an existing file; the original is kept as .pre-{dist})"
        if item.dest == "docs/webhooks.yaml":
            suffix += " [engine-loaded config]"
        elif item.dest.startswith("docs/hooks/"):
            suffix += " [hooks the engine executes]"
        lines.append(f"{label} {item.dest}{suffix}")

    claimed: dict[tuple[str, str], str] = {}
    for owner, entry in ledger.items():
        if owner == dist or not isinstance(entry, dict):
            continue
        for recorded in entry.get("patches") or []:
            if isinstance(recorded, dict):
                for key in recorded.get("keys") or []:
                    claimed[(str(recorded.get("agent")), str(key))] = owner
    for patch in assets.patches:
        try:
            workspace_path(
                root,
                f"docs/agents/{patch.agent}.yaml",
                allowed_prefix="docs/agents",
                label="patched manifest",
            )
        except TemplateSecurityError as exc:
            raise AssetError(str(exc)) from exc
        for key in sorted(patch_keys(patch.install) | patch_keys(patch.remove)):
            for (target_agent, theirs), owner in claimed.items():
                if target_agent == patch.agent and _overlaps(key, theirs):
                    raise AssetError(
                        f"{dist} patches `{key}` on agent {patch.agent!r}, but {owner} "
                        f"already patches `{theirs}` there. Two plugins may not patch "
                        "the same key; remove one of them or change its patch."
                    )
        keys = ", ".join(sorted(patch_keys(patch.install)))
        lines.append(f"patch    docs/agents/{patch.agent}.yaml ({keys})")
    return tuple(lines)


# ---------------------------------------------------------------------------
# patches
# ---------------------------------------------------------------------------


def _merge(target: Any, changes: dict[str, Any]) -> None:
    """Deep merge for mappings; any other value, lists included, replaces."""
    for key, value in changes.items():
        current = target.get(key) if hasattr(target, "get") else None
        if isinstance(value, dict) and isinstance(current, dict):
            _merge(current, value)
        else:
            target[key] = copy.deepcopy(value)


def _delete(target: Any, dotted: str) -> None:
    *parents, leaf = dotted.split(".")
    node = target
    for part in parents:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return
    if isinstance(node, dict) and leaf in node:
        del node[leaf]


def _edit(document: Any, section: dict[str, Any]) -> None:
    _merge(document, section.get("set") or {})
    for key in section.get("delete") or []:
        _delete(document, key)


def _patch_would_change(path: Path, section: dict[str, Any]) -> bool:
    """Whether *section* changes *path*. A patch already applied -- by an
    earlier sync, or by an installer that ran first -- changes nothing."""
    try:
        before = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise AssetError(f"{path.name} is not valid YAML; it was not patched.") from exc
    if not isinstance(before, dict):
        raise AssetError(f"{path.name} is not a YAML mapping; it was not patched.")
    after = copy.deepcopy(before)
    _edit(after, section)
    return after != before


def _patch_manifest(path: Path, section: dict[str, Any]) -> bool:
    """Apply one section to *path* in place. True when the file changed."""
    # The comment-preserving round trip `genus agent` already uses (ruamel.yaml
    # when it is importable, PyYAML otherwise), imported rather than copied.
    from robothor.cli.agent import _load_manifest, _save_manifest

    if not _patch_would_change(path, section):
        return False
    _touch(path)
    document, handler = _load_manifest(path)
    _edit(document, section)
    _save_manifest(path, document, handler)
    return True


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _stage_agent(agent: AgentDecl, version: str, into: Path) -> Path:
    """A private copy of the bundle, with a setup.yaml if the plugin shipped none."""
    staging = into / agent.agent_id
    staging.mkdir()
    for member in agent.bundle.iterdir():
        if member.is_file() and not member.is_symlink():
            shutil.copyfile(member, staging / member.name)
    if not (staging / "setup.yaml").exists():
        (staging / "setup.yaml").write_text(
            yaml.safe_dump({"agent_id": agent.agent_id, "version": version, "variables": {}}),
            encoding="utf-8",
        )
    return staging


def _engine_would_load(path: Path, agent_id: str) -> None:
    """Refuse a rendered manifest the engine itself would refuse at load.

    The engine's own gate (``robothor.engine.config._enforce_manifest_schema``),
    so its mode decides: under ``enforce`` a schema error refuses the agent,
    on the other rungs it is reported by the engine as it always was. The
    template installer's checks A-R stay advisory, as they are for ``genus
    agent install`` -- they cannot see plugin tools, and they run before the
    instruction file exists.
    """
    try:
        from robothor.engine.config import _enforce_manifest_schema
        from robothor.engine.manifest_schema import ManifestSchemaError
    except Exception as exc:  # noqa: BLE001 - no engine on the path is a normal state
        logger.debug("Engine schema gate unavailable (%s)", type(exc).__name__)
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AssetError(f"agent {agent_id}: the rendered manifest is not valid YAML") from exc
    try:
        _enforce_manifest_schema(data, manifest_dir=path.parent)
    except ManifestSchemaError as exc:
        raise AssetError(f"agent {agent_id}: the engine would refuse it ({exc})") from exc


@dataclass(frozen=True)
class Rendered:
    """What one agent bundle renders to on THIS instance, without writing it."""

    manifest_text: str
    manifest: dict[str, Any]
    instruction: str | None


def _render(agent: AgentDecl, dist: str, version: str, root: Path, instance: Path) -> Rendered:
    """Render *agent* exactly as :func:`_install_agent` would, into a scratch tree.

    The same template installer, the same ``_defaults.yaml`` (the workspace's,
    when it has one), the instance's ``config.yaml`` and the agent's
    ``overrides/<id>.yaml`` -- so the result is what an install would write,
    and the workspace is not touched.
    """
    from robothor.templates.installer import install

    with tempfile.TemporaryDirectory(prefix="genus-plugin-render-") as raw:
        scratch = Path(raw).resolve()
        (scratch / "docs" / "agents").mkdir(parents=True)
        (scratch / "brain").mkdir()
        defaults = root / "templates" / "agents" / "_defaults.yaml"
        if defaults.is_file() and not defaults.is_symlink():
            (scratch / "templates" / "agents").mkdir(parents=True)
            shutil.copyfile(defaults, scratch / "templates" / "agents" / "_defaults.yaml")
        scratch_instance = scratch / ".robothor"
        (scratch_instance / "overrides").mkdir(parents=True)
        for relative in ("config.yaml", f"overrides/{agent.agent_id}.yaml"):
            source = instance / relative
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, scratch_instance / relative)
        (scratch / "bundle").mkdir()
        staging = _stage_agent(agent, version, scratch / "bundle")
        try:
            install(
                staging,
                auto_yes=True,
                instance_dir=scratch_instance,
                repo_root=scratch,
                source="plugin",
                source_ref=dist,
            )
        except (
            TemplateSecurityError,
            FileNotFoundError,
            ValueError,
            OSError,
            AttributeError,
            yaml.YAMLError,
        ) as exc:
            # A malformed config.yaml, overrides file or _defaults.yaml: one
            # plugin's sentence, never a traceback that aborts the whole sync.
            raise AssetError(
                f"agent {agent.agent_id}: could not render it ({type(exc).__name__}: {exc})"
            ) from exc
        text = (scratch / "docs" / "agents" / f"{agent.agent_id}.yaml").read_text(encoding="utf-8")
        instruction = None
        if agent.instruction:
            rendered = scratch / agent.instruction
            if rendered.is_file():
                instruction = rendered.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return Rendered(text, data if isinstance(data, dict) else {}, instruction)


def _matches(
    agent: AgentDecl,
    rendered: Rendered,
    root: Path,
    *,
    exact: bool,
    only: str | None = None,
) -> bool:
    """Whether the workspace already holds what *rendered* would write.

    ``exact`` compares the manifest's bytes (a file the plugin wrote itself);
    otherwise its parsed DATA, so a hand copy with other comments, quoting or
    key order still matches. The instruction file is always compared by bytes.
    ``only`` ("manifest" or "instruction") limits the check to one of the two.
    """
    if only != "instruction" and not _manifest_matches(agent, rendered, root, exact=exact):
        return False
    if only == "manifest":
        return True
    if agent.instruction and rendered.instruction is not None:
        instruction = root / agent.instruction
        if not instruction.is_file() or instruction.is_symlink():
            return False
        if instruction.read_text(encoding="utf-8") != rendered.instruction:
            return False
    return True


def _manifest_matches(agent: AgentDecl, rendered: Rendered, root: Path, *, exact: bool) -> bool:
    manifest = root / "docs" / "agents" / f"{agent.agent_id}.yaml"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    text = manifest.read_text(encoding="utf-8")
    if exact:
        if text != rendered.manifest_text:
            return False
    else:
        try:
            if (yaml.safe_load(text) or {}) != rendered.manifest:
                return False
        except yaml.YAMLError:
            return False
    return True


def _mark_adopted(agent: AgentDecl, dist: str, version: str, instance: Path) -> None:
    from robothor.templates.instance import InstanceConfig

    config = InstanceConfig.load(instance)
    _touch(config.installed_path)
    config.record_install(
        agent_id=agent.agent_id,
        source=f"plugin:{dist}",
        source_path=dist,
        version=version,
        variables={},
        manifest_path=f"docs/agents/{agent.agent_id}.yaml",
        instruction_path=agent.instruction,
    )
    agents = config.installed_agents
    agents[agent.agent_id]["adopted"] = True
    config._save_installed(agents)


def _install_agent(
    agent: AgentDecl,
    dist: str,
    version: str,
    root: Path,
    instance: Path,
    report: Report,
    *,
    previous: dict[str, Any] | None = None,
    retired: dict[str, Any] | None = None,
    patched: set[str] | None = None,
) -> dict[str, Any]:
    """Render one agent; return what the ledger records of what it wrote.

    :func:`plan` has already refused any manifest, or leftover instruction file,
    that is not this plugin's, so this only ever writes the plugin's own agent.

    *previous* is the ledger's record of what the plugin last wrote for this
    agent: a file the operator has changed since then is left in place with a
    warning -- the rule ``files:`` follows -- while an unedited one takes the
    new release. Manifest keys in *patched* belong to some plugin's patch and
    are not an operator edit. *retired* is :func:`read_retired`, for an agent
    being installed again after a remove.
    """
    from robothor.templates.installer import install

    manifest = workspace_path(
        root,
        f"docs/agents/{agent.agent_id}.yaml",
        allowed_prefix="docs/agents",
        label="agent manifest",
    )
    instruction = None
    if agent.instruction:
        try:
            instruction = workspace_path(
                root, agent.instruction, allowed_prefix="brain", label="instruction file"
            )
        except TemplateSecurityError as exc:
            raise AssetError(f"agent {agent.agent_id}: {exc}") from exc
    from robothor.templates.instance import InstanceConfig

    record = InstanceConfig.load(instance).installed_agents.get(agent.agent_id)
    adopted = isinstance(record, dict) and bool(record.get("adopted"))
    rendered = _render(agent, dist, version, root, instance)
    written: dict[str, Any] = {
        "manifest": _manifest_fingerprint(rendered.manifest),
        "instruction_file": agent.instruction,
        "instruction": _text_sha(rendered.instruction),
    }
    if record is None and manifest.exists():
        # plan() only lets this through when the data matched: adopt the copy.
        _mark_adopted(agent, dist, version, instance)
        report.lines.append(f"adopted  {agent.agent_id} (the existing copy matches the plugin's)")
        return written

    # What the plugin last wrote: the ledger for an agent it has, the retired
    # record for one it is installing again after a remove.
    last = previous if isinstance(previous, dict) and record is not None else {}
    last_instruction = last.get("instruction")
    if record is None and agent.instruction:
        reclaim = _reclaimable(retired or {}, agent.instruction, dist, agent.agent_id)
        last_instruction = reclaim.get("sha256") if reclaim else None

    keep: dict[Path, bytes] = {}
    edited: list[str] = []
    if record is not None and _manifest_edited(manifest, last.get("manifest"), patched or set()):
        keep[manifest] = manifest.read_bytes()
        edited.append(f"docs/agents/{agent.agent_id}.yaml")
    leftover = (
        instruction is not None
        and rendered.instruction is not None
        and instruction.is_file()
        and not instruction.is_symlink()
    )
    if leftover:
        assert instruction is not None and rendered.instruction is not None
        current = instruction.read_bytes()
        if (
            current != rendered.instruction.encode("utf-8")
            and last_instruction is not None
            and hashlib.sha256(current).hexdigest() != last_instruction
        ):
            keep[instruction] = current
            assert agent.instruction is not None
            edited.append(agent.instruction)
    for relative in edited:
        report.warnings.append(
            f"{relative}: edited since the plugin wrote it; left in place. Delete it "
            "and sync again to take the plugin's version."
        )

    manifest_same = manifest in keep or (
        manifest.is_file() and _matches(agent, rendered, root, exact=not adopted, only="manifest")
    )
    instruction_same = (
        instruction is None
        or rendered.instruction is None
        or instruction in keep
        or _matches(agent, rendered, root, exact=True, only="instruction")
    )
    if manifest_same and instruction_same:
        return written  # already what an install would write: nothing to do
    if adopted:
        # The first write over an adopted copy keeps it, once.
        for path in (manifest, instruction):
            if path is not None and path.is_file() and path not in keep:
                _backup_once(path, dist, root)
    for path in (manifest, instruction, instance / "installed.yaml"):
        if path is not None:
            _touch(path)
    if instruction is not None and instruction.is_file() and not manifest.is_file():
        # No manifest claims the instruction file (a retired agent's, or the
        # operator deleted the manifest), so the template installer would
        # refuse it. plan() vetted it as this plugin's; clear the way. Its
        # bytes are restored below when it is being kept.
        instruction.unlink()
    with tempfile.TemporaryDirectory(prefix="genus-plugin-agent-") as scratch:
        # Resolved: the template installer refuses a bundle path that runs
        # through a symlink, and the default macOS temp dir does (/var ->
        # /private/var).
        staging = _stage_agent(agent, version, Path(scratch).resolve())
        try:
            result = install(
                staging,
                auto_yes=True,
                instance_dir=instance,
                repo_root=root,
                source="plugin",
                source_ref=dist,
            )
        except (TemplateSecurityError, FileNotFoundError, ValueError, OSError) as exc:
            raise AssetError(f"agent {agent.agent_id}: {exc}") from exc
    for path, data in keep.items():
        _write_atomically(path, data)
    _engine_would_load(manifest, agent.agent_id)
    # Only facts this module owns reach the report: the id and the plugin's
    # version, never values from the install result. That result carries the
    # rendered variable context (instance config, operator overrides) and
    # validation text built from environment lookups (check L, secret_refs);
    # none of it belongs on an operator's terminal or in a CI log.
    report.lines.append(f"agent    {agent.agent_id} {version}")
    findings = len(result.get("validation") or []) + len(result.get("chain_validation") or [])
    if findings:
        report.warnings.append(
            f"{agent.agent_id}: {findings} advisory validation finding(s); see them with "
            f"`python scripts/validate_agents.py --agent {agent.agent_id}`"
        )
    return written


def _write_file(
    item: FileDecl, dist: str, root: Path, previous: dict[str, Any] | None, report: Report
) -> dict[str, Any] | None:
    """Write one file; return its ledger entry, or None when it is not ours.

    Identical content is a no-op with no backup -- which is what makes running
    after an installer that already copied the same file safe -- and an
    existing ``<dst>.pre-<plugin>`` is never overwritten.
    """
    target = _dest(root, item.dest)
    data = item.source.read_bytes()
    if item.mode == "if_missing" and target.exists():
        if previous is None:
            report.lines.append(f"kept     {item.dest} (exists; if_missing)")
        return previous
    if (
        previous is not None
        and target.is_file()
        and target.read_bytes() != data
        and _sha256(target) != previous.get("sha256")
    ):
        # The operator edited what the plugin wrote. Never revert that
        # silently; the same rule retire follows.
        report.warnings.append(
            f"{item.dest}: edited since the plugin wrote it; left in place. Delete it "
            "and sync again to take the plugin's version."
        )
        return previous
    adopted = bool(previous and previous.get("adopted"))
    if target.is_file() and target.read_bytes() == data:
        if previous is None or adopted:
            # Already in place with these bytes (an installer copied it first):
            # adopted, not written. The plugin did not create it, so remove
            # will leave it and plan keeps judging it as someone else's file.
            return {"sha256": hashlib.sha256(data).hexdigest(), "backup": None, "adopted": True}
        return previous
    backup = previous.get("backup") if previous and not adopted else None
    if (previous is None or adopted) and target.exists():
        backup = _backup_once(target, dist, root)
    _write_atomically(target, data)
    report.lines.append(f"wrote    {item.dest}")
    return {"sha256": hashlib.sha256(data).hexdigest(), "backup": backup}


def apply(
    assets: PluginAssets,
    dist: str,
    version: str,
    *,
    workspace: Path | None = None,
    instance_dir: Path | None = None,
) -> Report:
    """Write the declared workspace files. Idempotent; see the module docstring.

    All or nothing per plugin: on any failure every file this call touched is
    restored and the ledger is left as it was, then the :class:`AssetError`
    propagates. A caller looping over plugins (``sync``) reports it and moves
    on to the next one.
    """
    dist = canonical(dist)
    report = Report()
    if assets.empty:
        # Nothing declared now; only an upgrade that dropped everything it
        # used to ship has anything to do, and that needs a ledger entry.
        raw = workspace if workspace is not None else default_workspace_root()
        instance = _instance_dir(Path(raw), instance_dir)
        if not instance.is_dir() or dist not in read_ledger(instance):
            return report
    plan(assets, dist, workspace=workspace, instance_dir=instance_dir, version=version)
    root = _workspace(workspace)
    instance = _instance_dir(root, instance_dir)
    ledger = read_ledger(instance)
    retired = read_retired(instance)
    found = ledger.get(dist)
    previous: dict[str, Any] = found if isinstance(found, dict) else {}
    before_agents = set(previous.get("agents") or [])
    before_agent_files = previous.get("agent_files")
    if not isinstance(before_agent_files, dict):
        before_agent_files = {}
    before_files: dict[str, Any] = dict(previous.get("files") or {})
    before_patches = {
        p.get("agent"): p for p in previous.get("patches") or [] if isinstance(p, dict)
    }

    entry: dict[str, Any] = {
        "version": version,
        "agents": [],
        "agent_files": {},
        "files": {},
        "patches": [],
    }
    journal: dict[Path, bytes | None] = {}
    token = _JOURNAL.set(journal)
    try:
        for agent in assets.agents:
            entry["agent_files"][agent.agent_id] = _install_agent(
                agent,
                dist,
                version,
                root,
                instance,
                report,
                previous=before_agent_files.get(agent.agent_id),
                retired=retired,
                patched=_patched_keys(ledger, agent.agent_id, assets.patches),
            )
            entry["agents"].append(agent.agent_id)
            if agent.instruction and _reclaimable(retired, agent.instruction, dist, agent.agent_id):
                retired.pop(agent.instruction)  # taken back: no longer retired
        for item in assets.files:
            recorded = _write_file(item, dist, root, before_files.get(item.dest), report)
            if recorded is not None:
                entry["files"][item.dest] = recorded
        for patch in assets.patches:
            target = workspace_path(
                root,
                f"docs/agents/{patch.agent}.yaml",
                allowed_prefix="docs/agents",
                label="patched manifest",
            )
            if not target.is_file():
                report.warnings.append(
                    f"patch for {patch.agent!r} skipped: docs/agents/{patch.agent}.yaml "
                    "does not exist"
                )
                continue
            if _patch_would_change(target, patch.install):
                if patch.agent not in before_patches:
                    _backup_once(target, dist, root)
                _patch_manifest(target, patch.install)
                report.lines.append(f"patched  docs/agents/{patch.agent}.yaml")
            entry["patches"].append(
                {
                    "agent": patch.agent,
                    "keys": sorted(patch_keys(patch.install) | patch_keys(patch.remove)),
                    "remove": patch.remove,
                }
            )

        # What an upgrade stopped shipping is retired like a removal would.
        patched = {x["agent"] for x in entry["patches"]}
        stale = {
            "agents": sorted(before_agents - set(entry["agents"])),
            "agent_files": before_agent_files,
            "files": {k: v for k, v in before_files.items() if k not in entry["files"]},
            "patches": [p for a, p in before_patches.items() if a not in patched],
        }
        _undo(stale, dist, root, instance, report, retired)

        if not entry["agent_files"]:
            del entry["agent_files"]
        if entry["agents"] or entry["files"] or entry["patches"]:
            ledger[dist] = entry
        else:
            ledger.pop(dist, None)
        _write_ledger(instance, ledger, retired)
    except Exception as exc:
        _rollback(journal)
        if isinstance(exc, AssetError):
            raise
        raise AssetError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        _JOURNAL.reset(token)
    return report


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


def _retire_agent(
    agent_id: str,
    dist: str,
    root: Path,
    instance: Path,
    report: Report,
    written: Any = None,
    retired: dict[str, Any] | None = None,
) -> None:
    """Retire one agent. *written* is the ledger's record of what the plugin
    wrote for it; the instruction file it leaves behind goes into *retired*, so
    installing the plugin again can take it back."""
    from robothor.templates.instance import InstanceConfig

    config = InstanceConfig.load(instance)
    record = config.installed_agents.get(agent_id)
    if not isinstance(record, dict) or record.get("source") != f"plugin:{dist}":
        where = record.get("source") if isinstance(record, dict) else "nowhere it records"
        report.warnings.append(
            f"agent {agent_id} is not recorded as this plugin's (source: {where}); left alone"
        )
        return
    agents_dir = root / "docs" / "agents"
    manifest = workspace_path(
        root, f"docs/agents/{agent_id}.yaml", allowed_prefix="docs/agents", label="agent manifest"
    )
    if manifest.is_file() and not manifest.is_symlink():
        (agents_dir / "retired").mkdir(parents=True, exist_ok=True)
        target = contained_path(agents_dir, f"retired/{agent_id}.yaml", label="retired manifest")
        _touch(manifest)
        _touch(target)
        _touch(config.installed_path)
        manifest.replace(target)
        report.lines.append(f"retired  {agent_id} -> docs/agents/retired/{agent_id}.yaml")
    if retired is not None:
        _remember_instruction(agent_id, dist, root, record, written, retired)
    _touch(config.installed_path)
    config.record_remove(agent_id)


def _remember_instruction(
    agent_id: str,
    dist: str,
    root: Path,
    record: dict[str, Any],
    written: Any,
    retired: dict[str, Any],
) -> None:
    """Record the instruction file a retired agent leaves behind, and its sha256
    as the plugin last wrote it (or as it is now, when the ledger predates that)."""
    written = written if isinstance(written, dict) else {}
    relative = written.get("instruction_file") or record.get("instruction_path")
    if not isinstance(relative, str) or not relative:
        return
    try:
        path = workspace_path(root, relative, allowed_prefix="brain", label="instruction file")
    except TemplateSecurityError:
        return
    if not path.is_file() or path.is_symlink():
        return
    sha = written.get("instruction") or _sha256(path)
    retired[relative] = {"plugin": dist, "agent": agent_id, "sha256": sha}


def _remove_file(
    dest: str, recorded: dict[str, Any], dist: str, root: Path, report: Report
) -> None:
    try:
        target = _dest(root, dest)
    except AssetError as exc:
        report.warnings.append(f"{dest}: left in place ({type(exc).__name__})")
        return
    backup = _backup_path(target, dist)
    if recorded.get("adopted"):
        # It was there before the plugin; it is not the plugin's to delete,
        # and neither is a backup some other installer made of it.
        # Report text is built from the DECLARED relative `dest` and the
        # distribution name only, never from a resolved path.
        declared_backup = f"{pathlib.PurePosixPath(dest).name}.pre-{dist}"
        kept = f"; its backup {declared_backup} kept" if backup.is_file() else ""
        report.lines.append(f"left     {dest} (was in place before the plugin{kept})")
        return
    if target.is_file() and _sha256(target) != recorded.get("sha256"):
        report.warnings.append(f"{dest}: edited since the plugin wrote it; left in place")
        return
    if recorded.get("backup") and backup.is_file() and not backup.is_symlink():
        _touch(target)
        _touch(backup)
        backup.replace(target)
        report.lines.append(f"restored {dest}")
        return
    if target.is_file():
        _touch(target)
        target.unlink()
        report.lines.append(f"removed  {dest}")


def _why(exc: BaseException) -> str:
    """A fixed phrase for a report line, never the exception's own text (which
    can carry a resolved path)."""
    if isinstance(exc, AssetError):
        return "its manifest is not valid YAML, or the patch is malformed"
    return type(exc).__name__


def _undo(
    entry: dict[str, Any],
    dist: str,
    root: Path,
    instance: Path,
    report: Report,
    retired: dict[str, Any] | None = None,
) -> None:
    agent_files = entry.get("agent_files")
    if not isinstance(agent_files, dict):
        agent_files = {}
    for agent_id in entry.get("agents") or []:
        try:
            _retire_agent(
                validate_identifier(agent_id),
                dist,
                root,
                instance,
                report,
                agent_files.get(agent_id),
                retired,
            )
        except (TemplateSecurityError, OSError, ValueError, yaml.YAMLError) as exc:
            report.warnings.append(f"agent {agent_id}: not retired ({type(exc).__name__})")
    for dest, recorded in (entry.get("files") or {}).items():
        _remove_file(dest, recorded or {}, dist, root, report)
    for patch in entry.get("patches") or []:
        agent = patch.get("agent") if isinstance(patch, dict) else None
        try:
            target = workspace_path(
                root,
                f"docs/agents/{validate_identifier(agent)}.yaml",
                allowed_prefix="docs/agents",
                label="patched manifest",
            )
            section = _patch_section(patch.get("remove"), "ledger remove")
        except (TemplateSecurityError, AssetError) as exc:
            report.warnings.append(f"patch for {agent!r}: not undone ({_why(exc)})")
            continue
        try:
            if target.is_file() and _patch_manifest(target, section):
                report.lines.append(f"unpatched docs/agents/{agent}.yaml")
        except (AssetError, OSError) as exc:
            report.warnings.append(f"patch for {agent!r}: not undone ({_why(exc)})")


def check_retire(
    dist: str, *, workspace: Path | None = None, instance_dir: Path | None = None
) -> None:
    """Raise :class:`AssetError` now if :func:`retire` could not run later.

    ``remove`` calls this BEFORE it deletes the package: once the files are
    gone, a failure here would leave nothing to re-run against.
    """
    from robothor.templates.instance import InstanceConfig

    root = workspace if workspace is not None else default_workspace_root()
    instance = _instance_dir(Path(root), instance_dir)
    if not (Path(root).is_dir() and instance.is_dir()):
        return
    entry = read_ledger(instance).get(canonical(dist))
    if isinstance(entry, dict) and entry.get("agents"):
        try:
            InstanceConfig(instance).installed_agents  # noqa: B018 - parse it now
        except (ValueError, yaml.YAMLError) as exc:
            raise AssetError(
                ".robothor/installed.yaml is not readable, so this plugin's agents "
                "cannot be retired; repair it and run the remove again."
            ) from exc


def retire(dist: str, *, workspace: Path | None = None, instance_dir: Path | None = None) -> Report:
    """Undo what :func:`apply` recorded for *dist*. Needs no wheel."""
    dist = canonical(dist)
    report = Report()
    root_raw = workspace if workspace is not None else default_workspace_root()
    if not Path(root_raw).is_dir():
        return report
    root = _workspace(root_raw)
    instance = _instance_dir(root, instance_dir)
    if not instance.is_dir():
        return report
    ledger = read_ledger(instance)
    retired = read_retired(instance)
    entry = ledger.get(dist)
    if not isinstance(entry, dict):
        return report
    _undo(entry, dist, root, instance, report, retired)
    ledger.pop(dist, None)
    _write_ledger(instance, ledger, retired)
    return report


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@dataclass
class SyncedPlugin:
    name: str
    report: Report | None = None
    skipped: str = ""
    #: True for a plugin the ledger remembers that is no longer installed.
    orphaned: bool = False
    #: True when the plugin's workspace apply was refused or failed -- not for
    #: a deliberate skip (disabled) or an orphan. What ``sync --strict`` counts.
    failed: bool = False


#: What sync says about a plugin that has disappeared since the last sync.
ORPHAN_NOTE = (
    "no longer installed; its agents and files were left in place. Run "
    "`genus plugin remove --force {name}` to retire them"
)


def sync_installed(
    distributions: dict[str, Any] | None = None,
    *,
    lock_path: Path | None = None,
    workspace: Path | None = None,
    instance_dir: Path | None = None,
) -> list[SyncedPlugin]:
    """Apply every installed plugin's workspace files, under the loader's gate.

    This is the whole install path for a plugin put on the path by hand
    (``pip install --no-deps --target <plugin dir> <wheel>``, then ``genus
    plugin sync``), so it does everything ``genus plugin install`` does to the
    workspace, from the installed copy.

    * **The loader's gate, not a second one.** A distribution the loader would
      refuse to import gets nothing written. Under ``genus plugin sync`` the
      lockfile rows are re-recorded first, so the manifest digest always
      matches here and only ``disabled`` filters; the drift half of the gate
      matters only to a caller that has not just re-recorded. A distribution
      with no manifest declares nothing, which is the only thing ``enforce``
      changes for workspace files: in every mode, nothing undeclared is ever
      written.
    * **One plugin at a time.** A plugin whose apply fails is rolled back to
      what it had and reported; the rest still sync.
    * **A plugin that disappeared is reported, not retired.** Discovery reads
      the interpreter's path; a sync run from an environment that is missing
      the plugin directory would otherwise retire the whole plugin fleet. The
      report names the command that retires them on purpose.
    """
    from robothor.plugins import lockfile

    found = lockfile.installed_distributions() if distributions is None else distributions
    lock = lockfile.read_lockfile(lock_path)
    results: list[SyncedPlugin] = []
    for name, dist in sorted(found.items()):
        try:
            declared = declaration_from_distribution(dist)
        except AssetError as exc:
            results.append(SyncedPlugin(name, skipped=str(exc), failed=True))
            continue
        refusal = lockfile.refusal_for(dist, lock)
        if refusal is not None:
            if not declared.empty:
                results.append(SyncedPlugin(name, skipped=refusal))
            continue
        try:
            report = apply(
                declared,
                name,
                str(getattr(dist, "version", "") or ""),
                workspace=workspace,
                instance_dir=instance_dir,
            )
        except (AssetError, OSError) as exc:
            results.append(
                SyncedPlugin(
                    name,
                    skipped=f"{exc} (its previous files were left as they were)",
                    failed=True,
                )
            )
            continue
        if report.lines or report.warnings:
            results.append(SyncedPlugin(name, report=report))

    raw = workspace if workspace is not None else default_workspace_root()
    instance = _instance_dir(Path(raw), instance_dir)
    if instance.is_dir():
        present = {canonical(n) for n in found}
        results.extend(
            SyncedPlugin(gone, skipped=ORPHAN_NOTE.format(name=gone), orphaned=True)
            for gone in sorted(set(read_ledger(instance)) - present)
        )
    return results

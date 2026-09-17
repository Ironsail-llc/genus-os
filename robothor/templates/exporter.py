"""`genus agent export` — turn an installed agent into a bundle somebody else can install.

``import_agent`` already reverse-engineers an installed agent into a template
bundle, and that is the half of this that existed: a directory of
``setup.yaml`` / ``manifest.template.yaml`` / ``instructions.template.md``
suitable for the hub. What it does not produce is something you can hand to a
person: it names no requirements, pins no hashes, copies none of the skills the
agent actually uses, and happily writes out an instruction file with an API key
in it.

This module is the rest. It stages an import into a temporary directory, adds
what the agent needs to run elsewhere, runs the two gates over EVERY staged
file, and only then writes a directory or a reproducible tarball.

**Nothing is written until every gate has passed.** The staging directory is the
whole reason: a gate that fired halfway through a direct write would leave a
partial bundle containing exactly the file it was refusing to publish.

**What is NOT in an export.** Memory, CRM rows, the instance's own config, and
— unless the operator asks for them — adapter definitions. An adapter names a
command to run and the credentials to run it with, which makes it the one part
of an agent that is a remote-code-execution primitive rather than a
description. ``--include-adapters`` carries them anyway, with every
credential-shaped value collapsed to a ``${NAME}`` reference the far side has to
supply for itself.
"""

from __future__ import annotations

import gzip
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from robothor.templates.bundle import (
    BUNDLE_FILENAME,
    BundleManifest,
    Finding,
    Requires,
    bundle_document,
    files_for,
    scan_instance_leaks,
    scan_secret_literals,
)
from robothor.templates.installer import import_agent
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    default_workspace_root,
    trusted_directory,
    validate_identifier,
    workspace_path,
)

__all__ = ["ExportError", "ExportResult", "archive_name", "export_agent"]

#: Suffixes that mean "write one file, not a directory".
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")

#: ``${NAME}`` references anywhere in the exported text. Each one is a variable
#: the far side must set, which is precisely what ``requires.secrets`` is for.
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,127})\}")

#: A key whose NAME means credential, whatever its value turns out to be.
_CREDENTIAL_KEY = re.compile(
    r"(?i)(?:secret|password|passwd|passphrase|credential|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|auth[_-]?key|token|authorization|cookie)"
)

#: A name that is already an environment variable, so collapsing it does not
#: need to invent one.
_ENV_SHAPED = re.compile(r"[A-Z][A-Z0-9_]{0,127}")

#: Adapter sections where a credential actually lives. Collapsing is limited to
#: these plus credential-named keys anywhere: replacing every string in the file
#: would turn ``transport: http`` into a variable the far side has to set.
_ADAPTER_SECRET_SECTIONS = ("headers", "env")


class ExportError(Exception):
    """A refusal, with the findings that caused it.

    The findings carry ``file:line`` and a reason, never the text — an error
    message that quotes the credential it found has published it to the
    terminal, the shell history and the CI log.
    """

    def __init__(self, message: str, findings: tuple[Finding, ...] = ()) -> None:
        self.findings = findings
        if findings:
            detail = "\n".join(f"  {finding.describe()}" for finding in findings)
            message = f"{message}\n{detail}"
        super().__init__(message)


@dataclass(frozen=True)
class ExportResult:
    """Where the bundle went, and what it says about itself."""

    agent_id: str
    version: str
    manifest: BundleManifest
    path: Path
    is_archive: bool


def archive_name(agent_id: str, version: str) -> str:
    """The conventional filename for an exported bundle."""
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "-", version) or "0.0.0"
    return f"agent-{agent_id}-{safe_version}.tar.gz"


# ---------------------------------------------------------------------------
# requirements
# ---------------------------------------------------------------------------


def _declared_requires(manifest: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """The optional ``requires:`` block an agent manifest may carry.

    The manifest is the only place an agent can SAY what it needs: skills are a
    workspace-wide library and plugins are a process-wide seam, so neither can
    be derived by looking at the agent alone. Adapters and env references can
    be derived, and are — a declaration is merged with them rather than
    replacing them, so forgetting to declare an adapter does not quietly drop
    it from the bundle.
    """
    declared = manifest.get("requires") or {}
    if not isinstance(declared, dict):
        raise ExportError("The agent manifest's requires: block is not a mapping.")
    result: dict[str, tuple[str, ...]] = {}
    for kind in ("plugins", "adapters", "secrets", "skills"):
        values = declared.get(kind) or []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ExportError(f"The agent manifest's requires.{kind} is not a list of strings.")
        result[kind] = tuple(values)
    return result


def _env_references(texts: list[str]) -> set[str]:
    return {match.group(1) for text in texts for match in _ENV_REFERENCE.finditer(text)}


def _adapters_for_agent(adapter_dir: Path | None, agent_id: str) -> dict[str, dict[str, Any]]:
    """Adapters this agent is named in, by name.

    ``agents: ["*"]`` is deliberately excluded. A wildcard adapter is a property
    of the INSTANCE — everything it has runs against that server — not of the
    agent being exported, and carrying it would hand the far side a connection
    the agent never asked for.
    """
    if adapter_dir is None or not Path(adapter_dir).is_dir():
        return {}
    root = trusted_directory(adapter_dir, label="adapter directory")
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.yaml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        agents = data.get("agents") or []
        if not isinstance(agents, list) or agent_id not in agents:
            continue
        name = str(data.get("name") or path.stem)
        try:
            found[validate_identifier(name, label="adapter name")] = data
        except TemplateSecurityError:
            continue
    return found


def _env_name_for(adapter: str, key: str) -> str:
    if _ENV_SHAPED.fullmatch(key):
        return key
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{adapter}_{key}").strip("_")
    return slug.upper()


#: A URL with no userinfo. An endpoint is the one thing an adapter must state
#: in the clear, and versioned paths (``/v1/_mcp``) are the norm — so "has a
#: digit" made every ordinary MCP endpoint a secret the receiver had to
#: hand-supply. A URL that DOES carry userinfo is caught by the credential
#: shapes long before this, so exempting the rest costs nothing.
_PLAIN_URL = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://(?![^/\s:@]{1,256}:[^/\s@]{1,256}@)\S*$")


def _is_opaque(value: str) -> bool:
    """Whether *value* reads as an issued identifier rather than as a setting.

    Long, unbroken, and mixing letters with digits. ``live``, ``http`` and
    ``2026-07-28`` are settings; ``s3ss10n-abcdefghijklmnop`` is something a
    server handed this instance, and the far side must get its own.

    Deliberately a heuristic, and deliberately only used where a false positive
    is cheap: it turns one adapter value into a ``${NAME}`` the receiver has to
    fill in. Guessing at entropy anywhere the cost is an unreadable error is
    what :mod:`robothor.secrets.redaction` refuses to do, and rightly.
    """
    if _PLAIN_URL.match(value):
        return False
    return (
        len(value) >= 16
        and not any(character.isspace() for character in value)
        and any(character.isdigit() for character in value)
        and any(character.isalpha() for character in value)
    )


def _looks_like_a_credential(key: str, value: str) -> bool:
    from robothor.templates.bundle import credential_shape

    if _CREDENTIAL_KEY.search(key):
        return True
    return credential_shape(value) is not None


def _collapse_adapter(name: str, data: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """A copy of *data* with credential values replaced by ``${NAME}`` references.

    Three rules, widened after a hostile review exported an adapter carrying a
    password in its ``url``, a GitHub token in its ``command`` and an opaque
    session id in a header — while declaring ``requires.secrets: []``.

    * **Every value under ``headers:``** is parameterised, not just the ones
      that look like a credential. A header is authentication or it is routing;
      both belong to the receiving instance, and an opaque session id is not
      distinguishable from a bearer token by shape.
    * **Under ``env:``**, and for any top-level string**, a value is
      parameterised when its key names a credential, its value carries a known
      credential shape, or it reads as an issued identifier. ``MODE: live``
      survives, because turning every setting into a required variable would
      make the bundle unusable rather than safe.
    * **``command:`` is never rewritten.** Rewriting an argv element would
      break the command, so a credential there is left where it is — and the
      export gate then refuses the whole bundle, naming the file and line. That
      is the honest answer: the operator has to take it out.
    """
    collapsed: dict[str, Any] = {}
    needed: set[str] = set()

    def parameterise(section: str, key: str, raw: Any, *, always: bool) -> Any:
        text = str(raw)
        if _ENV_REFERENCE.fullmatch(text):
            needed.update(_env_references([text]))
            return text
        if not isinstance(raw, str):
            return raw
        if always or _looks_like_a_credential(key, text) or _is_opaque(text):
            env = _env_name_for(section, key)
            needed.add(env)
            return "${" + env + "}"
        return raw

    for key, value in data.items():
        if key in _ADAPTER_SECRET_SECTIONS and isinstance(value, dict):
            collapsed[key] = {
                inner: parameterise(name, str(inner), raw, always=key == "headers")
                for inner, raw in value.items()
            }
            continue
        if isinstance(value, str):
            collapsed[key] = parameterise(name, str(key), value, always=False)
            continue
        collapsed[key] = value
    return collapsed, needed


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


#: The platform's bundled tree, and the default instance tree. The instance one
#: is only the DEFAULT: ``_skill_roots`` asks the engine where skills actually
#: land, so an instance that moved the directory can still export a bundle.
_SKILL_ROOTS = ("agents/skills", "brain/skills")


def _skill_roots(repo_root: Path) -> tuple[str, ...]:
    """Where a required skill can live, in the engine's own read order.

    Workspace-relative, because staging resolves each as its own contained
    sub-root. A configured instance directory outside the workspace cannot be
    staged safely, so it is left out and the caller's "this instance does not
    have it" error stands -- the doctor's ``skills.instance_dir`` is what says
    that directory is misplaced.
    """
    roots = list(_SKILL_ROOTS)
    try:
        from robothor.engine.skills import instance_skills_dir

        configured = instance_skills_dir().resolve()
        relative = configured.relative_to(repo_root.resolve()).as_posix()
    except Exception:  # noqa: BLE001 - an unreadable override is not an export failure
        return tuple(roots)
    if relative and relative not in roots:
        roots.append(relative)
    return tuple(roots)


def _copy_skill(repo_root: Path, staging: Path, skill: str) -> None:
    source: Path | None = None
    for root in _skill_roots(repo_root):
        try:
            candidate = workspace_path(
                repo_root,
                f"{root}/{skill}",
                allowed_prefix=root,
                label="skill directory",
            )
        except TemplateSecurityError as exc:
            raise ExportError(str(exc)) from exc
        if candidate.is_dir():
            source = candidate
    if source is None:
        raise ExportError(
            f"The manifest requires the skill {skill!r}, which this instance does not "
            "have at agents/skills/ or brain/skills/. Export it from the instance that "
            "owns it, or drop it from requires.skills."
        )
    destination = contained_path(staging, f"skills/{skill}", label="staged skill path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False, ignore_dangling_symlinks=False)


def _staged_texts(staging: Path) -> list[tuple[str, str]]:
    """Every staged file as (relative path, text), for the gates to read.

    A member that is not valid UTF-8 is decoded with replacement and scanned
    anyway, rather than refused. Two reasons, and the second is the important
    one: a skill legitimately carries a diagram or a screenshot, and a gate that
    declined to look at a file because of one stray byte would be a gate an
    attacker could switch off by appending one.

    The bundle's hashes are computed from the BYTES (:func:`file_digest`), so
    the lossy decode here never reaches what is pinned or what is shipped.
    """
    texts: list[tuple[str, str]] = []
    for path in sorted(staging.rglob("*")):
        if path.is_symlink():
            raise ExportError(
                f"{path.relative_to(staging).as_posix()} is a symlink. A bundle carries "
                "files, never links into the exporting instance."
            )
        if not path.is_file():
            continue
        relative = path.relative_to(staging).as_posix()
        try:
            texts.append((relative, path.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            raise ExportError(f"{relative} could not be read ({type(exc).__name__}).") from exc
    return texts


def _run_gates(texts: list[tuple[str, str]], *, workspace: str) -> None:
    secrets: list[Finding] = []
    leaks: list[Finding] = []
    for relative, text in texts:
        secrets.extend(scan_secret_literals(text, relative))
        leaks.extend(scan_instance_leaks(text, relative, workspace=workspace))
    if secrets:
        raise ExportError(
            "Refusing to export: a credential literal is in the bundle. Replace it with "
            "a ${NAME} reference and list the name under requires.secrets. Nothing was "
            "written.",
            tuple(secrets),
        )
    if leaks:
        raise ExportError(
            "Refusing to export: a path that only exists on this instance is in the "
            "bundle. Use a workspace-relative path. Nothing was written.",
            tuple(leaks),
        )


def _prepare_destination(out: Path, *, is_archive: bool) -> None:
    if is_archive:
        if out.exists():
            raise ExportError(f"{out.name} already exists; refusing to overwrite an export.")
        out.parent.mkdir(parents=True, exist_ok=True)
        return
    if out.exists():
        if not out.is_dir():
            raise ExportError(f"{out} is not a directory.")
        if any(out.iterdir()):
            raise ExportError(
                f"{out} is not empty. An export writes a whole bundle; pick an empty or "
                "new directory so nothing already there is mistaken for part of it."
            )


def _write_archive(staging: Path, out: Path, root_name: str) -> None:
    """A tarball whose bytes depend only on its contents.

    Every field a tar member carries that is NOT its path or its data is fixed:
    mtime, mode, uid/gid and the owner names. The gzip header's own timestamp is
    zeroed too — it is outside the tar entirely, and leaving it is the usual
    reason two "identical" archives differ. Members are added in sorted path
    order, and directory entries are not written at all (the extractor creates
    parents), so there is nothing left for the filesystem's iteration order to
    influence.
    """
    members = sorted(
        (path for path in staging.rglob("*") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.relative_to(staging).as_posix(),
    )
    temporary = out.with_name(out.name + ".partial")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for path in members:
                        info = tarfile.TarInfo(
                            f"{root_name}/{path.relative_to(staging).as_posix()}"
                        )
                        info.size = path.stat().st_size
                        info.mtime = 0
                        info.mode = 0o644
                        info.type = tarfile.REGTYPE
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        with path.open("rb") as handle:
                            tar.addfile(info, handle)
        temporary.replace(out)
    finally:
        if temporary.exists():
            temporary.unlink()


def _resolved_exported_at(exported_at: str | None) -> str:
    """When the export happened, pinnable for a reproducible build.

    A PARAMETER rather than an environment convention. ``exported_at`` is the
    only field in a bundle that changes between two exports of an unchanged
    agent, so a publisher who wants byte-identical output has to be able to pin
    it — and the CLI exposes it as ``--exported-at`` rather than as an
    undeclared variable the settings model does not know about.
    """
    if exported_at:
        return exported_at
    return datetime.now(UTC).isoformat(timespec="seconds")


def export_agent(
    agent_id: str,
    *,
    out: str | Path | None = None,
    include_adapters: bool = False,
    repo_root: Path | None = None,
    adapter_dir: str | Path | None = None,
    exported_at: str | None = None,
) -> ExportResult:
    """Export the installed agent *agent_id* as a bundle directory or tarball.

    Raises :class:`FileNotFoundError` if the agent is not installed and
    :class:`ExportError` for every refusal, each of which happens before
    anything is written.
    """
    if repo_root is None:
        repo_root = default_workspace_root()
    repo_root = Path(repo_root).resolve(strict=True)
    agent_id = validate_identifier(agent_id, label="agent ID")

    manifest_path = workspace_path(
        repo_root,
        f"docs/agents/{agent_id}.yaml",
        allowed_prefix="docs/agents",
        label="agent manifest",
    )
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Agent {agent_id!r} is not installed on this instance.")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict):
        raise ExportError(f"The manifest for {agent_id} is not a YAML mapping.")

    version = str(manifest.get("version") or "0.0.0")
    declared = _declared_requires(manifest)

    out_path = Path(out) if out is not None else Path.cwd() / archive_name(agent_id, version)
    is_archive = out_path.name.endswith(ARCHIVE_SUFFIXES)
    _prepare_destination(out_path, is_archive=is_archive)

    staging_root = Path(tempfile.mkdtemp(prefix="genus-export-")).resolve()
    staging = staging_root / agent_id
    staging.mkdir()
    try:
        import_agent(agent_id, output_dir=staging, repo_root=repo_root, record=False)

        for skill in declared["skills"]:
            _copy_skill(repo_root, staging, validate_identifier(skill, label="skill name"))

        adapters = _adapters_for_agent(
            Path(adapter_dir) if adapter_dir is not None else _default_adapter_dir(),
            agent_id,
        )
        adapter_names = tuple(sorted(set(adapters) | set(declared["adapters"])))
        adapter_secrets: set[str] = set()
        if include_adapters and adapters:
            directory = contained_path(staging, "adapters", label="staged adapter path")
            directory.mkdir(parents=True, exist_ok=True)
            for name in sorted(adapters):
                collapsed, needed = _collapse_adapter(name, adapters[name])
                adapter_secrets |= needed
                target = contained_path(
                    staging, f"adapters/{name}.yaml", label="staged adapter path"
                )
                target.write_text(yaml.dump(collapsed, sort_keys=False, default_flow_style=False))

        texts = _staged_texts(staging)
        _run_gates(texts, workspace=str(repo_root))

        secrets = (
            set(declared["secrets"])
            | adapter_secrets
            | _env_references([text for _, text in texts])
        )
        requires = Requires(
            plugins=tuple(sorted(set(declared["plugins"]))),
            adapters=adapter_names,
            secrets=tuple(sorted(secrets)),
            skills=tuple(sorted(set(declared["skills"]))),
        )
        bundle_manifest = BundleManifest(
            id=agent_id,
            name=str(manifest.get("name") or agent_id),
            version=version,
            exported_at=_resolved_exported_at(exported_at),
            platform_version=_platform_version(),
            requires=requires,
            files=files_for(staging, [relative for relative, _ in texts]),
        )
        (staging / BUNDLE_FILENAME).write_text(bundle_document(bundle_manifest))

        if is_archive:
            _write_archive(staging, out_path, agent_id)
        else:
            out_path.mkdir(parents=True, exist_ok=True)
            for item in sorted(staging.iterdir()):
                target = out_path / item.name
                if item.is_dir():
                    shutil.copytree(item, target, symlinks=False)
                else:
                    shutil.copy2(item, target)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return ExportResult(
        agent_id=agent_id,
        version=version,
        manifest=bundle_manifest,
        path=out_path,
        is_archive=is_archive,
    )


def _default_adapter_dir() -> Path | None:
    from robothor.engine.adapters import ADAPTER_DIR

    return ADAPTER_DIR if ADAPTER_DIR.is_dir() else None


def _platform_version() -> str:
    from robothor import __version__

    return str(__version__)

"""The agent fleet's files — manifests and instruction files — in the engine.

The Helm's agent builder used to do this in the bridge, against
``EngineConfig.from_env().workspace`` resolved in the BRIDGE process. That is
only the engine's workspace where every service happens to mount the same
volume. Where only the engine mounts it (several bridge replicas, a
ReadWriteOnce volume), the bridge saw an empty directory: the fleet list was
empty, the chat switcher offered nothing, and a write landed somewhere the
engine never reads. So the store lives here, in the process that owns the
workspace, and ``admin_manifests.py`` serves it under ``/api/admin``; the bridge
proxies and keeps the parts that are about the OPERATOR (the session gate, the
audit trail).

Four properties this module is built around:

* **The operator's own keys survive an edit.** A manifest is hand-written by
  people; the form knows about twenty paths out of a schema with hundreds. An
  edit sets only the paths it was asked about (:data:`FORM_OWNED_PATHS`) on the
  document as parsed, and never ``dict.update``\\ s a block — a hand-written
  ``model.temperature`` is not collateral for renaming an agent.
* **A refusal names the manifest, not the box.** Every error and log line
  carries ids, dotted schema paths and error codes. Never a filesystem path,
  never a manifest value (CLAUDE.md rules 1 and 2).
* **A validation failure leaves nothing behind.** The temp file goes in the
  destination directory and is unlinked on any exception, including
  ``KeyboardInterrupt`` — a torn manifest is not a lost setting, it is an
  unparseable layer that stops an agent from loading at all.
* **An edit is recoverable.** Every write snapshots the previous document into
  a per-agent ring under ``.history/``, and retiring moves the manifest to
  ``retired/`` rather than unlinking it. ``load_manifest_dir`` globs one level,
  so neither directory is visible to the loader.

Everything here is synchronous file work; the routes hand it to
``asyncio.to_thread`` (rule 10).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from robothor.engine.sanitize import sanitize_log
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    trusted_directory,
    validate_identifier,
    workspace_path,
)

logger = logging.getLogger(__name__)


class ManifestError(Exception):
    """A refusal the caller turns into an HTTP answer: a status and a detail.

    Framework-free so the store can be exercised without a request, and so the
    one place that decides what a refusal LOOKS like on the wire is the routes
    module. ``detail`` is a sentence or, for a validation refusal, the verdict.
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


#: The only paths a simple-form edit may write. Everything else in the
#: document survives untouched, including keys this code has never heard of.
#:
#: An allowlist and not a denylist: the schema has hundreds of fields, several
#: of them load-bearing for safety (``v2.sandbox``, ``v2.exec_allowlist``,
#: ``tools_denied`` semantics), and "the form posted it so write it" is how a
#: browser ends up able to hand an agent a shell.
FORM_OWNED_PATHS: tuple[str, ...] = (
    "name",
    "description",
    "department",
    "model.primary",
    "model.fallbacks",
    "schedule.cron",
    "schedule.timezone",
    "schedule.enabled",
    "schedule.max_iterations",
    "schedule.session_target",
    "schedule.catch_up",
    "delivery.mode",
    "delivery.channel",
    "delivery.to",
    "tools_allowed",
    "tools_denied",
    "instruction_file",
    "version",
    "changelog",
)

#: Paths whose value REPLACES rather than merges. ``resolver.deep_merge``
#: unions lists, which is right for inheritance and wrong for a form: an
#: operator who removes a fallback model or a tool expects it gone, and a
#: union would silently keep it.
_REPLACED_LIST_PATHS = frozenset({"model.fallbacks", "tools_allowed", "tools_denied", "changelog"})

#: How many previous versions of a manifest are kept per agent.
HISTORY_DEPTH = 5

#: Sub-directories of the manifest dir that the engine's loader cannot see:
#: ``load_manifest_dir`` globs ``*.yaml`` one level deep, non-recursively.
HISTORY_DIR = ".history"
RETIRED_DIR = "retired"

#: The scaffold templates ``genus agent scaffold`` renders, so a Helm-created
#: agent and a CLI-created one are the same kind of object.
_INSTRUCTION_TEMPLATE = "agent-instructions.md"
_MANIFEST_TEMPLATE = "agent-manifest.yaml"


# ─── Paths ───────────────────────────────────────────────────────────


def workspace() -> Path:
    """The workspace this engine resolves — the one its loader reads."""
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().workspace


def manifest_dir() -> Path:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().manifest_dir


def main_agent_id() -> str:
    """The agent whose conversation a chat request reaches by sending NO key.

    Read out of ``EngineConfig.main_session_key``, not ``default_chat_agent``.
    They are two independent environment variables and they are free to
    disagree, and only one of them answers the question the browser is asking.
    ``chat.py::_effective_session_key`` substitutes ``main_session_key`` for an
    empty key and then derives the agent as ``parts[1]``; this derives it the
    same way, so the listing cannot name one agent while the engine runs
    another. ``default_chat_agent`` says which agent an interactive turn is
    *dispatched* to — a different question, and the wrong answer here.

    A key that is not shaped ``agent:<id>:…`` leaves nothing to derive, and the
    honest fallback is the other configured value rather than a guess.

    Resolved ONCE per listing by the caller: this builder reads the environment
    and touches the filesystem, and a fleet of twenty is twenty of those.
    """
    from robothor.engine.config import EngineConfig

    config = EngineConfig.from_env()
    parts = config.main_session_key.split(":")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return config.default_chat_agent


#: A full disk or a read-only volume is the operator's to fix, and it should
#: read as one. The error's own text carries the path, so it never comes out.
_UNWRITABLE = "the agent workspace is not writable"


def _unwritable(error: OSError) -> ManifestError:
    logger.warning("Agent workspace write failed: %s", type(error).__name__)
    return ManifestError(422, _UNWRITABLE)


def _refused(error: TemplateSecurityError) -> ManifestError:
    """A containment refusal, as a 422 the operator can read.

    A symlinked ``brain/`` (or ``docs/agents/``) is a deployment the appliance
    will not write through, and saying so plainly is the difference between
    "fix your symlink" and an opaque 500. The message comes from ``safety.py``
    and names the rule, never the path.
    """
    return ManifestError(422, str(error))


def manifest_root() -> Path:
    """The manifest directory, created and canonicalized.

    Wrapped like every other path helper here: ``trusted_directory`` refuses a
    root reached through a symlink, and that refusal is an operator's
    misconfiguration rather than a bug in this process.
    """
    directory = manifest_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return trusted_directory(directory, label="agent manifest root")
    except TemplateSecurityError as error:
        raise _refused(error) from error
    except OSError as error:
        logger.warning("Manifest directory unusable: %s", type(error).__name__)
        raise ManifestError(422, _UNWRITABLE) from error


def safe_id(agent_id: object) -> str:
    """A kebab-case agent id, or a 422 naming the rule it broke."""
    try:
        return validate_identifier(agent_id, label="agent id")
    except TemplateSecurityError as error:
        raise _refused(error) from error


def manifest_path(agent_id: str) -> Path:
    try:
        return contained_path(manifest_root(), f"{agent_id}.yaml", label="agent manifest")
    except TemplateSecurityError as error:
        raise _refused(error) from error


def require_manifest(agent_id: object) -> tuple[str, Path]:
    """``(id, path)`` for an agent that exists here, else a 422 or a 404."""
    agent_id = safe_id(agent_id)
    path = manifest_path(agent_id)
    if not path.is_file():
        raise ManifestError(404, "no such agent")
    return agent_id, path


def _instruction_filename(agent_id: str) -> str:
    return f"{agent_id.upper().replace('-', '_')}.md"


def instruction_path(relative: object) -> Path:
    """Resolve ``brain/<FILE>.md`` inside the workspace, or refuse.

    ``allowed_prefix="brain"`` is what the template installer uses, so a
    Helm-created agent lands exactly where ``genus agent install`` puts one.
    A ``brain/`` that is a symlink outside the workspace is refused here
    rather than followed.
    """
    try:
        return workspace_path(
            workspace(),
            relative,
            allowed_prefix="brain",
            label="agent instruction file",
        )
    except TemplateSecurityError as error:
        raise _refused(error) from error


# ─── Atomic writes + history ─────────────────────────────────────────


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
    is the only place it can actually be atomic.
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
    finally:
        invalidate_listing()


def dump(document: dict[str, Any]) -> str:
    """The YAML the engine will parse back.

    PyYAML and not a round-tripping library: the engine reads this file with
    PyYAML, and a second YAML implementation on the write side would be a
    second opinion about what the file means. Comments do not survive an edit —
    stated here because it is a real cost, and the same one ``providers.py``
    accepted for ``_defaults.yaml``.
    """
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


def snapshot(agent_id: str) -> None:
    """Copy the current manifest into the agent's history ring.

    Best-effort by design: losing a snapshot is a smaller harm than refusing an
    edit the operator asked for, and the ring is a convenience over git, not
    the backup story.
    """
    source = manifest_path(agent_id)
    if not source.is_file():
        return
    # Microseconds, not whole seconds: two edits inside one second are a normal
    # thing for a form (toggle disabled, fix the cron), and a second-resolution
    # name silently overwrote the earlier snapshot — a ring of five that held
    # one entry. Every name shares this shape, so a plain sort is chronological.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    try:
        target = contained_path(
            manifest_root(),
            f"{HISTORY_DIR}/{agent_id}/{stamp}.yaml",
            label="manifest history entry",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        for stale in sorted(target.parent.glob("*.yaml"))[:-HISTORY_DEPTH]:
            stale.unlink(missing_ok=True)
    except Exception as error:  # noqa: BLE001 — a snapshot must not block an edit
        # The TYPE only: an OSError's str() is "[Errno 13] Permission denied:
        # '/home/<user>/robothor/docs/agents/...'" (CLAUDE.md rule 2).
        logger.warning(
            "Could not snapshot manifest for %s: %s",
            sanitize_log(agent_id),
            type(error).__name__,
        )


def retire(agent_id: str, path: Path) -> None:
    """Move the manifest to ``retired/``. Nothing is unlinked."""
    root = manifest_root()
    try:
        (root / RETIRED_DIR).mkdir(parents=True, exist_ok=True)
        target = contained_path(root, f"{RETIRED_DIR}/{agent_id}.yaml", label="retired manifest")
        path.replace(target)
    except TemplateSecurityError as error:
        raise _refused(error) from error
    except OSError as error:
        raise _unwritable(error) from error
    finally:
        invalidate_listing()


def replace_manifest(path: Path, document: dict[str, Any]) -> None:
    """Rewrite an existing manifest atomically, or refuse with a 422."""
    try:
        write_atomically(path, dump(document))
    except OSError as error:
        raise _unwritable(error) from error


# ─── Reading ─────────────────────────────────────────────────────────


def scan() -> Any:
    """Every manifest the engine's own loader sees, and every one it refused.

    Always fresh. Validation before a write reads through this: a verdict about
    a write must not rest on a cached view of the fleet.
    """
    from robothor.engine.config import load_manifest_dir

    return load_manifest_dir(manifest_dir())


# ─── The listing's cached scan ───────────────────────────────────────
#
# The dashboard asks for the fleet listing each time a message goes to a named
# agent, and every ask re-read and YAML-parsed every manifest. ``listing()``
# keeps the last scan and the signature it was read under; a hit costs one
# ``stat`` per file and no reads. Two ways it is dropped: the signature moves
# (anything that changed on disk, whoever changed it), and every write path in
# this process calls ``invalidate_listing()`` so its own writes never depend on
# the filesystem's clock.

_listing_lock = threading.Lock()
_listing_cache: dict[str, Any] = {"signature": None, "scan": None}
#: Bumped by every invalidation. A scan stores its result only if no
#: invalidation ran while it was reading, so a write that lands mid-scan cannot
#: leave a pre-write view behind under a signature that still matches.
_listing_generation = 0


def listing_signature(directory: Path) -> tuple[Any, ...] | None:
    """What has to change for the cached scan to be stale.

    The directory's own mtime (an add, a remove, a rename), and each
    ``*.yaml`` file's name, mtime and size (an edit in place, which does not
    move the directory's mtime); ``_defaults.yaml`` matches the glob. The
    schema mode rides along (under ``enforce`` the same files scan
    differently), and so does a fingerprint of the environment the loader
    expands ``${VAR}`` from. ``None`` -- never cached -- when the directory
    cannot be stat'ed, so a missing directory is reported by the loader.
    """
    from robothor.engine import manifest_schema

    try:
        directory_mtime = directory.stat().st_mtime_ns
        files = []
        for path in directory.glob("*.yaml"):
            stat = path.stat()
            files.append((path.name, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return None
    files.sort()
    return (
        str(directory),
        directory_mtime,
        tuple(files),
        manifest_schema.schema_mode(),
        hash(frozenset(os.environ.items())),
    )


def listing() -> Any:
    """``scan()``, unless nothing it would read has changed since the last one.

    For the fleet LISTING only; anything that decides about a write calls
    :func:`scan`.
    """
    directory = manifest_dir()
    with _listing_lock:
        generation = _listing_generation
    signature = listing_signature(directory)
    with _listing_lock:
        if signature is not None and _listing_cache["signature"] == signature:
            return _listing_cache["scan"]
    result = scan()
    with _listing_lock:
        if generation == _listing_generation:
            _listing_cache["signature"] = signature
            _listing_cache["scan"] = result
    return result


def invalidate_listing() -> None:
    """Drop the cached listing. Every write path in the engine calls this."""
    global _listing_generation
    with _listing_lock:
        _listing_generation += 1
        _listing_cache["signature"] = None
        _listing_cache["scan"] = None


def parse(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ManifestError(
            422, f"the manifest is not valid YAML ({type(error).__name__})"
        ) from error
    if not isinstance(document, dict):
        raise ManifestError(422, "the manifest must be a YAML mapping")
    return document


def read_instructions(document: dict[str, Any]) -> str:
    """The agent's instruction file, or an empty string if it has none."""
    relative = document.get("instruction_file")
    if not relative:
        return ""
    try:
        path = instruction_path(relative)
    except ManifestError:
        # A READ of the fleet must not 422 because one agent names an
        # instruction file outside the workspace. The refusal is right; making
        # it fatal to the page is not.
        logger.warning("Refusing to read an instruction file outside the workspace")
        return ""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# ─── Deep set of only the form-owned paths ───────────────────────────


def _set_path(document: dict[str, Any], dotted: str, value: Any) -> None:
    """Set one dotted path, creating intermediate mappings as needed."""
    *parents, leaf = dotted.split(".")
    cursor = document
    for part in parents:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[leaf] = value


def merge_owned(document: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply the form's fields to the document as parsed, and nothing else.

    Only :data:`FORM_OWNED_PATHS` are writable; an omitted path is untouched,
    which is the difference between "the operator cleared this field" and "the
    form did not know about it". Lists in :data:`_REPLACED_LIST_PATHS` replace
    rather than union, because ``resolver.deep_merge`` unions and an operator
    removing a fallback model means it should be gone.
    """
    for dotted in FORM_OWNED_PATHS:
        if dotted not in changes:
            continue
        value = changes[dotted]
        if dotted in _REPLACED_LIST_PATHS and value is not None and not isinstance(value, list):
            raise ManifestError(422, f"{dotted} must be a list")
        _set_path(document, dotted, value)
    return document


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def bump_version(document: dict[str, Any], change: str) -> None:
    """Stamp today's date and append one changelog entry.

    The manifest's own record of who changed what, kept alongside the audit
    event rather than instead of it: the audit log is the appliance's trail and
    the changelog is the file's, and only one of them travels with the manifest
    when it is copied to another instance.
    """
    document["version"] = _today()
    entries = document.get("changelog")
    if not isinstance(entries, list):
        entries = []
    entries.append({"date": _today(), "change": change})
    document["changelog"] = entries


# ─── Request bodies ──────────────────────────────────────────────────
#
# One definition, imported by the bridge for its own request validation: the
# body the browser posts and the body the engine acts on are the same object.


class CreateRequest(BaseModel):
    """A new agent, as the builder form posts it.

    ``id`` is optional. Supplied, it is taken literally and validated strictly,
    so a caller cannot smuggle a path through it. Omitted, it is derived from
    the display name — and slugifying is only ever applied to a NAME, never to
    an id somebody typed: quietly turning ``../x`` into ``x`` would mean a
    request to escape the manifest directory got a 201.
    """

    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    instructions: str = ""
    department: str | None = None
    model: str | None = None
    fallbacks: list[str] | None = None
    cron: str | None = None
    timezone: str | None = None
    delivery_mode: str | None = None
    delivery_channel: str | None = None
    delivery_to: str | None = None
    tools_allowed: list[str] | None = None

    def owned_paths(self) -> dict[str, Any]:
        """The subset of :data:`FORM_OWNED_PATHS` this body actually sets."""
        candidates = {
            "name": self.name.strip(),
            "description": self.description.strip() or None,
            "department": self.department,
            "schedule.cron": self.cron,
            "schedule.timezone": self.timezone,
            "delivery.mode": self.delivery_mode,
            "delivery.channel": self.delivery_channel,
            "delivery.to": self.delivery_to,
            "tools_allowed": self.tools_allowed,
        }
        return {path: value for path, value in candidates.items() if value is not None}


class PatchRequest(BaseModel):
    """An edit. Every field optional: absent means "leave it alone"."""

    name: str | None = None
    description: str | None = None
    department: str | None = None
    model: str | None = None
    fallbacks: list[str] | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    max_iterations: int | None = None
    session_target: str | None = None
    catch_up: str | None = None
    delivery_mode: str | None = None
    delivery_channel: str | None = None
    delivery_to: str | None = None
    tools_allowed: list[str] | None = None
    tools_denied: list[str] | None = None
    change: str = "Edited via the Helm agent builder"

    def owned_paths(self) -> dict[str, Any]:
        candidates = {
            "name": self.name,
            "description": self.description,
            "department": self.department,
            "model.primary": self.model,
            "model.fallbacks": self.fallbacks,
            "schedule.cron": self.cron,
            "schedule.timezone": self.timezone,
            "schedule.enabled": self.enabled,
            "schedule.max_iterations": self.max_iterations,
            "schedule.session_target": self.session_target,
            "schedule.catch_up": self.catch_up,
            "delivery.mode": self.delivery_mode,
            "delivery.channel": self.delivery_channel,
            "delivery.to": self.delivery_to,
            "tools_allowed": self.tools_allowed,
            "tools_denied": self.tools_denied,
        }
        return {path: value for path, value in candidates.items() if value is not None}


class ValidateRequest(BaseModel):
    """Either a parsed document or the raw YAML from the editor pane."""

    manifest: dict[str, Any] | None = None
    yaml: str | None = None


class DeleteRequest(BaseModel):
    confirm: str = ""


# ─── Rendering a new agent ───────────────────────────────────────────


def _template_dir() -> Path:
    from robothor.setup import _find_template_dir

    directory = _find_template_dir()
    if directory is None:
        raise ManifestError(500, "the agent scaffold templates are not installed")
    return directory


def _render(template: Path, replacements: dict[str, str]) -> str:
    """Single-brace placeholder substitution, as the CLI scaffold does it.

    Not resolver syntax: these templates predate it and use ``{AGENT_NAME}``,
    so the plain replace loop is what keeps ``genus agent scaffold`` and this
    route producing the same file.
    """
    text = template.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text


def kebab(name: str) -> str:
    """A display name reduced to the id shape the schema demands."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _fleet_default_model() -> str:
    from robothor.engine import config as engine_config

    block = engine_config._load_defaults(manifest_dir()).get("model") or {}
    return str(block.get("primary") or "")


def scaffold(agent_id: str, body: CreateRequest) -> tuple[dict[str, Any], str, str]:
    """``(manifest, instruction_relpath, instruction_text)`` for a new agent."""
    name = body.name.strip() or agent_id
    description = body.description.strip() or f"A new agent: {agent_id}"
    instruction_filename = _instruction_filename(agent_id)
    status_file = f"brain/memory/{agent_id}-status.md"
    common = {
        "{AGENT_ID}": agent_id,
        "{VERSION}": _today(),
        "{INSTRUCTION_FILENAME}": instruction_filename,
        "{STATUS_FILE}": status_file,
    }
    templates = _template_dir()

    # The template is YAML with bare, unquoted placeholders (`name: {AGENT_NAME}`),
    # so substituting operator text into it and then parsing hands a browser a
    # YAML injection: a name containing ": " or a leading "&" either fails the
    # parse or silently becomes a different document. Render with the id — which
    # `validate_identifier` has already constrained to [a-z0-9-] — and put the
    # real strings on the PARSED document, where PyYAML quotes them on the way
    # back out. The instruction file is Markdown and takes the real values.
    document = parse(_render(templates / _MANIFEST_TEMPLATE, {**common, "{AGENT_NAME}": agent_id}))
    replacements = {**common, "{AGENT_NAME}": name, "{DESCRIPTION}": description}

    # The scaffold template hardcodes a model id and a fallback. Writing either
    # would point a brand-new agent at a provider this instance may not even
    # hold a key for, so the block is rebuilt from scratch: the id the operator
    # chose, else the one the fleet already runs on, else nothing at all — and
    # nothing means the agent inherits from _defaults.yaml, which is what the
    # templates intend and what `check_structure` accepts.
    model: dict[str, Any] = {}
    primary = (body.model or "").strip() or _fleet_default_model()
    if primary:
        model["primary"] = primary
    if body.fallbacks:
        model["fallbacks"] = list(body.fallbacks)
    if model:
        document["model"] = model
    else:
        document.pop("model", None)

    instructions = _render(templates / _INSTRUCTION_TEMPLATE, replacements)
    if body.instructions.strip():
        instructions = _with_operator_role(instructions, body.instructions.strip())
    merge_owned(document, body.owned_paths())
    # Set last and unconditionally: these three decide which agent this IS and
    # where it reads its instructions from, so they are not up to the form.
    document["id"] = agent_id
    document["name"] = name
    document["description"] = description
    document["instruction_file"] = f"brain/{instruction_filename}"
    return document, f"brain/{instruction_filename}", instructions


def _with_operator_role(rendered: str, instructions: str) -> str:
    """Put the operator's own words under ``## Your Role``.

    ``docs/agents/INSTRUCTION_CONTRACT.md`` requires the four headings
    (``# <Name>``, ``## Your Role``, ``## Tasks``, ``## Output``), and an agent
    whose instructions are only free text loses every one of them. Substituting
    inside the rendered template keeps the contract satisfied no matter what the
    operator typed — including nothing.
    """
    marker = "\n## Tasks\n"
    head, _, tail = rendered.partition(marker)
    if not tail:
        return rendered.rstrip() + "\n\n" + instructions + "\n"
    role_heading = "\n## Your Role\n"
    before, _, _role = head.partition(role_heading)
    return f"{before}{role_heading}\n{instructions}\n{marker}{tail}"


def write_pair(
    agent_id: str, document: dict[str, Any], instruction_relative: str, instructions: str
) -> None:
    """Manifest and instructions, each replaced atomically.

    Not one transaction: two files on a POSIX filesystem cannot be. The order
    is deliberate — instructions first, manifest second — so the window between
    them is "an instruction file nothing references" rather than "an agent
    whose instructions do not exist", which is the half that makes a run fail.
    """
    instruction_file = instruction_path(instruction_relative)
    try:
        if not instruction_file.exists():
            write_atomically(instruction_file, instructions)
        write_atomically(manifest_path(agent_id), dump(document), exclusive=True)
    except FileExistsError as error:
        # The 409 before the scaffold is a courtesy; this is the decision. Two
        # concurrent creates of the same id both pass `.exists()`, and without
        # O_EXCL the second would silently destroy the first.
        raise ManifestError(409, "an agent with that id already exists") from error
    except OSError as error:
        raise _unwritable(error) from error

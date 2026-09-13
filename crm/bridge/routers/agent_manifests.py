"""Agent manifests from the browser — create, edit, retire, validate, run.

Until this router, building an agent meant an ssh session, a text editor, and
``sudo systemctl restart robothor-engine``. That is the single largest step
between "the appliance is installed" and "the appliance does something", which
is why the Helm shipped with a fleet view that could only read.

The split with the engine is the same one ``providers.py`` follows and for the
same reason: the bridge owns the manifest WRITE, because writing an agent is an
act an operator performs and the audit trail has to name them; the engine owns
the job registry, so the write only takes effect once ``POST
/api/admin/scheduler/reconcile`` has been called. Both halves are reported. A
save that reports success and changes nothing is the failure mode this whole
surface exists to remove, so "saved" and "in effect" are two different fields.

Four properties this file is built around:

* **The operator's own keys survive an edit.** A manifest is hand-written by
  people; the form knows about twenty paths out of a schema with hundreds. A
  PATCH sets only the paths it was asked about (:data:`FORM_OWNED_PATHS`) on the
  document as parsed, and never ``dict.update``\\ s a block — a hand-written
  ``model.temperature`` is not collateral for renaming an agent.
* **A refusal names the manifest, not the box.** Every response, audit detail
  and log line carries ids, dotted schema paths and error codes. Never a
  filesystem path, never a manifest value (CLAUDE.md rules 1 and 2).
* **A validation failure leaves nothing behind.** The temp file goes in the
  destination directory and is unlinked on any exception, including
  ``KeyboardInterrupt`` — a torn manifest is not a lost setting, it is an
  unparseable layer that stops an agent from loading at all.
* **An edit is recoverable.** Every write snapshots the previous document into
  a per-agent ring under ``.history/``, and DELETE moves the manifest to
  ``retired/`` rather than unlinking it. ``load_manifest_dir`` globs one level,
  so neither directory is visible to the engine.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias
from urllib.parse import quote

import yaml
from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from robothor.engine.sanitize import sanitize_log
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    trusted_directory,
    validate_identifier,
    workspace_path,
)
from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_operator

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: What every validator here answers: ``(blocking errors, advisory warnings)``.
_IssueSplit: TypeAlias = tuple[list[dict[str, str]], list[dict[str, str]]]


def _require_primary_tenant(tenant_id: str = Depends(get_tenant_id)) -> None:
    """The agent fleet is appliance-global, so deny secondary tenants.

    Symmetric with ``installed_agents``: one engine owns one manifest
    directory, and a second tenant's operator editing it would be editing
    somebody else's fleet.
    """
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        raise HTTPException(
            status_code=403,
            detail="appliance administration not authorized for tenant",
        )


router = APIRouter(
    tags=["agent-manifests"],
    dependencies=[Depends(_require_primary_tenant)],
)

#: The only paths a simple-form PATCH may write. Everything else in the
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


def _workspace() -> Path:
    """The workspace the ENGINE resolves, not the bridge's own view of it.

    Same authority ``providers.py`` reads for the identical reason: writing a
    manifest anywhere the engine does not look produces a save that reports
    success, survives a reload, and changes nothing about the fleet.
    """
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().workspace


def _manifest_dir() -> Path:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().manifest_dir


def _refused(error: TemplateSecurityError) -> HTTPException:
    """A containment refusal, as a 422 the operator can read.

    Brief decision 4: a symlinked ``brain/`` (or ``docs/agents/``) is a
    deployment the appliance will not write through, and saying so plainly is
    the difference between "fix your symlink" and an opaque 500. The message
    comes from ``safety.py`` and names the rule, never the path.
    """
    return HTTPException(status_code=422, detail=str(error))


def _manifest_root() -> Path:
    """The manifest directory, created and canonicalized.

    Wrapped like every other path helper here: ``trusted_directory`` refuses a
    root reached through a symlink, and that refusal is an operator's
    misconfiguration rather than a bug in this process.
    """
    directory = _manifest_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        return trusted_directory(directory, label="agent manifest root")
    except TemplateSecurityError as error:
        raise _refused(error) from error
    except OSError as error:
        logger.warning("Manifest directory unusable: %s", type(error).__name__)
        raise HTTPException(
            status_code=422, detail="the agent manifest directory is not writable"
        ) from error


def _safe_id(agent_id: object) -> str:
    """A kebab-case agent id, or a 422 naming the rule it broke."""
    try:
        return validate_identifier(agent_id, label="agent id")
    except TemplateSecurityError as error:
        raise _refused(error) from error


def _manifest_path(agent_id: str) -> Path:
    try:
        return contained_path(_manifest_root(), f"{agent_id}.yaml", label="agent manifest")
    except TemplateSecurityError as error:
        raise _refused(error) from error


def _instruction_filename(agent_id: str) -> str:
    return f"{agent_id.upper().replace('-', '_')}.md"


def _instruction_path(relative: object) -> Path:
    """Resolve ``brain/<FILE>.md`` inside the workspace, or refuse.

    ``allowed_prefix="brain"`` is what the template installer uses, so a
    Helm-created agent lands exactly where ``genus agent install`` puts one.
    A ``brain/`` that is a symlink outside the workspace is refused here
    rather than followed.
    """
    try:
        return workspace_path(
            _workspace(),
            relative,
            allowed_prefix="brain",
            label="agent instruction file",
        )
    except TemplateSecurityError as error:
        raise _refused(error) from error


# ─── Atomic writes + history ─────────────────────────────────────────


def _write_atomically(path: Path, text: str, *, exclusive: bool = False) -> None:
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


def _dump(document: dict[str, Any]) -> str:
    """The YAML the engine will parse back.

    PyYAML and not a round-tripping library: the engine reads this file with
    PyYAML, and a second YAML implementation on the write side would be a
    second opinion about what the file means. Comments do not survive an edit —
    stated here because it is a real cost, and the same one ``providers.py``
    accepted for ``_defaults.yaml``.
    """
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


def _snapshot(agent_id: str) -> None:
    """Copy the current manifest into the agent's history ring.

    Best-effort by design: losing a snapshot is a smaller harm than refusing an
    edit the operator asked for, and the ring is a convenience over git, not
    the backup story.
    """
    source = _manifest_path(agent_id)
    if not source.is_file():
        return
    # Microseconds, not whole seconds: two edits inside one second are a normal
    # thing for a form (toggle disabled, fix the cron), and a second-resolution
    # name silently overwrote the earlier snapshot — a ring of five that held
    # one entry. Every name shares this shape, so a plain sort is chronological.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    try:
        target = contained_path(
            _manifest_root(),
            f"{HISTORY_DIR}/{agent_id}/{stamp}.yaml",
            label="manifest history entry",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        for stale in sorted(target.parent.glob("*.yaml"))[:-HISTORY_DEPTH]:
            stale.unlink(missing_ok=True)
    except Exception as error:  # noqa: BLE001 — a snapshot must not block an edit
        # The TYPE only: an OSError's str() is "[Errno 13] Permission denied:
        # '/home/<user>/robothor/docs/agents/...'". This was the one log line
        # in the router that could print a path (CLAUDE.md rule 2).
        logger.warning(
            "Could not snapshot manifest for %s: %s",
            sanitize_log(agent_id),
            type(error).__name__,
        )


# ─── Reading ─────────────────────────────────────────────────────────


def _summary(document: dict[str, Any]) -> dict[str, Any]:
    """The fields one fleet-list row shows.

    A whole manifest per row would put every agent's tool list, warmup files
    and delivery target into a single response the list view never reads.
    """
    schedule = document.get("schedule") or {}
    delivery = document.get("delivery") or {}
    model = document.get("model") or {}
    return {
        "id": str(document.get("id") or ""),
        "name": document.get("name") or "",
        "description": document.get("description") or "",
        "version": str(document.get("version") or ""),
        "department": document.get("department") or "",
        "cron": schedule.get("cron") or "",
        "timezone": schedule.get("timezone") or "",
        "enabled": bool(schedule.get("enabled", True)),
        "delivery": delivery.get("mode") or "none",
        "model": model.get("primary") or "",
    }


def _scan() -> Any:
    from robothor.engine.config import load_manifest_dir

    return load_manifest_dir(_manifest_dir())


def _broken(scan: Any) -> list[dict[str, str]]:
    """The failure bucket, as ids and error TYPES.

    ``ManifestFailure.detail`` is a parser message and routinely carries the
    absolute path of the file; it does not come out here.
    """
    return [
        {
            "id": failure.agent_id or Path(failure.filename).stem,
            "filename": failure.filename,
            "error_type": failure.error_type,
        }
        for failure in scan.failures
    ]


def _parse(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise HTTPException(
            status_code=422,
            detail=f"the manifest is not valid YAML ({type(error).__name__})",
        ) from error
    if not isinstance(document, dict):
        raise HTTPException(status_code=422, detail="the manifest must be a YAML mapping")
    return document


# ─── Validation ──────────────────────────────────────────────────────


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _not_validatable(where: str, error: BaseException) -> dict[str, str]:
    """A validator that raised, reported as a finding instead of a 500.

    The exception TYPE and nothing else: a ``KeyError`` or a ``TypeError`` from
    deep inside a check carries the offending manifest value in its text, and
    this body reaches a browser (CLAUDE.md rules 1 and 2).
    """
    return _issue(
        "",
        "not_validatable",
        f"{where} could not judge this manifest ({type(error).__name__}) — "
        "a value is the wrong shape",
    )


def _guarded(where: str, call: Callable[[], _IssueSplit]) -> _IssueSplit:
    """Run one validator, turning a raise into an error finding.

    ``manifest_schema.validate`` documents "Never raises" and
    ``manifest_checks.validate_agent`` implies it, and neither is true of an
    arbitrary document: ``sandbox`` as a mapping hits ``x not in {...}`` on an
    unhashable, ``schedule`` as a list hits ``.get()`` on a list, a non-string
    ``id`` hits ``re.match``. Every one of those is a manifest an operator needs
    the editor for MORE than a well-formed one, so a crash here does not just
    lose a verdict — it hides the file. ``_runtime_issues`` already wrapped its
    call for exactly this reason; this is the same guard at the other two.
    """
    try:
        return call()
    except Exception as error:  # noqa: BLE001 — every shape of bad value lands here
        logger.warning("%s raised on a manifest: %s", where, type(error).__name__)
        return [_not_validatable(where, error)], []


def _schema_issues(document: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``manifest_schema`` findings, split into blocking and advisory.

    Validated as the MERGED document — file plus ``_defaults.yaml`` — because
    that is what a run is judged on. Validating the fragment alone reports a
    missing model on every agent that correctly inherits one.
    """
    from robothor.engine import config as engine_config
    from robothor.engine import manifest_schema

    merged = engine_config._merged_manifest(
        document,
        agent_id=str(document.get("id") or ""),
        defaults=engine_config._load_defaults(_manifest_dir()),
        workspace=None,
        trigger_type=None,
    )
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for issue in manifest_schema.validate(merged, strict=False):
        bucket = errors if issue.severity == "error" else warnings
        bucket.append(_issue(issue.path, issue.code, issue.message))
    return errors, warnings


def _check_issues(
    document: dict[str, Any], fleet: dict[str, Any], tools: set[str]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The 13 A–M checks, run server-side with ``ci=True``.

    ``ci=True`` skips the two checks (C, J) that assert files exist through
    symlinks the appliance may not have; the 422 decision is the remaining
    eleven plus the schema. A ``WARN`` never blocks — it is advice, and a
    surface that refuses to save on advice is one an operator learns to work
    around.
    """
    from robothor.templates.manifest_checks import validate_agent

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for result in validate_agent(document, fleet, tools, repo_root=_workspace(), ci=True):
        if result.status not in {"FAIL", "WARN"}:
            continue
        detail = "; ".join([result.message, *result.details]).strip("; ")
        entry = _issue(f"check.{result.check_id}", f"check_{result.check_id.lower()}", detail)
        (errors if result.status == "FAIL" else warnings).append(entry)
    return errors, warnings


def _runtime_issues(document: dict[str, Any]) -> list[dict[str, str]]:
    """Can the engine actually turn this document into a scheduled agent?

    The two questions no static check answers: does ``manifest_to_agent_config``
    survive the document, and does APScheduler accept the cron in the timezone
    it is paired with. Both have been the difference between a manifest that
    validates and an agent that never fires.
    """
    from apscheduler.triggers.cron import CronTrigger

    from robothor.engine.config import manifest_to_agent_config

    issues: list[dict[str, str]] = []
    try:
        agent = manifest_to_agent_config(document)
    except Exception as error:  # noqa: BLE001 — every shape of bad value lands here
        return [_issue("", "not_loadable", f"the engine cannot load this manifest: {error}")]

    crons = [("schedule.cron", agent.cron_expr, agent.timezone)]
    if agent.heartbeat:
        crons.append(("heartbeat.cron", agent.heartbeat.cron_expr, agent.heartbeat.timezone))
    if agent.worker:
        crons.append(("worker.cron", agent.worker.cron_expr, agent.worker.timezone))
    for path, cron, timezone in crons:
        if not cron:
            continue
        try:
            CronTrigger.from_crontab(cron, timezone=timezone)
        except Exception as error:  # noqa: BLE001 — bad cron, bad tz, both
            issues.append(_issue(path, "bad_cron", f"the scheduler refuses this schedule: {error}"))
    return issues


async def _engine_tools() -> tuple[set[str], list[dict[str, str]]]:
    """The engine's registered tool names, and a warning if it would not say.

    An unreachable engine downgrades the tool check to a warning rather than
    failing the save: the operator would otherwise be unable to edit an agent
    while the engine is down, which is exactly when they most want to.
    """
    status, body = await engine_request("GET", "/api/admin/tools")
    if status >= 400 or not isinstance(body, dict):
        return set(), [
            _issue(
                "tools_allowed",
                "tools_unverified",
                "the engine did not answer, so tool names were not checked",
            )
        ]
    return {str(name) for name in body.get("tools") or []}, []


async def _validation_context() -> tuple[dict[str, Any], set[str], list[dict[str, str]]]:
    """The fleet and the engine's tool list, read once.

    Its own function so an edit can validate the BEFORE and AFTER documents
    against the same view of the world — two round trips to the engine for one
    save would let the two verdicts disagree about which tools exist.
    """
    scan = await asyncio.to_thread(_scan)
    fleet = {str(m["id"]): m for m in scan.manifests if isinstance(m.get("id"), str)}
    tools, tool_warnings = await _engine_tools()
    return fleet, tools, tool_warnings


async def _validate(
    document: dict[str, Any],
    context: tuple[dict[str, Any], set[str], list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """Everything that can be said about one manifest, in one body.

    Ordered cheapest-first so the operator sees the structural complaint before
    the semantic one: a document missing ``id`` produces a readable answer
    rather than a cascade from every check that dereferences it.
    """
    fleet, tools, tool_warnings = context if context is not None else await _validation_context()

    errors, warnings = await asyncio.to_thread(
        _guarded, "the schema validator", lambda: _schema_issues(document)
    )
    check_errors, check_warnings = await asyncio.to_thread(
        _guarded, "the manifest checks", lambda: _check_issues(document, fleet, tools)
    )
    errors.extend(check_errors)
    warnings.extend(check_warnings)
    warnings.extend(tool_warnings)
    errors.extend(_runtime_issues(document))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _refuse(validation: dict[str, Any]) -> None:
    """Turn a failed validation into a 422 carrying the validator's own words.

    The messages are the ones ``manifest_schema`` and ``manifest_checks``
    produce, verbatim, with their stable codes. A route that replaced them with
    "invalid manifest" would leave the operator guessing which of forty fields
    to look at.
    """
    if not validation["ok"]:
        raise HTTPException(status_code=422, detail=validation)


def _introduced(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The verdict on an EDIT: only the errors this edit is responsible for.

    "A save must not break a manifest" and "a save is gated on the manifest
    being unbroken" are different promises with opposite outcomes, and the
    second one locks the operator out of the file exactly when they need it.
    Running the full validator on the merged document made an agent with a bad
    cron or a tool the engine no longer registers impossible to repair AND
    impossible to `disable` — and `disable` is the stop control. Since the tool
    half reads the LIVE engine's registry, uninstalling one plugin froze every
    agent that named its tools. `DELETE` does not validate, so retiring the
    agent was the only remedy the Helm had left.

    Identity is ``(path, code)``: same complaint about the same field. A
    pre-existing fault is therefore never a free pass for a second one — it is
    reported as a warning instead, because "allowed through" must not read as
    "blessed".
    """
    already = {(issue["path"], issue["code"]) for issue in before["errors"]}
    new = [issue for issue in after["errors"] if (issue["path"], issue["code"]) not in already]
    carried = [issue for issue in after["errors"] if (issue["path"], issue["code"]) in already]
    return {
        "ok": not new,
        "errors": new,
        "warnings": [*after["warnings"], *carried],
        "pre_existing": carried,
    }


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


def _merge_owned(document: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
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
            raise HTTPException(status_code=422, detail=f"{dotted} must be a list")
        _set_path(document, dotted, value)
    return document


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _bump_version(document: dict[str, Any], change: str) -> None:
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


# ─── Rendering a new agent ───────────────────────────────────────────


def _template_dir() -> Path:
    from robothor.setup import _find_template_dir

    directory = _find_template_dir()
    if directory is None:
        raise HTTPException(
            status_code=500,
            detail="the agent scaffold templates are not installed",
        )
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


def _kebab(name: str) -> str:
    """A display name reduced to the id shape the schema demands."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _fleet_default_model() -> str:
    from robothor.engine import config as engine_config

    block = engine_config._load_defaults(_manifest_dir()).get("model") or {}
    return str(block.get("primary") or "")


def _scaffold(agent_id: str, body: CreateRequest) -> tuple[dict[str, Any], str, str]:
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
    document = _parse(_render(templates / _MANIFEST_TEMPLATE, {**common, "{AGENT_NAME}": agent_id}))
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
    _merge_owned(document, body.owned_paths())
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


# ─── Request bodies ──────────────────────────────────────────────────


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


# ─── Engine reconcile ────────────────────────────────────────────────


async def reconcile_engine_schedules() -> dict[str, Any]:
    """Tell the engine to re-derive its job set, and report what it said.

    Never raises. A manifest write that succeeded is not rolled back because
    the engine could not be reached — the file on disk is the source of truth
    and the next watchdog pass (five minutes) reconciles it anyway. What must
    not happen is the operator being told the agent is live when it is not, so
    the failure is reported in the response instead of swallowed.
    """
    status, body = await engine_request("POST", "/api/admin/scheduler/reconcile")
    if status >= 400 or not isinstance(body, dict):
        detail = body.get("error") if isinstance(body, dict) else None
        return {"applied": False, "error": detail or "the engine did not reconcile"}
    return {"applied": True, **body}


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/api/agent-manifests")
async def list_manifests(request: Request) -> dict[str, Any]:
    """Every agent this appliance has, including the ones that will not load.

    ``require_operator`` on a GET deliberately: this enumerates the fleet, its
    models and its delivery targets, which is not member data.
    """
    require_operator(request)
    scan = await asyncio.to_thread(_scan)
    agents = sorted((_summary(m) for m in scan.manifests), key=lambda row: row["id"])
    return {"agents": agents, "broken": _broken(scan), "count": len(agents)}


@router.get("/api/agent-manifests/{agent_id}")
async def get_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """One agent: its document, its raw YAML, its instructions, its verdict.

    A broken manifest answers 200 with the verdict filled in, not 404. "Absent"
    and "unreadable" are different problems with different fixes, and telling
    the operator their agent does not exist when the file is sitting there with
    a typo in it is the confusion the whole scan machinery exists to remove.
    """
    require_operator(request)
    agent_id = _safe_id(agent_id)
    path = _manifest_path(agent_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    try:
        document = _parse(text)
    except HTTPException as unreadable:
        return {
            "manifest": None,
            "yaml": text,
            "instructions": "",
            "validation": {
                "ok": False,
                "errors": [_issue("", "unparseable", str(unreadable.detail))],
                "warnings": [],
            },
        }
    return {
        "manifest": document,
        "yaml": text,
        "instructions": await asyncio.to_thread(_read_instructions, document),
        "validation": await _validate(document),
    }


def _read_instructions(document: dict[str, Any]) -> str:
    """The agent's instruction file, or an empty string if it has none."""
    relative = document.get("instruction_file")
    if not relative:
        return ""
    try:
        path = _instruction_path(relative)
    except HTTPException:
        # A READ of the fleet must not 422 because one agent names an
        # instruction file outside the workspace. The refusal is right; making
        # it fatal to the page is not.
        logger.warning("Refusing to read an instruction file outside the workspace")
        return ""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@router.post("/api/agent-manifests/validate")
async def validate_manifest(body: ValidateRequest, request: Request) -> dict[str, Any]:
    """Always 200. The verdict is the payload, not the status code.

    A 422 here would make "the editor asked whether this is valid" and "the
    editor sent a malformed request" the same answer, which is unusable for a
    form that validates as the operator types.
    """
    require_operator(request)
    if body.manifest is not None:
        document = body.manifest
    elif body.yaml is not None:
        try:
            document = _parse(body.yaml)
        except HTTPException as unreadable:
            return {
                "ok": False,
                "errors": [_issue("", "unparseable", str(unreadable.detail))],
                "warnings": [],
            }
    else:
        raise HTTPException(status_code=422, detail="send either 'manifest' or 'yaml'")
    return await _validate(document)


@router.post("/api/agent-manifests", status_code=201)
async def create_manifest(body: CreateRequest, request: Request) -> dict[str, Any]:
    """Scaffold an agent, validate it, write it, and make it fire.

    409 rather than an overwrite on a colliding id: the ids are derived from a
    display name, so a collision is far more likely to be an operator who
    forgot they already built this agent than one who meant to replace it.
    """
    require_operator(request)
    agent_id = _safe_id(body.id if body.id is not None else _kebab(body.name))
    if _manifest_path(agent_id).exists():
        raise HTTPException(status_code=409, detail="an agent with that id already exists")

    document, instruction_relative, instructions = await asyncio.to_thread(
        _scaffold, agent_id, body
    )
    validation = await _validate(document)
    _refuse(validation)

    await asyncio.to_thread(_write_pair, agent_id, document, instruction_relative, instructions)
    audited(request, "helm.agent_manifest.create", action=agent_id, agent_id=agent_id)
    return {
        "id": agent_id,
        "manifest": document,
        "warnings": validation["warnings"],
        "reconcile": await reconcile_engine_schedules(),
    }


def _write_pair(
    agent_id: str, document: dict[str, Any], instruction_relative: str, instructions: str
) -> None:
    """Manifest and instructions, each replaced atomically.

    Not one transaction: two files on a POSIX filesystem cannot be. The order
    is deliberate — instructions first, manifest second — so the window between
    them is "an instruction file nothing references" rather than "an agent
    whose instructions do not exist", which is the half that makes a run fail.
    """
    instruction_path = _instruction_path(instruction_relative)
    if not instruction_path.exists():
        _write_atomically(instruction_path, instructions)
    try:
        _write_atomically(_manifest_path(agent_id), _dump(document), exclusive=True)
    except FileExistsError as error:
        # The 409 above is a courtesy; this is the decision. Two concurrent
        # creates of the same id both pass `.exists()`, and without O_EXCL the
        # second would silently destroy the first.
        raise HTTPException(
            status_code=409, detail="an agent with that id already exists"
        ) from error


@router.patch("/api/agent-manifests/{agent_id}")
async def patch_manifest(agent_id: str, body: PatchRequest, request: Request) -> dict[str, Any]:
    """Edit the paths the form owns, and nothing else. See :func:`_merge_owned`."""
    require_operator(request)
    return await _apply_patch(request, agent_id, body.owned_paths(), body.change)


async def _apply_patch(
    request: Request, agent_id: str, changes: dict[str, Any], change: str
) -> dict[str, Any]:
    """The one write path every edit goes through — PATCH, enable, disable.

    Enable and disable are the same act as any other field change, so they take
    the same route through validation, the history ring, the audit event and the
    reconcile. A shortcut that wrote the flag directly would be a second way to
    change a manifest, and the second way is always the one that forgets to
    reconcile.

    The document is judged twice — as it was and as the edit leaves it — and
    refused only on what the edit INTRODUCED. See :func:`_introduced` for why a
    single verdict on the merged document locked the operator out of exactly
    the manifests they needed to fix or silence.
    """
    agent_id = _safe_id(agent_id)
    path = _manifest_path(agent_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")

    original = _parse(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    document = _parse(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    _merge_owned(document, changes)
    _bump_version(document, change)
    # One context for both verdicts: two reads of the engine's tool list across
    # one save could disagree about which tools exist, and the difference
    # between the two answers is what decides the refusal.
    context = await _validation_context()
    validation = _introduced(await _validate(original, context), await _validate(document, context))
    _refuse(validation)

    await asyncio.to_thread(_snapshot, agent_id)
    await asyncio.to_thread(_write_atomically, path, _dump(document))
    audited(
        request,
        "helm.agent_manifest.update",
        action=agent_id,
        agent_id=agent_id,
        fields=sorted(changes),
        version=document["version"],
    )
    return {
        "id": agent_id,
        "manifest": document,
        "warnings": validation["warnings"],
        "reconcile": await reconcile_engine_schedules(),
    }


@router.post("/api/agent-manifests/{agent_id}/enable")
async def enable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Put the agent back on its schedule. Never a no-op.

    Writes ``schedule.enabled: true`` even when the key is already absent —
    absent means enabled, so a route that skipped the write would report
    success without producing a document that says so, and the fleet view would
    keep showing whatever it showed before.
    """
    require_operator(request)
    return await _apply_patch(request, agent_id, {"schedule.enabled": True}, "Enabled via the Helm")


@router.post("/api/agent-manifests/{agent_id}/disable")
async def disable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Silence the agent without retiring it — cron, heartbeat and worker."""
    require_operator(request)
    return await _apply_patch(
        request, agent_id, {"schedule.enabled": False}, "Disabled via the Helm"
    )


@router.delete("/api/agent-manifests/{agent_id}")
async def retire_manifest(agent_id: str, body: DeleteRequest, request: Request) -> dict[str, Any]:
    """Move the manifest to ``retired/``. Nothing is unlinked.

    The confirmation has to be the agent's own id, typed: this is the one route
    that takes an agent off the fleet, and a boolean ``confirm: true`` is a
    field a misdirected form fills in for you. The instruction file stays where
    it is — it is the operator's writing, and re-creating the agent with the
    same id picks it back up.
    """
    require_operator(request)
    agent_id = _safe_id(agent_id)
    if body.confirm != agent_id:
        raise HTTPException(status_code=422, detail="confirm must be the agent's id")
    path = _manifest_path(agent_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")

    await asyncio.to_thread(_snapshot, agent_id)
    await asyncio.to_thread(_retire, agent_id, path)
    audited(request, "helm.agent_manifest.retire", action=agent_id, agent_id=agent_id)
    return {"id": agent_id, "retired": True, "reconcile": await reconcile_engine_schedules()}


def _retire(agent_id: str, path: Path) -> None:
    root = _manifest_root()
    (root / RETIRED_DIR).mkdir(parents=True, exist_ok=True)
    try:
        target = contained_path(root, f"{RETIRED_DIR}/{agent_id}.yaml", label="retired manifest")
    except TemplateSecurityError as error:
        raise _refused(error) from error
    path.replace(target)


@router.post("/api/agent-manifests/{agent_id}/run")
async def run_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Fire the agent once, now. Proxied to the engine, which owns the runner."""
    require_operator(request)
    agent_id = _safe_id(agent_id)
    # Same 404 every sibling route gives for an id that is not an agent here.
    # Without it an unknown id was proxied to the engine and came back 502,
    # which reads as "the engine is down" rather than "no such agent".
    if not _manifest_path(agent_id).is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    # Percent-encoded as well as identifier-validated. ``validate_identifier``
    # already limits this to [a-z0-9-], so nothing can survive both — which is
    # the point: this value is the only part of an engine URL that a caller
    # chooses, and a URL a request is built from gets two locks, not one.
    path = f"/api/agents/{quote(agent_id, safe='')}/trigger"
    status, body = await engine_request("POST", path)
    # The engine's trigger route answers 200 with an ``error`` key for an agent
    # it cannot load, so the status code alone is not the verdict.
    refused = status >= 400 or (isinstance(body, dict) and body.get("error"))
    audited(
        request,
        "helm.agent_manifest.run",
        action=agent_id,
        agent_id=agent_id,
        status="error" if refused else "ok",
    )
    if refused:
        raise HTTPException(status_code=502, detail="the engine did not accept the trigger")
    return {"id": agent_id, "triggered": body}

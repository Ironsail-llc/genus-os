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
import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import yaml
from deps import get_tenant_id
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from robothor.engine.manifest_store import main_agent_id
from robothor.engine.sanitize import sanitize_log
from robothor.templates.safety import TemplateSecurityError, validate_identifier
from routers import _manifest_validation as validation
from routers._audit import audited
from routers._engine_client import engine_request
from routers._manifest_backend import Backend, LocalBackend, backend_for
from routers._manifest_rows import _broken, _chat_fields, _summary
from routers._operator import require_tenant_admin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

logger = logging.getLogger(__name__)


router = APIRouter(tags=["agent-manifests"])

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

#: The scaffold templates ``genus agent scaffold`` renders, so a Helm-created
#: agent and a CLI-created one are the same kind of object.
_INSTRUCTION_TEMPLATE = "agent-instructions.md"
_MANIFEST_TEMPLATE = "agent-manifest.yaml"


# ─── Where the acting tenant's manifests are ─────────────────────────


def _backend(request: Request) -> Backend:
    """The platform tenant's files on this host, or a child tenant's engine.

    The ACTING tenant decides: a root admin who switched into a child edits
    the child's fleet, on the child's engine.
    """
    return backend_for(get_tenant_id(request))


def _main_agent_id() -> str:
    """The agent a chat request reaches by sending NO key, on this host's engine.

    Resolved ONCE per listing by the caller: ``EngineConfig.from_env`` reads
    the environment and touches the filesystem, and a fleet of twenty is twenty
    of those. ``robothor.engine.manifest_store.main_agent_id`` explains why it
    is derived from ``main_session_key`` and not ``default_chat_agent``.
    """
    return main_agent_id()


def _refused(error: TemplateSecurityError) -> HTTPException:
    """A containment refusal, as a 422 the operator can read.

    Brief decision 4: a symlinked ``brain/`` (or ``docs/agents/``) is a
    deployment the appliance will not write through, and saying so plainly is
    the difference between "fix your symlink" and an opaque 500. The message
    comes from ``safety.py`` and names the rule, never the path.
    """
    return HTTPException(status_code=422, detail=str(error))


def _safe_id(agent_id: object) -> str:
    """A kebab-case agent id, or a 422 naming the rule it broke."""
    try:
        return validate_identifier(agent_id, label="agent id")
    except TemplateSecurityError as error:
        raise _refused(error) from error


def _instruction_filename(agent_id: str) -> str:
    return f"{agent_id.upper().replace('-', '_')}.md"


# ─── Serialisation ───────────────────────────────────────────────────


def _dump(document: dict[str, Any]) -> str:
    """The YAML the engine will parse back.

    PyYAML and not a round-tripping library: the engine reads this file with
    PyYAML, and a second YAML implementation on the write side would be a
    second opinion about what the file means. Comments do not survive an edit —
    stated here because it is a real cost, and the same one ``providers.py``
    accepted for ``_defaults.yaml``.
    """
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)


# ─── Reading ─────────────────────────────────────────────────────────
#
# The row shapers — `_block`, `_summary`, `_chat_fields`, `_broken` — live in
# `_manifest_rows`, which `automations.py` now imports from directly. The seam
# is the module, not a re-export through this one: a name that exists here only
# so another router can reach it reads as dead to every linter and every human,
# and the first thing either does is delete it.


def _scan() -> Any:
    """The PLATFORM tenant's manifests, synchronously (``automations`` and ``fleet``)."""
    return LocalBackend().scan_sync()


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
#
# The verdicts themselves live in `_manifest_validation`; what stays here is
# the part that has to touch the engine and the filesystem to assemble one
# ValidationContext. See that module's header for the seam.


async def _engine_tools(tenant: str | None) -> tuple[set[str], list[dict[str, str]]]:
    """The tenant's engine's registered tool names, and a warning if it would not say.

    An unreachable engine downgrades the tool check to a warning rather than
    failing the save: the operator would otherwise be unable to edit an agent
    while the engine is down, which is exactly when they most want to.
    """
    status, body = await engine_request("GET", "/api/admin/tools", tenant=tenant)
    if status >= 400 or not isinstance(body, dict):
        return set(), [
            validation.issue(
                "tools_allowed",
                "tools_unverified",
                "the engine did not answer, so tool names were not checked",
            )
        ]
    return {str(name) for name in body.get("tools") or []}, []


@contextlib.asynccontextmanager
async def _validation_context(
    backend: Backend, *documents: dict[str, Any]
) -> AsyncIterator[validation.ValidationContext]:
    """One view of the world for every verdict in this request.

    Its own function so an edit can validate the BEFORE and AFTER documents
    against the same fleet and the same tool list — see ValidationContext. A
    context manager because, for a child tenant, the files the validators read
    are a throwaway copy of that tenant's (``RemoteBackend.validation_paths``).
    """
    scan = await backend.scan()
    fleet = {str(m["id"]): m for m in scan.manifests if isinstance(m.get("id"), str)}
    tools, tool_warnings = await _engine_tools(backend.tenant)
    async with backend.validation_paths(list(documents)) as (manifest_dir, repo_root):
        yield validation.ValidationContext(
            fleet=fleet,
            tools=tools,
            manifest_dir=manifest_dir,
            repo_root=repo_root,
            tool_warnings=tool_warnings,
        )


async def _validate(backend: Backend, document: dict[str, Any]) -> dict[str, Any]:
    """One document's verdict, against a context fetched for it alone."""
    async with _validation_context(backend, document) as context:
        return await validation.validate(document, context)


def _refuse(verdict: dict[str, Any]) -> None:
    """Turn a failed validation into a 422 carrying the validator's own words.

    The messages are the ones ``manifest_schema`` and ``manifest_checks``
    produce, verbatim, with their stable codes. A route that replaced them with
    "invalid manifest" would leave the operator guessing which of forty fields
    to look at.
    """
    if not verdict["ok"]:
        raise HTTPException(status_code=422, detail=verdict)


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


def _scaffold(
    agent_id: str, body: CreateRequest, fleet_model: str
) -> tuple[dict[str, Any], str, str]:
    """``(manifest, instruction_relpath, instruction_text)`` for a new agent.

    ``fleet_model`` is the acting tenant's ``_defaults.yaml`` primary.
    """
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
    primary = (body.model or "").strip() or fleet_model
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


async def reconcile_engine_schedules(tenant: str | None = None) -> dict[str, Any]:
    """Tell *tenant*'s engine to re-derive its job set, and report what it said.

    Never raises. A manifest write that succeeded is not rolled back because
    the engine could not be reached — the file on disk is the source of truth
    and the next watchdog pass (five minutes) reconciles it anyway. What must
    not happen is the operator being told the agent is live when it is not, so
    the failure is reported in the response instead of swallowed.
    """
    status, body = await engine_request("POST", "/api/admin/scheduler/reconcile", tenant=tenant)
    if status >= 400 or not isinstance(body, dict):
        detail = body.get("error") if isinstance(body, dict) else None
        return {"applied": False, "error": detail or "the engine did not reconcile"}
    return {"applied": True, **body}


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/api/agent-manifests")
async def list_manifests(request: Request) -> dict[str, Any]:
    """Every agent the ACTING tenant has, including the ones that will not load.

    Gated on a GET deliberately: this enumerates the fleet, its models and its
    delivery targets, which is not member data.

    Per manifest rather than one generator over all of them, so that sentence
    stays true. ``_block`` handles the shapes known to break ``_summary`` today;
    this handles the field nobody has added yet. One unreadable file belongs in
    ``broken``, where the operator can see which one to fix — not in a 500 that
    takes the other nineteen agents off the page with it.
    """
    require_tenant_admin(request)
    scan = await _backend(request).scan()
    # A child tenant's engine names its own main agent in the scan.
    remote_main = getattr(scan, "main_agent_id", None)
    main_agent_id = remote_main if remote_main is not None else _main_agent_id()
    agents: list[dict[str, Any]] = []
    undescribable: list[dict[str, str]] = []
    for manifest in scan.manifests:
        agent_id = str(manifest.get("id") or "")
        try:
            agents.append({**_summary(manifest), **_chat_fields(manifest, main_agent_id)})
        except Exception as error:  # noqa: BLE001 — one manifest, not the page
            logger.warning(
                "Could not summarise %s: %s", sanitize_log(agent_id), type(error).__name__
            )
            undescribable.append(
                {"id": agent_id, "filename": "", "error_type": type(error).__name__}
            )
    agents.sort(key=lambda row: row["id"])
    broken = [*_broken(scan), *undescribable]
    return {
        "agents": agents,
        "broken": broken,
        "count": len(agents),
        # Named rather than inferred. The chat sends NO session_key for this
        # agent — that omission is what keeps the operator's shared main
        # session (webchat and Telegram, on purpose) exactly as it was — and a
        # browser that had to guess which id that is would guess "main".
        "default_agent": main_agent_id,
    }


@router.get("/api/agent-manifests/{agent_id}")
async def get_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """One agent: its document, its raw YAML, its instructions, its verdict.

    A broken manifest answers 200 with the verdict filled in, not 404. "Absent"
    and "unreadable" are different problems with different fixes, and telling
    the operator their agent does not exist when the file is sitting there with
    a typo in it is the confusion the whole scan machinery exists to remove.
    """
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    backend = _backend(request)
    text = await backend.read(agent_id)
    if text is None:
        raise HTTPException(status_code=404, detail="no such agent")
    try:
        document = _parse(text)
    except HTTPException as unreadable:
        return {
            "manifest": None,
            "yaml": text,
            "instructions": "",
            "validation": {
                "ok": False,
                "errors": [validation.issue("", "unparseable", str(unreadable.detail))],
                "warnings": [],
            },
        }
    return {
        "manifest": document,
        "yaml": text,
        # An instruction file outside the workspace reads as "" rather than a
        # 422: the refusal is right, making it fatal to the page is not.
        "instructions": await backend.read_instructions(document),
        "validation": await _validate(backend, document),
    }


@router.post("/api/agent-manifests/validate")
async def validate_manifest(body: ValidateRequest, request: Request) -> dict[str, Any]:
    """Always 200. The verdict is the payload, not the status code.

    A 422 here would make "the editor asked whether this is valid" and "the
    editor sent a malformed request" the same answer, which is unusable for a
    form that validates as the operator types.
    """
    require_tenant_admin(request)
    if body.manifest is not None:
        document = body.manifest
    elif body.yaml is not None:
        try:
            document = _parse(body.yaml)
        except HTTPException as unreadable:
            return {
                "ok": False,
                "errors": [validation.issue("", "unparseable", str(unreadable.detail))],
                "warnings": [],
            }
    else:
        raise HTTPException(status_code=422, detail="send either 'manifest' or 'yaml'")
    return await _validate(_backend(request), document)


@router.post("/api/agent-manifests", status_code=201)
async def create_manifest(body: CreateRequest, request: Request) -> dict[str, Any]:
    """Scaffold an agent, validate it, write it, and make it fire.

    409 rather than an overwrite on a colliding id: the ids are derived from a
    display name, so a collision is far more likely to be an operator who
    forgot they already built this agent than one who meant to replace it.
    The check here is a courtesy; the backend's exclusive create is the
    decision, so two concurrent creates of one id cannot both win.
    """
    require_tenant_admin(request)
    agent_id = _safe_id(body.id if body.id is not None else _kebab(body.name))
    backend = _backend(request)
    if await backend.read(agent_id) is not None:
        raise HTTPException(status_code=409, detail="an agent with that id already exists")

    document, instruction_relative, instructions = await asyncio.to_thread(
        _scaffold, agent_id, body, await backend.default_model()
    )
    verdict = await _validate(backend, document)
    _refuse(verdict)

    # Instructions first, manifest second (ManifestStore.create).
    await backend.create(agent_id, _dump(document), instruction_relative, instructions)
    audited(request, "helm.agent_manifest.create", action=agent_id, agent_id=agent_id)
    return {
        "id": agent_id,
        "manifest": document,
        "warnings": verdict["warnings"],
        "reconcile": await reconcile_engine_schedules(backend.tenant),
    }


@router.patch("/api/agent-manifests/{agent_id}")
async def patch_manifest(agent_id: str, body: PatchRequest, request: Request) -> dict[str, Any]:
    """Edit the paths the form owns, and nothing else. See :func:`_merge_owned`."""
    require_tenant_admin(request)
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
    refused only on what the edit INTRODUCED. See
    :func:`_manifest_validation.introduced` for why a single verdict on the
    merged document locked the operator out of exactly the manifests they
    needed to fix or silence.
    """
    agent_id = _safe_id(agent_id)
    backend = _backend(request)
    text = await backend.read(agent_id)
    if text is None:
        raise HTTPException(status_code=404, detail="no such agent")

    original = _parse(text)
    document = _parse(text)
    _merge_owned(document, changes)
    _bump_version(document, change)
    # ONE context for both verdicts: two reads of the engine's tool list across
    # a single save could disagree about which tools exist, and the difference
    # between the two answers is what decides the refusal.
    async with _validation_context(backend, original, document) as context:
        verdict = validation.introduced(
            await validation.validate(original, context),
            await validation.validate(document, context),
        )
    _refuse(verdict)

    # The store snapshots the previous version into the history ring first.
    await backend.update(agent_id, _dump(document))
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
        "warnings": verdict["warnings"],
        # The faults this edit did not introduce and did not fix. Also present
        # in `warnings`; named separately so a UI can say "saved — still broken
        # for these reasons" without diffing two lists to find out which of the
        # warnings were errors a moment ago. Empty on a healthy manifest.
        "pre_existing": verdict["pre_existing"],
        "reconcile": await reconcile_engine_schedules(backend.tenant),
    }


@router.post("/api/agent-manifests/{agent_id}/enable")
async def enable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Put the agent back on its schedule. Never a no-op.

    Writes ``schedule.enabled: true`` even when the key is already absent —
    absent means enabled, so a route that skipped the write would report
    success without producing a document that says so, and the fleet view would
    keep showing whatever it showed before.
    """
    require_tenant_admin(request)
    return await _apply_patch(request, agent_id, {"schedule.enabled": True}, "Enabled via the Helm")


@router.post("/api/agent-manifests/{agent_id}/disable")
async def disable_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Silence the agent without retiring it — cron, heartbeat and worker."""
    require_tenant_admin(request)
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
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    if body.confirm != agent_id:
        raise HTTPException(status_code=422, detail="confirm must be the agent's id")
    backend = _backend(request)
    # Snapshot, then move aside (404 if there is no such agent).
    await backend.retire(agent_id)
    audited(request, "helm.agent_manifest.retire", action=agent_id, agent_id=agent_id)
    return {
        "id": agent_id,
        "retired": True,
        "reconcile": await reconcile_engine_schedules(backend.tenant),
    }


@router.post("/api/agent-manifests/{agent_id}/run")
async def run_manifest(agent_id: str, request: Request) -> dict[str, Any]:
    """Fire the agent once, now. Proxied to the tenant's engine, which owns the runner."""
    require_tenant_admin(request)
    agent_id = _safe_id(agent_id)
    backend = _backend(request)
    # Same 404 every sibling route gives for an id that is not an agent here.
    # Without it an unknown id was proxied to the engine and came back 502,
    # which reads as "the engine is down" rather than "no such agent".
    if await backend.read(agent_id) is None:
        raise HTTPException(status_code=404, detail="no such agent")
    # Percent-encoded as well as identifier-validated. ``validate_identifier``
    # already limits this to [a-z0-9-], so nothing can survive both — which is
    # the point: this value is the only part of an engine URL that a caller
    # chooses, and a URL a request is built from gets two locks, not one.
    path = f"/api/agents/{quote(agent_id, safe='')}/trigger"
    status, body = await engine_request("POST", path, tenant=backend.tenant)
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

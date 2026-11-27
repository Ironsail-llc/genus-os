"""Remote tools, target side: a parent tenant's agent runs one of this tenant's tools.

The path is home engine -> bridge -> this engine; engines never call each other.
The bridge is the only caller (``admin_remote_tools`` refuses anyone else) and
this module believes nothing it forwards beyond what it re-derives here:

* the plugin's own declaration (``PluginSet.remote``): undeclared is unknown,
* who is asking: a person's standing in the parent (``acting_role``), or for
  a person-less read the parent's grant (``parent_agent_grant``) and the
  ``parent_agent`` role,
* the attestations the declaration requires, against what the bridge presents,
* the arguments, against the tool's own JSON schema.

A write runs at most once per request id (``remote_tool_requests``), and a
replay is answered only for the same tool and arguments. A read carries no
request id here: whatever one the body names is ignored. The call
itself goes through the normal registry dispatch, so RBAC (with the derived role
and the person's user id), kill switches and the plugin's own audit all apply,
confined to this tenant. Every call leaves one ``agent.remote_tool_call`` audit
row under this tenant carrying a digest of the arguments, never the arguments
or the result.

Blocking work (the plugin loader, the database) runs off the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from robothor.tenancy import remote_contract as rc

logger = logging.getLogger(__name__)

#: The target's own execution budget. A module attribute so a test can shorten it.
TIMEOUT_S: float = rc.TARGET_TIMEOUT_S

#: The longest ``agent_id`` carried into the actor string and the audit row.
_AGENT_ID_MAX = 64

_MESSAGES = {
    rc.ERR_WRITE_FORBIDDEN: "This remote call may not write in this tenant.",
    rc.ERR_TOOL_UNKNOWN: "This tenant offers no remote tool by that name.",
    rc.ERR_ACCESS_MISMATCH: "The tool is not offered for that access.",
    rc.ERR_CALLER_FORBIDDEN: "Only the bridge may make remote tool calls.",
    rc.ERR_NOT_PERMITTED: "The caller is not permitted to run this tool in this tenant.",
    rc.ERR_ATTESTATION_MISSING: "The tool needs an attestation the caller did not present.",
    rc.ERR_BAD_ARGUMENTS: "The arguments do not match the tool's parameters.",
    rc.ERR_OUTCOME_UNKNOWN: "An earlier call with this request id has no known outcome.",
    rc.ERR_REQUEST_MISMATCH: (
        "This request id was used for another tool or other arguments; nothing was run."
    ),
    rc.ERR_TOO_LARGE: "The tool's result is larger than a remote call may return.",
    rc.ERR_TIMEOUT: "The tool did not finish in time.",
}


class Refused(Exception):  # noqa: N818 - a refusal, read as one
    """A refusal with its HTTP status and contract code."""

    def __init__(self, status: int, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or _MESSAGES[code]
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "attestation": None, **self.extra}


def refusal_body(code: str) -> dict[str, Any]:
    """The refusal body for ``code`` with its fixed message."""
    return Refused(0, code).body()


# ── Declarations ─────────────────────────────────────────────────────────────

_declarations: tuple[int, dict[str, dict[str, Any]]] | None = None
_extras: tuple[float, list[dict[str, Any]], list[dict[str, Any]]] | None = None


def reset_caches() -> None:
    """Drop the declaration and catalogue caches. For tests."""
    global _declarations, _extras
    _declarations = None
    _extras = None


def declarations() -> dict[str, dict[str, Any]]:
    """Tool -> its remote declaration, from the loaded plugins. Blocking.

    Cached per plugin generation, as every other cache built from discovery.
    """
    global _declarations
    from robothor.plugins import generation, load_plugins

    current = generation()
    if _declarations is not None and _declarations[0] == current:
        return _declarations[1]
    declared = dict(load_plugins().remote)
    _declarations = (current, declared)
    return declared


def _schema(tool: str) -> dict[str, Any] | None:
    from robothor.engine.tools.registry import get_registry

    schema = get_registry().get_schema(tool)
    function = (schema or {}).get("function")
    return function if isinstance(function, dict) else None


async def _declared(tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The declaration and the registered schema of a remote tool, or 404."""
    declared = (await asyncio.to_thread(declarations)).get(tool)
    function = _schema(tool) if declared is not None else None
    if declared is None or function is None:
        raise Refused(404, rc.ERR_TOOL_UNKNOWN)
    return declared, function


# ── Catalogue ────────────────────────────────────────────────────────────────

#: Absolute-looking paths in a check's detail: where an instance keeps its
#: files is not a fact another tenant gets.
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s\\/'\"]+){2,}[\\/]?")
_DETAIL_MAX = 200


def _clean(text: str) -> str:
    from robothor.secrets.redaction import redact

    return _PATH_RE.sub("<path>", redact(str(text or "")[:4096]))[:_DETAIL_MAX]


def _check_status(severity: str, status: str) -> str:
    if status == "pass":
        return "ok"
    if status == "skip":
        return "skip"
    return "fail" if severity == "required" else "warn"


async def _run_checks() -> list[dict[str, Any]]:
    """The installed plugins' doctor checks, run offline, redacted."""
    from robothor.doctor import runner
    from robothor.doctor.context import DoctorContext
    from robothor.doctor.registry import plugin_checks

    checks = await asyncio.to_thread(plugin_checks)
    if not checks:
        return []
    report = await runner.run(
        DoctorContext(offline=True, total_timeout_s=30.0), checks=list(checks)
    )
    return [
        {
            "name": row.id,
            "status": _check_status(row.severity, row.status),
            "detail": _clean(row.detail),
        }
        for row in report.results
    ]


def _plugins() -> list[dict[str, Any]]:
    from robothor.plugins.inventory import inventory

    return [
        {"name": s.name, "version": s.version, "enabled": bool(s.enabled and s.state == "loaded")}
        for s in inventory()
    ]


async def _plugins_and_checks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Plugins and checks, cached for ``CHECK_CACHE_TTL_S``."""
    global _extras
    now = time.monotonic()
    if _extras is not None and now - _extras[0] < rc.CHECK_CACHE_TTL_S:
        return _extras[1], _extras[2]
    try:
        plugins = await asyncio.to_thread(_plugins)
    except Exception:  # noqa: BLE001 - a broken inventory must not break the catalogue
        logger.warning("remote tools: plugin inventory unavailable", exc_info=True)
        plugins = []
    try:
        checks = await _run_checks()
    except Exception:  # noqa: BLE001 - a broken check must not break the catalogue
        logger.warning("remote tools: plugin checks unavailable", exc_info=True)
        checks = []
    _extras = (now, plugins, checks)
    return plugins, checks


async def catalogue(tenant_id: str) -> dict[str, Any]:
    """``GET /api/admin/remote-tools``: what this tenant offers its parent."""
    declared = await asyncio.to_thread(declarations)
    tools: list[dict[str, Any]] = []
    for name in sorted(declared):
        function = _schema(name)
        if function is None:
            continue
        spec = declared[name]
        tools.append(
            {
                "name": name,
                "plugin": spec["plugin"],
                "access": spec["access"],
                "attestations": list(spec["attestations"]),
                "description": str(function.get("description") or "")[: rc.DESCRIPTION_MAX_CHARS],
            }
        )
    plugins, checks = await _plugins_and_checks()
    return {
        "tenant_id": tenant_id,
        "as_of": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": plugins,
        "tools": tools,
        "checks": checks,
    }


async def describe(tool: str) -> dict[str, Any]:
    """``GET /api/admin/remote-tools/{tool}``: one tool's declaration and schema."""
    spec, function = await _declared(tool)
    return {
        "name": tool,
        "plugin": spec["plugin"],
        "access": spec["access"],
        "attestations": list(spec["attestations"]),
        "notes": spec["notes"],
        "description": str(function.get("description") or ""),
        "parameters": function.get("parameters") or {"type": "object", "properties": {}},
    }


# ── Argument validation (the JSON Schema subset tool schemas use) ────────────

_TYPES: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def schema_problem(value: Any, schema: Any, where: str = "args") -> str | None:
    """Why ``value`` does not fit ``schema``, or None.

    Types, ``enum``, an object's ``required``, ``properties`` and
    ``additionalProperties: false``, and an array's ``items``. A keyword
    outside that subset is not checked: the tool's handler still validates
    what it reads.
    """
    if not isinstance(schema, dict):
        return None
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        known = [n for n in names if n in _TYPES]
        if known and not any(_TYPES[n](value) for n in known):
            return f"{where} must be {' or '.join(known)}"
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        return f"{where} must be one of the allowed values"
    if isinstance(value, dict):
        raw_properties = schema.get("properties")
        properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
        for key in schema.get("required") or ():
            if key not in value:
                return f"{where}.{key} is required"
        if schema.get("additionalProperties") is False:
            extra = sorted(str(k) for k in value if k not in properties)
            if extra:
                return f"{where} has unknown parameter(s): {', '.join(extra)}"
        for key, sub in properties.items():
            if key in value:
                problem = schema_problem(value[key], sub, f"{where}.{key}")
                if problem:
                    return problem
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problem = schema_problem(item, schema["items"], f"{where}[{index}]")
            if problem:
                return problem
    return None


# ── The request ledger (remote_tool_requests) ────────────────────────────────


def claim_request(
    tenant_id: str, request_id: str, tool: str, args_digest: str
) -> dict[str, Any] | None:
    """Claim ``request_id`` as running for ``tool`` with ``args_digest``.

    None when claimed now, else the existing row ``{state, result, tool,
    args_digest}``, unchanged. Blocking; raises when the ledger cannot be read.
    """
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO remote_tool_requests (tenant_id, request_id, tool, args_digest, state) "
            "VALUES (%s, %s, %s, %s, 'running') ON CONFLICT (tenant_id, request_id) DO NOTHING "
            "RETURNING request_id",
            (tenant_id, request_id, tool, args_digest),
        )
        if cur.fetchone() is not None:
            return None
        cur.execute(
            "SELECT state, result, tool, args_digest FROM remote_tool_requests "
            "WHERE tenant_id = %s AND request_id = %s",
            (tenant_id, request_id),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError("a claimed request could not be read back")
    result = row[1]
    if isinstance(result, str):
        result = json.loads(result)
    return {"state": row[0], "result": result, "tool": row[2], "args_digest": row[3]}


def finish_request(tenant_id: str, request_id: str, result: dict[str, Any]) -> None:
    """Mark a claimed request done with the answer it gave. Blocking."""
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE remote_tool_requests SET state = 'done', result = %s::jsonb, "
            "finished_at = NOW() WHERE tenant_id = %s AND request_id = %s",
            (json.dumps(result, default=str), tenant_id, request_id),
        )


def release_request(tenant_id: str, request_id: str) -> None:
    """Drop a RUNNING claim whose tool provably never ran. Blocking."""
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant_id), get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM remote_tool_requests "
            "WHERE tenant_id = %s AND request_id = %s AND state = 'running'",
            (tenant_id, request_id),
        )


# ── Audit ────────────────────────────────────────────────────────────────────


def audit(
    *,
    tenant_id: str,
    home_tenant: str,
    user_id: str,
    agent_id: str,
    tool: str,
    access: str,
    outcome: str,
    dry_run: bool,
    request_id: str | None,
    args: Any,
) -> None:
    """One ``agent.remote_tool_call`` row under this tenant. Never raises."""
    try:
        from robothor.audit.logger import log_event
        from robothor.engine.event_writer import get_event_writer

        details = {
            "home_tenant": home_tenant,
            "user_id": user_id,
            "agent_id": agent_id,
            "tool": tool,
            "access": access,
            "outcome": outcome,
            "dry_run": dry_run,
            "request_id": request_id,
            "args_digest": rc.args_digest(args),
        }
        get_event_writer().submit(
            log_event,
            event_type=rc.EVENT_REMOTE_TOOL_CALL,
            action=tool,
            category="agent",
            actor=f"remote:{home_tenant}:{agent_id}",
            user_id=user_id,
            details=details,
            target=tenant_id,
            status=outcome,
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 - audit must not break the call it records
        logger.warning("remote tool audit row could not be queued", exc_info=True)


# ── Execute ──────────────────────────────────────────────────────────────────


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


async def _identity(requested_by: Any, home: str, here: str, access: str) -> tuple[str, str]:
    """``(user_id, role)`` for this call, re-derived here; or a refusal."""
    if not home or home == here:
        raise Refused(403, rc.ERR_NOT_PERMITTED)
    if requested_by is not None:
        from robothor.auth.acting import acting_role
        from robothor.rooms.keys import canonical_uuid

        user_id = canonical_uuid(requested_by)
        role = await asyncio.to_thread(acting_role, user_id, home, here) if user_id else None
        if not user_id or role is None:
            raise Refused(403, rc.ERR_NOT_PERMITTED)
        if access == rc.ACCESS_WRITE and role != "owner":
            raise Refused(403, rc.ERR_WRITE_FORBIDDEN)
        return user_id, role
    if access == rc.ACCESS_WRITE:
        raise Refused(403, rc.ERR_WRITE_FORBIDDEN)
    from robothor.auth.acting import parent_agent_grant

    if not await asyncio.to_thread(parent_agent_grant, home, here):
        raise Refused(403, rc.ERR_NOT_PERMITTED)
    return "", rc.ROLE_PARENT_AGENT


def _answer(
    outcome: str, request_id: Any, tool: str, result: Any, would: Any = None
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "request_id": request_id,
        "tool": tool,
        "result": result,
        "would": would,
    }


def _rbac_denied(result: dict[str, Any]) -> bool:
    return bool(result.get("refused")) and result.get("gate") == "role"


async def execute(tenant_id: str, body: Any) -> tuple[int, dict[str, Any]]:
    """``POST /api/admin/remote-tools/call``, after the caller check.

    Returns ``(status, body)`` and audits every answer, refusals included.
    """
    body = body if isinstance(body, dict) else {}
    tool = _text(body.get("tool"))
    access = _text(body.get("access"))
    home = _text(body.get("home_tenant"))
    agent_id = _text(body.get("agent_id"))[:_AGENT_ID_MAX]
    raw_request_id = body.get("request_id")
    # Only a write is deduplicated, so only a write carries a request id: a
    # read's is ignored, and can neither replay nor release anything.
    request_id = (
        raw_request_id if access == rc.ACCESS_WRITE and isinstance(raw_request_id, str) else None
    )
    args = body.get("args", {})
    dry_run_asked = body.get("dry_run", False)
    seen: dict[str, Any] = {"user_id": "", "dry_run": dry_run_asked is True}

    def audited(status: int, payload: dict[str, Any], outcome: str) -> tuple[int, dict[str, Any]]:
        audit(
            tenant_id=tenant_id,
            home_tenant=home,
            user_id=seen["user_id"],
            agent_id=agent_id,
            tool=tool,
            access=access,
            outcome=outcome,
            dry_run=seen["dry_run"],
            request_id=request_id,
            args=args,
        )
        return status, payload

    try:
        status, payload = await _execute(
            tenant_id, tool, access, home, agent_id, request_id, args, dry_run_asked, body, seen
        )
    except Refused as refused:
        return audited(refused.status, refused.body(), refused.code)
    return audited(status, payload, str(payload.get("outcome") or payload.get("error")))


async def _execute(
    tenant_id: str,
    tool: str,
    access: str,
    home: str,
    agent_id: str,
    request_id: str | None,
    args: Any,
    dry_run_asked: Any,
    body: dict[str, Any],
    seen: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    write = access == rc.ACCESS_WRITE
    # 2. the declaration
    spec, function = await _declared(tool)
    if spec["access"] != access:
        raise Refused(409, rc.ERR_ACCESS_MISMATCH)
    # 3. who is asking
    user_id, role = await _identity(body.get("requested_by"), home, tenant_id, access)
    seen["user_id"] = user_id
    # 4. attestations
    presented = {t for t in body.get("attestations") or () if isinstance(t, str)}
    for token in spec["attestations"]:
        if token not in presented:
            raise Refused(403, rc.ERR_ATTESTATION_MISSING, attestation=token)
    # 5. arguments
    if not isinstance(dry_run_asked, bool):
        raise Refused(422, rc.ERR_BAD_ARGUMENTS, "dry_run must be true or false.")
    parameters = function.get("parameters") or {"type": "object", "properties": {}}
    problem = (
        "args must be an object" if not isinstance(args, dict) else schema_problem(args, parameters)
    )
    if problem:
        raise Refused(422, rc.ERR_BAD_ARGUMENTS, f"The arguments do not fit the tool: {problem}.")
    # 6. a dry run executes nothing
    if write and dry_run_asked:
        seen["dry_run"] = True
        return 200, _answer(
            rc.OUTCOME_DRY_RUN, request_id, tool, None, {"tool": tool, "args": args}
        )
    # 7. a write runs at most once
    if write:
        if not request_id:
            raise Refused(422, rc.ERR_BAD_ARGUMENTS, "A remote write needs a request_id.")
        replay = await _claim(tenant_id, request_id, tool, rc.args_digest(args))
        if replay is not None:
            return replay
    # 8. the normal dispatch, confined to this tenant
    status, payload = await _dispatch(
        tenant_id, tool, args, home, agent_id, user_id, role, request_id, write=write
    )
    if write and request_id:
        try:
            await asyncio.to_thread(
                finish_request, tenant_id, request_id, {"status": status, "body": payload}
            )
        except Exception:  # noqa: BLE001 - the claim stays running: never re-run
            logger.warning("remote tool request could not be marked done", exc_info=True)
    return status, payload


async def _claim(
    tenant_id: str, request_id: str, tool: str, digest: str
) -> tuple[int, dict[str, Any]] | None:
    """None when this call may run; else the stored answer (replayed); or 409.

    A finished row answers only the call it was claimed for: another tool or
    other arguments under the same id is ``remote_request_mismatch``.
    """
    try:
        prior = await asyncio.to_thread(claim_request, tenant_id, request_id, tool, digest)
    except Exception:  # noqa: BLE001 - an unreadable ledger is an unknown outcome
        logger.warning("remote tool request ledger unavailable", exc_info=True)
        raise Refused(409, rc.ERR_OUTCOME_UNKNOWN) from None
    if prior is None:
        return None
    stored = prior.get("result")
    if (
        prior.get("state") != "done"
        or not isinstance(stored, dict)
        or not isinstance(stored.get("body"), dict)
    ):
        raise Refused(409, rc.ERR_OUTCOME_UNKNOWN)
    if prior.get("tool") != tool or prior.get("args_digest") != digest:
        raise Refused(409, rc.ERR_REQUEST_MISMATCH)
    status = int(stored.get("status") or 200)
    body = dict(stored["body"])
    if status == 200:
        body["outcome"] = rc.OUTCOME_REPLAYED
    return status, body


async def _dispatch(
    tenant_id: str,
    tool: str,
    args: dict[str, Any],
    home: str,
    agent_id: str,
    user_id: str,
    role: str,
    request_id: str | None,
    *,
    write: bool,
) -> tuple[int, dict[str, Any]]:
    from robothor.engine.tools.registry import get_registry

    try:
        async with asyncio.timeout(TIMEOUT_S):
            result = await get_registry().execute(
                tool,
                args,
                agent_id=f"remote:{home}:{agent_id}",
                run_id="",
                tenant_id=tenant_id,
                user_id=user_id,
                user_role=role,
                accessible_tenant_ids=(tenant_id,),
                timeout=0,  # this module's own budget, so a timeout is told apart
            )
    except TimeoutError:
        raise Refused(504, rc.ERR_TIMEOUT) from None
    if not isinstance(result, dict):
        result = {"result": result}
    if _rbac_denied(result):
        # The role refused before the handler ran, so a write's claim is
        # released for a retry. A read has no claim to release.
        if write and request_id:
            try:
                await asyncio.to_thread(release_request, tenant_id, request_id)
            except Exception:  # noqa: BLE001 - a kept claim is a 409 later, never a re-run
                logger.warning("remote tool request claim could not be released", exc_info=True)
        raise Refused(403, rc.ERR_NOT_PERMITTED)
    try:
        size = len(json.dumps(result, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        size = rc.REMOTE_RESULT_MAX_BYTES + 1
    outcome = rc.OUTCOME_ERROR if "error" in result else rc.OUTCOME_OK
    if size > rc.REMOTE_RESULT_MAX_BYTES:
        if not write:
            return 413, refusal_body(rc.ERR_TOO_LARGE)
        # A write has acted: it is answered as done, its result withheld,
        # never as a refusal a caller would retry.
        answer = _answer(outcome, request_id, tool, None)
        answer["result_withheld"] = {
            "reason": rc.WITHHELD_TOO_LARGE,
            "bytes": size,
            "max_bytes": rc.REMOTE_RESULT_MAX_BYTES,
        }
        return 200, answer
    return 200, _answer(outcome, request_id, tool, result)

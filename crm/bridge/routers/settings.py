"""The settings API — ``genus config`` for an operator who is not on the box.

Three routes behind the Helm's Config and Flags pages:

``GET /api/settings/schema``  what CAN be configured — the registry as a form
                              description, group by group, with the type, the
                              default, the enum, and the restart/secret/
                              governed/since metadata each field declares.
``GET /api/settings``         what IS configured — the effective value, which
                              layer supplied it, and whether this surface may
                              change it.
``PATCH /api/settings``       change a batch, all of it or none of it.

None of this logic lives here. Every verb is
:mod:`robothor.settings.operator`, which ``genus config`` calls too, because
the alternative — a second implementation behind an HTTP surface — is how a
platform ends up with ``genus config set X`` and the Settings page writing to
different places, each with its own green test.  What this module owns is the
HTTP shape: the gate, the JSON, the status codes, the audit row.

Four rules the routes enforce that the library does not:

1. **A secret is never returned.** ``{configured, fingerprint}`` and nothing
   else, for a browser tab, a screenshot and a proxy log that all outlive the
   session. And a secret is never WRITTEN here: config.yaml is a plain file
   that gets copied into bug reports, so credentials go to the vault
   (``genus vault set``, and the Helm's Secrets page) — a later task.
2. **All-or-nothing.** The whole batch is validated before a byte is written.
   A form that half-applies leaves the operator unable to say what the
   instance is now configured to do.
3. **The environment wins, and the refusal says so.** A field an environment
   variable supplies is not editable here: writing config.yaml underneath it
   would report applied and change nothing, which is the exact failure the
   settings package exists to end. The CLI, which runs on the box where the
   variable can be cleared, still writes the file — and ``genus doctor``
   reports the disagreement.
4. **Operator-only, and audited by name.** Same ``require_operator`` gate as
   Controls (no service tokens, owner/admin only, platform tenant only), and
   every write leaves an audit row naming the settings that changed. Never a
   value: the audit log is exported to a SIEM.

Handlers are plain ``def`` (not ``async def``) on purpose: the flag store is
synchronous psycopg2 and the config write is synchronous file I/O, so FastAPI
must run them in its worker threadpool rather than on the event loop (see
``crm/bridge/tests/test_route_concurrency.py``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from robothor.settings import operator, provenance
from robothor.settings.registry import field_index, groups
from routers._audit import audited
from routers._operator import require_operator

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: Provenance labels, as the API names them. The library's labels are the
#: CLI's, and two of them read badly in a UI badge: ``config.yaml`` carries a
#: filename the browser cannot act on, and ``runtime`` is produced by exactly
#: one thing — an operator-written ``feature_flags`` row — which the Flags page
#: needs to name as such. Mapped here rather than renamed there, so
#: ``genus config get`` keeps saying what it has always said.
_API_SOURCE = {
    operator.SOURCE_DEFAULT: "default",
    operator.SOURCE_FILE: "config",
    operator.SOURCE_ENV: "env",
    operator.SOURCE_RUNTIME: "db",
}


#: How many changes one request may carry. The registry declares 371 fields and
#: no form edits them all; the bound exists because validating an unknown name
#: runs a fuzzy match over every candidate, and a plain ``def`` handler holds a
#: threadpool worker for as long as it takes. Operator-only, so this is a
#: guard-rail rather than a defence — but one wedged worker per request is a
#: cheap enough self-DoS to close.
MAX_CHANGES = 200

#: Beyond this many unknown names in one batch, stop computing suggestions. The
#: first errors an operator reads are the ones worth a "did you mean"; a form
#: that sent two thousand typos wants the list, not the help.
MAX_SUGGESTIONS = 20

#: An operator's note is a sentence, not a payload. It reaches the audit log and
#: ``feature_flag_audit.reason``, both of which are read back by people.
MAX_NOTE = 500


class SettingsPatch(BaseModel):
    """A batch of changes, keyed by the setting's declared environment name.

    ``note`` is the operator's own reason for the change. It is carried into
    the ``feature_flags`` audit row for a governed flag (the same field
    ``/api/controls`` fills) and into the bridge audit row, so "who widened
    this, and why" has an answer six months later.
    """

    changes: dict[str, Any] = Field(default_factory=dict, max_length=MAX_CHANGES)
    note: str | None = Field(default=None, max_length=MAX_NOTE)


def _declared() -> list[dict[str, Any]]:
    """Every declared field once, in declaration order.

    ``field_index()`` is keyed by every spelling a field answers to, so the
    deprecated aliases point at records already seen. A page that listed them
    would offer the operator two boxes for one setting.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for record in field_index().values():
        if record["env"] in seen:
            continue
        seen.add(record["env"])
        out.append(record)
    return out


def _label(group_id: str) -> str:
    """A human heading for a group, derived rather than tabulated.

    A hand-maintained id -> label map is a list that drifts from what is
    actually registered, which this repo has paid for three times (see
    ``robothor/engine/tests/test_module_size_ratchet.py``'s siblings). A group
    added to the model gets a heading here for free.
    """
    return group_id.replace("_", " ").title()


def _enum(record: dict[str, Any]) -> list[str] | None:
    """The values this field may take, when the platform bounds them.

    Only governed flags do today, and ``robothor.flags.store.valid_values_for``
    is the single source of truth the write path validates against — so the
    form offers exactly the set the PATCH will accept, rather than a
    hand-mirrored copy that can be one flag behind.
    """
    if not record["governed"]:
        return None
    from robothor.flags import store

    return list(store.valid_values_for(record["env"]))


def _hot(record: dict[str, Any]) -> bool:
    """Does a change to this field take effect without restarting anything?

    Two ways to be hot, and they are different mechanisms: a governed flag is
    read from the DB on a five-second TTL, and a field declared
    ``restart_required=False`` is re-read by whoever reads it. The form shows
    one badge for both, because the operator's question is the same one.
    """
    return bool(record["governed"]) or not record["restart_required"]


def _default_for(record: dict[str, Any]) -> Any:
    """The default a form may preselect — never a credential, always in the enum."""
    if record["secret"]:
        return None
    if record["governed"]:
        from robothor.flags import store

        return store.default_value_for(record["env"])
    return record["default"]


def _field_schema(record: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": record["env"],
        "env": record["env"],
        "field": record["field"],
        "group": record["group"],
        "aliases": list(record["aliases"]),
        "type": record["type"],
        "description": record["description"],
        # A governed flag's default is what the ENGINE runs when nothing sets
        # it, spelled the way its own enum spells it. The registry types
        # ROBOTHOR_RIP_1_ENABLED as a bool, so the declared default is Python
        # False -- which is not in ["true","false"] and cannot be sent back.
        # A secret has no default by construction (``declare()`` refuses one),
        # but null rather than "" states that rather than relying on it.
        "default": _default_for(record),
        "secret": record["secret"],
        "governed": record["governed"],
        "restart_required": record["restart_required"],
        "restart_units": list(operator.units_for(record)),
        "since": record["since"],
        "hot": _hot(record),
    }
    values = _enum(record)
    if values is not None:
        payload["enum"] = values
    return payload


@router.get("/schema")
def settings_schema(request: Request) -> dict[str, Any]:
    """What can be configured, as a form description.

    Deterministic: groups in the model's declaration order, fields in theirs.
    A page whose field order changed between two loads is a page an operator
    cannot learn.
    """
    require_operator(request)
    by_group: dict[str, list[dict[str, Any]]] = {name: [] for name in groups()}
    for record in _declared():
        by_group.setdefault(record["group"], []).append(_field_schema(record))
    return {
        "groups": [
            {"id": name, "label": _label(name), "fields": fields}
            for name, fields in by_group.items()
        ]
    }


def _editable(record: dict[str, Any]) -> bool:
    """May this surface change the field?

    No for a credential (the vault owns those) and no for a field an
    environment variable supplies — writing config.yaml under a variable that
    overrides it reports applied and changes nothing.
    """
    return not record["secret"] and operator.env_override(record) is None


def _pending_restart() -> list[str]:
    """Units whose running process disagrees with config.yaml about a
    restart-required setting.

    The same question ``genus doctor``'s ``config.pending_restart`` check asks,
    and the same rule: the file holds a value, the process reads a different
    one from the environment, the environment wins by documented precedence,
    and only a restart (after the variable is cleared) applies the file.

    Deliberately NOT a memory of what this API has written. A PATCH reports the
    units ITS changes need in its own response, which is what the page should
    show after a save; this list answers the other question — "is something in
    this file being ignored right now" — and it is derived, so it needs no
    state and cannot go stale.
    """
    units: set[str] = set()
    for record in _declared():
        if not record["restart_required"]:
            continue
        present, raw = provenance.file_value(record)
        if not present:
            continue
        env_name = provenance.env_name_in_use(record)
        if env_name is None:
            continue
        running = provenance.running_env_value(env_name) or ""
        if operator.agree(record, raw, running):
            continue
        units.update(operator.units_for(record))
    return sorted(units)


@router.get("")
def settings_values(request: Request) -> dict[str, Any]:
    """What is configured, and which layer said so."""
    require_operator(request)
    # Per-request rather than per-process: this same process serves the
    # /api/controls writes, and a snapshot taken at boot would report a flag
    # the operator flipped an hour ago as unset.
    operator.reset_db_rows()

    values: dict[str, Any] = {}
    for record in _declared():
        value, source, _detail = operator.resolve(record)
        values[record["env"]] = {
            "value": operator.secret_status(record) if record["secret"] else value,
            "source": _API_SOURCE.get(source, source),
            "editable": _editable(record),
        }
    return {"values": values, "pending_restart": _pending_restart()}


def _env_refusal(record: dict[str, Any], env_name: str) -> str:
    units = ", ".join(operator.units_for(record)) or "the reader"
    return (
        f"{record['env']} is set in this instance's environment ({env_name}), which "
        "wins over config.yaml — a change saved here would apply to nothing. Clear "
        f"the variable on the box and restart {units}, then it can be managed from "
        "this page."
    )


def _plan(
    changes: dict[str, Any],
) -> tuple[list[tuple[dict[str, Any], Any]], list[dict[str, str]]]:
    """Validate the whole batch without writing any of it.

    Returns ``(planned, errors)``. Every name is checked even after one fails:
    an operator fixing a form wants all of the red boxes at once, not the
    first one repeatedly.
    """
    planned: list[tuple[dict[str, Any], Any]] = []
    errors: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    unknown = 0
    for name, raw in changes.items():
        record = operator.record(name)
        if record is None:
            unknown += 1
            errors.append(
                {
                    "name": name,
                    "message": operator.unknown_name_message(
                        name, suggest=unknown <= MAX_SUGGESTIONS
                    ),
                }
            )
            continue
        # A field answers to its declared name AND to every deprecated alias, so
        # two keys in one batch can be the same setting. Written, they are two
        # splices of one field and last-one-wins by dict order, and the response
        # names the field twice -- an operator cannot tell which value took.
        if record["env"] in seen:
            errors.append(
                {
                    "name": record["env"],
                    "message": (
                        f"{name} and {seen[record['env']]} are the same setting "
                        f"({record['env']}). Send it once."
                    ),
                }
            )
            continue
        seen[record["env"]] = name
        # Secret FIRST. A credential is the field class most likely to be
        # supplied by the environment, and the env refusal below would tell the
        # operator to clear the variable and manage it here -- advice that is
        # false (the vault owns credentials) and harmful (the running services
        # need that variable).
        if record["secret"]:
            errors.append({"name": record["env"], "message": operator.secret_refusal(record)})
            continue
        # Governed flags are exempt from the env check: the ``feature_flags``
        # row outranks the environment in ``robothor.flags.store.resolve``, so a
        # variable set for one does NOT make the write invisible.
        env_name = None if record["governed"] else operator.env_override(record)
        if env_name is not None:
            errors.append({"name": record["env"], "message": _env_refusal(record, env_name)})
            continue
        try:
            planned.append((record, operator.validate(record, raw)))
        except operator.SettingError as exc:
            errors.append({"name": exc.name, "message": exc.message})
    return planned, errors


@router.patch("")
def patch_settings(patch: SettingsPatch, request: Request) -> Any:
    """Apply a batch of changes — all of them, or none of them.

    422 with every error and nothing written when validation fails; 200 with
    the names applied and the units to restart when it succeeds. A failure
    DURING application (a full disk, a database that went away) cannot be
    rolled back across a file and a table, so it is reported as 500 with the
    same body: ``applied`` names exactly what landed.
    """
    actor = require_operator(request)
    operator.reset_db_rows()

    planned, errors = _plan(patch.changes)
    if errors:
        # Only names the REGISTRY knows. A rejected key is a string the client
        # chose, and an audit row is exported to a SIEM: logging it verbatim
        # turns a typo'd `sk-live-…` into a durable record of a credential.
        # The ones that do not resolve are a count.
        known = sorted({e["name"] for e in errors if operator.record(e["name"]) is not None})
        audited(
            request,
            "settings.change",
            action="patch",
            status="denied",
            names=known,
            count=len(errors),
            unknown_names=len(errors) - len(known),
        )
        return JSONResponse(
            status_code=422,
            content={"applied": [], "pending_restart": [], "errors": errors},
        )

    reason = patch.note or "settings page"
    applied, pending_units, errors = operator.apply_batch(planned, actor=actor, reason=reason)
    pending = set(pending_units)
    wrote_file = any(not record["governed"] for record, _ in planned)

    if wrote_file:
        # This process caches its settings; it has just changed the file they
        # came from. Without this the form an operator saved reverts on the
        # next load, and the page and the instance disagree about what is
        # configured.
        from robothor.settings import reset_settings

        reset_settings()

    body = {
        "applied": applied,
        "pending_restart": sorted(pending),
        "errors": errors,
    }
    audited(
        request,
        "settings.change",
        action="patch",
        status="error" if errors else "ok",
        names=sorted(applied),
        count=len(applied),
        pending_restart=sorted(pending),
        note=patch.note,
    )
    return JSONResponse(status_code=500, content=body) if errors else body

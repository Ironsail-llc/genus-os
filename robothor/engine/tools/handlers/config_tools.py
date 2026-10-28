"""``config_get`` / ``config_set`` — the operator's assistant reads and changes its settings.

Before these the assistant had ``exec`` + ``genus config set`` and nothing
else, and the instance env file was unreadable to it. Both tools are thin:
every lookup, coercion, refusal and write is a :mod:`robothor.settings.operator`
function, the same objects ``genus config`` and the bridge's
``PATCH /api/settings`` call, so the three surfaces cannot disagree about what
a value means or where it goes.

* ``config_get`` -- one setting (or every setting under a prefix): the
  effective value, the layer it came from, its description and whether a
  change needs a restart. A secret is shown as ``<set, b2:...>``, never as
  itself.
* ``config_set`` -- validated against the declared type and written where the
  field lives (config.yaml, or the flag store for a governed flag). A secret
  is refused, and the refusal does not echo it: ``vault_set`` owns
  credentials. ``apply=True`` changes the value in the layer that actually
  wins and schedules the restart -- which means writing systemd drop-ins
  under ``/etc``, which the engine process cannot do (``ProtectSystem=strict``).
  So it runs ``genus config set --apply`` through the authenticated host-exec
  service (:mod:`robothor.engine.host_execution`), eligible only for the
  verified owner on a top-level main run; otherwise it says so.

Both are the owner's: a verified identity with role ``owner`` in the run's own
tenant, and never a benchmark run. RBAC denies them to every other role as
well (migration 152).
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

#: The most settings one prefix query returns. There are several hundred, and
#: a model handed all of them at once has spent its context on a table.
_PREFIX_LIMIT = 100

#: Seconds the host-side ``genus config set --apply`` may take. It reads unit
#: files, writes drop-ins, reloads systemd and schedules a restart -- seconds,
#: not minutes -- but a slow ``systemctl`` must not hang the run.
_APPLY_TIMEOUT = 120


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _owner_denial(ctx: ToolContext, tool: str) -> dict[str, Any] | None:
    """``None`` for the verified owner, else the refusal. Fails closed."""
    from robothor.engine.owner_gate import is_verified_owner

    if is_verified_owner(ctx):
        return None
    return {
        "error": (
            f"{tool} is restricted to the instance owner (a verified owner identity "
            "on this run). Ask the operator's own assistant to read or change settings."
        ),
        "denied_by": "owner_role",
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _lookup(key: str) -> dict[str, Any] | None:
    from robothor.settings import operator

    return operator.record(key) or operator.record(key.strip().upper())


def _describe(row: dict[str, Any]) -> dict[str, Any]:
    """Everything an assistant needs about one setting, with no secret in it."""
    from robothor.settings import operator

    value, source, detail = operator.resolve(row)
    restart = bool(row["restart_required"]) and not row["governed"]
    return {
        "key": row["env"],
        "value": operator.display(row, value) if row["secret"] else _jsonable(value),
        "source": source,
        "source_detail": str(detail),
        "description": row["description"],
        "type": str(row["type"]),
        "default": operator.mask(row["default"]) if row["secret"] else _jsonable(row["default"]),
        "secret": bool(row["secret"]),
        "governed": bool(row["governed"]),
        "restart_required": restart,
        "restart_units": list(operator.units_for(row)) if restart else [],
    }


def _get_sync(key: str, prefix: str) -> dict[str, Any]:
    from robothor.settings import operator

    # A long-running engine must not answer from a flag snapshot taken at
    # boot; the CLI never needs this because it exits after one question.
    operator.reset_db_rows()
    if key:
        row = _lookup(key)
        if row is None:
            return {"error": operator.unknown_name_message(key)}
        return _describe(row)

    from robothor.settings.registry import field_index

    wanted = prefix.strip().upper()
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in field_index().values():
        if row["env"] in seen or not row["env"].upper().startswith(wanted):
            continue
        seen.add(row["env"])
        rows.append(row)
    rows.sort(key=lambda r: r["env"])
    settings = []
    for row in rows[:_PREFIX_LIMIT]:
        described = _describe(row)
        settings.append(
            {
                "key": described["key"],
                "value": described["value"],
                "source": described["source"],
                "secret": described["secret"],
                "restart_required": described["restart_required"],
                "description": str(described["description"])[:200],
            }
        )
    return {
        "prefix": wanted,
        "count": len(rows),
        "settings": settings,
        "truncated": len(rows) > _PREFIX_LIMIT,
    }


@_handler("config_get")
async def _config_get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    denial = _owner_denial(ctx, "config_get")
    if denial:
        return denial
    key = str(args.get("key") or "").strip()
    prefix = str(args.get("prefix") or "").strip()
    if not key and not prefix:
        return {
            "error": "config_get needs `key` (one setting, e.g. ROBOTHOR_LOG_LEVEL) "
            "or `prefix` (every setting whose name starts with it, e.g. ROBOTHOR_ENGINE_)."
        }
    return await asyncio.to_thread(_get_sync, key, prefix)


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _set_plain_sync(row: dict[str, Any], value: str, agent_id: str) -> dict[str, Any]:
    from robothor.settings import operator

    try:
        coerced = operator.validate(row, value)
        pending = operator.apply_change(
            row,
            coerced,
            actor=f"agent:{agent_id or 'unattributed'}",
            reason="config_set tool",
        )
    except operator.SettingError as exc:
        return {"error": exc.message}
    answer: dict[str, Any] = {
        "success": True,
        "key": row["env"],
        "value": _jsonable(coerced),
        "written_to": "feature_flags" if row["governed"] else "config.yaml",
        "pending_restart": list(pending),
        "restart_required": bool(pending),
    }
    if not row["governed"]:
        override = operator.env_override(row)
        if override:
            answer["warning"] = (
                f"{override} is set in the service environment, which wins over "
                "config.yaml, so the running services keep the old value. Call "
                "config_set again with apply=true to change it there and restart."
            )
    return answer


async def _set_apply(row: dict[str, Any], value: str, ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine import host_execution
    from robothor.settings import operator

    try:
        coerced = operator.validate(row, value)
    except operator.SettingError as exc:
        return {"error": exc.message}
    if not host_execution.socket_path() or not host_execution.eligible(ctx):
        return {
            "error": (
                "apply=true needs host exec: it writes systemd drop-ins under /etc and "
                "restarts services, which the engine process cannot do. Host exec is "
                "available only to the verified owner on a top-level main run with the "
                "host-exec service configured. Without it, call config_set with "
                "apply=false (writes config.yaml) and ask the operator to restart."
            ),
            "error_type": "host_exec_required",
        }
    # ``--`` ends the options: a value such as ``--fast`` or ``-x`` must reach
    # argparse as the positional value, not as an unknown flag.
    command = shlex.join(["genus", "config", "set", "--apply", "--json", "--", row["env"], value])
    result = await host_execution.execute(ctx, command, _APPLY_TIMEOUT, {})
    if result.get("error") and "stdout" not in result:
        return {"error": f"apply failed: {result['error']}", "key": row["env"]}
    try:
        payload = json.loads(result.get("stdout") or "")
    except ValueError:
        from robothor.secrets.redaction import redact

        return {
            "success": False,
            "key": row["env"],
            "error": "genus config set --apply did not return JSON",
            "stderr": redact(str(result.get("stderr") or ""))[-2000:],
            "exit_code": result.get("exit_code"),
        }
    errors = list(payload.get("errors") or [])
    return {
        "success": result.get("exit_code") == 0 and not errors,
        "key": row["env"],
        "value": _jsonable(coerced),
        "applied": bool(payload.get("applied")),
        "pending_restart": list(payload.get("pending_restart") or []),
        "dropins": list(payload.get("dropins") or []),
        "restart_scheduled": list(payload.get("restart_scheduled") or []),
        "restart_coalesced": list(payload.get("restart_coalesced") or []),
        "errors": errors,
        "execution_mode": result.get("execution_mode", "host_service"),
    }


@_handler("config_set")
async def _config_set(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    denial = _owner_denial(ctx, "config_set")
    if denial:
        return denial

    from robothor.settings import operator
    from robothor.settings.systemd_env import unsafe_value_message

    key = str(args.get("key") or "").strip()
    if not key:
        return {"error": "config_set needs `key`, the setting's environment variable name."}
    row = _lookup(key)
    if row is None:
        return {"error": operator.unknown_name_message(key)}
    if row["secret"]:
        # Never echo the submitted value: it is a credential.
        return {
            "error": (
                f"{row['env']} holds a credential, which config_set never writes. "
                "Store it with vault_set instead (vault_list shows the key naming)."
            ),
            "key": row["env"],
            "secret": True,
        }
    value = _as_text(args.get("value"))
    problem = unsafe_value_message(row["env"], value)
    if problem:
        return {"error": problem, "key": row["env"]}
    if args.get("apply") and not row["governed"]:
        return await _set_apply(row, value, ctx)
    return await asyncio.to_thread(_set_plain_sync, row, value, getattr(ctx, "agent_id", ""))

"""``genus config`` — read, change, explain and check the instance's settings.

An operator could previously answer "what is this set to?" only by grepping
``/etc/robothor/robothor.env``, the systemd drop-ins, the dashboard's Controls
page and the code, and could change a setting only by editing one of them and
guessing what to restart. The settings registry declares all of it — the exact
variable name, the type, the default, whether it holds a credential, whether a
change is live or needs a restart, whether it is a governed flag — so these
commands route by that metadata instead of asking the operator to know:

``get``       the effective value and where it came from -- including, for a
              value a systemd unit injects, the exact drop-in (or
              environment file) that sets it.
``explain``   everything declared about one setting.
``set``       the value, to the right place: a governed flag to the DB store
              (live), a secret refused (``genus vault set`` owns those), and
              everything else to the ``settings:`` block of config.yaml.
              ``--apply`` changes it where it actually wins: a systemd
              ``Environment=`` override is replaced by a ``zz-genus-config-*``
              drop-in (``--override`` to outrank a later drop-in), then
              systemd is reloaded and the restart scheduled 15 s out.
``list``      every setting, or one group, or only what is configured.
``validate``  an alias for ``genus doctor``, which asks everything this
              command used to ask and a dozen more. Kept so existing runbooks
              and scripts keep working.
``schema``    the JSON Schema, for tooling.

A secret is never printed by any of them. ``get`` and ``list`` show
``<set, b2:ab12cd34>`` — enough to compare two boxes without putting a
credential in a terminal, a screenshot or a scrollback buffer.
"""

from __future__ import annotations

import argparse  # noqa: TC003
import json
import sys
from typing import Any

from robothor.settings import operator
from robothor.settings.operator import (
    DEFAULT_UNITS,  # noqa: F401 — re-exported; this module named it first
    SOURCE_DEFAULT,
    SOURCE_ENV,  # noqa: F401 — re-exported for callers reading provenance labels
    SOURCE_FILE,  # noqa: F401
    SOURCE_RUNTIME,  # noqa: F401
)

__all__ = ["cmd_config"]

# ── the shared implementation ────────────────────────────────────────────────
#
# Every verb below routes by the same metadata, coerces with the same coercer
# and writes through the same writer as ``PATCH /api/settings`` on the bridge:
# they are the SAME function objects, bound here under the private names this
# module and the doctor's checks already call them by.
# ``crm/bridge/tests/test_settings_router.py::
# test_the_router_and_the_cli_call_the_same_functions`` asserts that identity,
# so a future edit cannot quietly fork one surface from the other.

_record = operator.record
_coerce = operator.coerce
_resolve = operator.resolve
_units_for = operator.units_for
_agree = operator.agree
_mask = operator.mask
_display = operator.display
_db_rows = operator.db_rows
_db_value = operator.db_value

#: Injectable for tests: how ``systemctl``/``sudo`` are run and unit files read.
#: ``None`` means the real ones (see ``robothor.settings.systemd_env``).
_systemd_runner: Any = None
_systemd_reader: Any = None

# ── shared helpers ───────────────────────────────────────────────────────────


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _operator_name() -> str:
    """Who to record as having made the change.

    ``getpass.getuser()`` rather than ``$USER``: the variable is absent in a
    systemd unit and a bare container, and an audit row stamped ``operator:``
    with nothing after it names nobody. ``flag_audit`` reads the ``operator:``
    prefix as a deliberate, supported flip rather than an anonymous pin.
    """
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return "cli"


def _unknown_name(name: str) -> int:
    """Exit code 2 and the closest declared names, on TWO stderr lines.

    A typo in a variable name is otherwise indistinguishable from a setting
    that does not exist yet, and both look like the command doing nothing.

    Two lines, as this command has always printed: the failure on the first,
    the fix on the second. The library returns them as a pair for exactly this
    reason — the bridge joins them into one HTTP string, and a terminal is not
    an HTTP body. ``robothor/cli/tests/test_config_cmd.py`` pins both lines.
    """
    for line in operator.unknown_name_lines(name):
        _err(line)
    return 2


def _systemd_reports(record: dict[str, Any]) -> list[Any]:
    """What each unit that reads this setting says about it, per systemd.

    Asked of the unit files, not this process: the shell an agent runs
    ``genus config get`` in is not the engine, and a drop-in on the engine is
    invisible to that shell's environment. A unit is reported when it sets
    the variable OR when some of its files could not be read -- "not set" is
    then unverified, and nothing downstream may treat it as known.
    """
    from robothor.settings import systemd_env

    units = _units_for(record)
    found: dict[str, Any] = {}
    for name in (record["env"], *record["aliases"]):
        for report in systemd_env.inspect(
            name, units, runner=_systemd_runner, reader=_systemd_reader
        ):
            have = found.get(report.unit)
            if have is None:
                found[report.unit] = report
                continue
            if have.origin is None and report.origin is not None:
                have.origin = report.origin
            have.unreadable = sorted(set(have.unreadable) | set(report.unreadable))
    return list(found.values())


def _systemd_origins(record: dict[str, Any]) -> list[Any]:
    return [r.origin for r in _systemd_reports(record) if r.origin is not None]


def _report_json(report: Any) -> dict[str, Any]:
    """A unit's report for ``--json``: where, never what (a value can be a credential)."""
    origin = report.origin
    return {
        "unit": report.unit,
        "name": origin.name if origin else None,
        "kind": origin.kind if origin else None,
        "path": origin.path if origin else None,
        "line": origin.line if origin else None,
        "declared_in": origin.declared_in if origin else None,
        "overrides": [f"{o.path}:{o.line}" for o in origin.shadowed] if origin else [],
        "unreadable": list(report.unreadable),
    }


def _unreadable_refusal(record: dict[str, Any], reports: list[Any]) -> str | None:
    """Why ``--apply`` cannot claim success: a file it could not read may win."""
    blind = [(r.unit, path) for r in reports for path in r.unreadable]
    if not blind:
        return None
    where = "; ".join(f"{unit}: {path}" for unit, path in blind)
    return (
        f"{record['env']}: cannot verify which value wins -- could not read {where}. "
        "An EnvironmentFile beats every Environment= line, so a change made here may "
        "not be the one the service reads. Nothing was changed. Run as a user that can "
        "read those files, or check them and edit them directly."
    )


# ── get / explain / list ─────────────────────────────────────────────────────


def _cmd_get(args: argparse.Namespace) -> int:
    record = _record(args.name)
    if record is None:
        return _unknown_name(args.name)

    from robothor.settings import systemd_env

    value, source, detail = _resolve(record)
    shown = _display(record, value)
    reports = [] if record["governed"] else _systemd_reports(record)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "name": record["env"],
                    "value": shown if record["secret"] else value,
                    "source": source,
                    "detail": detail,
                    "secret": record["secret"],
                    "systemd": [_report_json(r) for r in reports],
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return 0

    print(f"{record['env']} = {shown}")
    print(f"  source: {source} ({detail})")
    for report in reports:
        origin = report.origin
        if origin is not None:
            print(f"  systemd: {systemd_env.describe(origin, secret=record['secret'])}")
            for beaten in origin.shadowed:
                print(f"    overrides {beaten.path}:{beaten.line}")
        else:
            print(f"  systemd: {report.unit}: not set in the files that could be read")
        for path in report.unreadable:
            print(f"    could not read {path}; it may set this, unverified")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    record = _record(args.name)
    if record is None:
        return _unknown_name(args.name)

    value, source, detail = _resolve(record)
    payload = {
        "name": record["env"],
        "group": record["group"],
        "field": record["field"],
        "description": record["description"],
        "type": record["type"],
        "default": _mask(record["default"]) if record["secret"] else record["default"],
        "aliases": record["aliases"],
        "secret": record["secret"],
        "governed": record["governed"],
        "restart_required": record["restart_required"],
        "restart_units": list(_units_for(record)) if record["restart_required"] else [],
        "since": record["since"],
        "value": _display(record, value),
        "source": source,
        "source_detail": detail,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0

    print(f"{record['env']}")
    print(f"  {record['description']}")
    print(f"  group:    {record['group']} ({record['field']})")
    print(f"  type:     {record['type']}")
    print(f"  default:  {payload['default']!r}")
    if record["aliases"]:
        print(f"  aliases:  {', '.join(record['aliases'])} (deprecated)")
    print(f"  secret:   {'yes — set it with `genus vault set`' if record['secret'] else 'no'}")
    print(
        "  governed: "
        + (
            "yes — a guardrail flag; `genus config set` applies it live"
            if record["governed"]
            else "no"
        )
    )
    units = _units_for(record)
    if record["governed"]:
        # The governed path is live either way; the restart only applies to an
        # operator who edits the environment instead of using this command.
        restart = "not required — `genus config set` applies it through the flag store"
    elif not record["restart_required"]:
        restart = "not required — applies live"
    else:
        restart = f"required — {', '.join(units) if units else 'next invocation picks it up'}"
    print(f"  restart:  {restart}")
    print(f"  since:    {record['since']}")
    print(f"  value:    {payload['value']}  (source: {source}, {detail})")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from robothor.settings.registry import field_index, groups

    wanted = getattr(args, "group", None)
    if wanted and wanted not in groups():
        _err(f"{wanted}: no such group. Groups: {', '.join(groups())}")
        return 2

    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for record in field_index().values():
        if record["env"] in seen:
            continue
        seen.add(record["env"])
        if wanted and record["group"] != wanted:
            continue
        value, source, _detail = _resolve(record)
        if getattr(args, "changed", False) and source == SOURCE_DEFAULT:
            continue
        rows.append((record["env"], _display(record, value), source))
    rows.sort()

    if getattr(args, "json", False):
        print(
            json.dumps(
                [{"name": n, "value": v, "source": s} for n, v, s in rows],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    width = max((len(name) for name, _, _ in rows), default=0)
    for name, value, source in rows:
        print(f"{name:<{width}}  {value}  [{source}]")
    if not rows:
        print("(nothing configured)" if getattr(args, "changed", False) else "(no settings)")
    return 0


# ── set ──────────────────────────────────────────────────────────────────────


def _set_result(
    applied: bool,
    pending: list[str],
    errors: list[str],
    as_json: bool,
    extra: dict[str, Any] | None = None,
) -> int:
    extra = extra or {}
    if as_json:
        print(
            json.dumps(
                {"applied": applied, "pending_restart": pending, "errors": errors, **extra},
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if errors else 0
    # A partial apply is said on both streams: what landed on stdout, what did
    # not on stderr. Neither half is allowed to read as the whole.
    for path in extra.get("dropins", []):
        print(f"wrote {path}")
    for transient in extra.get("restart_scheduled", []):
        print(f"restart scheduled in 15s (transient unit {transient})")
    for transient in extra.get("restart_coalesced", []):
        print(f"restart already scheduled ({transient}); it will pick this change up")
    if errors:
        if extra.get("partial"):
            _err("PARTIAL: the lines above landed; the step below did not.")
        for error in errors:
            _err(error)
    elif not extra.get("restart_scheduled") and not extra.get("restart_coalesced"):
        if pending:
            print(f"restart required: {', '.join(pending)}")
        else:
            print("applied")
    return 1 if errors else 0


def _set_apply(args: argparse.Namespace, record: dict[str, Any], as_json: bool) -> int:
    """``set --apply``: change the value in the layer that wins, then restart.

    A value a systemd ``Environment=`` line injects sits above config.yaml, so
    writing the file changes nothing the service reads. Here the change goes
    to a drop-in that sorts after the current winner -- or, when the variable
    comes from an ``EnvironmentFile=``, is refused naming that file, because
    systemd lets an environment file beat every ``Environment=`` line. With
    no systemd origin it is the ordinary config.yaml write. Either way the
    restart is scheduled through ``systemd-run --on-active=15s`` so the run
    that asked for the change can reply before its engine restarts.

    Refused, with nothing written, when any unit or environment file the
    target units name could not be read: the variable may be set there and
    win, and claiming "applied" past a file nobody read is the defect this
    command exists to remove.

    A governed flag is live through the flag store already; ``--apply`` adds
    nothing to it.
    """
    from robothor.settings import systemd_env

    if record["governed"]:
        return _cmd_set_plain(args, record, as_json)

    try:
        value = operator.validate(record, args.value)
        reports = _systemd_reports(record)
        refusal = _unreadable_refusal(record, reports)
        if refusal:
            return _set_result(False, [], [refusal], as_json)
        origins = [r.origin for r in reports if r.origin is not None]
        if origins:
            plans = [
                systemd_env.plan_dropin(
                    record["env"],
                    str(args.value),
                    origin.unit,
                    origin,
                    override=getattr(args, "override", False),
                    note=f"by operator:{_operator_name()}",
                )
                for origin in origins
            ]
            result = systemd_env.apply_dropins(plans, runner=_systemd_runner)
        else:
            pending = operator.apply_change(
                record,
                value,
                actor=f"operator:{_operator_name()}",
                reason="genus config set --apply",
            )
            if not pending:
                return _set_result(True, [], [], as_json)
            result = systemd_env.apply_dropins(
                [], runner=_systemd_runner, reload=False, units=list(pending)
            )
    except operator.SettingError as exc:
        return _set_result(False, [], [exc.message], as_json)
    except (systemd_env.DropinConflictError, systemd_env.UnsafeValueError) as exc:
        return _set_result(False, [], [str(exc)], as_json)
    except systemd_env.ApplyError as exc:
        extra = {
            "partial": exc.partial,
            "dropins": exc.written,
            "reloaded": exc.reloaded,
            "restart_scheduled": exc.scheduled,
        }
        return _set_result(False, [], [f"{record['env']}: {exc}"], as_json, extra)

    extra = {
        "dropins": result.written,
        "restart_units": result.units,
        "restart_scheduled": result.scheduled,
        "restart_coalesced": result.coalesced,
    }
    return _set_result(True, result.units, [], as_json, extra)


def _cmd_set(args: argparse.Namespace) -> int:
    """Route one change by the field's own metadata, and say what it cost.

    The routing, the coercion, the refusals and the write all live in
    ``robothor.settings.operator``, which the bridge's ``PATCH /api/settings``
    calls too. What is left here is the CLI's half: an exit code and a line of
    text. A governed flag comes back with no units -- it resolves from the DB
    on the engine's next read (a five-second TTL), so it is live.

    A value with a control character is refused on every path before
    anything is routed: a newline in a value becomes a directive of its own
    in a unit file (run as root on the next restart) and a broken line in
    config.yaml.
    """
    from robothor.settings.systemd_env import unsafe_value_message

    as_json = getattr(args, "json", False)
    record = _record(args.name)
    if record is None:
        return _unknown_name(args.name)
    problem = unsafe_value_message(record["env"], str(args.value))
    if problem:
        return _set_result(False, [], [problem], as_json)
    if getattr(args, "apply", False):
        return _set_apply(args, record, as_json)
    return _cmd_set_plain(args, record, as_json)


def _cmd_set_plain(args: argparse.Namespace, record: dict[str, Any], as_json: bool) -> int:
    """The config.yaml / flag-store write, warning when systemd would shadow it."""
    try:
        value = operator.validate(record, args.value)
        pending = operator.apply_change(
            record,
            value,
            actor=f"operator:{_operator_name()}",
            reason="genus config set",
        )
    except operator.SettingError as exc:
        return _set_result(False, [], [exc.message], as_json)

    if not record["governed"]:
        # The file write landed, but a unit that sets the variable keeps
        # reading its own value. Say so on stderr (stdout stays the result).
        for origin in _systemd_origins(record):
            where = origin.path if origin.line is None else f"{origin.path}:{origin.line}"
            _err(
                f"note: {origin.unit} reads {record['env']} from {where}, which wins over "
                "config.yaml. Re-run with --apply to change it there and restart."
            )
    return _set_result(True, list(pending), [], as_json)


# ── validate (an alias for `genus doctor`) ───────────────────────────────────

#: ``_agree`` and ``_units_for``, which the doctor's checks import from here,
#: are now ``robothor.settings.operator`` functions bound at the top of this
#: module — the checks keep the names they were written against while the
#: implementation is the one the bridge also calls. The checks that used to
#: live in this section moved to ``robothor/doctor/checks/`` whole, so that
#: `genus doctor`, the bridge's ``/api/doctor`` and this command all ask the
#: same questions rather than three similar ones.


def _cmd_validate(args: argparse.Namespace) -> int:
    """Run `genus doctor`, and say that this is what it now is.

    Every question this command used to ask is a doctor check, and the doctor
    asks a dozen more that a fresh install actually fails on -- an unseeded
    ``service`` role, a drifted migration ledger, a provider that cannot make a
    completion. Keeping two commands would have meant two answers to "is this
    instance working", which is how the platform ended up with three partial
    ones in the first place.

    The exit codes are unchanged: 0 when nothing required failed, 1 when
    something did. The note goes to stderr so that
    ``genus config validate --json | jq`` keeps working.

    OFFLINE, deliberately. The command this replaces made no upstream call, and
    a deprecated alias is the last thing that should get more expensive: every
    runbook, cron entry and habit that still types it would start spending
    provider budget and calling api.telegram.org. So the alias runs the free
    checks and the note says where the full report lives. The JSON shape DOES
    change -- it is the doctor's ``{status, summary, checks}`` -- which the
    stderr note and docs/configuration.md both say, because a script doing
    ``--json | jq .errors`` would otherwise read null as healthy.
    """
    from robothor.cli.doctor_cmd import cmd_doctor

    print(
        "genus config validate is deprecated; it now runs 'genus doctor --offline', which "
        "checks more and can repair some of it. --json emits the doctor's payload "
        "({status, summary, checks}), not the old {checks, errors, pending_restart}. "
        "Run 'genus doctor' for the full report, including a live model call.",
        file=sys.stderr,
    )
    return cmd_doctor(
        argparse.Namespace(
            json=getattr(args, "json", False),
            fix=False,
            dry_run=False,
            offline=True,
            timeout=5.0,
            only=None,
            category=None,
        )
    )


# ── schema ───────────────────────────────────────────────────────────────────


def _cmd_schema(_args: argparse.Namespace) -> int:
    """Print the JSON Schema of every declared setting.

    The schema carries each field's declared environment name, default,
    description and the restart/secret/since/governed metadata, so tooling --
    an editor, a form generator, a config linter -- can read what is
    configurable without importing Python.
    """
    from robothor.settings.model import GenusSettings

    print(json.dumps(GenusSettings.model_json_schema(), indent=2, sort_keys=True))
    return 0


_COMMANDS = {
    "get": _cmd_get,
    "set": _cmd_set,
    "explain": _cmd_explain,
    "list": _cmd_list,
    "validate": _cmd_validate,
    "schema": _cmd_schema,
}


def cmd_config(args: argparse.Namespace) -> int:
    handler = _COMMANDS.get(getattr(args, "config_command", None) or "")
    if handler is None:
        print("Usage: genus config {get|set|explain|list|validate|schema}")
        return 0
    return handler(args)

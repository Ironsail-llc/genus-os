"""``genus config`` — read, change, explain and check the instance's settings.

An operator could previously answer "what is this set to?" only by grepping
``/etc/robothor/robothor.env``, the systemd drop-ins, the dashboard's Controls
page and the code, and could change a setting only by editing one of them and
guessing what to restart. The settings registry declares all of it — the exact
variable name, the type, the default, whether it holds a credential, whether a
change is live or needs a restart, whether it is a governed flag — so these
commands route by that metadata instead of asking the operator to know:

``get``       the effective value and where it came from.
``explain``   everything declared about one setting.
``set``       the value, to the right place: a governed flag to the DB store
              (live), a secret refused (``genus vault set`` owns those), and
              everything else to the ``settings:`` block of config.yaml.
``list``      every setting, or one group, or only what is configured.
``validate``  an alias for ``genus doctor``, which asks everything this
              command used to ask and a dozen more. Kept so existing runbooks
              and scripts keep working.
``schema``    the JSON Schema, for tooling.

A secret is never printed by any of them. ``get`` and ``list`` show
``<set, sha256:ab12cd34>`` — enough to compare two boxes without putting a
credential in a terminal, a screenshot or a scrollback buffer.
"""

from __future__ import annotations

import argparse  # noqa: TC003
import json
import sys

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
_digest = operator.digest
_display = operator.display
_db_rows = operator.db_rows
_db_value = operator.db_value

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
    """Exit code 2 and the closest declared names.

    A typo in a variable name is otherwise indistinguishable from a setting
    that does not exist yet, and both look like the command doing nothing.
    """
    _err(operator.unknown_name_message(name))
    return 2


# ── get / explain / list ─────────────────────────────────────────────────────


def _cmd_get(args: argparse.Namespace) -> int:
    record = _record(args.name)
    if record is None:
        return _unknown_name(args.name)

    value, source, detail = _resolve(record)
    shown = _display(record, value)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "name": record["env"],
                    "value": shown if record["secret"] else value,
                    "source": source,
                    "detail": detail,
                    "secret": record["secret"],
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return 0

    print(f"{record['env']} = {shown}")
    print(f"  source: {source} ({detail})")
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


def _set_result(applied: bool, pending: list[str], errors: list[str], as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {"applied": applied, "pending_restart": pending, "errors": errors},
                indent=2,
                sort_keys=True,
            )
        )
    elif errors:
        for error in errors:
            _err(error)
    elif pending:
        print(f"restart required: {', '.join(pending)}")
    else:
        print("applied")
    return 1 if errors else 0


def _cmd_set(args: argparse.Namespace) -> int:
    """Route one change by the field's own metadata, and say what it cost.

    The routing, the coercion, the refusals and the write all live in
    ``robothor.settings.operator``, which the bridge's ``PATCH /api/settings``
    calls too. What is left here is the CLI's half: an exit code and a line of
    text. A governed flag comes back with no units -- it resolves from the DB
    on the engine's next read (a five-second TTL), so it is live.
    """
    as_json = getattr(args, "json", False)
    record = _record(args.name)
    if record is None:
        return _unknown_name(args.name)

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

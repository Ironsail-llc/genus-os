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
import difflib
import hashlib
import json
import sys
from functools import lru_cache
from typing import Any

from robothor.settings import provenance
from robothor.settings.config_file import write_setting
from robothor.settings.provenance import (
    SOURCE_DEFAULT,
    SOURCE_ENV,  # noqa: F401 — re-exported for callers reading provenance labels
    SOURCE_FILE,  # noqa: F401
    SOURCE_RUNTIME,
)

__all__ = ["cmd_config"]

#: What a setting whose declaration names no units falls back to. It should
#: never be reached -- ``SettingsGroup`` stamps every field with its group's
#: units and ``tests/test_settings_registry.py`` fails if one is missing -- but
#: naming too FEW units is how a change reports applied and is not, so the
#: fallback is the conservative pair rather than nothing.
DEFAULT_UNITS: tuple[str, ...] = ("robothor-engine", "robothor-bridge")

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


def _record(name: str) -> dict[str, Any] | None:
    from robothor.settings.registry import field_index

    return field_index().get(name)


def _unknown_name(name: str) -> int:
    """Exit code 2 and the closest declared names.

    A typo in a variable name is otherwise indistinguishable from a setting
    that does not exist yet, and both look like the command doing nothing.
    """
    from robothor.settings.registry import field_index

    candidates = sorted({record["env"] for record in field_index().values()})
    close = difflib.get_close_matches(name.upper(), candidates, n=3, cutoff=0.6)
    _err(f"{name}: no such setting.")
    if close:
        _err("Did you mean: " + ", ".join(close) + "?")
    else:
        _err("`genus config list` shows every declared setting.")
    return 2


def _digest(value: str) -> str:
    """A short, stable fingerprint of a secret — never the secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _mask(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"<set, sha256:{_digest(text)}>" if text else "<unset>"


def _display(record: dict[str, Any], value: Any) -> str:
    return _mask(value) if record["secret"] else str(value)


@lru_cache(maxsize=1)
def _db_rows() -> dict[str, str]:
    """Operator-written ``feature_flags`` rows, read once.

    One query rather than one per flag: ``genus config list`` walks every
    declared setting, and twenty round trips (or twenty connection timeouts on
    a box whose database is down) is the difference between a command that
    answers and one an operator stops running.

    A database that is down or absent is not an error here -- flags then
    resolve from the environment, exactly as they do in the engine. Rows
    stamped by the migration seed are "unset", the same rule
    ``robothor.flags.store`` applies.
    """
    try:
        from robothor.db.connection import get_connection
        from robothor.flags.store import _SEED_ACTOR

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, value, updated_by FROM feature_flags")
            rows = cur.fetchall()
    except Exception:
        return {}
    return {name: value for name, value, updated_by in rows if updated_by != _SEED_ACTOR}


def _db_value(record: dict[str, Any]) -> str | None:
    """An operator-written ``feature_flags`` row for this field, or None."""
    return _db_rows().get(record["env"]) if record["governed"] else None


def _resolve(record: dict[str, Any]) -> tuple[Any, str, str]:
    """``(value, source, detail)`` — what the platform reads, and why.

    The DB layer is resolved here rather than in ``settings.provenance``: only
    governed flags have one, and the settings package has no business opening a
    database connection to answer what a variable is set to.
    """
    db = _db_value(record)
    if db is not None:
        return db, SOURCE_RUNTIME, f"feature_flags row for {record['env']}"
    return provenance.resolve(record)


def _units_for(record: dict[str, Any]) -> tuple[str, ...]:
    """The units declared on the field itself (see ``declare()``)."""
    declared = record["restart_units"]
    return DEFAULT_UNITS if declared is None else tuple(declared)


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


def _coerce(record: dict[str, Any], raw: Any) -> Any:
    """Validate ``raw`` against the field's own type, returning the value.

    The model does the coercing, so ``genus config set`` cannot write a value
    that would fail on the next start -- which is the failure this command
    exists to prevent, not to relocate. ``validate`` uses it for a second
    reason: a value from a YAML file is already typed and one from the
    environment is always text, so the only honest comparison of the two is
    the one made after both have been through the field.
    """
    from robothor.settings.model import GenusSettings

    group, field = record["field"].split(".", 1)
    model = GenusSettings.model_fields[group].annotation
    instance = model(**{field: raw})  # type: ignore[misc]
    return getattr(instance, field)


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
    as_json = getattr(args, "json", False)
    record = _record(args.name)
    if record is None:
        return _unknown_name(args.name)

    if record["secret"]:
        return _set_result(
            False,
            [],
            [
                f"{record['env']} holds a credential. `genus config set` never writes "
                "secrets -- config.yaml is a plain file that gets copied into bug "
                "reports. Store it with `genus vault set <key>` and give the service "
                "the key; `genus vault list` shows the naming in use."
            ],
            as_json,
        )

    if record["governed"]:
        from robothor.flags import store

        allowed = store.valid_values_for(record["env"])
        if args.value not in allowed:
            return _set_result(
                False,
                [],
                [
                    f"{record['env']}: {args.value!r} is not one of {', '.join(allowed)}. "
                    "The engine does not honour any other value, so storing it would "
                    "show one thing and do another."
                ],
                as_json,
            )
        actor = f"operator:{_operator_name()}"
        try:
            store.set_flag(record["env"], args.value, actor, "genus config set")
        except Exception as exc:  # a DB that is down must say so, not half-apply
            return _set_result(False, [], [f"{record['env']}: {exc}"], as_json)
        # Governed flags resolve from the DB on the engine's next read (a
        # five-second TTL), so this is live -- no restart, no file to edit.
        return _set_result(True, [], [], as_json)

    try:
        value = _coerce(record, args.value)
    except Exception as exc:
        first = str(exc).splitlines()[0]
        return _set_result(
            False, [], [f"{record['env']}: {args.value!r} is not a valid value ({first})"], as_json
        )

    from robothor.settings.sources import config_yaml_path

    path = config_yaml_path()
    if path is None:
        return _set_result(
            False, [], ["no workspace: set ROBOTHOR_WORKSPACE and try again"], as_json
        )

    group, field = record["field"].split(".", 1)
    spellings = (record["env"], *record["aliases"])
    try:
        # One writer, shared with the first-run wizard's operator step: two
        # implementations of "store a setting in config.yaml" would be two
        # opinions about indentation, comments and deprecated spellings, and a
        # surface that reports "applied" while the service reads something else.
        write_setting(group, field, value, names=spellings, path=path)
    except OSError as exc:
        return _set_result(False, [], [f"{path}: {exc}"], as_json)

    units = list(_units_for(record)) if record["restart_required"] else []
    return _set_result(True, units, [], as_json)


# ── validate (an alias for `genus doctor`) ───────────────────────────────────

#: Helpers the doctor's own checks call. ``_agree`` and ``_units_for`` stay
#: here because ``set`` and ``explain`` use them too; the checks that used to
#: live in this section moved to ``robothor/doctor/checks/`` whole, so that
#: `genus doctor`, the bridge's ``/api/doctor`` and this command all ask the
#: same questions rather than three similar ones.


def _agree(record: dict[str, Any], file_value: Any, env_value: str) -> bool:
    """Do a typed file value and a raw environment string mean the same thing?

    Falls back to comparing the text when either side will not coerce: a value
    the field cannot hold is a real disagreement worth reporting, and it is
    reported by the same line as any other.
    """
    try:
        return bool(_coerce(record, file_value) == _coerce(record, env_value))
    except Exception:
        return str(file_value) == env_value


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

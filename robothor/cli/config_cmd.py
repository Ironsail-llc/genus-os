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
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from robothor.settings import provenance
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


def _render(value: Any) -> str:
    """One YAML scalar, quoted only when it has to be."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    bare_ok = (
        text != ""
        and text.strip() == text
        and not any(ch in text for ch in ":#{}[],&*?|<>=!%@`\"'\n")
        and text.lower() not in {"true", "false", "null", "yes", "no", "on", "off", "~"}
    )
    try:
        float(text)
        bare_ok = False  # a string that looks like a number must stay a string
    except ValueError:
        pass
    return text if bare_ok else json.dumps(text)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _comment_at(text: str) -> int:
    """Index of the ``#`` that starts a YAML comment in ``text``, or -1.

    A ``#`` only opens a comment at the start of the scanned text or after
    whitespace: ``tag: a#b`` is the three-character value ``a#b``.
    """
    match = re.search(r"(?:^|\s)#", text)
    return -1 if match is None else match.end() - 1


def _inline_comment(stripped: str) -> str:
    """Any trailing ``# ...`` on a key line, as text to re-append.

    A ``#`` inside a quoted value is part of the value, and a comment can
    still follow the closing quote -- ``ai_name: "Ada"  # the boss`` is both
    at once. Treating the whole line as uncommentable dropped the operator's
    comment; treating the first ``#`` as the comment would have moved half
    their value into one. So a quoted value is scanned to its closing quote
    and only what follows is searched.

    An unterminated quote (a line a YAML parser would reject anyway) yields no
    comment: the write is going to replace the value, and inventing a comment
    boundary inside a broken string is the one outcome worse than losing it.
    """
    _key, _, value = stripped.partition(":")
    rest = value.lstrip()
    if rest[:1] not in {'"', "'"}:
        found = _comment_at(rest)
        return "" if found < 0 else "  " + rest[found:].rstrip()

    quote = rest[0]
    index = 1
    while index < len(rest):
        char = rest[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            if quote == "'" and rest[index + 1 : index + 2] == "'":
                index += 2  # '' is an escaped single quote, not the end
                continue
            break
        index += 1
    else:
        return ""
    tail = rest[index + 1 :]
    found = _comment_at(tail)
    return "" if found < 0 else "  " + tail[found:].rstrip()


def _splice(text: str, group: str, field: str, rendered: str, names: tuple[str, ...] = ()) -> str:
    """Set ``settings.<group>.<field>`` in ``text``, touching nothing else.

    A load-and-dump round trip through PyYAML (the only YAML library this
    platform ships -- ruamel is not a dependency, and adding one to change a
    single scalar is not a trade worth making) would reformat the file and
    delete every comment in it. config.yaml is a file operators hand-edit, so
    the edit is textual: find the line, replace the value, leave the rest of
    the bytes exactly as they were.

    ``names`` is every other spelling the field answers to -- its declared
    environment name and any deprecated alias, all legal keys here because the
    groups are ``populate_by_name``. An existing key under one of those is
    UPDATED IN PLACE, keeping the operator's spelling: writing the Python name
    beside it would leave one field configured twice in one mapping, which is
    a file whose meaning depends on which key pydantic reads last.
    """
    lines = text.splitlines()
    line = f"{field}: {rendered}"
    spellings = (field, *names)

    # 1. the settings: block
    start = next(
        (i for i, raw in enumerate(lines) if raw.rstrip() == "settings:" and _indent_of(raw) == 0),
        None,
    )
    if start is None:
        prefix = lines + ([""] if lines and lines[-1].strip() else [])
        return "\n".join([*prefix, "settings:", f"  {group}:", f"    {line}"]) + "\n"

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and _indent_of(lines[i]) == 0:
            end = i
            break

    body = [i for i in range(start + 1, end) if lines[i].strip()]
    step = _indent_of(lines[body[0]]) if body else 2

    # 2. the group within it
    header = next(
        (
            i
            for i in body
            if _indent_of(lines[i]) == step
            and lines[i].strip().rstrip(":") == group
            and lines[i].strip().endswith(":")
        ),
        None,
    )
    if header is None:
        insert = end
        while insert > start + 1 and not lines[insert - 1].strip():
            insert -= 1
        return (
            "\n".join(
                [
                    *lines[:insert],
                    f"{' ' * step}{group}:",
                    f"{' ' * step * 2}{line}",
                    *lines[insert:],
                ]
            )
            + "\n"
        )

    group_end = end
    for i in range(header + 1, end):
        if lines[i].strip() and _indent_of(lines[i]) <= step:
            group_end = i
            break
    inner = [i for i in range(header + 1, group_end) if lines[i].strip()]
    field_indent = _indent_of(lines[inner[0]]) if inner else step * 2

    # 3. the field within the group, under any spelling it answers to
    for i in inner:
        stripped = lines[i].strip()
        key = stripped.split(":", 1)[0]
        if _indent_of(lines[i]) == field_indent and key in spellings:
            lines[i] = f"{' ' * field_indent}{key}: {rendered}{_inline_comment(stripped)}"
            return "\n".join(lines) + "\n"

    insert = group_end
    while insert > header + 1 and not lines[insert - 1].strip():
        insert -= 1
    return "\n".join([*lines[:insert], f"{' ' * field_indent}{line}", *lines[insert:]]) + "\n"


#: Mode for a config.yaml this command creates. The file records how the
#: instance is wired -- ports, hosts, endpoints -- and nothing else on the box
#: needs to read it, so a new one starts private to the operator.
NEW_FILE_MODE = 0o600


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, or not at all.

    A half-written config.yaml is a box that will not start, and the write can
    be interrupted (a full disk, a reboot, a Ctrl-C). Same directory, so the
    rename cannot cross a filesystem boundary and stop being atomic.

    Rename replaces the file's identity, not just its contents, so the mode and
    the owner have to be carried across deliberately: a temp file takes the
    process umask and the process's own uid, and ``sudo genus config set``
    would otherwise hand the engine's config file to root and leave the service
    unable to write it again. An existing file keeps exactly the mode and
    owner it had; a new one is created private (:data:`NEW_FILE_MODE`).
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing: os.stat_result | None = path.stat()
    except OSError:
        existing = None

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config.yaml.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(NEW_FILE_MODE if existing is None else existing.st_mode & 0o7777)
        if existing is not None and os.geteuid() == 0:
            # Only root can give a file away; anyone else already owns it.
            os.chown(tmp, existing.st_uid, existing.st_gid)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    spellings = (record["env"], *record["aliases"])
    try:
        _write_atomically(path, _splice(text, group, field, _render(value), spellings))
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
    """
    from robothor.cli.doctor_cmd import cmd_doctor

    print(
        "genus config validate is deprecated; it now runs 'genus doctor', which checks "
        "more and can repair some of it.",
        file=sys.stderr,
    )
    return cmd_doctor(
        argparse.Namespace(
            json=getattr(args, "json", False),
            fix=False,
            dry_run=False,
            offline=False,
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

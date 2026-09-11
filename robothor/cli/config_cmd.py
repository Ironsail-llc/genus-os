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
``validate``  the connectivity checks plus the three ways a config file lies:
              keys nothing reads, names that moved, and a file that disagrees
              with the running process.
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

#: What to restart for a change to a given group's ``restart_required`` field.
#: The engine reads almost everything; the bridge and the dashboard read who
#: may talk to them and where the side services are. A group missing here gets
#: :data:`DEFAULT_UNITS` — the honest answer for a setting whose reader is not
#: pinned down, since naming too few units is how a change appears to apply and
#: does not.
GROUP_UNITS: dict[str, tuple[str, ...]] = {
    "engine": ("robothor-engine",),
    "flags": ("robothor-engine",),
    "providers": ("robothor-engine",),
    "ollama": ("robothor-engine",),
    "channels": ("robothor-engine",),
    "auth": ("robothor-bridge", "robothor-app"),
    "services": ("robothor-bridge", "robothor-app"),
    # Read by shell scripts and timers when they run, so there is nothing
    # holding a stale copy: the next invocation picks the new value up.
    "ops": (),
}
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
    return GROUP_UNITS.get(record["group"], DEFAULT_UNITS)


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


def _coerce(record: dict[str, Any], raw: str) -> Any:
    """Validate ``raw`` against the field's own type, returning the value.

    The model does the coercing, so ``genus config set`` cannot write a value
    that would fail on the next start -- which is the failure this command
    exists to prevent, not to relocate.
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


def _splice(text: str, group: str, field: str, rendered: str) -> str:
    """Set ``settings.<group>.<field>`` in ``text``, touching nothing else.

    A load-and-dump round trip through PyYAML (the only YAML library this
    platform ships -- ruamel is not a dependency, and adding one to change a
    single scalar is not a trade worth making) would reformat the file and
    delete every comment in it. config.yaml is a file operators hand-edit, so
    the edit is textual: find the line, replace the value, leave the rest of
    the bytes exactly as they were.
    """
    lines = text.splitlines()
    line = f"{field}: {rendered}"

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

    # 3. the field within the group
    for i in inner:
        stripped = lines[i].strip()
        if _indent_of(lines[i]) == field_indent and stripped.split(":", 1)[0] == field:
            comment = ""
            if "#" in stripped:
                comment = "  " + stripped[stripped.index("#") :].rstrip()
            lines[i] = f"{' ' * field_indent}{line}{comment}"
            return "\n".join(lines) + "\n"

    insert = group_end
    while insert > header + 1 and not lines[insert - 1].strip():
        insert -= 1
    return "\n".join([*lines[:insert], f"{' ' * field_indent}{line}", *lines[insert:]]) + "\n"


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, or not at all.

    A half-written config.yaml is a box that will not start, and the write can
    be interrupted (a full disk, a reboot, a Ctrl-C). Same directory, so the
    rename cannot cross a filesystem boundary and stop being atomic.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config.yaml.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
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
                f"secrets -- they would land in a world-readable config file. Use "
                f"`genus vault set {record['env'].lower()}` instead."
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
    try:
        _write_atomically(path, _splice(text, group, field, _render(value)))
    except OSError as exc:
        return _set_result(False, [], [f"{path}: {exc}"], as_json)

    units = list(_units_for(record)) if record["restart_required"] else []
    return _set_result(True, units, [], as_json)


# ── validate ─────────────────────────────────────────────────────────────────

#: A Telegram bot token is ``<digits>:<secret>``. Checking the shape is the
#: most that can be done without calling the API with the operator's token.
_TOKEN_PREFIX_DIGITS = 5


def _telegram_checks() -> list[tuple[str, str, str]]:
    """Telegram is optional. Configured badly is an error; absent is not.

    The daemon has always run without it -- agents with ``delivery: none``
    communicate through CRM tasks and notifications -- but validate demanded a
    bot token and a chat id, so every Telegram-free deploy failed a check it
    could never pass, and the operator learned to ignore the output.
    """
    from robothor.settings import get_settings

    channels = get_settings().channels
    token = channels.telegram_bot_token
    chat = channels.telegram_chat_id
    if not token and not chat:
        return [("telegram", "info", "not configured, delivery=none")]

    checks: list[tuple[str, str, str]] = []
    head, _, tail = token.partition(":")
    if not token:
        checks.append(
            ("telegram:token", "error", "a chat id is set but ROBOTHOR_TELEGRAM_BOT_TOKEN is not")
        )
    elif not (head.isdigit() and len(head) >= _TOKEN_PREFIX_DIGITS and tail):
        # The value itself is a credential even when it is malformed.
        checks.append(
            (
                "telegram:token",
                "error",
                "ROBOTHOR_TELEGRAM_BOT_TOKEN is not shaped like a bot token "
                "(expected <digits>:<secret>)",
            )
        )
    else:
        checks.append(("telegram:token", "pass", f"bot {head}"))

    if not chat:
        checks.append(
            ("telegram:chat", "error", "a bot token is set but ROBOTHOR_TELEGRAM_CHAT_ID is not")
        )
    elif chat.startswith("@") or chat.lstrip("-").isdigit():
        checks.append(("telegram:chat", "pass", chat))
    else:
        checks.append(
            (
                "telegram:chat",
                "error",
                f"ROBOTHOR_TELEGRAM_CHAT_ID={chat!r} is neither a numeric id nor an @name",
            )
        )
    return checks


def _unknown_key_checks() -> list[tuple[str, str, str]]:
    from robothor.settings.sources import config_yaml_path, strict_mode, unknown_config_keys

    try:
        unknown = unknown_config_keys()
    except ValueError as exc:  # malformed file: name it and stop there
        return [("config.yaml", "error", str(exc))]
    if not unknown:
        return [("config.yaml:keys", "pass", f"no unknown keys ({config_yaml_path()})")]
    mode = strict_mode()
    return [
        (
            f"config.yaml:{key}",
            "error",
            f"unknown setting {key!r} -- nothing reads it"
            + (
                " and ROBOTHOR_CONFIG_STRICT_MODE=enforce will refuse to start"
                if mode == "enforce"
                else ""
            ),
        )
        for key in unknown
    ]


def _alias_checks() -> list[tuple[str, str, str]]:
    from robothor.settings.aliases import DEPRECATED_ALIASES

    checks = [
        (f"deprecated:{old}", "warn", f"{old} is deprecated and still set; use {new}")
        for old, new in provenance.deprecated_names_in_use()
    ]
    for group, values in provenance.settings_block().items():
        if not isinstance(values, dict):
            continue
        checks.extend(
            (
                f"deprecated:{group}.{key}",
                "warn",
                f"{key} in config.yaml is deprecated; use {DEPRECATED_ALIASES[key]}",
            )
            for key in values
            if key in DEPRECATED_ALIASES
        )
    return checks


def _pending_restart_checks() -> list[tuple[str, str, str]]:
    """Settings whose file value is not what this process was started with.

    Only ``restart_required`` fields can be in this state: a hot field is read
    again on the next use. The comparison is against the environment, which is
    what a service was started from -- so a value edited into config.yaml after
    the engine came up, or one being overridden by a stale variable in
    ``/etc/robothor/robothor.env``, shows up here instead of looking applied.
    """
    from robothor.settings.registry import field_index

    checks = []
    seen: set[str] = set()
    for record in field_index().values():
        if record["env"] in seen or not record["restart_required"]:
            continue
        seen.add(record["env"])
        present, raw = provenance.file_value(record)
        if not present:
            continue
        env_name = provenance.env_name_in_use(record)
        if env_name is None:
            continue
        running = provenance.running_env_value(env_name) or ""
        if str(raw) == running:
            continue
        shown_file = "<set>" if record["secret"] else repr(raw)
        shown_env = "<set, differing>" if record["secret"] else repr(running)
        checks.append(
            (
                f"pending restart:{record['env']}",
                "error",
                f"config.yaml says {shown_file}, this process reads {shown_env} from "
                f"{env_name} -- restart {', '.join(_units_for(record)) or 'the reader'} "
                "after clearing the variable",
            )
        )
    return checks


def _cmd_validate(args: argparse.Namespace) -> int:
    from robothor.config import validate as connectivity_checks

    checks: list[tuple[str, str, str]] = []
    checks.extend(_telegram_checks())
    checks.extend(_unknown_key_checks())
    checks.extend(_alias_checks())
    checks.extend(_pending_restart_checks())
    checks.extend(
        (name, "pass" if ok else "error", detail) for name, ok, detail in connectivity_checks()
    )

    errors = [f"{name}: {detail}" for name, status, detail in checks if status == "error"]
    pending = [
        name.split(":", 1)[1] for name, _s, _d in checks if name.startswith("pending restart:")
    ]

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "checks": [
                        {"name": name, "status": status, "detail": detail}
                        for name, status, detail in checks
                    ],
                    "errors": errors,
                    "pending_restart": pending,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if errors else 0

    icons = {"pass": "\033[32m✓\033[0m", "error": "\033[31m✗\033[0m", "warn": "!", "info": "·"}
    for name, status, detail in checks:
        print(f"  {icons.get(status, '?')} {name}: {detail}")
    passed = sum(1 for _n, status, _d in checks if status == "pass")
    print(f"\n{passed} passed, {len(errors)} failed")
    return 1 if errors else 0


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

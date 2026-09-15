"""The operator's settings verbs — once, for every surface that offers them.

``genus config get|set|explain|list`` grew these: look a name up in the
registry, resolve what the platform actually reads and from which layer, coerce
a value against the field's own type, and route a change to the place that
field is actually stored — the flag store for a governed guardrail, the vault
for a credential, the ``settings:`` block of config.yaml for everything else.

The Helm's Config and Flags pages have to do the same four things. Doing them
again in ``crm/bridge/routers/settings.py`` would have been a second opinion
about what a value means, where it goes and what has to be restarted
afterwards — and this repo's recurring defect is exactly that: two surfaces
answering one question differently, with tests on both sides certifying each
answer. So the logic lives here and both callers import it.
``crm/bridge/tests/test_settings_router.py`` asserts the identity of the
function objects, not their similarity.

Why in ``robothor/settings/`` rather than beside either caller:

* ``tests/test_settings_registry.py`` ratchets raw ``os.environ`` reads outside
  this package. Resolution needs them (precedence is *about* the environment),
  and this is where the platform has agreed they belong.
* A library the bridge imports must not import the bridge, and a library the
  CLI imports must not import the CLI. Sitting under the model both already
  depend on is the only placement where neither is possible.

Deliberately free of ``pydantic_settings`` at import time: ``robothor.cli``
imports this module transitively, and ``tests/test_settings_registry.py``
asserts in a subprocess that ``genus --help`` does not pay for pydantic. The
model, the flag store and the database are all imported inside the functions
that need them.

No value of a secret is returned by anything here except
:func:`secret_status`, which returns a fingerprint and a boolean.
"""

from __future__ import annotations

import difflib
import hashlib
from functools import lru_cache
from typing import Any

from robothor.settings import provenance
from robothor.settings.config_file import write_setting
from robothor.settings.provenance import (
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_FILE,
    SOURCE_RUNTIME,
)

__all__ = [
    "DEFAULT_UNITS",
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "SOURCE_FILE",
    "SOURCE_RUNTIME",
    "SettingError",
    "agree",
    "apply_change",
    "coerce",
    "db_rows",
    "db_value",
    "digest",
    "display",
    "env_override",
    "mask",
    "record",
    "reset_db_rows",
    "resolve",
    "secret_status",
    "units_for",
    "unknown_name_message",
    "validate",
]

#: What a setting whose declaration names no units falls back to. It should
#: never be reached -- ``SettingsGroup`` stamps every field with its group's
#: units and ``tests/test_settings_registry.py`` fails if one is missing -- but
#: naming too FEW units is how a change reports applied and is not, so the
#: fallback is the conservative pair rather than nothing.
DEFAULT_UNITS: tuple[str, ...] = ("robothor-engine", "robothor-bridge")


class SettingError(Exception):
    """A change that must not be written, carrying the sentence to show.

    One exception type for every refusal — an unwritable secret, a value a
    governed flag does not honour, a type the field cannot hold, an
    unreachable file — because every caller does the same thing with all of
    them: show the operator the sentence and change nothing. The CLI prints
    ``message`` to stderr; the bridge returns ``{name, message}``.
    """

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name
        self.message = message


# ── looking a setting up ─────────────────────────────────────────────────────


def record(name: str) -> dict[str, Any] | None:
    """The declaration for ``name``, under its own spelling or a deprecated one."""
    from robothor.settings.registry import field_index

    return field_index().get(name)


def unknown_name_message(name: str) -> str:
    """Why ``name`` is not a setting, and what the operator probably meant.

    A typo in a variable name is otherwise indistinguishable from a setting
    that does not exist yet, and both look like the command doing nothing.
    """
    from robothor.settings.registry import field_index

    candidates = sorted({row["env"] for row in field_index().values()})
    close = difflib.get_close_matches(name.upper(), candidates, n=3, cutoff=0.6)
    if close:
        return f"{name}: no such setting. Did you mean: " + ", ".join(close) + "?"
    return f"{name}: no such setting. `genus config list` shows every declared setting."


def units_for(row: dict[str, Any]) -> tuple[str, ...]:
    """The units declared on the field itself (see ``settings.model.declare``)."""
    declared = row["restart_units"]
    return DEFAULT_UNITS if declared is None else tuple(declared)


# ── showing a value without showing a credential ─────────────────────────────


def digest(value: str) -> str:
    """A short, stable fingerprint of a secret — never the secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def mask(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"<set, sha256:{digest(text)}>" if text else "<unset>"


def display(row: dict[str, Any], value: Any) -> str:
    return mask(value) if row["secret"] else str(value)


def secret_status(row: dict[str, Any]) -> dict[str, Any]:
    """``{configured, fingerprint}`` for a credential — the whole of what an API
    may say about one.

    Enough to compare two boxes, or to see that the value changed, without
    putting a credential in a browser tab, a screenshot or a proxy log. The
    fingerprint is the same eight hex characters ``genus config get`` prints,
    so the two surfaces can be compared by eye.
    """
    value, _source, _detail = resolve(row)
    text = "" if value is None else str(value)
    return {
        "configured": bool(text),
        "fingerprint": f"sha256:{digest(text)}" if text else None,
    }


# ── resolving what the platform actually reads ───────────────────────────────


@lru_cache(maxsize=1)
def _db_rows() -> dict[str, str]:
    """Operator-written ``feature_flags`` rows, read once.

    One query rather than one per flag: a caller listing every declared setting
    walks 300+ of them, and 300 round trips (or 300 connection timeouts on a
    box whose database is down) is the difference between a surface that
    answers and one an operator stops opening.

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


def db_rows() -> dict[str, str]:
    """The cached ``feature_flags`` snapshot; see :func:`reset_db_rows`."""
    return _db_rows()


def reset_db_rows() -> None:
    """Drop the snapshot so the next read re-queries.

    The CLI never needs this: a ``genus config`` invocation is one question and
    then exit. A long-running process does — the bridge serves ``/api/controls``
    writes and ``/api/settings`` reads from the same process, and a snapshot
    taken at boot would report a flag the operator flipped an hour ago as
    unset. Each request resets, so the cache is per-request rather than
    per-process.
    """
    _db_rows.cache_clear()


def db_value(row: dict[str, Any]) -> str | None:
    """An operator-written ``feature_flags`` row for this field, or None."""
    return db_rows().get(row["env"]) if row["governed"] else None


def resolve(row: dict[str, Any]) -> tuple[Any, str, str]:
    """``(value, source, detail)`` — what the platform reads, and why.

    The DB layer is resolved here rather than in ``settings.provenance``: only
    governed flags have one, and the settings package proper has no business
    opening a database connection to answer what a variable is set to.
    """
    db = db_value(row)
    if db is not None:
        return db, SOURCE_RUNTIME, f"feature_flags row for {row['env']}"
    return provenance.resolve(row)


def env_override(row: dict[str, Any]) -> str | None:
    """The environment variable currently supplying this field, or None.

    Non-None means a file write would be invisible: the environment sits above
    config.yaml in the documented precedence, so the value the operator just
    saved would not be the value the service reads. A surface that cannot edit
    the environment (the Helm) refuses; one that can (the CLI, run on the box)
    writes the file and the doctor reports the disagreement.
    """
    return provenance.env_name_in_use(row)


# ── validating and applying a change ─────────────────────────────────────────


def coerce(row: dict[str, Any], raw: Any) -> Any:
    """Validate ``raw`` against the field's own type, returning the value.

    The model does the coercing, so a surface cannot write a value that would
    fail on the next start -- which is the failure these commands exist to
    prevent, not to relocate. ``validate`` in the doctor uses it for a second
    reason: a value from a YAML file is already typed and one from the
    environment is always text, so the only honest comparison of the two is
    the one made after both have been through the field.
    """
    from robothor.settings.model import GenusSettings

    group, field = row["field"].split(".", 1)
    model = GenusSettings.model_fields[group].annotation
    instance = model(**{field: raw})  # type: ignore[misc]
    return getattr(instance, field)


def agree(row: dict[str, Any], file_value: Any, env_value: str) -> bool:
    """Do a typed file value and a raw environment string mean the same thing?

    Falls back to comparing the text when either side will not coerce: a value
    the field cannot hold is a real disagreement worth reporting, and it is
    reported by the same line as any other.
    """
    try:
        return bool(coerce(row, file_value) == coerce(row, env_value))
    except Exception:
        return str(file_value) == env_value


def validate(row: dict[str, Any], raw: Any) -> Any:
    """The value to write, or a :class:`SettingError` saying why there is none.

    Writes nothing and touches nothing: every caller that applies a batch
    atomically needs the whole set checked before the first byte is written.

    Raises:
        SettingError: the field holds a credential, the value is not one a
            governed flag honours, or the field's own type rejects it.
    """
    if row["secret"]:
        raise SettingError(
            row["env"],
            f"{row['env']} holds a credential. `genus config set` never writes "
            "secrets -- config.yaml is a plain file that gets copied into bug "
            "reports. Store it with `genus vault set <key>` and give the service "
            "the key; `genus vault list` shows the naming in use.",
        )

    if row["governed"]:
        from robothor.flags import store

        allowed = store.valid_values_for(row["env"])
        if raw not in allowed:
            raise SettingError(
                row["env"],
                f"{row['env']}: {raw!r} is not one of {', '.join(allowed)}. "
                "The engine does not honour any other value, so storing it would "
                "show one thing and do another.",
            )
        return raw

    try:
        return coerce(row, raw)
    except Exception as exc:
        first = str(exc).splitlines()[0]
        raise SettingError(
            row["env"], f"{row['env']}: {raw!r} is not a valid value ({first})"
        ) from exc


def apply_change(row: dict[str, Any], value: Any, *, actor: str, reason: str) -> tuple[str, ...]:
    """Store ``value`` where this field actually lives. Returns the units to restart.

    Routed by the field's own metadata rather than by what the caller typed:

    * **governed** -- the ``feature_flags`` store, which the engine re-reads on
      a five-second TTL. Live, so no units come back.
    * **everything else** -- the ``settings:`` block of config.yaml, through the
      one writer (:func:`robothor.settings.config_file.write_setting`), which
      edits textually, preserves comments and unknown keys, and replaces the
      file atomically. The declared units come back when the field says a
      restart is required.

    Secrets never reach here; :func:`validate` refuses them first.

    Raises:
        SettingError: the flag store could not be written, no workspace
            resolves, or the file could not be replaced.
    """
    if row["governed"]:
        from robothor.flags import store

        try:
            store.set_flag(row["env"], value, actor, reason)
        except Exception as exc:  # a DB that is down must say so, not half-apply
            raise SettingError(row["env"], f"{row['env']}: {exc}") from exc
        return ()

    from robothor.settings.sources import config_yaml_path

    path = config_yaml_path()
    if path is None:
        raise SettingError(row["env"], "no workspace: set ROBOTHOR_WORKSPACE and try again")

    group, field = row["field"].split(".", 1)
    try:
        # One writer, shared with the first-run wizard's operator step: two
        # implementations of "store a setting in config.yaml" would be two
        # opinions about indentation, comments and deprecated spellings, and a
        # surface that reports "applied" while the service reads something else.
        write_setting(group, field, value, names=(row["env"], *row["aliases"]), path=path)
    except OSError as exc:
        raise SettingError(row["env"], f"{path}: {exc}") from exc

    return units_for(row) if row["restart_required"] else ()

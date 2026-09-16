"""DB-backed resolution for the governed guardrail flags.

Resolution order, never violated: operator DB row -> os.environ -> None.

A DB that is *unreachable* falls through to env — it never returns None (which a
caller would read as off). Only an operator-written row overrides env; a bare
migration seed row is treated as "unset" so the env->DB cutover is a no-op.
"""

from __future__ import annotations

import os
import threading
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from robothor.db.connection import get_connection

if TYPE_CHECKING:  # pragma: no cover - typing only
    #: Declared for type checkers; supplied at runtime by ``__getattr__``.
    GOVERNED_FLAGS: frozenset[str]


@lru_cache(maxsize=1)
def governed_flags() -> frozenset[str]:
    """Every flag an operator may govern, derived from the settings registry.

    A flag is governed when its field declares ``governed=True`` in
    ``robothor.settings.model`` -- which also means it is inventoried in
    ``infra/flags.yaml`` and read through this store. That was a hand-written
    set here until 1.66, and it had drifted: sixteen of its twenty names were
    declared in no settings field at all (they reach ``os.environ`` through a
    variable, so the registry's guard test could not see them), while fourteen
    fields marked ``governed=True`` were in neither this set nor the manifest.
    Deriving it means the three lists cannot disagree again; the reconciliation
    is pinned in ``tests/test_flag_registry_single_source.py``.

    Lazy on purpose. This module sits on the engine's hot path and is imported
    by the bridge; the settings model pulls all of pydantic-settings, and no
    process should pay that just to resolve a flag it may never read.
    """
    from robothor.settings.registry import field_index

    return frozenset(record["env"] for record in field_index().values() if record["governed"])


def __getattr__(name: str) -> Any:
    """Keep ``store.GOVERNED_FLAGS`` working as the module constant it was.

    Both spellings callers already use -- ``from robothor.flags.store import
    GOVERNED_FLAGS`` and ``store.GOVERNED_FLAGS`` -- route through here, so the
    derivation stays lazy without a single call site changing.
    """
    if name == "GOVERNED_FLAGS":
        return governed_flags()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_MODE_VALUES: tuple[str, ...] = ("off", "observe", "alert", "enforce")

#: ``ROBOTHOR_CALENDAR_SEND_UPDATES`` is the one governed flag that is not a
#: ladder at all: it names Google's ``sendUpdates`` audience directly. Without
#: an entry here it fell through to the four-rung ladder, so the one posture it
#: exists for — ``none``, "stop mailing my attendees" — was a 422 from Controls
#: while ``off`` was accepted, stored, and then silently read back as ``all`` by
#: the engine. An operator seeing a value saved and not honoured is exactly what
#: this function's docstring says must not happen.
_CALENDAR_SEND_UPDATES_VALUES: tuple[str, ...] = ("all", "externalOnly", "none")
_RIP_13_VALUES: tuple[str, ...] = ("observe", "enforce")
_HONESTY_SUITE_VALUES: tuple[str, ...] = ("off", "observe", "enforce")

#: ``ROBOTHOR_DNC_MODE`` shares that two-rung shape for a different reason:
#: it is a compliance opt-out, so it has no ``off``, and ``observe`` already
#: writes the guardrail-event row an ``alert`` rung would page on.
_TWO_RUNG_MODE_FLAGS: frozenset[str] = frozenset({"ROBOTHOR_RIP_13_MODE", "ROBOTHOR_DNC_MODE"})
_BOOL_VALUES: tuple[str, ...] = ("true", "false")

#: Flags on the three-rung ``off``/``observe``/``enforce`` ladder — no ``alert``,
#: because neither blocks an action there would be anything to page about.
#: ``ROBOTHOR_PER_USER_SESSIONS`` is an isolation switch (whose chat session a
#: caller lands on) and ``feature_flags.per_user_sessions_mode`` maps every
#: unrecognised value onto ``enforce``, so offering ``alert`` would let an
#: operator set a rung, see it stored, and get a different one.
#: ``ROBOTHOR_STEP_EFFICIENCY_MODE`` joins them as a pacing aid: it blocks no
#: operator-visible action, so an "alert" rung would page on nothing.
_THREE_RUNG_MODE_FLAGS: frozenset[str] = frozenset(
    {
        "ROBOTHOR_HONESTY_SUITE_MODE",
        "ROBOTHOR_PER_USER_SESSIONS",
        "ROBOTHOR_STEP_EFFICIENCY_MODE",
    }
)


def valid_values_for(name: str) -> tuple[str, ...]:
    """The single source of truth for what a governed flag may be set to.

    Boolean flags (``*_ENABLED``) accept ``true``/``false``. ``ROBOTHOR_RIP_13_MODE``
    and ``ROBOTHOR_DNC_MODE`` are mode flags that only honor ``observe``/``enforce``
    — the engine maps any other value onto one of those, so the API must not accept
    the full mode ladder for them. Offering a rung the engine does not honor lets an
    operator set it, see it stored, and get different behaviour.
    ``_THREE_RUNG_MODE_FLAGS`` are the same shape one rung wider
    (``off``/``observe``/``enforce``): neither blocks an action, so neither has a
    "would have blocked" event to page about — ``ROBOTHOR_HONESTY_SUITE_MODE`` is
    a grader (``feature_flags.honesty_suite_mode``) and
    ``ROBOTHOR_PER_USER_SESSIONS`` decides which session a caller lands on
    (``feature_flags.per_user_sessions_mode``).
    ``ROBOTHOR_CALENDAR_SEND_UPDATES`` is not a ladder: its values are Google's
    own ``sendUpdates`` audiences (``all``/``externalOnly``/``none``).
    Every other ``*_MODE`` flag accepts the full ladder: ``off``/``observe``/``alert``/``enforce``.

    Both the bridge's write-path validation (422 on an out-of-range value) and
    its read-path payload (``valid_values`` per flag, so the frontend doesn't
    hand-mirror this rule) import this function rather than duplicating the
    logic.
    """
    if name.endswith("_ENABLED"):
        return _BOOL_VALUES
    if name in _TWO_RUNG_MODE_FLAGS:
        return _RIP_13_VALUES
    if name in _THREE_RUNG_MODE_FLAGS:
        return _HONESTY_SUITE_VALUES
    if name == "ROBOTHOR_CALENDAR_SEND_UPDATES":
        return _CALENDAR_SEND_UPDATES_VALUES
    return _MODE_VALUES


_SEED_ACTOR = "migration-084"
_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _read_db(name: str) -> str | None:
    """Return the operator-written value, or None if only a seed row / no row.

    Raises on connection failure — the caller MUST fall through to env, never
    treat a DB outage as 'off'.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value, updated_by FROM feature_flags WHERE name = %s", (name,))
        row = cur.fetchone()
    if row is None:
        return None
    value: str | None
    value, updated_by = row
    if updated_by == _SEED_ACTOR:
        return None  # seed row == "unset", let env win during cutover
    return value


def resolve(name: str) -> str | None:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1] if hit[1] is not None else os.environ.get(name)
    db_val: str | None
    try:
        db_val = _read_db(name)
    except Exception:
        db_val = None  # DB unreachable -> fall through to env below
    with _lock:
        _cache[name] = (now, db_val)
    return db_val if db_val is not None else os.environ.get(name)


#: Flags whose engine default deliberately differs from the value their settings
#: field declares. ``ROBOTHOR_DNC_MODE`` is declared ``observe`` like every other
#: ladder, and ``feature_flags.do_not_contact_mode`` floors it at ``enforce``
#: because a compliance opt-out has no dark rung. Only that class of flag belongs
#: here: an entry that merely repeats the declaration is dead weight, and
#: ``crm/bridge/tests/test_controls_unset_defaults.py`` fails on one.
#:
#: Lives here rather than in a router because THREE surfaces need the answer --
#: the Controls page, the Settings page and ``genus config get`` -- and the one
#: that had it was a bridge module the other two must not import.
_UNSET_DEFAULTS: dict[str, str] = {"ROBOTHOR_DNC_MODE": "enforce"}


def _declared_default(name: str) -> str | None:
    """What ``robothor/settings/model.py`` says this flag is when nobody sets it.

    One source, read rather than restated. The registry is also where
    ``GOVERNED_FLAGS`` itself comes from, so every flag a page can render has a
    declared default by construction.
    """
    try:
        from robothor.settings.registry import field_index

        record = field_index().get(name)
    except Exception:  # noqa: BLE001 — a page must render without the model
        return None
    if record is None:
        return None
    declared = record.get("default")
    if isinstance(declared, bool):
        return "true" if declared else "false"
    text = str(declared or "").strip()
    return text or None


def default_value_for(name: str) -> str:
    """A flag-appropriate "unset" default — what the ENGINE runs, not a guess.

    Reached only when there is neither a DB row nor an environment variable
    (:func:`resolve` covers both), so the answer is the flag's own hardcoded
    default and every surface must show exactly that. It is DERIVED from the
    settings registry rather than inferred from the shape of the value set: the
    old rule ("boolean → false, else observe") was right for every flag that
    starts dark and gets promoted, and it silently became wrong the first time a
    governed flag shipped at ``enforce``, so a page would have contradicted the
    engine with no test noticing.

    :data:`_UNSET_DEFAULTS` still wins, for the flags whose engine accessor
    deliberately ignores the declared value. The heuristic survives only as the
    last resort for a declaration that is empty or outside the value set --
    ``ROBOTHOR_SANDBOX_DEFAULT_MODE`` declares ``""``.
    """
    if name in _UNSET_DEFAULTS:
        return _UNSET_DEFAULTS[name]
    valid = valid_values_for(name)
    declared = _declared_default(name)
    if declared is not None and declared in valid:
        return declared
    return "false" if "false" in valid else "observe"


#: What the ENGINE counts as on. ``robothor/engine/feature_flags.py`` imports
#: this rather than declaring its own, and so does ``scripts/flag_audit.py``:
#: there were three copies, and the moment a page grew its own fourth reading
#: of the same variable it reported a guardrail OFF while the engine ran it.
#: ``feature_flags.py``'s own docstring tells operators to write
#: ``systemctl set-environment ROBOTHOR_RIP_1_ENABLED=1``, so ``1`` is not an
#: edge case — it is the documented spelling.
TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def normalise(name: str, value: Any) -> str:
    """One flag value, spelled the way THE ENGINE reads one.

    Two jobs, and both exist because a surface must never contradict the
    process it is describing:

    * **Spelling.** The settings model types ``ROBOTHOR_RIP_1_ENABLED`` as a
      ``bool``, so its declared default resolves to Python ``False`` — not a
      member of ``valid_values_for``, so a form cannot preselect it and a PATCH
      of it is refused. A page that serves a value its own enum rejects is a
      form that cannot save what it displays.
    * **Parsing.** Every engine accessor does ``.strip().lower()`` before it
      reads, and a boolean flag is ``raw in TRUE_VALUES`` — so ``1``, ``yes``,
      ``on``, ``TRUE`` are all on, and ``Observe`` is ``observe``. Normalising
      without those rules is how a guardrail an operator set to ``1`` was
      reported as ``false`` on two pages at once.

    Anything still outside the value set falls back to
    :func:`default_value_for`, which mirrors what each accessor does with a
    value it does not recognise: a typo in a variable must not appear to have
    set a rung.
    """
    if value is True:
        text = "true"
    elif value is False:
        text = "false"
    else:
        text = str(value if value is not None else "").strip().lower()

    valid = valid_values_for(name)
    if valid == _BOOL_VALUES and text:
        # The engine reads a boolean flag as membership of TRUE_VALUES, so
        # everything it does not recognise is off -- not "unset".
        return "true" if text in TRUE_VALUES else "false"
    return text if text in valid else default_value_for(name)


def set_flag(name: str, value: str, actor: str, reason: str) -> None:
    if name not in governed_flags():
        raise ValueError(f"{name} is not a governed flag")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM feature_flags WHERE name = %s", (name,))
        prev = cur.fetchone()
        from_value = prev[0] if prev else None
        cur.execute(
            "INSERT INTO feature_flags (name, value, updated_by, reason) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
            "updated_by = EXCLUDED.updated_by, updated_at = now(), reason = EXCLUDED.reason",
            (name, value, actor, reason),
        )
        cur.execute(
            "INSERT INTO feature_flag_audit (name, from_value, to_value, actor, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, from_value, value, actor, reason),
        )
        cur.execute("NOTIFY feature_flags")
        conn.commit()
    invalidate()


def start_listener() -> None:
    """Hook for the daemon to attach a ``LISTEN feature_flags`` connection that
    calls :func:`invalidate` on notification, so writes on one process are
    picked up by others faster than the TTL alone.

    The TTL cache is the correctness guarantee (stale reads self-heal within
    ``_TTL_SECONDS``); this listener is a latency optimization on top of it.
    The full LISTEN/NOTIFY wiring — background thread, reconnect-on-drop — is
    built and exercised end-to-end in Task 7. Present now so callers have a
    stable import target during the cutover.
    """
    raise NotImplementedError("start_listener is wired up by the daemon in Task 7")

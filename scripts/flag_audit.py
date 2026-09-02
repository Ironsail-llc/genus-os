#!/usr/bin/env python3
"""The flag truth table as a command: which layer actually governs each
guardrail flag, and whether there is evidence it ever fired.

Four layers can set the same flag on a running instance, and until this script
existed nothing printed which one won:

1. ``infra/flags.yaml`` — INTENT. Owner, production mode, promotion deadline.
   It sets nothing; it records what the operator believes is running.
2. the systemd drop-ins — ``Environment=NAME=VALUE`` in
   ``/etc/systemd/system/robothor-engine.service.d/*.conf``. Versioned in the
   repo, drift-checked daily.
3. ``/etc/robothor/robothor.env`` — instance data, unversioned, and applied by
   systemd via ``EnvironmentFile=`` AFTER the drop-in's ``Environment=``
   directives, so a flag set in both is governed by this file. A flip applied
   to the drop-in then does nothing, silently (2026-07-25).
4. a ``feature_flags`` row — beats the process environment entirely for every
   name in ``robothor.flags.store.GOVERNED_FLAGS`` (``store.resolve``), unless
   it is the migration-084 seed row, which means "unset".

``check_dropin_drift.sh`` catches exactly one of the five ways these disagree
(a flag named in both the env file and the drop-in). It cannot see a DB pin, it
cannot see a flag that only exists in the env file, and it never compares any
of them against the manifest. This does all of that, from the artifact that
actually executes — the engine's own ``/proc/<MainPID>/environ`` — and exits 1
when a layer is being shadowed or the running process disagrees with the
manifest, so ``guardrail_watch`` nags about it daily.

Read-only by design: SELECTs only, no writes to ``/etc``, the database or the
running engine. It degrades rather than lying — no database prints ``?`` in
the evidence columns and says so; an unreadable ``/proc`` environ falls back to
the file layers and labels them as such.

Usage::

    scripts/flag_audit.py                 # aligned table, exit 1 on drift
    scripts/flag_audit.py --json          # same data, machine-readable
    scripts/flag_audit.py --no-db         # file layers only, no DB needed
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FLAG_MANIFEST = REPO_ROOT / "infra" / "flags.yaml"
FEATURE_FLAGS_SOURCE = REPO_ROOT / "robothor" / "engine" / "feature_flags.py"

# Run as `scripts/flag_audit.py`, sys.path[0] is scripts/, so `import robothor`
# would resolve to whatever copy happens to be installed rather than the tree
# this script ships with — and the audited flag set would silently be someone
# else's. The manifest, the gate map and GOVERNED_FLAGS must all come from one
# tree or the table describes a system that does not exist.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Instance paths. Platform-hardcoded like ``.robothor/`` — every instance puts
#: them here — and every one is overridable from the command line so the tests
#: (and a second instance) never touch the live box.
DEFAULT_ENV_FILE = Path("/etc/robothor/robothor.env")
DEFAULT_DROPIN_DIR = Path("/etc/systemd/system/robothor-engine.service.d")
ENGINE_UNIT = "robothor-engine"

#: Panic switches and self-test hooks. Not guardrails: they are debug state
#: that changes what every other flag does, and leaving one set is how a fleet
#: runs dark. Audited only when actually present on the box.
DEBUG_ENV_KEYS: tuple[str, ...] = (
    "ROBOTHOR_ALERT_SELFTEST",
    "ROBOTHOR_DISABLE_ALL_RIPS",
    "ROBOTHOR_DISABLE_ALL_GUARDRAILS",
)

#: The global kill switch ``_enforcement_mode`` consults before anything else.
PANIC_KEY = "ROBOTHOR_DISABLE_ALL_RIPS"

#: ``updated_by`` values written by an operator-facing surface.
#:
#: ``robothor.flags.store.set_flag`` is called from exactly one place —
#: ``crm/bridge/routers/controls.py``, whose ``require_operator`` returns
#: ``f"operator:{auth.actor_id}"`` — so a row stamped with one of these is a
#: deliberate Controls-dashboard flip: the supported way to govern a flag, and
#: the one layer an operator can see and change without editing /etc. Tagging
#: it SHADOW-LAYER made every legitimate flip page every morning forever,
#: which is how a daily nag becomes wallpaper. The bare names are accepted
#: alongside the prefix so a future CLI or admin surface reads as operator
#: intent the day it lands rather than as an anonymous pin.
OPERATOR_ACTOR_PREFIXES: tuple[str, ...] = ("operator:",)
OPERATOR_ACTORS: frozenset[str] = frozenset({"operator", "dashboard", "controls-api"})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Pre-promotion modes, same definition guardrail_watch.overdue_flags uses.
PENDING_MODES = ("observe", "alert")


# ── Layer parsers ───────────────────────────────────────────────────────────


def parse_environ_blob(data: bytes) -> dict[str, str]:
    """Parse a raw ``/proc/<pid>/environ`` blob (NUL-separated ``K=V``)."""
    out: dict[str, str] = {}
    for entry in data.split(b"\0"):
        if not entry:
            continue
        text = entry.decode("utf-8", "replace")
        name, sep, value = text.partition("=")
        if sep:
            out[name] = value
    return out


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a systemd ``EnvironmentFile=`` in the shape this instance writes.

    Deliberately not a shell parser: systemd's own EnvironmentFile syntax is
    ``NAME=VALUE`` per line with ``#`` comments, and pretending to evaluate
    shell here would report values the engine never received.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name or not name.replace("_", "").isalnum():
            continue
        out[name] = value.strip().strip('"').strip("'")
    return out


def parse_dropin_dir(path: Path) -> dict[str, str]:
    """Every ``Environment=NAME=VALUE`` under a drop-in directory.

    Only ``*.conf`` is read, because only ``*.conf`` is what systemd loads. The
    live directory carries ``upgrade-rip-flags.conf.bak-*`` files; treating one
    as a layer would invent state the engine has never seen.

    One directive may carry SEVERAL assignments — ``Environment=A=1 B=2`` and
    the quoted ``Environment="A=1" "B=2"`` are both valid systemd and both set
    two variables. Partitioning on the first ``=`` read those as one flag whose
    value was ``1 B=2``: the second flag was invisible to the audit and the
    first was reported with a value the engine never received. systemd splits
    the directive with shell-like word splitting, so ``shlex`` is the parser
    that agrees with it (and it strips the quotes on the way).
    """
    out: dict[str, str] = {}
    if not path.is_dir():
        return out
    for conf in sorted(path.glob("*.conf")):
        for raw in conf.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line.startswith("Environment="):
                continue
            directive = line.removeprefix("Environment=").strip()
            try:
                tokens = shlex.split(directive)
                unquote = False
            except ValueError:
                # A hand-edited drop-in with an unbalanced quote must not take
                # the whole audit down; read what can be read and move on.
                tokens = directive.split()
                unquote = True
            for token in tokens:
                name, sep, value = token.partition("=")
                if sep and name.strip():
                    out[name.strip()] = value.strip('"').strip("'") if unquote else value
    return out


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """``infra/flags.yaml`` keyed by flag name."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {e["name"]: e for e in data.get("flags", []) if e.get("name")}


def engine_environ_path(unit: str = ENGINE_UNIT) -> Path | None:
    """``/proc/<MainPID>/environ`` for the running engine, or None.

    None means "could not be determined" — the caller must say so rather than
    quietly reporting the file layers as if they were the live process.
    """
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pid = result.stdout.strip()
    if not pid.isdigit() or pid == "0":
        return None
    return Path(f"/proc/{pid}/environ")


class _Unread:
    """Marks a caller that has not read the process environment yet.

    Not None: None is the answer ``read_environ`` gives when the environ could
    not be read, and that answer must survive being passed to :func:`audit`.
    """


_UNREAD = _Unread()


def read_environ(path: Path | None) -> dict[str, str] | None:
    """The running process's environment, or None when it cannot be read."""
    if path is None:
        return None
    try:
        return parse_environ_blob(path.read_bytes())
    except OSError:
        return None


# ── The ENABLED/MODE gate map, derived from the engine's own source ─────────


@dataclass(frozen=True)
class ModeGate:
    """How ``feature_flags`` resolves one ``*_MODE`` flag.

    ``enabled_var`` is the companion whose falsiness forces ``off``; None means
    the flag is gated on its own value alone (the honesty suite). ``default``
    is what the reader falls back to when the mode var is unset.
    """

    enabled_var: str | None
    default: str


def mode_gate_map(source: Path | None = None) -> dict[str, ModeGate]:
    """Read each ``*_MODE`` flag's ENABLED companion out of feature_flags.py.

    Derived, never hand-listed. A parallel list here would drift from the code
    that actually reads the flags, and the audit would then print an effective
    value the engine does not compute — exactly the class of defect it exists
    to find. The concrete trap: ``ROBOTHOR_APPROVAL_MODE``'s gate is
    ``ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED``, not the ``ROBOTHOR_APPROVAL_``
    ``ENABLED`` a suffix rule would invent, and that var does not exist.

    Two call shapes are recognised, matching the two the module uses:
    ``_enforcement_mode(enabled_var, mode_var)`` for the generic ladder, and a
    function body pairing ``_env_bool("<X>_ENABLED")`` with
    ``_resolve_raw("<Y>_MODE", default)`` for the hand-rolled ones (RIP-7,
    RIP-13, the honesty suite).
    """
    path = source or FEATURE_FLAGS_SOURCE
    gates: dict[str, ModeGate] = {}
    tree = ast.parse(path.read_text())

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        enabled: str | None = None
        resolved: list[tuple[str, str]] = []
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            args = [a.value for a in call.args if isinstance(a, ast.Constant)]
            literals = [a for a in args if isinstance(a, str)]
            if call.func.id == "_enforcement_mode" and len(literals) == 2:
                gates[literals[1]] = ModeGate(enabled_var=literals[0], default="observe")
            elif call.func.id == "_env_bool" and literals:
                # `_disabled_all` is the global panic switch, not a per-flag gate.
                if literals[0].endswith("_ENABLED") and literals[0] != PANIC_KEY:
                    enabled = literals[0]
            elif call.func.id == "_resolve_raw" and literals:
                resolved.append((literals[0], literals[1] if len(literals) > 1 else "observe"))
        for name, default in resolved:
            if name.endswith("_MODE") and name not in gates:
                gates[name] = ModeGate(enabled_var=enabled, default=default)
    return gates


# ── Effective value ─────────────────────────────────────────────────────────


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _valid_values_for(flag: str) -> tuple[str, ...]:
    """The flag's value set, from the one place that defines it.

    ``robothor.flags.store.valid_values_for`` is what the bridge validates
    writes against and what ``feature_flags`` clamps reads to. Asking it
    (rather than hand-listing a ladder here) is the same rule the gate map
    follows: a parallel copy would drift from the code that reads the flag,
    and this table would then describe a system that does not exist.
    """
    from robothor.flags.store import valid_values_for

    return valid_values_for(flag)


def effective_value(
    flag: str,
    resolved: dict[str, str],
    gates: dict[str, ModeGate],
    *,
    notes: list[str] | None = None,
) -> str:
    """What ``feature_flags`` computes for *flag* given a resolved environment.

    Mirrors ``_enforcement_mode``: the panic switch wins over everything, a
    falsy ``*_ENABLED`` companion means ``off`` no matter what the mode says,
    and an enabled flag with no ``*_MODE`` set lands on the reader's default
    (``observe`` for the generic ladder).

    Including the clamp. Every reader coerces an out-of-range value rather
    than honoring it — ``symbolic_memory_mode`` returns ``observe`` for a
    RIP-13 flag set to ``alert``, silently — so printing the raw value as
    "effective" would report a mode the engine has never run. The raw value is
    not thrown away either: it goes into *notes*, because an /etc line that
    does nothing is precisely what an operator needs told.
    """
    if _truthy(resolved.get(PANIC_KEY)):
        return "off"
    if flag.endswith("_ENABLED"):
        return "true" if _truthy(resolved.get(flag)) else "false"
    gate = gates.get(flag)
    if gate is not None and gate.enabled_var and not _truthy(resolved.get(gate.enabled_var)):
        return "off"
    raw = (resolved.get(flag) or "").strip().lower()
    if not raw:
        return gate.default if gate is not None else "observe"
    valid = _valid_values_for(flag)
    if raw not in valid:
        if notes is not None:
            notes.append(
                f"{flag} is set to '{raw}', which is not one of "
                f"{', '.join(valid)} — the engine clamps it to 'observe', so that "
                "line governs nothing. Fix the value or remove it."
            )
        return "observe"
    return raw


def expected_from_manifest(flag: str, yaml_mode: str | None) -> str | None:
    """The manifest's mode in the vocabulary ``effective_value`` returns.

    ``mode: "on"`` in flags.yaml is how a boolean flag spells enabled; the
    engine spells the same state ``true``.
    """
    if yaml_mode is None:
        return None
    mode = str(yaml_mode).strip().lower()
    if flag.endswith("_ENABLED"):
        return {"on": "true", "off": "false", "true": "true", "false": "false"}.get(mode, mode)
    return "true" if mode == "on" else mode


# ── Evidence ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evidence:
    """One flag's DB evidence: 7d rows by action, last fire, last probe."""

    rows_7d: dict[str, int] = field(default_factory=dict)
    last_fired: dt.datetime | None = None
    last_probe: dt.datetime | None = None
    note: str = ""


@dataclass(frozen=True)
class DbState:
    """Everything the audit reads from the database, in one injectable value."""

    pins: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    pin_actors: dict[str, str] = field(default_factory=dict)


def fetch_db_state(flags: list[str]) -> DbState:
    """Read pins and evidence. SELECTs only; raises if the DB is unreachable.

    A missing evidence table is not an error — table presence is deploy
    specific (``robothor/flags/evidence.py`` says why) — so each query is
    guarded and a missing one leaves that flag's evidence unset rather than
    aborting the whole audit.
    """
    from robothor.db.connection import get_connection
    from robothor.flags.evidence import EVIDENCE_SOURCES
    from robothor.flags.store import _SEED_ACTOR

    pins: dict[str, str] = {}
    actors: dict[str, str] = {}
    evidence: dict[str, Evidence] = {}

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, value, updated_by FROM feature_flags WHERE name = ANY(%s)", (flags,)
        )
        for name, value, updated_by in cur.fetchall():
            if updated_by == _SEED_ACTOR:
                continue  # the migration seed means "unset" — env still governs
            pins[name] = value
            actors[name] = updated_by or "?"

        for flag in flags:
            src = EVIDENCE_SOURCES.get(flag)
            if src is None:
                continue
            evidence[flag] = _fetch_one_evidence(cur, flag, src)

    return DbState(pins=pins, evidence=evidence, pin_actors=actors)


def _fetch_one_evidence(cur: Any, flag: str, src: Any) -> Evidence:
    """Counts and timestamps for one flag, tolerating a missing table."""
    cur.execute("SELECT to_regclass(%s)", (f"public.{src.table}",))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return Evidence(note=f"no {src.table}")

    rows_7d: dict[str, int] = {}
    last_fired: dt.datetime | None = None
    try:
        if src.table == "agent_guardrail_events":
            # The only evidence table with an `action` column, and the one the
            # promotion decision is actually made on: `observed` is what would
            # have been blocked under enforce.
            cur.execute(
                f"SELECT action, count(*) FROM {src.table} "  # noqa: S608 -- code-declared constants, never user input
                f"WHERE {src.where} AND created_at > now() - interval '7 days' "
                "GROUP BY action ORDER BY action"
            )
            rows_7d = {str(action or "?"): int(n) for action, n in cur.fetchall()}
            cur.execute(
                f"SELECT max(created_at) FROM {src.table} WHERE {src.where}"  # noqa: S608 -- same
            )
        else:
            cur.execute(
                f"SELECT max({src.time_column}), "  # noqa: S608 -- same
                f"count(*) FILTER (WHERE {src.time_column} > now() - interval '7 days') "
                f"FROM {src.table} WHERE {src.where}"
            )
            fired, n = cur.fetchone()
            return Evidence(
                rows_7d={"rows": int(n or 0)},
                last_fired=fired,
                last_probe=_fetch_last_probe(cur, flag),
            )
        (last_fired,) = cur.fetchone()
    except Exception:  # noqa: BLE001 — a drifted schema must not abort the audit
        cur.connection.rollback()
        return Evidence(note=f"query failed on {src.table}")

    return Evidence(rows_7d=rows_7d, last_fired=last_fired, last_probe=_fetch_last_probe(cur, flag))


def _fetch_last_probe(cur: Any, flag: str) -> dt.datetime | None:
    """When this flag was last deliberately probed.

    A probe run is the only evidence that distinguishes "this control is quiet
    because nothing violated it" from "this control cannot fire" — the whole
    reason six built-and-tested controls sat inert. Probe runs tag themselves
    ``trigger_detail = 'probe:<FLAG>...'``.
    """
    try:
        cur.execute(
            "SELECT max(started_at) FROM agent_runs WHERE trigger_detail LIKE %s",
            (f"probe:{flag}%",),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001
        cur.connection.rollback()
        return None
    return row[0] if row else None


# ── The audit ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FlagRow:
    flag: str
    yaml_mode: str | None
    dropin: str | None
    envfile: str | None
    db_pin: str | None
    effective: str
    layer: str
    rows_7d: str
    last_fired: str | None
    last_probe: str | None
    tags: tuple[str, ...]
    db_pin_actor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "flag": self.flag,
            "yaml_mode": self.yaml_mode,
            "dropin": self.dropin,
            "envfile": self.envfile,
            "db_pin": self.db_pin,
            "db_pin_actor": self.db_pin_actor,
            "effective": self.effective,
            "layer": self.layer,
            "rows_7d": self.rows_7d,
            "last_fired": self.last_fired,
            "last_probe": self.last_probe,
            "tags": list(self.tags),
        }


def _fmt_ts(value: dt.datetime | None, *, date_only: bool = False) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")


def _fmt_rows(evidence: Evidence | None, have_db: bool) -> str:
    if not have_db:
        return "?"
    if evidence is None:
        return "-"
    if evidence.note and not evidence.rows_7d:
        return evidence.note
    if not evidence.rows_7d:
        return "0"
    return " ".join(f"{action}={n}" for action, n in sorted(evidence.rows_7d.items()))


def _is_overdue(entry: dict[str, Any] | None, today: dt.date) -> bool:
    if not entry or entry.get("mode") not in PENDING_MODES:
        return False
    planned = entry.get("planned_promotion")
    if not planned:
        return False
    try:
        return dt.date.fromisoformat(str(planned)) < today
    except ValueError:
        return False


def audit(
    *,
    flags_yaml: Path = FLAG_MANIFEST,
    env_file: Path = DEFAULT_ENV_FILE,
    dropin_dir: Path = DEFAULT_DROPIN_DIR,
    environ_path: Path | None = None,
    environ: dict[str, str] | None | _Unread = _UNREAD,
    db: DbState | None = None,
    today: dt.date | None = None,
    notes: list[str] | None = None,
) -> list[FlagRow]:
    """One row per governed/manifested flag, plus any debug key that is set.

    ``db=None`` means "no database": the evidence columns come back ``?`` and
    no pin can be seen, which the caller must state rather than presenting a
    partial table as complete. *notes* is an out-parameter: anything the table
    itself cannot say (an out-of-range value the engine clamps) is appended
    for the caller to print beneath it.

    *environ* lets a caller that has already read the process pass the result
    in — ``None`` is a real answer there ("unreadable"), which is why the
    default is a sentinel rather than None. See :func:`main`.
    """
    from robothor.flags.store import GOVERNED_FLAGS

    today = today or dt.datetime.now(tz=dt.UTC).date()
    manifest = load_manifest(flags_yaml)
    dropin = parse_dropin_dir(dropin_dir)
    envfile = parse_env_file(env_file.read_text(errors="replace")) if env_file.is_file() else {}
    if isinstance(environ, _Unread):
        environ = read_environ(environ_path)
    gates = mode_gate_map()
    pins = db.pins if db else {}

    # The running process is authoritative when we can read it. When we cannot,
    # the file layers are simulated in systemd's own order: EnvironmentFile=
    # is applied after the drop-in's Environment= directives, so the env file
    # wins — which is the shadowing this whole script exists to surface.
    base = dict(environ) if environ is not None else {**dropin, **envfile}
    resolved = {**base, **pins}

    names = sorted(set(GOVERNED_FLAGS) | set(manifest))
    rows = [
        _build_row(
            flag,
            manifest_entry=manifest.get(flag),
            dropin=dropin,
            envfile=envfile,
            environ=environ,
            db=db,
            resolved=resolved,
            gates=gates,
            today=today,
            notes=notes,
        )
        for flag in names
    ]
    rows.extend(_debug_rows(dropin=dropin, envfile=envfile, environ=environ, resolved=resolved))
    return rows


def _build_row(
    flag: str,
    *,
    manifest_entry: dict[str, Any] | None,
    dropin: dict[str, str],
    envfile: dict[str, str],
    environ: dict[str, str] | None,
    db: DbState | None,
    resolved: dict[str, str],
    gates: dict[str, ModeGate],
    today: dt.date,
    notes: list[str] | None = None,
) -> FlagRow:
    db_pin = db.pins.get(flag) if db else None
    yaml_mode = str(manifest_entry["mode"]) if manifest_entry and "mode" in manifest_entry else None
    effective = effective_value(flag, resolved, gates, notes=notes)
    layer = _winning_layer(flag, db_pin=db_pin, environ=environ, envfile=envfile, dropin=dropin)

    db_pin_actor = db.pin_actors.get(flag) if db else None
    tags: list[str] = []
    expected = expected_from_manifest(flag, yaml_mode)
    if expected is not None and expected != effective:
        tags.append("MISMATCH")
    # SHADOW-LAYER: this guardrail is governed from somewhere the versioned,
    # drift-checked drop-in is not — so a flip applied to the drop-in would do
    # nothing, and a rebuilt box would not reproduce the posture.
    #
    # Wider than check_dropin_drift.sh's SHADOWED, deliberately. That script
    # only fires when a name appears in BOTH files, which means the two flags
    # living ONLY in robothor.env are invisible to it: nothing in git records
    # that completion-contracts and the deliverable contract are on, and
    # nothing would restore them. "Set in both" is the noisy case, not the
    # dangerous one.
    #
    # A DB row is the exception, and only when an operator surface wrote it:
    # the Controls dashboard IS the supported place to govern a flag, so its
    # rows are reported (PINNED, in the findings summary) without failing the
    # run. Every other actor — a sync job, a script, a stray psql — is still
    # unversioned posture nothing would restore, and still exits 1.
    if layer == "db" and _is_operator_actor(db_pin_actor):
        tags.append(f"PINNED:db@{db_pin_actor}")
    elif layer in ("db", "envfile", "environ"):
        tags.append(f"SHADOW-LAYER:{layer}")
    if _is_overdue(manifest_entry, today):
        tags.append("OVERDUE")

    ev = db.evidence.get(flag) if db else None
    return FlagRow(
        flag=flag,
        yaml_mode=yaml_mode,
        dropin=dropin.get(flag),
        envfile=envfile.get(flag),
        db_pin=db_pin,
        db_pin_actor=db_pin_actor,
        effective=effective,
        layer=layer,
        rows_7d=_fmt_rows(ev, have_db=db is not None),
        last_fired=_fmt_ts(ev.last_fired) if ev else None,
        last_probe=_fmt_ts(ev.last_probe, date_only=True) if ev else None,
        tags=tuple(tags),
    )


def _is_operator_actor(actor: str | None) -> bool:
    """Did an operator-facing surface write this ``feature_flags`` row?

    Keyed on the actor, never on "there is a row at all": the migration seed
    is already filtered out in ``fetch_db_state``, an operator's dashboard
    flip is legitimate, and anything else is an anonymous pin.
    """
    if not actor:
        return False
    name = actor.strip()
    return name in OPERATOR_ACTORS or name.startswith(OPERATOR_ACTOR_PREFIXES)


def _winning_layer(
    flag: str,
    *,
    db_pin: str | None,
    environ: dict[str, str] | None,
    envfile: dict[str, str],
    dropin: dict[str, str],
) -> str:
    """Which layer supplied the effective value.

    ``yaml`` is never a possible answer: the manifest records intent and sets
    nothing at runtime. It gets its own column and drives the MISMATCH tag
    instead — treating it as a winning layer is precisely the confusion that
    let flags.yaml say ``observe`` for a year while the box ran ``enforce``.
    """
    if db_pin is not None:
        return "db"
    if environ is not None and flag not in environ:
        # We can read the process and the flag is not in it: no file layer
        # reached it (a stale process, or a line added after the last restart).
        return "code-default"
    if flag in envfile:
        return "envfile"
    if flag in dropin:
        return "dropin"
    if environ is not None and flag in environ:
        # Set somewhere that is neither the drop-in nor the env file — a
        # `systemctl set-environment`, the unit file itself, or the launching
        # shell. Unversioned and invisible; naming it is the point.
        return "environ"
    return "code-default"


def _debug_rows(
    *,
    dropin: dict[str, str],
    envfile: dict[str, str],
    environ: dict[str, str] | None,
    resolved: dict[str, str],
) -> list[FlagRow]:
    """One row per debug/panic key that is actually set somewhere.

    Listed only when present: a table that prints three permanently-unset kill
    switches teaches the reader to skim past them, and the one day one IS set
    is the day it matters.
    """
    rows = []
    for key in DEBUG_ENV_KEYS:
        if key not in resolved:
            continue
        rows.append(
            FlagRow(
                flag=key,
                yaml_mode=None,
                dropin=dropin.get(key),
                envfile=envfile.get(key),
                db_pin=None,
                effective=resolved[key],
                layer=_winning_layer(
                    key, db_pin=None, environ=environ, envfile=envfile, dropin=dropin
                ),
                rows_7d="-",
                last_fired=None,
                last_probe=None,
                tags=("DEBUG-ENV",),
            )
        )
    return rows


def has_drift(rows: list[FlagRow]) -> bool:
    """True when some layer is being shadowed or contradicts the manifest.

    OVERDUE, PINNED and DEBUG-ENV are reported but do not fail the run: a soak
    past its date is already nagged by ``check_soak_deadlines``, an
    operator-written DB pin is the supported way to govern a flag, and a debug
    key being set is a fact to surface, not by itself a disagreement between
    layers.
    """
    return any(t == "MISMATCH" or t.startswith("SHADOW-LAYER") for row in rows for t in row.tags)


# ── Rendering ───────────────────────────────────────────────────────────────

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("flag", "flag"),
    ("yaml", "yaml_mode"),
    ("dropin", "dropin"),
    ("envfile", "envfile"),
    ("db", "_db_cell"),
    ("effective", "effective"),
    ("layer", "layer"),
    ("rows_7d", "rows_7d"),
    ("last_fired", "last_fired"),
    ("last_probe", "last_probe"),
    ("tags", "_tags_cell"),
)


def _cell(row: FlagRow, attr: str) -> str:
    if attr == "_db_cell":
        if row.db_pin is None:
            return "-"
        return f"{row.db_pin}@{row.db_pin_actor}" if row.db_pin_actor else row.db_pin
    if attr == "_tags_cell":
        return " ".join(row.tags) or "-"
    return str(getattr(row, attr) or "-")


def format_table(rows: list[FlagRow]) -> str:
    widths = [
        max(len(header), *(len(_cell(r, attr)) for r in rows)) if rows else len(header)
        for header, attr in _COLUMNS
    ]
    lines = ["  ".join(h.ljust(w) for (h, _), w in zip(_COLUMNS, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(
        "  ".join(
            _cell(row, attr).ljust(w) for (_, attr), w in zip(_COLUMNS, widths, strict=True)
        ).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def _summary(rows: list[FlagRow]) -> list[str]:
    out = []
    for tag in ("MISMATCH", "SHADOW-LAYER", "PINNED", "OVERDUE", "DEBUG-ENV"):
        named = [r.flag for r in rows if any(t.startswith(tag) for t in r.tags)]
        if named:
            out.append(f"  {tag}: {', '.join(named)}")
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--flags-yaml", type=Path, default=FLAG_MANIFEST, help="manifest of intent")
    p.add_argument(
        "--env-file", type=Path, default=DEFAULT_ENV_FILE, help="systemd EnvironmentFile"
    )
    p.add_argument("--dropin-dir", type=Path, default=DEFAULT_DROPIN_DIR, help="drop-in directory")
    p.add_argument(
        "--environ",
        type=Path,
        default=None,
        help="raw NUL-separated environ blob (default: the running engine's /proc/<MainPID>/environ)",
    )
    p.add_argument("--no-db", action="store_true", help="skip every database read")
    p.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    environ_path = args.environ if args.environ is not None else engine_environ_path()

    notes: list[str] = []
    db: DbState | None = None
    if args.no_db:
        notes.append("no database read (--no-db): pins and evidence columns are unknown ('?').")
    else:
        from robothor.flags.store import GOVERNED_FLAGS

        names = sorted(set(GOVERNED_FLAGS) | set(load_manifest(args.flags_yaml)))
        try:
            db = fetch_db_state(names)
        except Exception as exc:  # noqa: BLE001 — a DB outage degrades, never aborts
            notes.append(
                f"no database ({exc}): pins and evidence columns are unknown ('?'). "
                "The file-layer columns above are still valid — this is a partial "
                "report, not a silent skip."
            )

    # Read the running process ONCE and pass it down. Reading it here for the
    # note and again inside audit() left the report free to describe two
    # different processes: the engine can restart between the two reads.
    environ = read_environ(environ_path)
    if environ is None:
        notes.append(
            f"could not read the running engine's environment ({environ_path or 'no MainPID'}): "
            "the effective column is simulated from the file layers in systemd order "
            "(drop-in, then EnvironmentFile), not read from the process."
        )

    rows = audit(
        flags_yaml=args.flags_yaml,
        env_file=args.env_file,
        dropin_dir=args.dropin_dir,
        environ_path=environ_path,
        environ=environ,
        db=db,
        notes=notes,
    )
    drift = has_drift(rows)

    if args.as_json:
        print(
            json.dumps(
                {"flags": [r.as_dict() for r in rows], "drift": drift, "notes": notes}, indent=2
            )
        )
        return 1 if drift else 0

    print(format_table(rows))
    for note in notes:
        print(f"\nNOTE: {note}")
    summary = _summary(rows)
    if summary:
        print("\n=== findings ===")
        print("\n".join(summary))
    if drift:
        print(
            "\nFAIL: a layer is shadowed or the running process disagrees with "
            "infra/flags.yaml. Keep each flag in exactly one place, then update "
            "the manifest (docs/runbooks/GUARDRAIL_FLIPS.md)."
        )
        return 1
    print("\nOK — every layer agrees with the manifest and nothing is shadowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

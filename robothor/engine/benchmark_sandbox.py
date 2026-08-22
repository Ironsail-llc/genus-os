"""Seeded CRM fixtures in an isolated sandbox tenant, and the reads that grade them.

Why this module exists
----------------------
A benchmark that denies the write tools its rubrics grade can only be passed by
narrating an action the agent is forbidden to perform. ``crm-hygiene`` was
exactly that: rubrics demanding "takes a scrub/flag/deactivate action" over
records (``p-9999``, ``p-1234``) that could not exist — ``crm_people.id`` is a
uuid — while the harness intersected the agent's tools down to a read-only
allow-list. The grade it produced was a fabrication score, and it carried
weight 5.0 on the agent's quality goal.

The fix is not to loosen the sandbox. It is to give the agent a *real* place to
act:

* a dedicated tenant (``benchmark-sandbox``, migration 102) whose rows are
  invisible to production reads and are deleted after every task;
* declarative fixtures seeded as real rows before the task and torn down after,
  so the record the prompt names exists and carries a real uuid;
* CRM writes re-allowed **only** while the run is scoped to that tenant;
* ``state_checks`` that read the sandbox back after the run, so the grade comes
  from the environment rather than the transcript.

What stays denied, always
-------------------------
:data:`EXTERNAL_SIDE_EFFECT_TOOLS` — mail, calendar, ``exec``, ``invoke_skill``,
spawning, browser, desktop, voice. On 2026-05-28 a benchmark agent reached a
real recipient through ``invoke_skill('send-email')`` → ``exec('gog gmail send
…')``. A sandbox tenant isolates the *database*; it does nothing about the
outside world, so nothing that touches the outside world becomes allowed here.
Deletes and merges are excluded too: they are irreversible, and "never deletes"
is precisely what the hygiene suite is supposed to be measuring.

Rollout: ``ROBOTHOR_BENCHMARK_SANDBOX_ENABLED`` + ``…_MODE``
(off → observe → alert → enforce). ``off`` is byte-for-byte today's behaviour.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from robothor.constants import DEFAULT_TENANT
from robothor.engine.feature_flags import benchmark_sandbox_mode

logger = logging.getLogger(__name__)


class FixtureError(RuntimeError):
    """A fixture spec, reference, or teardown target that must not proceed."""


# ---------------------------------------------------------------------------
# Rollout + tenant identity
# ---------------------------------------------------------------------------

#: Default id of the dedicated benchmark tenant. Created by migration 102 and
#: re-ensured at seed time so a fresh database (or the test database) works
#: without a migration run.
DEFAULT_SANDBOX_TENANT = "benchmark-sandbox"

SANDBOX_TENANT_DISPLAY_NAME = "Benchmark Sandbox"

#: Hard ceiling on rows a single fixture may seed. The old suite claimed "200
#: stale TODOs"; seeding 200 rows per task per night is a database-churn
#: liability and the grader learns nothing from row 13 onward.
MAX_FIXTURE_ROWS = 50


def sandbox_tenant_id() -> str:
    """Id of the tenant benchmark sub-runs execute against."""
    import os

    return os.environ.get("ROBOTHOR_BENCHMARK_TENANT", "").strip() or DEFAULT_SANDBOX_TENANT


def sandbox_active() -> bool:
    """True when benchmark sub-runs should execute against the sandbox tenant."""
    return benchmark_sandbox_mode() != "off"


def state_checks_scored() -> bool:
    """True when state-check results count toward the task score."""
    return benchmark_sandbox_mode() == "enforce"


# ---------------------------------------------------------------------------
# The deny-list split
# ---------------------------------------------------------------------------

#: Tools that reach outside this database. Denied in every benchmark mode, for
#: ever, regardless of tenant. See the module docstring for the incident.
EXTERNAL_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        # Shell / arbitrary code — the 2026-05-28 escape hatch.
        "exec",
        "invoke_skill",
        "write_file",
        "edit_file",
        "append_file",
        # Mail.
        "gws_gmail_send",
        "gws_gmail_reply",
        "gws_gmail_modify",
        "gws_gmail_draft",
        "send_email",
        # Calendar.
        "gws_calendar_create",
        "gws_calendar_update",
        "gws_calendar_delete",
        # Messaging / paging a human.
        "message",
        "send_message",
        "send_notification",
        "make_call",
        # Spawning more agents (a child inherits none of this gating for free).
        "spawn_agent",
        "spawn_agents",
        # Anything that drives a machine or a camera.
        "browser",
        "browser_navigate",
        "desktop_click",
        "desktop_type",
        "desktop_screenshot",
        "enroll_face",
        "speak",
        # Durable agent state outside the sandbox tenant.
        "store_memory",
        "memory_block_write",
        "append_to_block",
        "vault_put",
        "vault_delete",
    }
)

#: CRM writes that are safe *only* because every row they touch lives in the
#: sandbox tenant and is deleted when the task ends. Deliberately excludes every
#: delete and merge: they are irreversible, and refusing to delete is what the
#: hygiene suite grades.
SANDBOX_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_person",
        "update_person",
        "create_company",
        "update_company",
        "create_note",
        "update_note",
        "create_task",
        "update_task",
        "resolve_task",
    }
)


def benchmark_allowed_tools(*, sandbox: bool) -> frozenset[str]:
    """The allow-list a benchmark sub-agent's tools are intersected against.

    ``sandbox=False`` returns the read-only baseline unchanged. ``sandbox=True``
    adds :data:`SANDBOX_WRITE_TOOLS`. Either way :data:`EXTERNAL_SIDE_EFFECT_TOOLS`
    is subtracted last, so a tool cannot become allowed by being added to the
    read-only list by mistake.
    """
    from robothor.engine.tools.handlers.benchmark import _BENCHMARK_READONLY_TOOLS

    allowed = set(_BENCHMARK_READONLY_TOOLS)
    if sandbox:
        allowed |= SANDBOX_WRITE_TOOLS
    return frozenset(allowed - EXTERNAL_SIDE_EFFECT_TOOLS)


# ---------------------------------------------------------------------------
# Fixture spec
# ---------------------------------------------------------------------------

#: Columns a fixture may set, per table. An allow-list, not a reflection query:
#: the values come from a YAML file, and column names are interpolated into SQL
#: identifiers. Anything not listed here is refused at validation time.
SEEDABLE_COLUMNS: dict[str, frozenset[str]] = {
    "crm_people": frozenset(
        {
            "first_name",
            "last_name",
            "email",
            "phone",
            "job_title",
            "city",
            "linkedin_url",
            "x_url",
            "created_at",
            "updated_at",
            "deleted_at",
        }
    ),
    "crm_companies": frozenset(
        {
            "name",
            "domain_name",
            "employees",
            "address_city",
            "linkedin_url",
            "created_at",
            "updated_at",
            "deleted_at",
        }
    ),
    "crm_tasks": frozenset(
        {
            "title",
            "body",
            # A session goal is a crm_task; the text the agent reads through
            # get_goal comes from this column, not from `title`. Without it a
            # seeded goal reads back with an empty objective, which is how a
            # suite ends up asserting a goal that says nothing.
            "objective",
            "status",
            "priority",
            "tags",
            "created_by_agent",
            "assigned_to_agent",
            "person_id",
            "resolution",
            "due_at",
            "created_at",
            "updated_at",
            "deleted_at",
            "resolved_at",
        }
    ),
}

#: Timestamp columns that accept the ``<column>_days_ago: N`` shorthand and the
#: literal ``"now"``.
_TIMESTAMP_COLUMNS: frozenset[str] = frozenset(
    {"created_at", "updated_at", "deleted_at", "resolved_at", "due_at"}
)

_DAYS_AGO_SUFFIX = "_days_ago"

#: Delete order for the teardown sweep — children before parents. Every entry
#: carries a ``tenant_id`` column; one that does not (or does not exist in this
#: schema version) is skipped rather than failing the sweep.
_SWEEP_ORDER: tuple[str, ...] = (
    "crm_task_history",
    "crm_agent_notifications",
    "timeline_activity",
    "message_participant",
    "crm_messages",
    "crm_conversations",
    "crm_notes",
    "call_log",
    "channel_message_map",
    "crm_routines",
    "face_identities",
    "crm_tasks",
    "crm_people",
    "crm_companies",
)

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_FIXTURE_REF_RE = re.compile(r"\{\{\s*fixture\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*\}\}")


def validate_fixture_spec(spec: dict[str, Any] | None) -> str | None:
    """Return an error string if the fixture spec is unusable, else None.

    Validation is not politeness: column names reach SQL as identifiers, so an
    unknown column is refused rather than quoted-and-hoped.
    """
    if not isinstance(spec, dict):
        return "fixture spec must be a mapping"
    fixtures = spec.get("fixtures")
    if not isinstance(fixtures, dict) or not fixtures:
        return "fixture spec has no 'fixtures' mapping"
    for key, entry in fixtures.items():
        if not _IDENT_RE.match(str(key)):
            return f"fixture key {key!r} is not a valid identifier"
        if not isinstance(entry, dict):
            return f"fixture {key!r} must be a mapping"
        table = entry.get("table")
        if table not in SEEDABLE_COLUMNS:
            return f"fixture {key!r}: table {table!r} is not seedable"
        count = entry.get("count", 1)
        if not isinstance(count, int) or count < 1 or count > MAX_FIXTURE_ROWS:
            return f"fixture {key!r}: count must be 1..{MAX_FIXTURE_ROWS}, got {count!r}"
        values = entry.get("values") or {}
        if not isinstance(values, dict):
            return f"fixture {key!r}: values must be a mapping"
        allowed = SEEDABLE_COLUMNS[table]
        for column in values:
            base = _base_column(str(column))
            if base not in allowed:
                return f"fixture {key!r}: column {column!r} is not seedable on {table}"
            if base != column and base not in _TIMESTAMP_COLUMNS:
                return f"fixture {key!r}: column {column!r} is not a timestamp column"
    return None


def _base_column(column: str) -> str:
    """``created_at_days_ago`` → ``created_at``; anything else unchanged."""
    if column.endswith(_DAYS_AGO_SUFFIX):
        return column[: -len(_DAYS_AGO_SUFFIX)]
    return column


def load_fixture_spec(suite_path: Path | str) -> dict[str, Any] | None:
    """Load the ``fixtures.yaml`` sitting next to a suite file, if any."""
    path = Path(suite_path).parent / "fixtures.yaml"
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FixtureError(f"unreadable fixture spec at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise FixtureError(f"fixture spec at {path} is not a mapping")
    err = validate_fixture_spec(loaded)
    if err:
        raise FixtureError(f"{path}: {err}")
    return loaded


# ---------------------------------------------------------------------------
# Seeded state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeededRow:
    """One row written into the sandbox tenant, with the values it started at."""

    key: str
    table: str
    row_id: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class SeededFixtures:
    """Everything a single benchmark task seeded, keyed by fixture name."""

    tenant_id: str
    rows: dict[str, SeededRow] = field(default_factory=dict)
    groups: dict[str, list[SeededRow]] = field(default_factory=dict)

    def row(self, key: str) -> SeededRow:
        if key in self.rows:
            return self.rows[key]
        group = self.groups.get(key)
        if group:
            return group[0]
        raise FixtureError(f"no seeded fixture named {key!r}")

    def ids(self, key: str) -> list[str]:
        if key in self.groups:
            return [r.row_id for r in self.groups[key]]
        if key in self.rows:
            return [self.rows[key].row_id]
        return []

    def summary(self) -> dict[str, Any]:
        """Compact, log-safe description of what was seeded."""
        return {
            "tenant_id": self.tenant_id,
            "rows": {k: v.row_id for k, v in self.rows.items()},
            "groups": {k: len(v) for k, v in self.groups.items()},
        }


@dataclass(frozen=True)
class StateCheckResult:
    """One environment read-back: what was asserted and what the DB said."""

    kind: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "passed": self.passed, "detail": self.detail}


# ---------------------------------------------------------------------------
# Prompt interpolation
# ---------------------------------------------------------------------------


def referenced_fixture_keys(text: str) -> set[str]:
    """Fixture keys a prompt (or any string) interpolates."""
    return {m.group(1) for m in _FIXTURE_REF_RE.finditer(text or "")}


def render_fixture_refs(text: str, seeded: SeededFixtures) -> str:
    """Replace ``{{fixture.<key>.<field>}}`` with the seeded row's real value.

    ``<field>`` may be ``id``, ``count``, or any seeded column. An unresolvable
    reference raises: a prompt that silently keeps its placeholder would be a
    subtler version of the ``p-9999`` bug it replaces.
    """

    def _sub(match: re.Match[str]) -> str:
        key, attribute = match.group(1), match.group(2)
        if attribute == "count":
            ids = seeded.ids(key)
            if not ids:
                raise FixtureError(f"fixture {key!r} seeded no rows")
            return str(len(ids))
        row = seeded.row(key)
        if attribute == "id":
            return row.row_id
        if attribute in row.values:
            return str(row.values[attribute])
        raise FixtureError(f"fixture {key!r} has no field {attribute!r}")

    return _FIXTURE_REF_RE.sub(_sub, text or "")


# ---------------------------------------------------------------------------
# Database access — every statement is tenant-scoped
# ---------------------------------------------------------------------------


def _assert_sandbox(tenant_id: str) -> None:
    """Refuse to mutate anything that is not the dedicated sandbox tenant."""
    if not tenant_id or tenant_id in {DEFAULT_TENANT, "robothor-primary", "default"}:
        raise FixtureError(f"refusing to seed or sweep tenant {tenant_id!r}")
    if tenant_id != sandbox_tenant_id():
        raise FixtureError(
            f"tenant {tenant_id!r} is not the benchmark sandbox ({sandbox_tenant_id()!r})"
        )


def _check_identifier(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise FixtureError(f"unsafe SQL identifier: {name!r}")
    return name


def ensure_sandbox_tenant(tenant_id: str | None = None) -> str:
    """Idempotently create the sandbox tenant row so FK constraints hold.

    Migration 102 does this for a migrated database; doing it here as well means
    a fresh instance, a test database, or a rolled-back migration still runs the
    benchmark instead of failing every task on a foreign-key violation.
    """
    from robothor.db.connection import get_connection

    tenant = tenant_id or sandbox_tenant_id()
    _assert_sandbox(tenant)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO crm_tenants (id, display_name, active)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (id) DO NOTHING
            """,
            (tenant, SANDBOX_TENANT_DISPLAY_NAME),
        )
    return tenant


def _coerce_value(column: str, raw: Any) -> tuple[str, Any]:
    """Map a YAML value onto (column, parameter), resolving age shorthands."""
    base = _base_column(column)
    if base != column:
        days = int(raw)
        return base, datetime.now(UTC) - timedelta(days=days)
    if base in _TIMESTAMP_COLUMNS and isinstance(raw, str) and raw.strip().lower() == "now":
        return base, datetime.now(UTC)
    return base, raw


def insert_row(table: str, values: dict[str, Any], tenant_id: str) -> str:
    """Insert one row into the sandbox tenant and return its uuid."""
    _assert_sandbox(tenant_id)
    allowed = SEEDABLE_COLUMNS.get(table)
    if allowed is None:
        raise FixtureError(f"table {table!r} is not seedable")
    columns: list[str] = ["id", "tenant_id"]
    params: list[Any] = [str(uuid.uuid4()), tenant_id]
    for raw_column, raw_value in values.items():
        column, value = _coerce_value(str(raw_column), raw_value)
        if column not in allowed:
            raise FixtureError(f"column {raw_column!r} is not seedable on {table}")
        columns.append(_check_identifier(column))
        params.append(value)

    from robothor.db.connection import get_connection

    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {_check_identifier(table)} ({', '.join(columns)}) VALUES ({placeholders})"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
    return str(params[0])


def update_row(table: str, row_id: str, values: dict[str, Any], tenant_id: str) -> bool:
    """Update one sandbox row. Used by tests and by teardown verification."""
    _assert_sandbox(tenant_id)
    allowed = SEEDABLE_COLUMNS.get(table)
    if allowed is None:
        raise FixtureError(f"table {table!r} is not seedable")
    sets: list[str] = []
    params: list[Any] = []
    for raw_column, raw_value in values.items():
        column, value = _coerce_value(str(raw_column), raw_value)
        if column not in allowed:
            raise FixtureError(f"column {raw_column!r} is not seedable on {table}")
        sets.append(f"{_check_identifier(column)} = %s")
        params.append(value)
    if not sets:
        return False
    params.extend([row_id, tenant_id])

    from robothor.db.connection import get_connection

    sql = (
        f"UPDATE {_check_identifier(table)} SET {', '.join(sets)} WHERE id = %s AND tenant_id = %s"
    )
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return int(cur.rowcount) > 0


def read_row(table: str, row_id: str, tenant_id: str) -> dict[str, Any] | None:
    """Read one row back, scoped to ``tenant_id``. None when absent."""
    if table not in SEEDABLE_COLUMNS:
        raise FixtureError(f"table {table!r} is not readable as a fixture")

    from psycopg2.extras import RealDictCursor

    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"SELECT * FROM {_check_identifier(table)} WHERE id = %s AND tenant_id = %s",
            (row_id, tenant_id),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def count_rows(table: str, tenant_id: str) -> int:
    """Count live (not soft-deleted) rows for a tenant."""
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT count(*) FROM {_check_identifier(table)} "
            "WHERE tenant_id = %s AND deleted_at IS NULL",
            (tenant_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _table_exists(conn: Any, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = 'tenant_id'",
        (table,),
    )
    return cur.fetchone() is not None


def teardown_sandbox(tenant_id: str | None = None) -> int:
    """Hard-delete every CRM row in the sandbox tenant. Returns rows removed.

    Sweeping by tenant rather than by seeded id is deliberate: the agent files
    its own rows during a task, and a fixture set that only cleans what it wrote
    leaves the next night's run reading last night's mess.
    """
    tenant = tenant_id or sandbox_tenant_id()
    _assert_sandbox(tenant)

    from robothor.db.connection import get_connection

    removed = 0
    with get_connection() as conn:
        for table in _SWEEP_ORDER:
            if not _table_exists(conn, table):
                continue
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {_check_identifier(table)} WHERE tenant_id = %s", (tenant,))
            removed += cur.rowcount or 0
    return removed


def seed_fixtures(
    spec: dict[str, Any],
    keys: list[str] | tuple[str, ...],
    tenant_id: str | None = None,
) -> SeededFixtures:
    """Write the named fixtures into the sandbox tenant as real rows.

    Args:
        spec: parsed ``fixtures.yaml`` (``{"fixtures": {key: {...}}}``).
        keys: the fixture keys this task asked for.
        tenant_id: override the sandbox tenant (tests only).

    Returns:
        A :class:`SeededFixtures` carrying the real uuids, for prompt
        interpolation and for the read-backs that grade the run.
    """
    err = validate_fixture_spec(spec)
    if err:
        raise FixtureError(err)
    tenant = tenant_id or sandbox_tenant_id()
    ensure_sandbox_tenant(tenant)

    seeded = SeededFixtures(tenant_id=tenant)
    for key in keys:
        entry = spec["fixtures"].get(key)
        if entry is None:
            raise FixtureError(f"suite references unknown fixture {key!r}")
        table = entry["table"]
        count = int(entry.get("count", 1))
        rows: list[SeededRow] = []
        for index in range(count):
            values = _materialise_values(entry.get("values") or {}, index)
            row_id = insert_row(table, values, tenant)
            rows.append(
                SeededRow(
                    key=key,
                    table=table,
                    row_id=row_id,
                    values={**values, "tenant_id": tenant},
                )
            )
        if count == 1:
            seeded.rows[key] = rows[0]
        else:
            seeded.groups[key] = rows
    return seeded


def _materialise_values(values: dict[str, Any], index: int) -> dict[str, Any]:
    """Expand ``{n}`` in string values to the 1-based row number."""
    out: dict[str, Any] = {}
    for column, raw in values.items():
        out[column] = raw.replace("{n}", str(index + 1)) if isinstance(raw, str) else raw
    return out


# ---------------------------------------------------------------------------
# State checks — grade the environment, never the transcript
# ---------------------------------------------------------------------------


def run_state_checks(
    checks: list[dict[str, Any]] | None,
    seeded: SeededFixtures,
) -> list[StateCheckResult]:
    """Evaluate ``expected.state_checks`` against the sandbox database.

    Every failure mode resolves to ``passed=False``: an unknown kind, a missing
    fixture, a database error. A check that cannot be evaluated must never be
    scored as a pass — that is how an inert control gets promoted.
    """
    results: list[StateCheckResult] = []
    for check in checks or []:
        kind = str(check.get("kind", "")).strip()
        try:
            results.append(_run_one_check(kind, check, seeded))
        except FixtureError as exc:
            results.append(StateCheckResult(kind=kind or "unknown", passed=False, detail=str(exc)))
        except Exception as exc:  # noqa: BLE001 — a checker bug is a failed check
            logger.warning("state check %s errored: %s", kind, exc)
            results.append(
                StateCheckResult(kind=kind or "unknown", passed=False, detail=f"error: {exc}")
            )
    return results


def _run_one_check(kind: str, check: dict[str, Any], seeded: SeededFixtures) -> StateCheckResult:
    if kind == "row_present":
        row = _fixture_row(check, seeded)
        current = read_row(row.table, row.row_id, seeded.tenant_id)
        present = current is not None and current.get("deleted_at") is None
        return StateCheckResult(kind, present, f"{row.table}:{row.row_id} present={present}")

    if kind in {"field_equals", "field_changed", "field_matches", "field_not_matches"}:
        row = _fixture_row(check, seeded)
        field_name = str(check.get("field", ""))
        if field_name not in SEEDABLE_COLUMNS.get(row.table, frozenset()):
            raise FixtureError(f"field {field_name!r} is not readable on {row.table}")
        current = read_row(row.table, row.row_id, seeded.tenant_id)
        if current is None:
            return StateCheckResult(kind, False, f"{row.table}:{row.row_id} no longer exists")
        value = current.get(field_name)
        return _compare_field(kind, check, row, field_name, value)

    if kind == "rows_match":
        return _check_rows_match(check, seeded)

    raise FixtureError(f"unknown state check kind {kind!r}")


def _fixture_row(check: dict[str, Any], seeded: SeededFixtures) -> SeededRow:
    key = check.get("fixture") or check.get("group")
    if not key:
        raise FixtureError("state check needs a 'fixture' key")
    return seeded.row(str(key))


def _compare_field(
    kind: str,
    check: dict[str, Any],
    row: SeededRow,
    field_name: str,
    value: Any,
) -> StateCheckResult:
    detail = f"{row.table}:{row.row_id}.{field_name}={value!r}"
    if kind == "field_equals":
        return StateCheckResult(kind, str(value) == str(check.get("value")), detail)
    if kind == "field_changed":
        seeded_value = row.values.get(field_name)
        return StateCheckResult(kind, str(value) != str(seeded_value), detail)
    pattern = str(check.get("pattern", ""))
    try:
        matched = bool(re.search(pattern, "" if value is None else str(value), re.IGNORECASE))
    except re.error as exc:
        raise FixtureError(f"bad pattern {pattern!r}: {exc}") from exc
    return StateCheckResult(kind, matched if kind == "field_matches" else not matched, detail)


def _check_rows_match(check: dict[str, Any], seeded: SeededFixtures) -> StateCheckResult:
    table = str(check.get("table", ""))
    if table not in SEEDABLE_COLUMNS:
        raise FixtureError(f"table {table!r} is not readable as a fixture")
    match = check.get("match") or {}
    if not isinstance(match, dict):
        raise FixtureError("'match' must be a mapping")
    allowed = SEEDABLE_COLUMNS[table]
    where = ["tenant_id = %s", "deleted_at IS NULL"]
    params: list[Any] = [seeded.tenant_id]
    for column, expected in match.items():
        if column not in allowed:
            raise FixtureError(f"column {column!r} is not readable on {table}")
        where.append(f"{_check_identifier(str(column))} = %s")
        params.append(expected)
    group = check.get("group")
    if group:
        ids = seeded.ids(str(group))
        if not ids:
            raise FixtureError(f"fixture group {group!r} seeded no rows")
        # ``id`` is a uuid column and psycopg2 adapts a list of str to text[],
        # which Postgres refuses to compare ("operator does not exist:
        # uuid = text"). The cast keeps the fixture spec in plain strings.
        where.append("id = ANY(%s::uuid[])")
        params.append(ids)

    from robothor.db.connection import get_connection

    sql = f"SELECT count(*) FROM {_check_identifier(table)} WHERE {' AND '.join(where)}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        fetched = cur.fetchone()
    count = int(fetched[0]) if fetched else 0

    min_count = check.get("min_count")
    max_count = check.get("max_count")
    if min_count is None and max_count is None:
        min_count = 1
    passed = True
    if min_count is not None:
        passed = passed and count >= int(min_count)
    if max_count is not None:
        passed = passed and count <= int(max_count)
    return StateCheckResult(
        "rows_match",
        passed,
        f"{table} matched {count} row(s) (min={min_count}, max={max_count})",
    )

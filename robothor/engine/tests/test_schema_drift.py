"""Schema/enum drift tests — pure file I/O, no database.

The agent_runs CHECK constraints live only in raw SQL migrations while the
enums live only in Python (robothor/engine/models.py). Nothing connected
them, so adding an enum member green-lit CI while guaranteeing production
INSERT failures: TriggerType.CHANNEL_EVENT (plus SLACK, WEBHOOK, IDE) was
added long after migration 025 last rebuilt agent_runs_trigger_type_check,
and every channel-bus wake of main failed create_run silently.

These tests parse the *newest* constraint definition from the canonical
migration manifest (same discovery + ordering as robothor.db.migrate) and
assert every enum member is allowed. They fail at PR time for the next
enum addition that ships without its constraint migration.
"""

from __future__ import annotations

import re
from pathlib import Path

import robothor.engine
from robothor.db.migrate import _discover
from robothor.engine.models import RunStatus, TriggerType


def _balanced_slice(text: str, open_paren_index: int) -> str:
    """Return the text inside the parenthesis at ``open_paren_index``."""
    assert text[open_paren_index] == "("
    depth = 0
    for i in range(open_paren_index, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_index + 1 : i]
    raise AssertionError("Unbalanced parentheses in migration SQL")


def _inline_check_values(sql: str, table: str, column: str) -> list[str] | None:
    """Extract CHECK values for ``column`` inside ``CREATE TABLE table (...)``."""
    m = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\(", sql)
    if m is None:
        return None
    body = _balanced_slice(sql, m.end() - 1)
    c = re.search(rf"CHECK\s*\(\s*{column}\b", body)
    if c is None:
        return None
    check_open = body.index("(", c.start())
    return re.findall(r"'([A-Za-z0-9_]+)'", _balanced_slice(body, check_open))


def _named_check_values(sql: str, constraint: str) -> list[str] | None:
    """Extract CHECK values from the last ``ADD CONSTRAINT constraint CHECK (...)``."""
    values: list[str] | None = None
    for m in re.finditer(rf"ADD CONSTRAINT {constraint}\s+CHECK\s*\(", sql):
        values = re.findall(r"'([A-Za-z0-9_]+)'", _balanced_slice(sql, m.end() - 1))
    return values


def _newest_check_values(table: str, column: str) -> tuple[list[str], Path]:
    """Walk the canonical migration chain in order; return the newest CHECK
    definition for ``table.column`` (inline in CREATE TABLE, or the named
    ``{table}_{column}_check`` constraint Postgres auto-names) and the file
    that defined it."""
    constraint = f"{table}_{column}_check"
    newest: tuple[list[str], Path] | None = None
    for migration in _discover():  # manifest-based, already sorted
        sql = migration.path.read_text(encoding="utf-8")
        values = _named_check_values(sql, constraint)
        if values is None:
            values = _inline_check_values(sql, table, column)
        if values is not None:
            newest = (values, migration.path)
    assert newest is not None, f"No CHECK found for {table}.{column} in any migration"
    return newest


class TestTriggerTypeConstraintDrift:
    def test_every_trigger_type_member_is_allowed_by_the_check(self):
        allowed, source = _newest_check_values("agent_runs", "trigger_type")
        missing = {t.value for t in TriggerType} - set(allowed)
        assert not missing, (
            f"TriggerType members {sorted(missing)} are not in "
            f"agent_runs_trigger_type_check (newest definition: {source.name}). "
            "Runs with these trigger types fail create_run and become invisible "
            "to accounting. Add a migration that recreates the constraint "
            "(see crm/migrations/025_federation.sql for the DO $$ pattern)."
        )

    def test_check_allows_no_duplicates(self):
        allowed, source = _newest_check_values("agent_runs", "trigger_type")
        assert len(allowed) == len(set(allowed)), f"Duplicate values in {source.name}"


_ENGINE_ROOT = Path(robothor.engine.__file__).resolve().parent

# Literal notification_type values the engine writes into
# crm_agent_notifications: kwarg-style call sites plus the positional
# first argument of alerts._write_notification.
_NOTIFICATION_TYPE_WRITE_PATTERNS = (
    re.compile(r"""notification_type=["']([A-Za-z0-9_]+)["']"""),
    re.compile(r"""_write_notification\(\s*["']([A-Za-z0-9_]+)["']"""),
)


def _engine_written_notification_types() -> dict[str, set[str]]:
    """Map notification_type literal -> engine source files that write it."""
    written: dict[str, set[str]] = {}
    for source in _ENGINE_ROOT.rglob("*.py"):
        if "tests" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        for pattern in _NOTIFICATION_TYPE_WRITE_PATTERNS:
            for value in pattern.findall(text):
                written.setdefault(value, set()).add(source.name)
    return written


class TestNotificationTypeConstraintDrift:
    def test_every_engine_written_notification_type_is_allowed_by_the_check(self):
        allowed, source = _newest_check_values("crm_agent_notifications", "notification_type")
        written = _engine_written_notification_types()
        assert written, "No notification_type write sites found in engine source"
        missing = {
            f"{value} (written by {', '.join(sorted(files))})"
            for value, files in written.items()
            if value not in allowed
        }
        assert not missing, (
            f"Engine writes notification types not allowed by "
            f"crm_agent_notifications_notification_type_check (newest definition: "
            f"{source.name}): {sorted(missing)}. Those INSERTs fail at runtime. "
            "Add a migration that recreates the constraint "
            "(see crm/migrations/099_notification_types.sql for the DO $$ pattern)."
        )

    def test_check_allows_no_duplicates(self):
        allowed, source = _newest_check_values("crm_agent_notifications", "notification_type")
        assert len(allowed) == len(set(allowed)), f"Duplicate values in {source.name}"


class TestRunStatusConstraintDrift:
    def test_every_run_status_member_is_allowed_by_the_check(self):
        allowed, source = _newest_check_values("agent_runs", "status")
        missing = {s.value for s in RunStatus} - set(allowed)
        assert not missing, (
            f"RunStatus members {sorted(missing)} are not in "
            f"agent_runs_status_check (newest definition: {source.name}). "
            "Terminal-state updates for these statuses will fail. Add a "
            "constraint migration (see crm/migrations/032_run_status_skipped.sql)."
        )

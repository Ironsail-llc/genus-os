"""Every remote-tools audit row fits the ``audit_log`` columns.

``audit_log.status`` is ``varchar(20)``: a longer value makes the INSERT fail
and ``log_event`` drops the row with one log line. The specific outcome code
(``remote_attestation_missing`` and the like) therefore travels in
``details.outcome``; ``status`` is one word from a short fixed vocabulary.
The limits come from one constant (``robothor.audit.logger.COLUMN_LIMITS``),
which the integration test checks against the real table.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.audit.logger import COLUMN_LIMITS
from robothor.tenancy import remote_contract as rc

#: Every outcome the target can audit: each refusal code, each answer, and the
#: internal failure.
TARGET_OUTCOMES = sorted(rc.ERROR_CODES | rc.OUTCOMES | {"internal_error"})


class _Writer:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def writer(monkeypatch) -> _Writer:
    writer = _Writer()
    monkeypatch.setattr("robothor.engine.event_writer.get_event_writer", lambda: writer)
    return writer


def _fits(row: dict[str, Any]) -> None:
    for column, limit in COLUMN_LIMITS.items():
        value = row.get(column)
        if value is not None:
            assert len(str(value)) <= limit, (column, value)


@pytest.mark.parametrize("outcome", TARGET_OUTCOMES)
@pytest.mark.parametrize("access", ["read", "write"])
def test_every_target_row_fits_and_keeps_its_code_in_details(writer, outcome, access) -> None:
    from robothor.engine.remote_tools import audit

    audit(
        tenant_id="t" * 128,
        home_tenant="h" * 128,
        user_id="u",
        agent_id="a" * 64,
        tool="x" * 128,
        access=access,
        outcome=outcome,
        dry_run=False,
        request_id="r",
        args={},
    )

    [row] = writer.rows
    _fits(row)
    assert row["status"] in rc.AUDIT_STATUSES
    assert row["details"]["outcome"] == outcome


def test_the_status_vocabulary_fits_the_column() -> None:
    assert all(len(status) <= COLUMN_LIMITS["status"] for status in rc.AUDIT_STATUSES)


@pytest.mark.parametrize("outcome", ["ok", "error", "denied", "replay", "unknown"])
def test_every_home_row_fits(writer, outcome) -> None:
    from robothor.engine.tools.cross_tenant import audit_cross_tenant

    audit_cross_tenant(
        home_tenant="h" * 128,
        target_tenant="t" * 128,
        user_id="u",
        agent_id="a" * 64,
        tool="org_tool_write",
        outcome=outcome,
        reason="r" * 5000,
        remote_tool="x" * 300,
    )

    [row] = writer.rows
    _fits(row)
    assert row["status"] in rc.AUDIT_STATUSES

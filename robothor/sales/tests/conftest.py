"""Shared isolated PostgreSQL fixtures, and the marker that puts them in the
lane that actually has a database.

The `sales` fixture applies migrations 126+ and creates a tenant; `ops` does
the same for the operations ledger. Without PostgreSQL neither fails cleanly,
they ERROR in setup — and CI's unit lane (`pytest tests/ robothor/ -m "not
integration and not llm and not slow and not e2e"`) has no database service.
461 tests in this package were erroring there, while the integration lane,
which HAS a database and runs `-m "integration and not llm"`, was skipping
them by marker. They ran in neither.

Marked by fixture rather than by hand, for the reason this campaign keeps
rediscovering: a convention that depends on the next author remembering is a
convention that drifts. Anything that asks for a database fixture — directly
or transitively, through `native`, `deployment`, `business` and the rest — is
integration; the ~180 tests here that need no database stay in the fast lane
and keep their coverage there.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.operations.tests.test_store import ops as ops
from robothor.sales.tests.test_service import sales as sales

#: Requesting one of these, at any depth, means the test touches PostgreSQL.
DATABASE_FIXTURES = frozenset({"ops", "sales"})


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    for item in items:
        if DATABASE_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)

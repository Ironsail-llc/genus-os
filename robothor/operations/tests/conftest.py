"""Every test here needs a real PostgreSQL, so every test here is integration.

The `ops` fixture opens a connection, applies migrations and creates a tenant.
Without a database it does not fail, it ERRORS in setup — and CI's unit lane
(`pytest tests/ robothor/ -m "not integration and not llm and not slow and not
e2e"`) has no PostgreSQL service. 21 tests in this package were erroring there
while the integration lane, which HAS a database and runs
`-m "integration and not llm"`, was skipping them by marker.

Marked by fixture rather than by hand so a new test cannot forget. Anything
that asks for `ops` — directly or through another fixture — is integration;
anything that does not stays in the fast lane.
"""

from __future__ import annotations

from typing import Any

import pytest

#: Requesting one of these means the test touches PostgreSQL.
DATABASE_FIXTURES = frozenset({"ops"})


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    for item in items:
        if DATABASE_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)

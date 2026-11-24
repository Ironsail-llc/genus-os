"""The deprecated bridge DAL's timeline asks the memory service for ITS tenant.

``crm_dal.get_timeline`` enriches a contact timeline with memory facts fetched
from the memory service over HTTP. It used to send no tenant, so the service
answered from the default tenant's memory whichever tenant asked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import crm_dal


def _connection_with_one_mapping():
    cursor = MagicMock()
    cursor.fetchall.return_value = [{"identifier": "alice@example.com", "person_id": None}]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def test_the_memory_lookup_names_the_callers_tenant():
    response = MagicMock(status_code=200)
    response.json.return_value = {"results": []}
    with (
        patch.object(crm_dal, "_conn", return_value=_connection_with_one_mapping()),
        patch("httpx.get", return_value=response) as get,
    ):
        crm_dal.get_timeline("alice@example.com", tenant_id="tenant-a")
    assert get.call_args.kwargs["headers"]["X-Tenant-Id"] == "tenant-a"

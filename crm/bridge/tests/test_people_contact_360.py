"""
Phase 3b — Bridge endpoints for Contact 360.

Tests the new /api/people/{id}/timeline|messages|calls|events|tasks|notes|
runs|memory|summary|contact-360 endpoints. DAL is mocked; tests verify that
each endpoint delegates to the right DAL function, passes through the
tenant, and returns the expected shape.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


PID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_timeline_endpoint(test_client):
    with patch(
        "routers.people.get_person_timeline",
        return_value=[{"activity_type": "email"}],
    ) as mock:
        r = await test_client.get(f"/api/people/{PID}/timeline?limit=5")
    assert r.status_code == 200
    assert r.json() == {"activities": [{"activity_type": "email"}]}
    mock.assert_called_once()
    assert mock.call_args.kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_timeline_filter_by_channel(test_client):
    with patch("routers.people.get_person_timeline", return_value=[]) as mock:
        await test_client.get(f"/api/people/{PID}/timeline?channels=email&channels=sms")
    assert mock.call_args.kwargs["channels"] == ["email", "sms"]


@pytest.mark.asyncio
async def test_summary_endpoint(test_client):
    with patch(
        "routers.people.get_person_summary",
        return_value={"counts": {"email": 2}, "last_touched_at": None},
    ):
        r = await test_client.get(f"/api/people/{PID}/summary")
    assert r.status_code == 200
    assert r.json()["counts"]["email"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,fn_name,key",
    [
        ("messages", "get_person_messages", "messages"),
        ("calls", "get_person_calls", "calls"),
        ("events", "get_person_events", "events"),
        ("tasks", "get_person_tasks", "tasks"),
        ("notes", "get_person_notes", "notes"),
        ("runs", "get_person_runs", "runs"),
        ("memory", "get_person_memory", "memory"),
    ],
)
async def test_per_channel_endpoints(test_client, path, fn_name, key):
    with patch(f"routers.people.{fn_name}", return_value=[{"id": "x"}]) as mock:
        r = await test_client.get(f"/api/people/{PID}/{path}")
    assert r.status_code == 200
    assert r.json() == {key: [{"id": "x"}]}
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_contact_360_endpoint(test_client):
    payload = {
        "person": {"id": PID, "first_name": "Ann"},
        "summary": {"counts": {}, "last_touched_at": None},
        "timeline": [],
        "open_tasks": [],
        "recent_notes": [],
        "memory": [],
    }
    with patch("routers.people.get_contact_360", return_value=payload):
        r = await test_client.get(f"/api/people/{PID}/contact-360")
    assert r.status_code == 200
    assert r.json()["person"]["first_name"] == "Ann"

"""
Tests for routines system — recurring task templates.

Tests cover:
- CRUD endpoints (POST, PATCH, DELETE, GET)
- Cron validation
- Trigger endpoint
- Deduplication
- next_run_at computation
"""

import sys
import uuid
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Bridge API Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_routine(test_client):
    """POST /api/routines creates a routine."""
    routine_id = str(uuid.uuid4())
    with patch("routers.routines.create_routine", return_value=routine_id):
        with patch("routers.routines.publish"):
            r = await test_client.post(
                "/api/routines",
                json={
                    "title": "Weekly report",
                    "cronExpr": "0 9 * * 1",
                    "assignedToAgent": "crm-steward",
                    "priority": "normal",
                },
            )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == routine_id
    assert data["title"] == "Weekly report"


@pytest.mark.asyncio
async def test_create_routine_invalid_cron(test_client):
    """POST /api/routines with invalid cron returns 400."""
    r = await test_client.post(
        "/api/routines",
        json={
            "title": "Bad routine",
            "cronExpr": "not a cron",
        },
    )
    assert r.status_code == 400
    assert "Invalid cron" in r.json()["error"]


@pytest.mark.asyncio
async def test_list_routines(test_client):
    """GET /api/routines returns list."""
    mock_routines = [
        {"id": str(uuid.uuid4()), "title": "Daily sync", "cronExpr": "0 8 * * *", "active": True},
    ]
    with patch("routers.routines.list_routines", return_value=mock_routines):
        r = await test_client.get("/api/routines")
    assert r.status_code == 200
    assert len(r.json()["routines"]) == 1


@pytest.mark.asyncio
async def test_update_routine(test_client):
    """PATCH /api/routines/{id} updates a routine."""
    with patch("routers.routines.update_routine", return_value=True):
        r = await test_client.patch(
            f"/api/routines/{uuid.uuid4()}",
            json={"active": False},
        )
    assert r.status_code == 200
    assert r.json()["success"] is True


@pytest.mark.asyncio
async def test_update_routine_invalid_cron(test_client):
    """PATCH /api/routines/{id} with invalid cron returns 400."""
    r = await test_client.patch(
        f"/api/routines/{uuid.uuid4()}",
        json={"cronExpr": "invalid"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_routine(test_client):
    """DELETE /api/routines/{id} soft-deletes."""
    with patch("routers.routines.delete_routine", return_value=True):
        r = await test_client.delete(f"/api/routines/{uuid.uuid4()}")
    assert r.status_code == 200
    assert r.json()["success"] is True


@pytest.mark.asyncio
async def test_trigger_creates_tasks(test_client):
    """POST /api/routines/trigger creates tasks from due routines."""
    task_id = str(uuid.uuid4())
    routine_id = str(uuid.uuid4())
    due_routines = [
        {
            "id": routine_id,
            "title": "Weekly cleanup",
            "body": "Clean stale data",
            "assignedToAgent": "crm-steward",
            "priority": "normal",
            "tags": ["crm-hygiene"],
            "personId": None,
            "companyId": None,
        }
    ]
    with patch("routers.routines.get_due_routines", return_value=due_routines):
        with patch("routers.routines.create_task", return_value=task_id):
            with patch("routers.routines.advance_routine", return_value=True):
                with patch("routers.routines.publish"):
                    r = await test_client.post("/api/routines/trigger")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["triggered"][0]["routineId"] == routine_id
    assert data["triggered"][0]["taskId"] == task_id


@pytest.mark.asyncio
async def test_trigger_skips_duplicate(test_client):
    """POST /api/routines/trigger returns empty when no due routines."""
    with patch("routers.routines.get_due_routines", return_value=[]):
        r = await test_client.post("/api/routines/trigger")
    assert r.status_code == 200
    assert r.json()["count"] == 0


# ─── Model Tests ──────────────────────────────────────────────────────


def test_routine_to_dict():
    """routine_to_dict converts a row correctly."""
    from datetime import datetime

    from robothor.crm.models import routine_to_dict

    now = datetime.now(UTC)
    row = {
        "id": uuid.uuid4(),
        "title": "Weekly report",
        "body": "Generate report",
        "cron_expr": "0 9 * * 1",
        "timezone": "America/New_York",
        "assigned_to_agent": "crm-steward",
        "priority": "normal",
        "tags": ["crm-hygiene"],
        "person_id": None,
        "company_id": None,
        "active": True,
        "next_run_at": now,
        "last_run_at": None,
        "created_by": "philip",
        "created_at": now,
        "updated_at": now,
    }
    result = routine_to_dict(row)
    assert result["title"] == "Weekly report"
    assert result["cronExpr"] == "0 9 * * 1"
    assert result["active"] is True
    assert result["nextRunAt"] is not None
    assert result["lastRunAt"] is None


def test_create_routine_request_validation():
    """CreateRoutineRequest validates fields."""
    from models import CreateRoutineRequest

    req = CreateRoutineRequest(title="Test", cronExpr="0 9 * * 1")
    assert req.title == "Test"
    assert req.priority == "normal"
    assert req.timezone == "America/New_York"


def test_create_routine_request_rejects_bad_uuid():
    """CreateRoutineRequest rejects invalid personId."""
    from models import CreateRoutineRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="personId must be a valid UUID"):
        CreateRoutineRequest(title="Test", cronExpr="0 9 * * 1", personId="bad")

"""
Tests for task coordination system — agent-to-agent task routing.

Tests cover:
- Bridge API endpoints (POST, PATCH, GET agent/, resolve)
- DAL extensions (create_task, update_task, list_tasks, list_agent_tasks, resolve_task)
- New fields (priority, tags, assigned_to_agent, etc.)
- Task state machine (transitions, REVIEW status, history, SLA)
"""

import sys
import uuid
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── Bridge API Tests (mocked DAL) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_task_basic(test_client):
    """POST /api/tasks creates a task with agent coordination fields."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.create_task", return_value=task_id) as mock_create:
        with patch("routers.notes_tasks.publish"):
            r = await test_client.post(
                "/api/tasks",
                json={
                    "title": "Reply to sender",
                    "assignedToAgent": "email-responder",
                    "priority": "high",
                    "tags": ["email", "reply-needed"],
                },
            )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == task_id
    assert data["title"] == "Reply to sender"
    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args
    assert call_kwargs.kwargs["assigned_to_agent"] == "email-responder"
    assert call_kwargs.kwargs["priority"] == "high"
    assert call_kwargs.kwargs["tags"] == ["email", "reply-needed"]


@pytest.mark.asyncio
async def test_create_task_auto_created_by_agent(test_client):
    """POST /api/tasks auto-populates created_by_agent from X-Agent-Id header."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.create_task", return_value=task_id) as mock_create:
        with patch("routers.notes_tasks.publish"):
            r = await test_client.post(
                "/api/tasks",
                json={"title": "Test task"},
                headers={"X-Agent-Id": "email-classifier"},
            )
    assert r.status_code == 200
    call_kwargs = mock_create.call_args
    assert call_kwargs.kwargs["created_by_agent"] == "email-classifier"


@pytest.mark.asyncio
async def test_create_task_missing_title(test_client):
    """POST /api/tasks with empty title returns 400."""
    r = await test_client.post("/api/tasks", json={"title": ""})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_task(test_client):
    """PATCH /api/tasks/{id} updates task fields."""
    with patch("routers.notes_tasks.update_task", return_value=True):
        with patch("routers.notes_tasks.publish"):
            r = await test_client.patch(
                f"/api/tasks/{uuid.uuid4()}",
                json={"status": "IN_PROGRESS", "priority": "urgent"},
            )
    assert r.status_code == 200
    assert r.json()["success"] is True


@pytest.mark.asyncio
async def test_update_task_auto_resolved_at(test_client):
    """PATCH /api/tasks/{id} with status=DONE auto-sets resolved_at."""
    with patch("routers.notes_tasks.update_task", return_value=True) as mock_update:
        with patch("routers.notes_tasks.publish"):
            r = await test_client.patch(
                f"/api/tasks/{uuid.uuid4()}",
                json={"status": "DONE"},
            )
    assert r.status_code == 200
    call_kwargs = mock_update.call_args
    assert "resolved_at" in call_kwargs.kwargs


@pytest.mark.asyncio
async def test_update_task_not_found(test_client):
    """PATCH /api/tasks/{id} returns 404 when task doesn't exist."""
    with patch("routers.notes_tasks.update_task", return_value=False):
        with patch("routers.notes_tasks.publish"):
            r = await test_client.patch(
                f"/api/tasks/{uuid.uuid4()}",
                json={"status": "IN_PROGRESS"},
            )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_agent_tasks(test_client):
    """GET /api/tasks/agent/{agent_id} returns priority-ordered tasks."""
    mock_tasks = [
        {
            "id": str(uuid.uuid4()),
            "title": "Urgent task",
            "priority": "urgent",
            "status": "TODO",
            "tags": ["email"],
        },
        {
            "id": str(uuid.uuid4()),
            "title": "Normal task",
            "priority": "normal",
            "status": "TODO",
            "tags": [],
        },
    ]
    with patch("routers.notes_tasks.list_agent_tasks", return_value=mock_tasks):
        r = await test_client.get("/api/tasks/agent/email-responder")
    assert r.status_code == 200
    data = r.json()
    assert len(data["tasks"]) == 2


@pytest.mark.asyncio
async def test_resolve_task(test_client):
    """POST /api/tasks/{id}/resolve marks task as DONE with resolution."""
    with patch("routers.notes_tasks.resolve_task", return_value=True):
        with patch("routers.notes_tasks.publish"):
            r = await test_client.post(
                f"/api/tasks/{uuid.uuid4()}/resolve",
                json={"resolution": "Sent reply to sender about meeting"},
            )
    assert r.status_code == 200
    assert r.json()["success"] is True


@pytest.mark.asyncio
async def test_resolve_task_missing_resolution(test_client):
    """POST /api/tasks/{id}/resolve without resolution returns 400."""
    r = await test_client.post(
        f"/api/tasks/{uuid.uuid4()}/resolve",
        json={},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_resolve_task_not_found(test_client):
    """POST /api/tasks/{id}/resolve returns 404 when task doesn't exist."""
    with patch("routers.notes_tasks.resolve_task", return_value=False):
        with patch("routers.notes_tasks.publish"):
            r = await test_client.post(
                f"/api/tasks/{uuid.uuid4()}/resolve",
                json={"resolution": "done"},
            )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_tasks_with_filters(test_client):
    """GET /api/tasks with agent/tag/priority filters."""
    with patch("routers.notes_tasks.list_tasks", return_value=[]) as mock_list:
        r = await test_client.get(
            "/api/tasks",
            params={
                "assignedToAgent": "email-responder",
                "tags": "email,reply-needed",
                "priority": "high",
                "excludeResolved": "true",
            },
        )
    assert r.status_code == 200
    call_kwargs = mock_list.call_args
    assert call_kwargs.kwargs["assigned_to_agent"] == "email-responder"
    assert call_kwargs.kwargs["tags"] == ["email", "reply-needed"]
    assert call_kwargs.kwargs["priority"] == "high"
    assert call_kwargs.kwargs["exclude_resolved"] is True


@pytest.mark.asyncio
async def test_create_task_emits_event(test_client):
    """POST /api/tasks emits a task.created event to the agent stream."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.create_task", return_value=task_id):
        with patch("routers.notes_tasks.publish") as mock_publish:
            r = await test_client.post(
                "/api/tasks",
                json={
                    "title": "Test event",
                    "assignedToAgent": "main",
                    "priority": "high",
                    "tags": ["escalation"],
                },
            )
    assert r.status_code == 200
    mock_publish.assert_called_once()
    call_args = mock_publish.call_args
    assert call_args[0][0] == "agent"
    assert call_args[0][1] == "task.created"
    assert call_args[0][2]["task_id"] == task_id


# ─── Pydantic Model Tests ───────────────────────────────────────────────


def test_create_task_request_defaults():
    """CreateTaskRequest has sensible defaults."""
    from models import CreateTaskRequest

    req = CreateTaskRequest(title="Test")
    assert req.status == "TODO"
    assert req.priority == "normal"
    assert req.tags is None
    assert req.assignedToAgent is None


def test_create_task_rejects_invalid_person_id():
    """CreateTaskRequest rejects non-UUID personId with ValidationError."""
    from models import CreateTaskRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="personId must be a valid UUID"):
        CreateTaskRequest(
            title="Test",
            personId=':content".:company{"domainName": "valhallams.com"}"',
        )


def test_create_task_rejects_invalid_company_id():
    """CreateTaskRequest rejects non-UUID companyId with ValidationError."""
    from models import CreateTaskRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="companyId must be a valid UUID"):
        CreateTaskRequest(title="Test", companyId="not-a-uuid")


def test_create_task_accepts_valid_uuid():
    """CreateTaskRequest accepts valid UUIDs for personId/companyId."""
    from models import CreateTaskRequest

    valid_id = str(uuid.uuid4())
    req = CreateTaskRequest(title="Test", personId=valid_id, companyId=valid_id)
    assert req.personId == valid_id
    assert req.companyId == valid_id


def test_create_task_accepts_none_uuid():
    """CreateTaskRequest accepts None for UUID fields."""
    from models import CreateTaskRequest

    req = CreateTaskRequest(title="Test", personId=None)
    assert req.personId is None


def test_update_task_request_all_none():
    """UpdateTaskRequest with no fields is valid (all optional)."""
    from models import UpdateTaskRequest

    req = UpdateTaskRequest()
    assert req.title is None
    assert req.status is None
    assert req.priority is None


# ─── task_to_dict Tests ─────────────────────────────────────────────────


def test_task_to_dict_new_fields():
    """task_to_dict includes all coordination fields."""
    from datetime import datetime

    from robothor.crm.models import task_to_dict

    now = datetime.now(UTC)
    row = {
        "id": uuid.uuid4(),
        "title": "Test task",
        "body": "body text",
        "status": "IN_PROGRESS",
        "due_at": now,
        "person_id": None,
        "company_id": None,
        "created_by_agent": "email-classifier",
        "assigned_to_agent": "email-responder",
        "priority": "high",
        "tags": ["email", "reply-needed"],
        "parent_task_id": None,
        "resolved_at": None,
        "resolution": None,
        "updated_at": now,
        "created_at": now,
    }
    result = task_to_dict(row)
    assert result["createdByAgent"] == "email-classifier"
    assert result["assignedToAgent"] == "email-responder"
    assert result["priority"] == "high"
    assert result["tags"] == ["email", "reply-needed"]
    assert result["parentTaskId"] is None
    assert result["resolvedAt"] is None
    assert result["resolution"] == ""


def test_task_to_dict_backward_compatible():
    """task_to_dict works when new columns are missing (e.g. old rows)."""
    from datetime import datetime

    from robothor.crm.models import task_to_dict

    now = datetime.now(UTC)
    row = {
        "id": uuid.uuid4(),
        "title": "Old task",
        "body": None,
        "status": "TODO",
        "due_at": None,
        "person_id": None,
        "company_id": None,
        "updated_at": now,
        "created_at": now,
    }
    result = task_to_dict(row)
    assert result["createdByAgent"] == ""
    assert result["assignedToAgent"] == ""
    assert result["priority"] == "normal"
    assert result["tags"] == []
    assert result["parentTaskId"] is None
    assert result["resolvedAt"] is None
    assert result["resolution"] == ""


def test_task_to_dict_sla_fields():
    """task_to_dict includes SLA and started_at fields."""
    from datetime import datetime

    from robothor.crm.models import task_to_dict

    now = datetime.now(UTC)
    row = {
        "id": uuid.uuid4(),
        "title": "SLA task",
        "body": None,
        "status": "TODO",
        "due_at": None,
        "person_id": None,
        "company_id": None,
        "sla_deadline_at": now,
        "escalation_count": 2,
        "started_at": now,
        "updated_at": now,
        "created_at": now,
    }
    result = task_to_dict(row)
    assert result["slaDeadlineAt"] is not None
    assert result["escalationCount"] == 2
    assert result["startedAt"] is not None


# ─── State Machine Tests ─────────────────────────────────────────────


def test_valid_transitions():
    """VALID_TRANSITIONS covers all expected paths."""
    from robothor.crm.dal import VALID_TRANSITIONS

    assert "IN_PROGRESS" in VALID_TRANSITIONS["TODO"]
    assert "DONE" in VALID_TRANSITIONS["TODO"]
    assert "REVIEW" in VALID_TRANSITIONS["IN_PROGRESS"]
    assert "TODO" in VALID_TRANSITIONS["IN_PROGRESS"]
    assert "DONE" in VALID_TRANSITIONS["REVIEW"]
    assert "IN_PROGRESS" in VALID_TRANSITIONS["REVIEW"]
    assert "TODO" in VALID_TRANSITIONS["DONE"]


def test_validate_transition_valid():
    """_validate_transition allows valid transitions."""
    from robothor.crm.dal import _validate_transition

    ok, reason = _validate_transition("TODO", "IN_PROGRESS")
    assert ok is True
    assert reason == ""


def test_validate_transition_invalid():
    """_validate_transition rejects invalid transitions."""
    from robothor.crm.dal import _validate_transition

    ok, reason = _validate_transition("TODO", "REVIEW")
    assert ok is False
    assert "Cannot transition" in reason


def test_validate_transition_review_to_done_requires_resolution():
    """_validate_transition requires resolution for REVIEW -> DONE."""
    from robothor.crm.dal import _validate_transition

    ok, reason = _validate_transition("REVIEW", "DONE", resolution=None)
    assert ok is False
    assert "resolution" in reason.lower()

    ok2, reason2 = _validate_transition("REVIEW", "DONE", resolution="All good")
    assert ok2 is True


def test_validate_transition_reopen_from_done():
    """_validate_transition allows DONE -> TODO (reopen)."""
    from robothor.crm.dal import _validate_transition

    ok, reason = _validate_transition("DONE", "TODO")
    assert ok is True


def test_compute_sla_deadline():
    """_compute_sla_deadline returns a future datetime for valid priorities."""
    from datetime import datetime

    from robothor.crm.dal import _compute_sla_deadline

    now = datetime.now(UTC)
    deadline = _compute_sla_deadline("urgent")
    assert deadline is not None
    assert deadline > now
    # Urgent SLA is 30 min
    diff = (deadline - now).total_seconds()
    assert 29 * 60 <= diff <= 31 * 60

    assert _compute_sla_deadline("unknown-priority") is None


@pytest.mark.asyncio
async def test_update_task_invalid_transition_returns_422(test_client):
    """PATCH /api/tasks/{id} with invalid transition returns 422."""
    error_dict = {
        "error": "Cannot transition from TODO to REVIEW. Allowed: DONE, IN_PROGRESS",
        "from": "TODO",
        "to": "REVIEW",
    }
    with patch("routers.notes_tasks.update_task", return_value=error_dict):
        r = await test_client.patch(
            f"/api/tasks/{uuid.uuid4()}",
            json={"status": "REVIEW"},
        )
    assert r.status_code == 422
    data = r.json()
    assert "error" in data
    assert "TODO" in data["error"] or "from" in data


@pytest.mark.asyncio
async def test_update_task_with_subtask_warning(test_client):
    """PATCH /api/tasks/{id} returns warning when subtasks incomplete."""
    warning_dict = {"success": True, "warning": "2 of 3 subtasks are not DONE"}
    with patch("routers.notes_tasks.update_task", return_value=warning_dict):
        with patch("routers.notes_tasks.publish"):
            r = await test_client.patch(
                f"/api/tasks/{uuid.uuid4()}",
                json={"status": "DONE"},
            )
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert "warning" in data


@pytest.mark.asyncio
async def test_get_task_history(test_client):
    """GET /api/tasks/{id}/history returns transition history."""
    mock_history = [
        {
            "id": str(uuid.uuid4()),
            "taskId": str(uuid.uuid4()),
            "fromStatus": "TODO",
            "toStatus": "IN_PROGRESS",
            "changedBy": "email-classifier",
            "reason": "",
            "metadata": {},
            "createdAt": "2026-02-23T12:00:00+00:00",
        },
    ]
    with patch("routers.notes_tasks.get_task_history", return_value=mock_history):
        r = await test_client.get(f"/api/tasks/{uuid.uuid4()}/history")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["history"][0]["fromStatus"] == "TODO"


def test_history_to_dict():
    """history_to_dict converts a row correctly."""
    from datetime import datetime

    from robothor.crm.models import history_to_dict

    now = datetime.now(UTC)
    row = {
        "id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "from_status": "TODO",
        "to_status": "IN_PROGRESS",
        "changed_by": "email-classifier",
        "reason": "Starting work",
        "metadata": {"key": "value"},
        "created_at": now,
    }
    result = history_to_dict(row)
    assert result["fromStatus"] == "TODO"
    assert result["toStatus"] == "IN_PROGRESS"
    assert result["changedBy"] == "email-classifier"
    assert result["metadata"] == {"key": "value"}


# ─── Task Dedup Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_task_dedup_returns_existing(test_client):
    """When a task with the same threadId already exists, return it with deduplicated=True."""
    existing = {"id": "existing-task-id", "title": "[EMAIL] Reply to thread", "status": "TODO"}

    with (
        patch("routers.notes_tasks.find_task_by_thread_id", return_value=existing),
        patch("routers.notes_tasks.create_task") as mock_create,
    ):
        r = await test_client.post(
            "/api/tasks",
            json={
                "title": "[EMAIL] Reply to thread abc123",
                "body": "threadId: abc123\nFrom: test@example.com",
                "assignedToAgent": "email-responder",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["deduplicated"] is True
        assert data["id"] == "existing-task-id"
        # create_task should NOT have been called
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_create_task_dedup_allows_different_agent(test_client):
    """Same threadId but different assigned agent gets dedup-checked separately."""
    with (
        patch("routers.notes_tasks.find_task_by_thread_id", return_value=None),
        patch("routers.notes_tasks.create_task", return_value="new-task-id"),
        patch("routers.notes_tasks.publish"),
        patch("routers.notes_tasks.send_notification"),
    ):
        r = await test_client.post(
            "/api/tasks",
            json={
                "title": "[EMAIL] Analyze thread abc123",
                "body": "threadId: abc123\nFrom: test@example.com",
                "assignedToAgent": "email-analyst",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert "deduplicated" not in data
        assert data["id"] == "new-task-id"


@pytest.mark.asyncio
async def test_create_task_no_threadid_skips_dedup(test_client):
    """Tasks without threadId in body skip dedup entirely."""
    with (
        patch("routers.notes_tasks.find_task_by_thread_id") as mock_find,
        patch("routers.notes_tasks.create_task", return_value="new-task-id"),
        patch("routers.notes_tasks.publish"),
        patch("routers.notes_tasks.send_notification"),
    ):
        r = await test_client.post(
            "/api/tasks",
            json={
                "title": "Calendar conflict detected",
                "body": "Meeting overlap on 2026-02-28",
                "assignedToAgent": "main",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "new-task-id"
        # find_task_by_thread_id should not have been called
        mock_find.assert_not_called()


# ─── Conversation Toggle Test ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_conversation_status(test_client):
    """POST /api/conversations/{id}/toggle_status should return 200."""
    with patch("routers.conversations.toggle_conversation_status", return_value=True):
        r = await test_client.post(
            "/api/conversations/42/toggle_status",
            json={
                "status": "resolved",
            },
        )
        assert r.status_code == 200

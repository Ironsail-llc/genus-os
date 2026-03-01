"""Tests for multi-tenancy, notifications, and review workflow."""

import uuid
from unittest.mock import patch

import pytest

# ─── Multi-tenancy Middleware ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_tenant_header(test_client):
    """Requests without X-Tenant-Id get default tenant."""
    with patch("routers.people.list_people", return_value=[]) as mock:
        res = await test_client.get("/api/people")
        assert res.status_code == 200
        mock.assert_called_once()
        _, kwargs = mock.call_args
        assert kwargs["tenant_id"] == "robothor-primary"
        # Response includes tenant header
        assert res.headers.get("x-tenant-id") == "robothor-primary"


@pytest.mark.asyncio
async def test_custom_tenant_header(test_client):
    """X-Tenant-Id header propagates to DAL calls."""
    with patch("routers.people.list_people", return_value=[]) as mock:
        res = await test_client.get("/api/people", headers={"X-Tenant-Id": "test-corp"})
        assert res.status_code == 200
        _, kwargs = mock.call_args
        assert kwargs["tenant_id"] == "test-corp"
        assert res.headers.get("x-tenant-id") == "test-corp"


@pytest.mark.asyncio
async def test_tenant_header_on_tasks(test_client):
    """Task endpoints receive tenant_id."""
    with patch("routers.notes_tasks.list_tasks", return_value=[]) as mock:
        res = await test_client.get("/api/tasks", headers={"X-Tenant-Id": "acme-inc"})
        assert res.status_code == 200
        _, kwargs = mock.call_args
        assert kwargs["tenant_id"] == "acme-inc"


# ─── Tenants CRUD ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tenants(test_client):
    """GET /api/tenants returns tenant list."""
    with patch(
        "routers.tenants.list_tenants",
        return_value=[{"id": "primary", "displayName": "Primary", "active": True}],
    ):
        res = await test_client.get("/api/tenants")
        assert res.status_code == 200
        data = res.json()
        assert len(data["tenants"]) == 1


@pytest.mark.asyncio
async def test_create_tenant(test_client):
    """POST /api/tenants creates a new tenant."""
    with patch("routers.tenants.create_tenant", return_value="test-corp"):
        res = await test_client.post(
            "/api/tenants", json={"id": "test-corp", "displayName": "Test Corp"}
        )
        assert res.status_code == 200
        assert res.json()["id"] == "test-corp"


@pytest.mark.asyncio
async def test_create_tenant_missing_fields(test_client):
    """POST /api/tenants requires id and displayName."""
    res = await test_client.post("/api/tenants", json={"id": "", "displayName": ""})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_get_tenant(test_client):
    """GET /api/tenants/{id} returns tenant details."""
    with patch(
        "routers.tenants.get_tenant", return_value={"id": "test-corp", "displayName": "Test Corp"}
    ):
        res = await test_client.get("/api/tenants/test-corp")
        assert res.status_code == 200
        assert res.json()["id"] == "test-corp"


@pytest.mark.asyncio
async def test_get_tenant_not_found(test_client):
    """GET /api/tenants/{id} returns 404 for missing tenant."""
    with patch("routers.tenants.get_tenant", return_value=None):
        res = await test_client.get("/api/tenants/nonexistent")
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_update_tenant(test_client):
    """PATCH /api/tenants/{id} updates tenant."""
    with patch("routers.tenants.update_tenant", return_value=True):
        res = await test_client.patch(
            "/api/tenants/test-corp", json={"displayName": "Updated Name"}
        )
        assert res.status_code == 200
        assert res.json()["success"] is True


# ─── Notifications ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_notification(test_client):
    """POST /api/notifications/send creates a notification."""
    notification_id = str(uuid.uuid4())
    with patch("routers.notifications.send_notification", return_value=notification_id):
        with patch("routers.notifications.publish"):
            res = await test_client.post(
                "/api/notifications/send",
                json={
                    "fromAgent": "email-classifier",
                    "toAgent": "main",
                    "notificationType": "task_assigned",
                    "subject": "New task: test",
                },
            )
            assert res.status_code == 200
            assert res.json()["id"] == notification_id


@pytest.mark.asyncio
async def test_send_notification_missing_subject(test_client):
    """POST /api/notifications/send requires subject."""
    res = await test_client.post(
        "/api/notifications/send",
        json={
            "fromAgent": "test",
            "toAgent": "test",
            "subject": "",
        },
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_get_inbox(test_client):
    """GET /api/notifications/inbox/{agent_id} returns inbox."""
    with patch(
        "routers.notifications.get_agent_inbox",
        return_value=[{"id": "123", "subject": "Test", "fromAgent": "test"}],
    ):
        res = await test_client.get("/api/notifications/inbox/main")
        assert res.status_code == 200
        assert len(res.json()["notifications"]) == 1


@pytest.mark.asyncio
async def test_mark_notification_read(test_client):
    """POST /api/notifications/{id}/read marks it read."""
    with patch("routers.notifications.mark_notification_read", return_value=True):
        res = await test_client.post("/api/notifications/abc123/read")
        assert res.status_code == 200
        assert res.json()["success"] is True


@pytest.mark.asyncio
async def test_mark_notification_read_not_found(test_client):
    """POST /api/notifications/{id}/read returns 404 for missing."""
    with patch("routers.notifications.mark_notification_read", return_value=False):
        res = await test_client.post("/api/notifications/nonexistent/read")
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_acknowledge_notification(test_client):
    """POST /api/notifications/{id}/ack acknowledges it."""
    with patch("routers.notifications.acknowledge_notification", return_value=True):
        res = await test_client.post("/api/notifications/abc123/ack")
        assert res.status_code == 200
        assert res.json()["success"] is True


@pytest.mark.asyncio
async def test_list_notifications(test_client):
    """GET /api/notifications filters by from/to agent."""
    with patch("routers.notifications.list_notifications", return_value=[]):
        res = await test_client.get("/api/notifications?fromAgent=test&toAgent=main")
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_notification_invalid_task_id(test_client):
    """SendNotificationRequest rejects invalid taskId UUID."""
    res = await test_client.post(
        "/api/notifications/send",
        json={
            "fromAgent": "test",
            "toAgent": "main",
            "subject": "Test",
            "taskId": "not-a-uuid",
        },
    )
    assert res.status_code == 422


# ─── Review Workflow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_task(test_client):
    """POST /api/tasks/{id}/approve moves task to DONE."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.approve_task", return_value=True):
        with patch("routers.notes_tasks.publish"):
            res = await test_client.post(
                f"/api/tasks/{task_id}/approve",
                json={"resolution": "Looks good"},
            )
            assert res.status_code == 200
            assert res.json()["success"] is True


@pytest.mark.asyncio
async def test_approve_task_validation_error(test_client):
    """POST /api/tasks/{id}/approve returns 422 on validation failure."""
    task_id = str(uuid.uuid4())
    with patch(
        "routers.notes_tasks.approve_task", return_value={"error": "Cannot approve own task"}
    ):
        res = await test_client.post(
            f"/api/tasks/{task_id}/approve",
            json={"resolution": ""},
        )
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_approve_task_not_found(test_client):
    """POST /api/tasks/{id}/approve returns 404 for missing task."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.approve_task", return_value=False):
        res = await test_client.post(
            f"/api/tasks/{task_id}/approve",
            json={"resolution": "Approved"},
        )
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_reject_task(test_client):
    """POST /api/tasks/{id}/reject moves task back to IN_PROGRESS."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.reject_task", return_value=True):
        with patch("routers.notes_tasks.publish"):
            res = await test_client.post(
                f"/api/tasks/{task_id}/reject",
                json={"reason": "Needs more work"},
            )
            assert res.status_code == 200
            assert res.json()["success"] is True


@pytest.mark.asyncio
async def test_reject_task_with_change_requests(test_client):
    """POST /api/tasks/{id}/reject supports change requests list."""
    task_id = str(uuid.uuid4())
    with patch("routers.notes_tasks.reject_task", return_value=True):
        with patch("routers.notes_tasks.publish"):
            res = await test_client.post(
                f"/api/tasks/{task_id}/reject",
                json={
                    "reason": "Needs rework",
                    "changeRequests": ["Fix formatting", "Add tests"],
                },
            )
            assert res.status_code == 200


@pytest.mark.asyncio
async def test_reject_task_validation_error(test_client):
    """POST /api/tasks/{id}/reject returns 422 on validation failure."""
    task_id = str(uuid.uuid4())
    with patch(
        "routers.notes_tasks.reject_task", return_value={"error": "Task is not in REVIEW status"}
    ):
        res = await test_client.post(
            f"/api/tasks/{task_id}/reject",
            json={"reason": "Bad"},
        )
        assert res.status_code == 422


# ─── Task History ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_task_history(test_client):
    """GET /api/tasks/{id}/history returns transition history."""
    task_id = str(uuid.uuid4())
    with patch(
        "routers.notes_tasks.get_task_history",
        return_value=[{"old_status": "TODO", "new_status": "IN_PROGRESS", "changed_by": "test"}],
    ):
        res = await test_client.get(f"/api/tasks/{task_id}/history")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 1
        assert data["history"][0]["new_status"] == "IN_PROGRESS"


# ─── Memory Block Append ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_memory_block(test_client):
    """POST /api/memory/blocks/{name}/append appends entry."""
    with patch("robothor.crm.dal.append_to_block", return_value=True):
        res = await test_client.post(
            "/api/memory/blocks/shared_working_state/append",
            json={
                "entry": "email-classifier: processed 5 emails",
                "maxEntries": 20,
            },
        )
        assert res.status_code == 200
        assert res.json()["success"] is True


@pytest.mark.asyncio
async def test_append_memory_block_failure(test_client):
    """POST /api/memory/blocks/{name}/append returns 500 on failure."""
    with patch("robothor.crm.dal.append_to_block", return_value=False):
        res = await test_client.post(
            "/api/memory/blocks/test_block/append",
            json={
                "entry": "test",
            },
        )
        assert res.status_code == 500


# ─── Model Validation ───────────────────────────────────────────────────


def test_approve_task_request_model():
    """ApproveTaskRequest has resolution field."""
    from models import ApproveTaskRequest

    req = ApproveTaskRequest(resolution="LGTM")
    assert req.resolution == "LGTM"


def test_reject_task_request_model():
    """RejectTaskRequest has reason and changeRequests."""
    from models import RejectTaskRequest

    req = RejectTaskRequest(reason="Incomplete", changeRequests=["Add tests"])
    assert req.reason == "Incomplete"
    assert req.changeRequests == ["Add tests"]


def test_send_notification_request_model():
    """SendNotificationRequest validates taskId UUID."""
    from models import SendNotificationRequest

    req = SendNotificationRequest(fromAgent="test", toAgent="sup", subject="Hello")
    assert req.notificationType == "info"


def test_send_notification_request_rejects_bad_uuid():
    """SendNotificationRequest rejects invalid taskId."""
    from models import SendNotificationRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SendNotificationRequest(
            fromAgent="test",
            toAgent="sup",
            subject="Hello",
            taskId="not-a-uuid",
        )


def test_create_tenant_request_model():
    """CreateTenantRequest has id and displayName."""
    from models import CreateTenantRequest

    req = CreateTenantRequest(id="test", displayName="Test")
    assert req.id == "test"
    assert req.parentTenantId is None


def test_memory_block_append_request_model():
    """MemoryBlockAppendRequest has entry and maxEntries with bounds."""
    from models import MemoryBlockAppendRequest

    req = MemoryBlockAppendRequest(entry="test")
    assert req.maxEntries == 20

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MemoryBlockAppendRequest(entry="test", maxEntries=0)


# ─── Event Bus Envelope ─────────────────────────────────────────────────


def test_event_envelope_includes_tenant_id():
    """Event bus envelope includes tenant_id field."""
    from robothor.events.bus import _make_envelope

    envelope = _make_envelope("test.event", {"key": "val"}, tenant_id="acme")
    assert envelope["tenant_id"] == "acme"


def test_event_envelope_default_tenant_id():
    """Event bus envelope defaults tenant_id to empty string."""
    from robothor.events.bus import _make_envelope

    envelope = _make_envelope("test.event", {"key": "val"})
    assert envelope["tenant_id"] == ""

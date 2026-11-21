"""Marking a notification read or acknowledged is the recipient's (story I4).

``POST /api/notifications/{id}/read`` and ``/ack`` matched the id and tenant
and nothing else, so any member could clear another member's workspace notes.
Now only the notification's recipient (its ``toAgent`` is their own
``actor_id``) may, and an operator (owner/admin) keeps the access they had.
A refused call never reaches the write, and a notification that does not
exist and one that belongs to somebody else read differently only to an
operator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HUMAN_SUBJECT = "human-1"
OTHER_MEMBER = "8a2f1c00-0000-4000-8000-000000000999"
NOTE = "3f0e4c1a-0000-4000-8000-000000000001"


def _call(client, action: str, owner: str | None):
    row = None if owner is None else {"id": NOTE, "toAgent": owner}
    write = "mark_notification_read" if action == "read" else "acknowledge_notification"
    with (
        patch("routers.notifications.get_notification", return_value=row),
        patch(f"routers.notifications.{write}", return_value=True) as written,
    ):
        response = client.post(f"/api/notifications/{NOTE}/{action}")
    return response, written


@pytest.mark.parametrize("action", ["read", "ack"])
def test_the_recipient_may(controls_client_as_user, action):
    response, written = _call(controls_client_as_user, action, HUMAN_SUBJECT)
    assert response.status_code == 200
    written.assert_called_once()


@pytest.mark.parametrize("action", ["read", "ack"])
def test_another_member_may_not(controls_client_as_user, action):
    response, written = _call(controls_client_as_user, action, OTHER_MEMBER)
    assert response.status_code == 403
    written.assert_not_called()


@pytest.mark.parametrize("action", ["read", "ack"])
def test_a_viewer_may_not_clear_someone_elses(controls_client_as_viewer, action):
    response, written = _call(controls_client_as_viewer, action, OTHER_MEMBER)
    assert response.status_code == 403
    written.assert_not_called()


@pytest.mark.parametrize("action", ["read", "ack"])
def test_a_missing_notification_is_404_and_writes_nothing(controls_client_as_user, action):
    response, written = _call(controls_client_as_user, action, None)
    assert response.status_code == 404
    written.assert_not_called()


@pytest.mark.parametrize("action", ["read", "ack"])
def test_an_operator_keeps_access_to_any(controls_client_as_operator, action):
    response, written = _call(controls_client_as_operator, action, OTHER_MEMBER)
    assert response.status_code == 200
    written.assert_called_once()


@pytest.mark.parametrize("action", ["read", "ack"])
def test_an_admin_keeps_access_to_any(controls_client_as_admin, action):
    response, written = _call(controls_client_as_admin, action, OTHER_MEMBER)
    assert response.status_code == 200

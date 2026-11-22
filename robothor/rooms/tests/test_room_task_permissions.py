# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""The task tools under the role seed: staff and admins may use them, viewers may not.

Against the real database-backed evaluator, as ``test_room_permissions.py``.
"""

from __future__ import annotations

import pytest

from robothor.rooms.tests.conftest import rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401
from robothor.rooms.tests.test_room_permissions import _denied, rbac  # noqa: F401

TASK_TOOLS = ("room_task_accept", "room_task_dismiss", "room_open_task", "room_propose_email")

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("role", ["member", "user", "owner", "admin"])
def test_staff_and_admins_may_use_the_task_tools(rbac, role):
    for tool in TASK_TOOLS:
        assert not _denied(role, rbac.tenant, tool), tool


def test_a_viewer_may_not(rbac):
    for tool in TASK_TOOLS:
        assert _denied("viewer", rbac.tenant, tool), tool


def test_a_member_still_cannot_send_mail_directly(rbac):
    """The member path proposes; it never grants the send tools (migration 088)."""
    for tool in ("gws_gmail_send", "gws_gmail_reply"):
        assert _denied("member", rbac.tenant, tool), tool


def test_the_user_roles_direct_send_is_unchanged(rbac):
    """Regression only: ``user`` already allows every tool, sending included."""
    for tool in ("gws_gmail_send", "gws_gmail_reply"):
        assert not _denied("user", rbac.tenant, tool), tool

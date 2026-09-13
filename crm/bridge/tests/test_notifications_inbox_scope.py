"""Whose inbox a caller may read.

``GET /api/notifications/inbox/{agent_id}`` was tenant-scoped and nothing else:
``get_agent_inbox`` matches ``to_agent = %s AND tenant_id = %s``, the route had
no ownership check, ``/api/notifications`` is not in the Helm's
``DENIED_BRIDGE_PATHS``, and the BFF proxy attaches the caller's own bearer to
anything not denied. That was survivable while ``to_agent`` held AGENT ids and
the bodies were ops chatter between agents.

The webchat channel changed what is in there. It writes a member's delivery —
the full text of what an agent said to that person — with
``to_agent = <user_accounts.id>``. So the un-scoped read became a way for any
authenticated same-tenant member to read another member's chat deliveries: the
exact isolation the per-user session work exists to provide, bypassed through
the other half of the same write.

The rule these tests pin: owner/admin may read any inbox (an operator triaging
the fleet needs that, and ``require_operator`` already admits exactly those two);
everybody else may read only the inbox whose id is their own ``actor_id`` — their
``user_accounts.id`` for a human session, the agent id for a service token.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: The subjects ``conftest``'s client fixtures mint tokens for.
OPERATOR_SUBJECT = "operator-1"
HUMAN_SUBJECT = "human-1"
OTHER_MEMBER = "8a2f1c00-0000-4000-8000-000000000999"

INBOX = [{"id": "n-1", "subject": "Two invoices need approval.", "fromAgent": "worker"}]


def _inbox(client, agent_id: str):
    with patch("routers.notifications.get_agent_inbox", return_value=INBOX) as read:
        response = client.get(f"/api/notifications/inbox/{agent_id}")
    return response, read


def test_an_operator_may_read_any_inbox(controls_client_as_operator):
    """Owner/admin keep the fleet-wide view they already have everywhere else."""
    response, read = _inbox(controls_client_as_operator, OTHER_MEMBER)

    assert response.status_code == 200
    assert len(response.json()["notifications"]) == 1
    assert read.call_args.kwargs["agent_id"] == OTHER_MEMBER


def test_an_admin_may_read_any_inbox(controls_client_as_admin):
    response, _ = _inbox(controls_client_as_admin, OTHER_MEMBER)
    assert response.status_code == 200


def test_a_member_may_read_their_own_inbox(controls_client_as_user):
    """The case that has to keep working: a member's own webchat deliveries."""
    response, read = _inbox(controls_client_as_user, HUMAN_SUBJECT)

    assert response.status_code == 200
    assert read.call_args.kwargs["agent_id"] == HUMAN_SUBJECT


def test_a_member_may_not_read_another_members_inbox(controls_client_as_user):
    """The hole. A 403, and the DAL is never reached — a refused read must not
    be a read that happened and was discarded."""
    response, read = _inbox(controls_client_as_user, OTHER_MEMBER)

    assert response.status_code == 403
    read.assert_not_called()


def test_a_viewer_may_not_read_another_members_inbox(controls_client_as_viewer):
    response, read = _inbox(controls_client_as_viewer, OTHER_MEMBER)
    assert response.status_code == 403
    read.assert_not_called()


def test_a_service_token_may_read_only_its_own_agents_inbox(controls_client_as_service):
    """An agent's own tool call reads the DAL directly (``handlers/crm.py``), so
    this route is only reached by a client naming an inbox — and naming somebody
    else's is the same defect whether the caller is a person or an agent."""
    response, read = _inbox(controls_client_as_service, OTHER_MEMBER)

    assert response.status_code == 403
    read.assert_not_called()


def test_a_member_reading_another_inbox_through_a_query_string_is_still_refused(
    controls_client_as_user,
):
    """The id is a path segment, so there is no second place to smuggle it —
    pinned because a future rewrite to a query parameter would need its own gate."""
    with patch("routers.notifications.get_agent_inbox", return_value=INBOX) as read:
        response = controls_client_as_user.get(
            f"/api/notifications/inbox/{OTHER_MEMBER}?unreadOnly=false&limit=200"
        )

    assert response.status_code == 403
    read.assert_not_called()


# ─── The sibling route that reads the same table ─────────────────────
#
# Guarding the inbox path alone would have been theatre: `GET /api/notifications`
# takes `toAgent` as a QUERY parameter and answers from the same rows, and with
# no filter at all it returns the whole tenant's notifications. "Guard the path
# every caller crosses, not the one the current caller uses."


def _listed(client, query: str = ""):
    with patch("routers.notifications.list_notifications", return_value=INBOX) as read:
        response = client.get(f"/api/notifications{query}")
    return response, read


def test_an_operator_may_list_any_notifications(controls_client_as_operator):
    response, read = _listed(controls_client_as_operator, f"?toAgent={OTHER_MEMBER}")

    assert response.status_code == 200
    assert read.call_args.kwargs["to_agent"] == OTHER_MEMBER


def test_an_operator_listing_without_a_filter_still_sees_the_tenant(controls_client_as_operator):
    response, read = _listed(controls_client_as_operator)

    assert response.status_code == 200
    assert read.call_args.kwargs["to_agent"] is None


def test_a_member_listing_is_scoped_to_themselves(controls_client_as_user):
    """Not a 403: a member asking for "my notifications" is a legitimate read.
    The server decides whose, exactly as ``_effective_session_key`` decides which
    session — an unfiltered list must not answer with the tenant's."""
    response, read = _listed(controls_client_as_user)

    assert response.status_code == 200
    assert read.call_args.kwargs["to_agent"] == HUMAN_SUBJECT


def test_a_member_may_not_list_another_members_notifications(controls_client_as_user):
    response, read = _listed(controls_client_as_user, f"?toAgent={OTHER_MEMBER}")

    assert response.status_code == 403
    read.assert_not_called()


def test_a_member_may_still_filter_their_own_by_sender(controls_client_as_user):
    response, read = _listed(controls_client_as_user, f"?fromAgent=worker&toAgent={HUMAN_SUBJECT}")

    assert response.status_code == 200
    assert read.call_args.kwargs["from_agent"] == "worker"
    assert read.call_args.kwargs["to_agent"] == HUMAN_SUBJECT

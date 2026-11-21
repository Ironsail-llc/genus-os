"""Who proposed an action: the run, the person whose turn it was, the workspace.

``Operations.propose`` records ``proposed_by_run_id``, ``proposed_for_user_id``
and ``room_id`` from the run it is called in. The person is recorded only for
an interactive turn of a verified user; a scheduled, cron, workflow, hook or
sub-agent run records nobody. Member approval re-checks every one of these
against the run's own row, so they are attribution, not authority.
"""

from __future__ import annotations

import uuid

import pytest
from robothor.operations.proposer import (
    Proposer,
    current_proposer,
    proposer_for_run,
    proposer_scope,
)

from robothor.operations.tests.test_store import ops  # noqa: F401
from robothor.rooms.run_context import RoomRun, room_run_scope

USER = str(uuid.uuid4())
ROOM = str(uuid.uuid4())
RUN = str(uuid.uuid4())
ROOM_KEY = f"agent:main:room:{ROOM}"


def test_an_interactive_webchat_turn_in_a_workspace_names_the_person_and_the_workspace():
    p = proposer_for_run(
        run_id=RUN, trigger_type="webchat", trigger_detail=f"webchat:{ROOM_KEY}", user_id=USER
    )
    assert p == Proposer(run_id=RUN, user_id=USER, room_id=ROOM)


def test_a_main_conversation_turn_names_the_person_and_no_workspace():
    p = proposer_for_run(
        run_id=RUN,
        trigger_type="webchat",
        trigger_detail=f"webchat:agent:main:web:{USER}",
        user_id=USER,
    )
    assert p == Proposer(run_id=RUN, user_id=USER, room_id=None)


@pytest.mark.parametrize("trigger", ["cron", "hook", "workflow", "event", "sub_agent", "manual"])
def test_a_run_with_no_human_turn_names_nobody(trigger):
    p = proposer_for_run(
        run_id=RUN, trigger_type=trigger, trigger_detail=f"webchat:{ROOM_KEY}", user_id=USER
    )
    assert p.user_id is None
    assert p.run_id == RUN


def test_a_scheduled_workspace_run_names_its_workspace_from_the_run_carrier():
    with room_run_scope(RoomRun(tenant_id="test-tenant", room_id=ROOM, session_key=ROOM_KEY)):
        p = proposer_for_run(run_id=RUN, trigger_type="cron", trigger_detail="room", user_id="")
    assert p == Proposer(run_id=RUN, user_id=None, room_id=ROOM)


@pytest.mark.parametrize("user", ["", "service:main", "loopback-development-operator", "12345"])
def test_an_identity_that_is_not_an_account_names_nobody(user):
    p = proposer_for_run(
        run_id=RUN, trigger_type="webchat", trigger_detail=f"webchat:{ROOM_KEY}", user_id=user
    )
    assert p.user_id is None


def test_the_scope_restores_what_was_there():
    outer = Proposer(run_id="outer", user_id=None, room_id=None)
    with proposer_scope(outer):
        with proposer_scope(None):
            assert current_proposer() is None
        assert current_proposer() == outer
    assert current_proposer() is None


def _row(ops, action):  # noqa: F811
    return ops.get_action(action)


def test_a_proposal_records_the_run_it_was_made_in(ops):  # noqa: F811
    with proposer_scope(Proposer(run_id=RUN, user_id=USER, room_id=ROOM)):
        action = ops.propose("sales.email", "k1", {"subject": "Hi"})

    row = _row(ops, action)
    assert row["proposed_by_run_id"] == RUN
    assert str(row["proposed_for_user_id"]) == USER
    assert str(row["room_id"]) == ROOM


def test_a_proposal_outside_a_run_records_nobody(ops):  # noqa: F811
    action = ops.propose("sales.email", "k2", {"subject": "Hi"})

    row = _row(ops, action)
    assert row["proposed_by_run_id"] is None
    assert row["proposed_for_user_id"] is None
    assert row["room_id"] is None


def test_an_explicit_proposer_wins_over_the_run(ops):  # noqa: F811
    with proposer_scope(Proposer(run_id=RUN, user_id=USER, room_id=ROOM)):
        action = ops.propose(
            "sales.email",
            "k3",
            {"subject": "Hi"},
            proposer=Proposer(run_id="other", user_id=None, room_id=None),
        )

    row = _row(ops, action)
    assert row["proposed_by_run_id"] == "other"
    assert row["proposed_for_user_id"] is None


def test_a_replayed_proposal_keeps_its_first_attribution(ops):  # noqa: F811
    with proposer_scope(Proposer(run_id=RUN, user_id=USER, room_id=ROOM)):
        first = ops.propose("sales.email", "k4", {"subject": "Hi"})
    with proposer_scope(Proposer(run_id="later", user_id=None, room_id=None)):
        again = ops.propose("sales.email", "k4", {"subject": "Hi"})

    assert again == first
    assert _row(ops, first)["proposed_by_run_id"] == RUN

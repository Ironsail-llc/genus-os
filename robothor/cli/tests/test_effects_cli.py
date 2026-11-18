"""`genus effects`: list a tenant's unresolved writes and record one's outcome with a reason."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from robothor.cli import main

ROW = {
    "id": "8a1f0c62-0000-4000-8000-000000000001",
    "principal_id": "service:workflow:acme",
    "tool_name": "acme_post_message",
    "target": {"thread_id": "thread-1"},
    "state": "uncertain",
    "goal_id": None,
    "created_at": "2026-01-01T10:00:00+00:00",
    "updated_at": "2026-01-01T10:00:05+00:00",
}


def test_list_prints_the_tenants_unresolved_effects(capsys):
    with patch("robothor.engine.runtime.effects.unresolved", return_value=[ROW]) as listed:
        assert main(["effects", "list", "--tenant", "test-tenant"]) == 0
    listed.assert_called_once_with("test-tenant")
    printed = json.loads(capsys.readouterr().out)
    assert printed["effects"][0]["id"] == ROW["id"]
    assert printed["effects"][0]["target"] == {"thread_id": "thread-1"}


def test_list_defaults_to_the_default_tenant():
    from robothor.constants import DEFAULT_TENANT

    with patch("robothor.engine.runtime.effects.unresolved", return_value=[]) as listed:
        assert main(["effects", "list"]) == 0
    listed.assert_called_once_with(DEFAULT_TENANT)


@pytest.mark.parametrize(
    ("flag", "outcome"), [("applied", "applied"), ("not-applied", "not_applied")]
)
def test_resolve_records_the_outcome_and_reason(capsys, flag, outcome):
    with patch("robothor.engine.runtime.effects.operator_resolve", return_value=True) as resolve:
        code = main(
            [
                "effects",
                "resolve",
                ROW["id"],
                "--tenant",
                "test-tenant",
                "--outcome",
                flag,
                "--reason",
                "checked the thread: the message is there",
                "--actor",
                "operator:alice",
            ]
        )
    assert code == 0
    resolve.assert_called_once_with(
        "test-tenant",
        ROW["id"],
        outcome=outcome,
        actor="operator:alice",
        reason="checked the thread: the message is there",
    )
    assert json.loads(capsys.readouterr().out) == {"resolved": True, "effect_id": ROW["id"]}


def test_resolve_needs_a_reason_and_an_outcome(capsys):
    with pytest.raises(SystemExit):
        main(["effects", "resolve", ROW["id"], "--outcome", "applied"])
    with pytest.raises(SystemExit):
        main(["effects", "resolve", ROW["id"], "--reason", "checked"])


def test_resolve_says_when_nothing_changed(capsys):
    with patch("robothor.engine.runtime.effects.operator_resolve", return_value=False):
        code = main(
            ["effects", "resolve", ROW["id"], "--outcome", "applied", "--reason", "checked"]
        )
    assert code == 1
    assert "not unresolved" in capsys.readouterr().err


def test_resolve_refuses_a_blank_reason(capsys):
    with patch(
        "robothor.engine.runtime.effects.operator_resolve",
        side_effect=ValueError("a reason and an actor are required"),
    ):
        code = main(["effects", "resolve", ROW["id"], "--outcome", "applied", "--reason", " "])
    assert code == 1
    assert "reason" in capsys.readouterr().err

"""Every agent runs as the allow-all `service` role, and nothing says so.

`rbac` is ENABLED, at ENFORCE, and its call site is reachable — proven by
firing a real violation in test_controls_are_armed.py. It has still logged
zero events in its entire existence, and the reason is not a wiring defect:

    config.py:477  service_role = manifest.get("role", manifest.get("service_role", "service"))
    migration 107  service -> ('*', 'allow')

All 25 live manifests declare no role, so every agent resolves to `service`,
and `service` permits everything. The gate works perfectly and has never had
anything to deny. An operator reading "RBAC: enforce" on the dashboard is
reading a true statement that means nothing.

This is worse than an inert control, because inert controls at least look
suspicious. Two things fix it without breaking a fleet that currently depends
on being unrestricted:

  - the fleet-wide default becomes configurable, so an operator can move
    everything to least privilege without editing 25 files
  - falling back to the allow-all role is stated out loud, once per agent,
    instead of being the silent default
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine.service_roles import (
    ALLOW_ALL_ROLE,
    DEFAULT_SERVICE_ROLE_ENV,
    default_service_role,
    resolve_service_role,
)

# ── The fleet-wide default ───────────────────────────────────────────


def test_the_default_is_still_the_allow_all_role(monkeypatch):
    """Unchanged out of the box. Flipping this would deny every tool call on
    every instance on upgrade, which is not a change to make on someone's
    behalf."""
    monkeypatch.delenv(DEFAULT_SERVICE_ROLE_ENV, raising=False)
    assert default_service_role() == ALLOW_ALL_ROLE


def test_an_operator_can_move_the_whole_fleet_to_least_privilege(monkeypatch):
    """The point: 25 manifests do not have to be edited to change posture."""
    monkeypatch.setenv(DEFAULT_SERVICE_ROLE_ENV, "viewer")
    assert default_service_role() == "viewer"


def test_a_blank_setting_does_not_produce_a_roleless_agent(monkeypatch):
    """An empty role fails closed in check_tool_permission ('Missing execution
    role'), which would take the fleet down. A blank env var is far more likely
    a deployment slip than a request for that."""
    monkeypatch.setenv(DEFAULT_SERVICE_ROLE_ENV, "   ")
    assert default_service_role() == ALLOW_ALL_ROLE


# ── Per-agent resolution ─────────────────────────────────────────────


def test_an_explicit_role_on_the_manifest_wins(monkeypatch):
    monkeypatch.setenv(DEFAULT_SERVICE_ROLE_ENV, "viewer")
    assert resolve_service_role("crm-hygiene", declared="member") == "member"


def test_an_agent_with_no_declared_role_takes_the_fleet_default(monkeypatch):
    monkeypatch.setenv(DEFAULT_SERVICE_ROLE_ENV, "member")
    assert resolve_service_role("crm-hygiene", declared="") == "member"


def test_falling_back_to_allow_all_is_stated_out_loud(monkeypatch, caplog):
    """The silence is the defect. An agent running unrestricted should say so."""
    monkeypatch.delenv(DEFAULT_SERVICE_ROLE_ENV, raising=False)
    with caplog.at_level(logging.WARNING):
        resolve_service_role("crm-hygiene", declared="")

    assert any("crm-hygiene" in r.getMessage() for r in caplog.records), caplog.text
    assert any("unrestricted" in (r.getMessage()).lower() for r in caplog.records)


def test_it_says_so_once_per_agent_not_once_per_run(monkeypatch, caplog):
    """1,757 exec calls in 7 days. A per-call warning is a log flood, and a log
    flood is how the credential pool logged the same outage 452 times."""
    monkeypatch.delenv(DEFAULT_SERVICE_ROLE_ENV, raising=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            resolve_service_role("crm-hygiene", declared="")

    warned = [r for r in caplog.records if "crm-hygiene" in (r.getMessage())]
    assert len(warned) == 1, f"warned {len(warned)} times for one agent"


def test_a_restricted_agent_is_not_warned_about(monkeypatch, caplog):
    monkeypatch.delenv(DEFAULT_SERVICE_ROLE_ENV, raising=False)
    with caplog.at_level(logging.WARNING):
        resolve_service_role("careful-agent", declared="viewer")

    assert not [r for r in caplog.records if "careful-agent" in (r.getMessage())]


def test_a_restrictive_fleet_default_is_not_warned_about(monkeypatch, caplog):
    """Once the operator has moved the fleet, the warning must stop — otherwise
    it becomes noise they learn to ignore."""
    monkeypatch.setenv(DEFAULT_SERVICE_ROLE_ENV, "member")
    with caplog.at_level(logging.WARNING):
        resolve_service_role("some-agent", declared="")

    assert not [r for r in caplog.records if "some-agent" in (r.getMessage())]


# ── The posture is reportable ────────────────────────────────────────


def test_the_unrestricted_agents_can_be_counted(monkeypatch):
    """So `/ready`, the dashboard and a ratchet can all read the same number
    instead of three places re-deriving it."""
    from robothor.engine.service_roles import unrestricted_agents

    monkeypatch.delenv(DEFAULT_SERVICE_ROLE_ENV, raising=False)
    manifests = [
        {"id": "a"},
        {"id": "b", "role": "member"},
        {"id": "c", "service_role": "service"},
        {"id": "d", "role": ""},
    ]

    assert unrestricted_agents(manifests) == ["a", "c", "d"]


def test_nothing_is_unrestricted_once_the_default_is_restrictive(monkeypatch):
    from robothor.engine.service_roles import unrestricted_agents

    monkeypatch.setenv(DEFAULT_SERVICE_ROLE_ENV, "member")

    assert unrestricted_agents([{"id": "a"}, {"id": "b", "role": "service"}]) == ["b"]


@pytest.fixture(autouse=True)
def _reset_warning_state():
    from robothor.engine import service_roles

    service_roles._warned.clear()
    yield
    service_roles._warned.clear()

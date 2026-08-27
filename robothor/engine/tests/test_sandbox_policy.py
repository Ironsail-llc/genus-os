"""`sandbox: host` beats `enforce`, so enforce is not enforcement.

Measured 2026-08-27. Six agents hold `exec`. Four of them — `main`,
`crm-hygiene`, `conversation-inbox`, `vision-monitor` — declare
`sandbox: host`, and `_resolve_sandbox_decision` honours that opt-out BEFORE it
looks at the mode. So promoting `ROBOTHOR_SANDBOX_DEFAULT_MODE` to `enforce`
would containerise `auto-agent` and `email-analyst` and nothing else, while the
operator reasonably believes they have just contained their fleet.

That is the same shape as the RBAC finding: a control whose dashboard state is
true and whose practical effect is nil. The opt-out is a legitimate escape
hatch — some agents genuinely need the host — but three things have to be true
for it to be safe:

  - it is visible, so nobody promotes a flag believing it covers everything
  - it is countable, so one number can be reported rather than re-derived
  - the operator can override it fleet-wide, so `enforce` can be made to mean
    enforce without editing every manifest

Extracted from runner.py, which is a 2,957-line god-object held at a
decomposition ratchet. This is the documented remedy — move a cohesive cluster
out — rather than raising the cap by the size of the fix.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from robothor.engine.sandbox_policy import (
    OVERRIDE_OPT_OUT_ENV,
    opted_out_of_containment,
    resolve_sandbox_decision,
)


def _agent(sandbox="", exec_tool=True):
    return SimpleNamespace(
        sandbox=sandbox,
        tools_allowed=["exec"] if exec_tool else ["search_memory"],
        tools_denied=[],
    )


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from robothor.engine import sandbox_policy

    sandbox_policy._warned.clear()
    monkeypatch.delenv(OVERRIDE_OPT_OUT_ENV, raising=False)
    yield
    sandbox_policy._warned.clear()


# ── The behaviour that was already there, pinned ─────────────────────


def test_an_explicit_docker_request_is_always_honoured():
    assert resolve_sandbox_decision(_agent(sandbox="docker"), "off") == "docker"


def test_an_exec_agent_is_observed_in_observe_mode():
    assert resolve_sandbox_decision(_agent(), "observe") == "observe"


def test_an_exec_agent_is_contained_in_enforce_mode():
    assert resolve_sandbox_decision(_agent(), "enforce") == "docker"


def test_an_agent_without_exec_is_left_on_the_host():
    assert resolve_sandbox_decision(_agent(exec_tool=False), "enforce") == "host"


def test_the_mode_being_off_leaves_everything_on_the_host():
    assert resolve_sandbox_decision(_agent(), "off") == "host"


# ── The finding ──────────────────────────────────────────────────────


def test_a_manifest_opt_out_currently_beats_enforce():
    """Pinned deliberately. This is the default and it is why promoting the
    flag would have contained two agents out of six."""
    assert resolve_sandbox_decision(_agent(sandbox="host"), "enforce") == "host"


def test_opting_out_of_containment_is_stated_out_loud(caplog):
    with caplog.at_level(logging.WARNING):
        resolve_sandbox_decision(_agent(sandbox="host"), "enforce")

    messages = [r.getMessage() for r in caplog.records]
    assert any("opts out" in m or "opt-out" in m for m in messages), messages
    assert any("enforce" in m for m in messages)


def test_the_opt_out_is_only_worth_mentioning_when_it_matters(caplog):
    """An agent with no `exec` that says `sandbox: host` is not weakening
    anything, and warning about it would be noise."""
    with caplog.at_level(logging.WARNING):
        resolve_sandbox_decision(_agent(sandbox="host", exec_tool=False), "enforce")

    assert not caplog.records


def test_it_is_not_mentioned_while_the_control_is_off(caplog):
    """Nothing is being bypassed if containment was never going to happen."""
    with caplog.at_level(logging.WARNING):
        resolve_sandbox_decision(_agent(sandbox="host"), "off")

    assert not caplog.records


# ── The override that makes enforce mean enforce ─────────────────────


def test_an_operator_can_make_enforce_beat_the_manifest(monkeypatch):
    monkeypatch.setenv(OVERRIDE_OPT_OUT_ENV, "1")

    assert resolve_sandbox_decision(_agent(sandbox="host"), "enforce") == "docker"


def test_the_override_reports_but_does_not_contain_during_observe(monkeypatch):
    """Observe must never change BEHAVIOUR — both answers run on the host — but
    it must change what is REPORTED. Otherwise the operator flips enforce
    holding a would-block set that silently excluded every opted-out agent,
    which is exactly how you contain two agents out of six and believe you
    contained the fleet."""
    monkeypatch.setenv(OVERRIDE_OPT_OUT_ENV, "1")

    assert resolve_sandbox_decision(_agent(sandbox="host"), "observe") == "observe"


def test_without_the_override_an_opted_out_agent_is_not_even_reported(monkeypatch):
    """The default, and the reason the would-block set looked reassuring: four
    of the six exec-holding agents never appear in it at all."""
    monkeypatch.delenv(OVERRIDE_OPT_OUT_ENV, raising=False)

    assert resolve_sandbox_decision(_agent(sandbox="host"), "observe") == "host"


def test_the_override_still_respects_an_explicit_docker_request(monkeypatch):
    monkeypatch.setenv(OVERRIDE_OPT_OUT_ENV, "1")

    assert resolve_sandbox_decision(_agent(sandbox="docker"), "enforce") == "docker"


def test_the_override_does_not_containerise_agents_without_exec(monkeypatch):
    """It lifts an opt-out; it does not widen who was in scope."""
    monkeypatch.setenv(OVERRIDE_OPT_OUT_ENV, "1")

    assert resolve_sandbox_decision(_agent(sandbox="host", exec_tool=False), "enforce") == "host"


# ── Countable, so a promotion can be argued from a number ────────────


def test_the_opted_out_agents_can_be_counted():
    manifests = [
        {"id": "main", "sandbox": "host", "tools_allowed": ["exec"]},
        {"id": "curator", "sandbox": "local", "tools_allowed": ["exec"]},
        {"id": "briefing", "sandbox": "host", "tools_allowed": ["search_memory"]},
        {"id": "auto", "tools_allowed": ["exec"]},
    ]

    assert opted_out_of_containment(manifests) == ["main"]


def test_nothing_is_opted_out_when_no_agent_says_host():
    assert opted_out_of_containment([{"id": "a", "tools_allowed": ["exec"]}]) == []

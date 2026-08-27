"""Checking a tool's RESULT before the model sees it.

Extracted from the tool-execution block in `_run_loop`. The security property
here is a once-vs-always pair that is easy to collapse by accident:

    redact  — EVERY time a credential is found
    notify  — ONCE per run

Collapsing them the wrong way is a real leak. If redaction were also
once-per-run, the second file containing a key would reach the model verbatim.
If notification were every time, the warning would crowd out the task.

Both halves exist because of live failures. Detection used to be a log line
alone: the platform could spot a credential in a file the agent had just read
and the agent would never know — it carried on, published it, and never warned
the user. And without redaction the agent quotes the key back while correctly
explaining why the key is dangerous, which lands it in the transcript, the
session store, and every log downstream of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robothor.engine.loop_guards import GuardState
from robothor.engine.post_execution import apply_post_execution_guardrails


def _session():
    return SimpleNamespace(messages=[])


def _engine(action="allowed", name="", reason=""):
    verdict = SimpleNamespace(action=action, guardrail_name=name, reason=reason)
    return SimpleNamespace(check_post_execution=lambda tool_name, result: verdict)


def _apply(session, engine, *, result="sk-live-abc123", error_msg=None, state=None):
    return apply_post_execution_guardrails(
        session,
        engine,
        tool_name="read_file",
        result=result,
        error_msg=error_msg,
        state=state or GuardState(),
    )


# ── Nothing to do ─────────────────────────────────────────────────────


def test_no_guardrail_engine_leaves_the_result_alone():
    assert _apply(_session(), None) == "sk-live-abc123"


def test_a_failed_call_is_not_checked():
    """The result of a failed call is an error string, not tool output."""
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key")
    session = _session()

    assert _apply(session, engine, error_msg="boom") == "sk-live-abc123"
    assert session.messages == []


def test_a_clean_result_passes_through():
    assert _apply(_session(), _engine(action="allowed")) == "sk-live-abc123"


# ── Redaction: every time ─────────────────────────────────────────────


def test_a_detected_credential_is_redacted_before_the_model_sees_it():
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key in config")

    out = _apply(_session(), engine, result="key=sk-live-abc123def456ghi789")

    assert "sk-live-abc123def456ghi789" not in str(out)


def test_redaction_still_happens_after_the_agent_has_been_notified():
    """The notice is once per run; the redaction is not. A second file with a
    key must not reach the model verbatim just because the first one did."""
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key")
    state = GuardState()
    session = _session()

    _apply(session, engine, result="first sk-live-aaaaaaaaaaaaaaaaaaaa", state=state)
    second = _apply(session, engine, result="second sk-live-bbbbbbbbbbbbbbbbbbbb", state=state)

    assert "sk-live-bbbbbbbbbbbbbbbbbbbb" not in str(second)


# ── Notification: once ────────────────────────────────────────────────


def test_the_agent_is_told_a_credential_was_found():
    """Detection that reaches no one is the same shape as a control that never
    runs."""
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key in config.py")
    session = _session()

    _apply(session, engine)

    assert len(session.messages) == 1
    content = session.messages[0]["content"]
    assert "credential exposure" in content
    assert "config.py" in content


def test_the_notice_never_repeats_the_value():
    """It is persisted with the conversation."""
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key found")
    session = _session()

    _apply(session, engine, result="sk-live-abc123def456")

    assert "sk-live-abc123def456" not in session.messages[0]["content"]


def test_the_notice_fires_once_per_run():
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key")
    session = _session()
    state = GuardState()

    for _ in range(4):
        _apply(session, engine, state=state)

    assert len(session.messages) == 1


def test_a_different_run_is_notified_again():
    engine = _engine(action="warned", name="no_sensitive_data", reason="AWS key")
    a, b = _session(), _session()

    _apply(a, engine, state=GuardState())
    _apply(b, engine, state=GuardState())

    assert len(a.messages) == 1 and len(b.messages) == 1


# ── Other guardrails ──────────────────────────────────────────────────


def test_another_warned_guardrail_does_not_redact_or_notify():
    """Only `no_sensitive_data` implies a credential in the payload. Redacting
    on every warning would mangle legitimate output."""
    engine = _engine(action="warned", name="no_external_http", reason="external host")
    session = _session()

    out = _apply(session, engine, result="sk-live-abc123def456")

    assert out == "sk-live-abc123def456"
    assert session.messages == []


@pytest.mark.parametrize("action", ["allowed", "blocked", "observed"])
def test_only_a_warning_triggers_the_path(action):
    engine = _engine(action=action, name="no_sensitive_data", reason="AWS key")
    session = _session()

    _apply(session, engine)

    assert session.messages == []

"""Activating a federation link while RLS is inert is not allowed.

Gate 3 of the responder wraps every inbound op in `tenant_scope`, so a tool
that forgets its WHERE clause still cannot read another tenant's rows. That is
only true if row-level security is actually enforcing. Two ways it silently is
not:

  - `ROBOTHOR_RLS_ENABLED` unset, so the policies exist and nothing applies
    them
  - the connection is a SUPERUSER, which bypasses RLS unconditionally. This is
    the normal shape of a single-box install, and it is how the orchestrator
    and vision services bypassed RLS for their entire existence while the
    instance reported it enabled.

Either way the tenancy gate is decoration. Admitting a remote principal at that
point would be shipping the third layer as a comment, and this box has a
documented history of exactly that — six controls found built, wired, tested
and totally inert. So activation refuses, loudly, rather than proceeding with
two layers and a decoration.
"""

from __future__ import annotations

import pytest

from robothor.federation.rls_gate import OVERRIDE_ENV, RLSInertError, require_enforcing_rls


@pytest.fixture(autouse=True)
def _no_blanket_override(monkeypatch):
    """conftest switches the gate off for the pairing tests. This file tests
    the gate itself, so it has to start from the real default — otherwise
    every assertion here would be satisfied by the escape hatch."""
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)


def test_it_refuses_when_rls_is_switched_off(monkeypatch):
    monkeypatch.setattr("robothor.federation.rls_gate._rls_flag_on", lambda: False)
    monkeypatch.setattr("robothor.federation.rls_gate._connection_is_superuser", lambda: False)

    with pytest.raises(RLSInertError, match="ROBOTHOR_RLS_ENABLED"):
        require_enforcing_rls()


def test_it_refuses_when_the_connection_is_a_superuser(monkeypatch):
    """The flag being on is not evidence. A superuser bypasses RLS whatever the
    flag says, which is the failure mode that actually happened here."""
    monkeypatch.setattr("robothor.federation.rls_gate._rls_flag_on", lambda: True)
    monkeypatch.setattr("robothor.federation.rls_gate._connection_is_superuser", lambda: True)

    with pytest.raises(RLSInertError, match="SUPERUSER"):
        require_enforcing_rls()


def test_it_passes_when_rls_is_really_enforcing(monkeypatch):
    monkeypatch.setattr("robothor.federation.rls_gate._rls_flag_on", lambda: True)
    monkeypatch.setattr("robothor.federation.rls_gate._connection_is_superuser", lambda: False)

    require_enforcing_rls()


def test_an_unanswerable_check_refuses_rather_than_assuming(monkeypatch):
    """If the superuser probe cannot run, the honest answer is 'unknown', and
    unknown must not activate a remote principal."""

    def _boom():
        raise RuntimeError("database gone")

    monkeypatch.setattr("robothor.federation.rls_gate._rls_flag_on", lambda: True)
    monkeypatch.setattr("robothor.federation.rls_gate._connection_is_superuser", _boom)

    with pytest.raises(RLSInertError, match="could not|unknown"):
        require_enforcing_rls()


def test_the_error_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr("robothor.federation.rls_gate._rls_flag_on", lambda: True)
    monkeypatch.setattr("robothor.federation.rls_gate._connection_is_superuser", lambda: True)

    with pytest.raises(RLSInertError) as exc:
        require_enforcing_rls()

    assert "ROBOTHOR_DB_USER" in str(exc.value)


def test_an_operator_can_override_it_deliberately(monkeypatch):
    """A single-box operator federating two of their own instances may accept
    the risk. It has to be a decision, spelled out, not a default."""
    monkeypatch.setattr("robothor.federation.rls_gate._rls_flag_on", lambda: False)
    monkeypatch.setattr("robothor.federation.rls_gate._connection_is_superuser", lambda: False)
    monkeypatch.setenv("ROBOTHOR_FEDERATION_ALLOW_INERT_RLS", "1")

    require_enforcing_rls()

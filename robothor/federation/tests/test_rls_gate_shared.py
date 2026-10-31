"""The inert-RLS gate is shared: federation activation and tenant switching.

Switching into a child tenant is only a boundary if row-level security is
genuinely enforcing — the same property federation activation already refuses
without. So the gate takes the override variable and a context for its
messages, and federation keeps its exact behaviour by default.

The superuser probe is cached per process: the switch endpoint calls the gate
on every request, and the role a process connects as does not change under it.
"""

from __future__ import annotations

import pytest

from robothor.federation import rls_gate
from robothor.federation.rls_gate import RLSInertError, require_enforcing_rls

SWITCH_ENV = "GENUS_TENANT_SWITCH_ALLOW_INERT_RLS"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(rls_gate.OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(SWITCH_ENV, raising=False)
    rls_gate.reset_superuser_cache()
    yield
    rls_gate.reset_superuser_cache()


def test_the_override_variable_is_a_parameter(monkeypatch):
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    monkeypatch.setenv(SWITCH_ENV, "1")
    require_enforcing_rls(override_env=SWITCH_ENV, context="switch tenants")


def test_federations_override_does_not_open_the_switch_gate(monkeypatch):
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    monkeypatch.setenv(rls_gate.OVERRIDE_ENV, "1")
    with pytest.raises(RLSInertError):
        require_enforcing_rls(override_env=SWITCH_ENV, context="switch tenants")


def test_the_message_names_the_context_and_its_own_override(monkeypatch):
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    with pytest.raises(RLSInertError) as exc:
        require_enforcing_rls(override_env=SWITCH_ENV, context="switch tenants")
    text = str(exc.value)
    assert "switch tenants" in text
    assert SWITCH_ENV in text
    assert rls_gate.OVERRIDE_ENV not in text
    assert "ROBOTHOR_DB_USER" in text


def test_an_explicit_override_value_beats_the_environment(monkeypatch):
    """The bridge resolves its override through settings (so the alias works)
    and hands the gate the answer."""
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    require_enforcing_rls(override_env=SWITCH_ENV, context="switch tenants", override=True)
    with pytest.raises(RLSInertError):
        require_enforcing_rls(override_env=SWITCH_ENV, context="switch tenants", override=False)


def test_federation_default_message_is_unchanged(monkeypatch):
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    with pytest.raises(RLSInertError) as exc:
        require_enforcing_rls()
    assert str(exc.value).startswith("Refusing to activate a federation link:")
    assert rls_gate.OVERRIDE_ENV in str(exc.value)


def test_the_superuser_probe_is_cached_per_process(monkeypatch):
    calls: list[int] = []

    def probe() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: True)
    monkeypatch.setattr(rls_gate, "_probe_superuser", probe)
    require_enforcing_rls()
    require_enforcing_rls(override_env=SWITCH_ENV, context="switch tenants")
    assert calls == [1]


def test_a_failed_probe_is_not_cached(monkeypatch):
    """'Could not tell' must be asked again next time, not remembered."""
    answers = iter([RuntimeError("database gone"), False])

    def probe() -> bool:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: True)
    monkeypatch.setattr(rls_gate, "_probe_superuser", probe)
    with pytest.raises(RLSInertError):
        require_enforcing_rls()
    require_enforcing_rls()

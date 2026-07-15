"""The write path must be reachable only by a human operator, never an agent."""

from __future__ import annotations


def test_patch_rejects_a_service_token(controls_client_as_service):
    r = controls_client_as_service.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "off", "reason": "x"}
    )
    assert r.status_code == 403, "an agent's service token must not flip a guardrail"


def test_patch_rejects_unknown_flag(controls_client_as_operator):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_TELEGRAM_BOT_TOKEN", json={"value": "x", "reason": "y"}
    )
    assert r.status_code == 404


def test_operator_can_promote_and_it_is_audited(controls_client_as_operator, fake_store):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RBAC_MODE", "enforce")


def test_get_lists_all_twelve_with_verdicts(controls_client_as_operator):
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 12
    assert all("verdict" in f and "status" in f["verdict"] for f in body)


def test_get_rejects_a_service_token(controls_client_as_service):
    r = controls_client_as_service.get("/api/controls")
    assert r.status_code == 403, "an agent's service token must not read guardrail modes either"


def test_patch_rejects_boolean_value_on_a_mode_flag(controls_client_as_operator):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "true", "reason": "x"}
    )
    assert r.status_code == 422


def test_patch_rejects_mode_value_on_a_boolean_flag(controls_client_as_operator):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RIP_1_ENABLED", json={"value": "observe", "reason": "x"}
    )
    assert r.status_code == 422


def test_patch_accepts_valid_mode_value(controls_client_as_operator, fake_store):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RBAC_MODE", "enforce")


def test_patch_accepts_valid_boolean_value(controls_client_as_operator, fake_store):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RIP_1_ENABLED", json={"value": "true", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RIP_1_ENABLED", "true")

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


def test_get_rejects_a_human_viewer(controls_client_as_viewer):
    r = controls_client_as_viewer.get("/api/controls")
    assert r.status_code == 403, "a verified but non-operator human must not read guardrail modes"


def test_get_rejects_a_human_user(controls_client_as_user):
    r = controls_client_as_user.get("/api/controls")
    assert r.status_code == 403, "role='user' is not an operator role"


def test_patch_rejects_a_human_viewer(controls_client_as_viewer):
    r = controls_client_as_viewer.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "off", "reason": "x"}
    )
    assert r.status_code == 403, "any authenticated org member must not flip a guardrail flag"


def test_patch_rejects_a_human_user(controls_client_as_user):
    r = controls_client_as_user.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "off", "reason": "x"}
    )
    assert r.status_code == 403


def test_get_allows_a_human_owner(controls_client_as_operator):
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200


def test_get_allows_a_human_admin(controls_client_as_admin):
    r = controls_client_as_admin.get("/api/controls")
    assert r.status_code == 200


def test_patch_allows_a_human_admin(controls_client_as_admin, fake_store):
    r = controls_client_as_admin.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RBAC_MODE", "enforce")

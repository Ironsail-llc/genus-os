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


def test_get_lists_every_governed_flag_with_verdicts(
    controls_client_as_operator, fake_store, fake_verdict
):
    """Counted against GOVERNED_FLAGS, never against a literal.

    This asserted `== 12`, so registering a thirteenth governed flag broke a
    test that has nothing to do with the flag — the whole drift class. What
    the endpoint owes is "every governed flag, each with a verdict", and that
    is what is asserted now.
    """
    from robothor.flags.store import GOVERNED_FLAGS

    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200
    body = r.json()
    assert {f["name"] for f in body} == set(GOVERNED_FLAGS)
    assert len(body) == len(GOVERNED_FLAGS)
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


def test_get_allows_a_human_owner(controls_client_as_operator, fake_store, fake_verdict):
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200


def test_get_allows_a_human_admin(controls_client_as_admin, fake_store, fake_verdict):
    r = controls_client_as_admin.get("/api/controls")
    assert r.status_code == 200


def test_patch_allows_a_human_admin(controls_client_as_admin, fake_store):
    r = controls_client_as_admin.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RBAC_MODE", "enforce")


# --- Platform-tenant gate: feature_flags is a GLOBAL table, not tenant-scoped ---


def test_get_rejects_an_owner_from_a_different_tenant(controls_client_as_other_tenant_owner):
    r = controls_client_as_other_tenant_owner.get("/api/controls")
    assert r.status_code == 403, (
        "an owner role from a non-platform tenant must not read the global flag table"
    )


def test_patch_rejects_an_owner_from_a_different_tenant(controls_client_as_other_tenant_owner):
    r = controls_client_as_other_tenant_owner.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "off", "reason": "x"}
    )
    assert r.status_code == 403, (
        "an owner role from a non-platform tenant must not write the global flag table"
    )


def test_get_allows_the_platform_tenant_owner(
    controls_client_as_operator, fake_store, fake_verdict
):
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200


def test_patch_allows_the_platform_tenant_owner(controls_client_as_operator, fake_store):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RBAC_MODE", "enforce")


# --- Per-flag valid values: RIP_13 is a 2-value mode flag, not the full ladder ---


def test_patch_rejects_alert_on_rip_13_mode(controls_client_as_operator):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RIP_13_MODE", json={"value": "alert", "reason": "x"}
    )
    assert r.status_code == 422, "RIP_13 only honors observe/enforce — the engine drops alert/off"


def test_patch_accepts_enforce_on_rip_13_mode(controls_client_as_operator, fake_store):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_RIP_13_MODE", json={"value": "enforce", "reason": "promote"}
    )
    assert r.status_code == 200
    assert fake_store.last_write == ("ROBOTHOR_RIP_13_MODE", "enforce")


def test_get_payload_includes_valid_values_per_flag(
    controls_client_as_operator, fake_store, fake_verdict
):
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200
    body = r.json()
    by_name = {f["name"]: f for f in body}
    assert by_name["ROBOTHOR_RIP_13_MODE"]["valid_values"] == ["observe", "enforce"]
    assert by_name["ROBOTHOR_RIP_1_ENABLED"]["valid_values"] == ["true", "false"]
    assert by_name["ROBOTHOR_RBAC_MODE"]["valid_values"] == ["off", "observe", "alert", "enforce"]


def test_an_unwritten_dnc_flag_reports_the_mode_the_engine_actually_runs(
    controls_client_as_operator, fake_store, fake_verdict
):
    """The unset default shown here must be the engine's unset default.

    `_default_value_for` picked "observe" for anything non-boolean, which is
    right for every flag that starts dark and gets promoted. ROBOTHOR_DNC_MODE
    starts ENFORCING — it is a compliance control with no soak — so the same
    rule would have this page report "observe" for a flag that is blocking
    mail. A controls page that misreports the live mode is worse than no
    controls page: an operator reading "observe" concludes the opt-out is not
    on yet and goes looking for why it did not fire.
    """
    r = controls_client_as_operator.get("/api/controls")
    assert r.status_code == 200
    by_name = {f["name"]: f for f in r.json()}
    assert by_name["ROBOTHOR_DNC_MODE"]["value"] == "enforce"
    assert by_name["ROBOTHOR_DNC_MODE"]["valid_values"] == ["observe", "enforce"]


def test_patch_rejects_off_on_the_dnc_flag(controls_client_as_operator):
    r = controls_client_as_operator.patch(
        "/api/controls/ROBOTHOR_DNC_MODE", json={"value": "off", "reason": "x"}
    )
    assert r.status_code == 422, (
        "a compliance opt-out has no 'off' rung — the engine maps it to enforce"
    )

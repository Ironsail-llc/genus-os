"""Owner-bound enrollment, atomic consumption and reference-only receipts."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr

from robothor.autonomy.models import ResourceInput, Scope


def request():
    from robothor.autonomy.enrollment import EnrollmentRequest

    return EnrollmentRequest(kind="credential", origin="https://shop.example")


def resource():
    return ResourceInput(
        kind="credential",
        label="Website login",
        origin="https://shop.example",
        payload=SecretStr(json.dumps({"username": "alice", "password": "intake-canary-93!"})),
    )


def test_owner_and_tenant_bound_single_use_with_reference_only_retry(store, identity):
    from robothor.autonomy.enrollment import EnrollmentStore

    intake = EnrollmentStore(store)
    link = intake.create(identity, request())
    token = link["token"]
    assert link["path"] == "/account/autonomy#enroll=" + token
    for foreign in (
        Scope(tenant_id=identity.tenant_id, owner_id="bob"),
        Scope(tenant_id="other", owner_id=identity.owner_id),
    ):
        with pytest.raises(PermissionError):
            intake.inspect(foreign, token)
        with pytest.raises(PermissionError):
            intake.complete(foreign, token, resource())
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: intake.complete(identity, token, resource()), range(2)))
    assert receipts[0] == receipts[1]
    assert len(store.resources(identity)) == 1
    assert "intake-canary" not in json.dumps(receipts)
    assert intake.inspect(identity, token)["resource_id"] == receipts[0]["id"]
    assert (
        store.consume_resource(identity, receipts[0]["id"], "https://shop.example")["password"]
        == "intake-canary-93!"
    )
    with store.transaction() as cur:
        cur.execute(
            "SELECT row_to_json(e)::text AS data FROM autonomy_enrollments e WHERE tenant_id=%s",
            (identity.tenant_id,),
        )
        saved = cur.fetchone()["data"]
        assert token not in saved and "intake-canary" not in saved


def test_scope_mismatch_bad_payload_and_storage_failure_do_not_consume(
    store, identity, monkeypatch
):
    from robothor.autonomy.enrollment import EnrollmentStore

    intake = EnrollmentStore(store)
    token = intake.create(identity, request())["token"]
    for change in (
        {"origin": "https://other.example"},
        {"kind": "profile"},
        {"payload": SecretStr("invalid-secret")},
    ):
        with pytest.raises((PermissionError, ValueError)):
            intake.complete(identity, token, resource().model_copy(update=change))
    original = store._event
    monkeypatch.setattr(store, "_event", lambda *a: (_ for _ in ()).throw(RuntimeError("fixture")))
    with pytest.raises(RuntimeError):
        intake.complete(identity, token, resource())
    monkeypatch.setattr(store, "_event", original)
    assert intake.inspect(identity, token)["resource_id"] is None
    assert not store.resources(identity)
    intake.complete(identity, token, resource())


def test_expired_or_revoked_enrollment_cannot_write(store, identity):
    from robothor.autonomy.enrollment import EnrollmentStore

    intake = EnrollmentStore(store)
    token = intake.create(identity, request())["token"]
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_enrollments SET expires_at=now()-interval '1 second' WHERE tenant_id=%s",
            (identity.tenant_id,),
        )
    with pytest.raises(PermissionError):
        intake.complete(identity, token, resource())
    assert not store.resources(identity)


def test_enrollment_spec_rejects_unusable_and_sensitive_metadata():
    from robothor.autonomy.enrollment import EnrollmentRequest

    for spec in (
        {"kind": "credential"},
        {"kind": "totp"},
        {"kind": "browser_session"},
        {"kind": "document", "label": "password=secret"},
        {"kind": "profile", "payload": "private"},
    ):
        with pytest.raises(ValueError):
            EnrollmentRequest.model_validate(spec)


def test_secure_intent_is_scrubbed_by_shared_history_and_runner_backstop():
    from robothor.autonomy.intake import protect_payment_text
    from robothor.engine.chat_history import ChatHistory
    from robothor.secrets.redaction import redact

    text = '/secure profile\n{"legal_name":"private-person-canary"}'
    for value in (
        protect_payment_text(text),
        redact(text),
        str(ChatHistory([{"role": "user", "content": text}])),
    ):
        assert "private-person-canary" not in value
        assert "withheld" in value


def test_enrollment_migration_is_packaged():
    from robothor.db.migrate import _discover

    ids = [m.migration_id for m in _discover()]
    assert ids.index("130_autonomy_request_context") < ids.index("131_autonomy_enrollments")


def test_configured_dashboard_origin_is_the_only_link_destination(store, identity, monkeypatch):
    from robothor.autonomy.enrollment import EnrollmentStore

    monkeypatch.setenv("ROBOTHOR_AUTONOMY_DASHBOARD_ORIGIN", "https://dashboard.example")
    link = EnrollmentStore(store).create(identity, request())
    assert link["url"] == "https://dashboard.example" + link["path"]


async def test_agent_requests_link_from_its_verified_owner(store, identity, monkeypatch):
    from robothor.autonomy.enrollment import EnrollmentStore
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import autonomy

    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(autonomy, "scope_for_actor", lambda tenant, actor: identity)
    result = await autonomy.handle(
        {
            "kind": "enrollment_link",
            "enrollment": {"kind": "credential", "origin": "https://shop.example"},
        },
        ToolContext(
            agent_id="main",
            user_id="verified-actor",
            user_role="owner",
            tenant_id=identity.tenant_id,
        ),
    )
    token = result["setup_path"].split("=", 1)[1]
    assert EnrollmentStore(store).inspect(identity, token)["kind"] == "credential"
    assert not store.resources(identity)

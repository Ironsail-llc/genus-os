"""Immutable private submission records cannot be read through another operation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from robothor.autonomy.models import Delegation, Scope, WebOperation


def operation(store, identity, *, allow_any_website=False, origins=("https://club.example",)):
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins=frozenset(origins),
            actions={"application"},
            allow_any_website=allow_any_website,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        ),
    )
    return store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://club.example",
            action="application",
            purpose="Requested membership",
            idempotency_key=uuid4().hex,
        ),
    )


def snapshot():
    from robothor.autonomy.terms_audit import TermsSnapshot

    return TermsSnapshot.model_validate(
        {
            "origin": "https://club.example",
            "phase": "before_input",
            "documents": [
                {
                    "origin": "https://club.example",
                    "text": "Private applicant name. Annual terms apply.",
                    "links": ["https://club.example/terms?private-reference=canary"],
                }
            ],
            "coverage": "visible_text_only",
        }
    )


def test_private_encrypted_versions_and_operation_binding(store, identity):
    from robothor.autonomy.terms_audit import TermsAudit

    audit = TermsAudit(store)
    op = operation(store, identity)
    first = audit.record(identity, op["id"], "main", snapshot())
    second = audit.record(identity, op["id"], "main", snapshot())
    assert [first["version"], second["version"]] == [1, 2]
    assert first["id"] != second["id"]
    assert "Private applicant" not in str(audit.list(identity, op["id"]))
    assert "private-reference" not in str(first)
    assert audit.read(identity, op["id"], first["id"])["snapshot"] == snapshot().model_dump(
        mode="json"
    )
    other = operation(store, identity)
    for scope, operation_id in (
        (identity, other["id"]),
        (Scope(tenant_id="foreign", owner_id=identity.owner_id), op["id"]),
        (Scope(tenant_id=identity.tenant_id, owner_id="foreign"), op["id"]),
    ):
        with pytest.raises(PermissionError):
            audit.read(scope, operation_id, first["id"])
    with store.transaction() as cur:
        cur.execute(
            "SELECT row_to_json(t)::text AS row FROM autonomy_terms_snapshots t WHERE id=%s",
            (first["id"],),
        )
        raw = cur.fetchone()["row"]
    assert "Private applicant" not in raw and "private-reference" not in raw
    # A ciphertext transplanted between operations fails authenticated decryption.
    foreign = audit.record(identity, other["id"], "main", snapshot())
    with store.transaction() as cur:
        cur.execute(
            "UPDATE autonomy_terms_snapshots SET encrypted_value=(SELECT encrypted_value FROM autonomy_terms_snapshots WHERE id=%s) WHERE id=%s",
            (first["id"], foreign["id"]),
        )
    with pytest.raises(ValueError, match="audit_unavailable"):
        audit.read(identity, other["id"], foreign["id"])


def test_audit_versions_serialize_and_wrong_actor_cannot_append(store, identity):
    from robothor.autonomy.terms_audit import TermsAudit

    audit = TermsAudit(store)
    op = operation(store, identity)
    with pytest.raises(PermissionError):
        audit.record(identity, op["id"], "other-agent", snapshot())
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(
            pool.map(lambda _: audit.record(identity, op["id"], "main", snapshot()), range(4))
        )
    assert sorted(row["version"] for row in rows) == [1, 2, 3, 4]
    store.finish(identity, op["id"], "cancelled")
    with pytest.raises(PermissionError):
        audit.record(identity, op["id"], "main", snapshot())
    assert len(audit.list(identity, op["id"])) == 4


def test_origin_limits_and_missing_operation_fail_without_values(store, identity):
    from robothor.autonomy.terms_audit import TermsAudit

    audit = TermsAudit(store)
    op = operation(store, identity)
    with pytest.raises(PermissionError):
        audit.record(identity, str(uuid4()), "main", snapshot())
    wrong = snapshot().model_copy(update={"origin": "https://other.example"})
    with pytest.raises(PermissionError):
        audit.record(identity, op["id"], "main", wrong)
    with pytest.raises(ValueError):
        snapshot().model_validate(
            {
                **snapshot().model_dump(),
                "documents": [{"origin": "https://club.example", "text": "x" * 200001}],
            }
        )


def _events(store, identity, subject_id):
    with store.transaction() as cur:
        cur.execute(
            "SELECT event FROM autonomy_events WHERE tenant_id=%s AND owner_id=%s AND subject_id=%s "
            "ORDER BY id",
            (identity.tenant_id, identity.owner_id, subject_id),
        )
        return [row["event"] for row in cur.fetchall()]


class _NoPageAccess:
    """Any attribute read means the suppressed path looked at the page."""

    def __getattr__(self, name):
        raise AssertionError(f"suppressed terms capture touched the page: {name}")


async def test_suppression_after_transient_code_is_itself_recorded(store, identity):
    """Zero snapshots must not be indistinguishable from the feature being off."""
    from robothor.autonomy.broker import BrowserBroker
    from robothor.autonomy.terms_audit import TermsAudit
    from robothor.autonomy.terms_capture import capture_terms

    op = operation(store, identity)
    store.begin_submit(identity, op["id"], "main")
    broker = BrowserBroker(store)
    broker._used_transient_code = True
    ref = await capture_terms(
        broker,
        identity,
        op["id"],
        "main",
        _NoPageAccess(),
        "https://club.example",
        frozenset(),
        phase="before_submit",
    )
    assert ref is not None
    audit = TermsAudit(store)
    rows = audit.list(identity, op["id"])
    assert [(r["phase"], r["coverage"], r["document_count"]) for r in rows] == [
        ("before_submit", "suppressed_after_code", 1)
    ]
    saved = audit.read(identity, op["id"], ref["id"])["snapshot"]
    assert saved["documents"][0]["text"] == "" and saved["documents"][0]["links"] == []
    assert "terms_suppressed" in _events(store, identity, op["id"])


async def test_suppression_still_refuses_material_terms_before_input(store, identity):
    from robothor.autonomy.broker import BrowserBroker
    from robothor.autonomy.material_documents import (
        MaterialTermsUnavailableError,
        MaterialTermTarget,
    )
    from robothor.autonomy.terms_audit import TermsAudit
    from robothor.autonomy.terms_capture import capture_terms

    op = operation(store, identity)
    broker = BrowserBroker(store)
    broker._used_transient_code = True
    with pytest.raises(MaterialTermsUnavailableError):
        await capture_terms(
            broker,
            identity,
            op["id"],
            "main",
            _NoPageAccess(),
            "https://club.example",
            frozenset(),
            phase="before_input",
            material_terms=[MaterialTermTarget(selector="#terms")],
        )
    assert TermsAudit(store).list(identity, op["id"]) == []


def test_suppressed_coverage_cannot_carry_page_data_or_a_receipt_phase():
    from robothor.autonomy.terms_audit import TermsDocument, TermsSnapshot

    with pytest.raises(ValueError):
        TermsSnapshot(
            origin="https://club.example",
            phase="before_submit",
            coverage="suppressed_after_code",
            documents=[TermsDocument(origin="https://club.example", text="leaked")],
        )
    with pytest.raises(ValueError):
        TermsSnapshot(
            origin="https://club.example",
            phase="after_confirmation",
            confirmation_sha256="a" * 64,
            coverage="suppressed_after_code",
            documents=[TermsDocument(origin="https://club.example", text="")],
        )

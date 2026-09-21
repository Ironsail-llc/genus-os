"""An isolated company moves from evidence through human review to retention."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from robothor.db.connection import get_connection
from robothor.operations.store import Conflict
from robothor.sales.models import Dossier, Evidence, QualificationPolicy
from robothor.sales.service import Sales


@pytest.fixture
def sales():
    with get_connection() as conn:
        with conn.cursor() as cur:
            for filename in (
                "126_durable_operations.sql",
                "127_sales_intelligence.sql",
                "128_sales_business_observations.sql",
                "129_sales_binding_repair.sql",
                "130_sales_deployments.sql",
                "131_sales_calibration.sql",
                "132_sales_research_delegation.sql",
                "133_operation_fragments.sql",
                "134_web_render_permission.sql",
                "135_sales_requests.sql",
                "136_sales_pipedrive_scope.sql",
                "137_sales_analyst_permissions.sql",
                "138_opt_in_tool_denies.sql",
            ):
                cur.execute((Path(__file__).parents[3] / "crm/migrations" / filename).read_text())
        conn.commit()
    service = Sales("test-" + uuid4().hex)
    with service.ops.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_tenants(id,display_name) VALUES(%s,%s)",
            (service.tenant, "Sales integration test"),
        )
    # email_provider is explicit: the platform default is "none" (every
    # integration switch defaults off), and these fixtures exercise a
    # configured provider.
    service.configure(
        {"research_enabled": True, "sending_enabled": False, "email_provider": "instantly"},
        "operator:test",
    )
    return service


def researched(sales):
    prospect = sales.discover(
        "Example Clinic", "https://clinic.example.com/services", "https://directory.example.com"
    )
    evidence = Evidence(
        id="service",
        field="prescribing",
        value=True,
        url="https://clinic.example.com/services",
        excerpt="Our medical team provides prescription management.",
        retrieved_at=datetime.now(UTC),
    )
    dossier = Dossier(
        evidence=[evidence],
        buying_case="network_access",
        criteria={"prescribing": ["service"]},
        unanswered=["Purchasing volume is unknown"],
    )
    sales.research(prospect["id"], dossier, expected_version=0)
    policy = QualificationPolicy(
        version="1",
        buying_case="network_access",
        required=["prescribing"],
        weights={"prescribing": 100},
        threshold=80,
    )
    sales.publish_policy(policy, "operator:test")
    sales.qualify(prospect["id"], "1")
    return prospect


def test_dedup_evidence_review_and_promotion(sales):
    prospect = researched(sales)
    same = sales.discover(
        "Other name", "https://clinic.example.com/", "https://directory.example.com"
    )
    assert same["id"] == prospect["id"]
    assert sales.get(prospect["id"])["qualification"]["score"] == 100
    sales.accept(
        prospect["id"], True, "operator:test", expected_version=1, expected_policy_version="1"
    )
    jobs = sales.ops.claim("sales.promote")
    assert jobs["payload"]["prospect_id"] == prospect["id"]
    assert Sales("another-tenant").get(prospect["id"]) is None


def test_unknown_criteria_do_not_become_negative_facts(sales):
    policy = QualificationPolicy(
        version="1",
        buying_case="network_access",
        required=["prescribing"],
        weights={"prescribing": 100},
        threshold=80,
    )
    result = policy.evaluate(Dossier(buying_case="network_access"))
    assert result["decision"] == "needs_research"
    assert result["missing"] == ["prescribing"]


def test_stale_research_and_unsupported_claims_are_rejected(sales):
    p = researched(sales)
    with pytest.raises(Conflict):
        sales.research(p["id"], Dossier(buying_case="network_access"), expected_version=0)
    with pytest.raises(ValueError):
        Dossier(buying_case="network_access", criteria={"prescribing": ["nonexistent"]})


def test_suppression_invalidates_pending_messages(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    sales.add_contact(
        p["id"],
        {
            "name": "Alice",
            "role": "Owner",
            "email": "alice@example.com",
            "source_url": "https://clinic.example.com/team",
            "verification": "valid",
            "verified_at": datetime.now(UTC).isoformat(),
        },
    )
    sales.publish_knowledge(
        "v1", {"claims": {"access": "Access the partner network."}}, "operator:test"
    )
    action = sales.draft(
        p["id"],
        {
            "recipient": "alice@example.com",
            "sender": "sales@example.com",
            "subject": "Pharmacy workflows",
            "body": "Access the partner network.",
            "claim_ids": ["access"],
            "knowledge_version": "v1",
            "evidence_ids": ["service"],
        },
    )
    sales.ops.decide(action, True, "operator:test")
    sales.suppress("alice@example.com", "opt_out", "operator:test")
    assert sales.ops.claim_action(kind="sales.email") is None


def test_customer_milestones_are_idempotent_and_matured(sales):
    p = researched(sales)
    start = datetime.now(UTC) - timedelta(days=70)
    for i, day in enumerate((0, 10, 40)):
        event = {
            "external_company_id": "customer-1",
            "event_id": str(i),
            "kind": "order_completed",
            "occurred_at": (start + timedelta(days=day)).isoformat(),
        }
        sales.bind_customer(p["id"], "customer-1", "operator:test")
        sales.record_outcome(event)
        sales.record_outcome(event)
    metrics = sales.retention(p["id"])
    assert metrics["completed_orders"] == 3
    assert metrics["repeat_within_30_days"] is True
    assert metrics["active_days_31_60"] is True
    assert metrics["active_days_61_90"] is None


def test_outcomes_reject_patient_data(sales):
    with pytest.raises(ValueError):
        sales.record_outcome(
            {
                "external_company_id": "1",
                "event_id": "1",
                "kind": "order_completed",
                "occurred_at": datetime.now(UTC).isoformat(),
                "patient_name": "must not enter sales",
            }
        )


def test_order_placement_does_not_count_as_completed_activation(sales):
    p = researched(sales)
    sales.bind_customer(p["id"], "customer-1", "operator:test")
    placed = datetime.now(UTC) - timedelta(days=3)
    sales.record_outcome(
        {
            "external_company_id": "customer-1",
            "event_id": "placed-1",
            "kind": "first_order_placed",
            "occurred_at": placed.isoformat(),
        }
    )
    result = sales.retention(p["id"])
    assert result["first_order_placed"] == placed.isoformat()
    assert result["completed_orders"] == 0
    assert result["first_completed_order"] is None
    assert result["repeat_within_30_days"] is None


def test_existing_unassigned_crm_person_is_attached_to_the_qualified_company(sales):
    p = researched(sales)
    person_id = str(uuid4())
    with sales.ops.transaction() as cur:
        cur.execute(
            "INSERT INTO crm_people(id,tenant_id,first_name,email) VALUES(%s,%s,%s,%s)",
            (person_id, sales.tenant, "Alice", "alice@example.com"),
        )
    sales.add_contact(
        p["id"],
        {
            "name": "Alice",
            "role": "Owner",
            "email": "alice@example.com",
            "source_url": "https://clinic.example.com/team",
        },
    )
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT company_id FROM crm_people WHERE tenant_id=%s AND id=%s",
            (sales.tenant, person_id),
        )
        assert str(cur.fetchone()["company_id"]) == str(p["company_id"])


@pytest.mark.parametrize(
    "email", ["@example.com", "alice@", "alice@@example.com", "alice@example.com,other@example.com"]
)
def test_invalid_email_cannot_enter_enrichment(email):
    from robothor.sales.models import Contact

    with pytest.raises(ValueError):
        Contact(
            name="Alice", role="Owner", email=email, source_url="https://clinic.example.com/team"
        )

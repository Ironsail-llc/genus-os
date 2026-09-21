"""Approved library versions are selected by kind and buying case, not label alone."""

import pytest

from robothor.operations.store import Conflict
from robothor.sales.models import QualificationPolicy
from robothor.sales.service import Sales
from robothor.sales.tests.test_guards import approved, draft, prepared
from robothor.sales.tests.test_service import researched


def policy(version="v1", buying_case="network_access"):
    return QualificationPolicy(
        version=version,
        buying_case=buying_case,
        required=["prescribing"],
        weights={"prescribing": 100},
        threshold=80,
    )


def test_context_does_not_admit_inactive_library_with_same_version(sales):
    p = researched(sales)
    sales.publish_knowledge("1", {"claims": {"old": "Retired positioning"}}, "operator:test")
    sales.publish_knowledge(
        "current", {"claims": {"access": "Approved positioning"}}, "operator:test"
    )
    sales.configure(
        {"active_policy_versions": {"network_access": "1"}, "active_knowledge_version": "current"},
        "operator:test",
    )
    assert {(row["kind"], row["version"]) for row in sales.context(p["id"])["policies"]} == {
        ("qualification", "1"),
        ("knowledge", "current"),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"active_knowledge_version": "missing"},
        {"active_policy_versions": {"network_access": "missing"}},
        {"active_policy_versions": {"wrong_case": "v1"}},
    ],
)
def test_configuration_cannot_select_missing_or_mismatched_approved_versions(sales, changes):
    sales.publish_policy(policy(), "operator:test")
    before = sales.settings_snapshot()
    with pytest.raises(Conflict, match="[Pp]ublished|[Bb]uying case"):
        sales.configure(changes, "operator:test")
    assert sales.settings_snapshot() == before


def test_library_catalog_paginates_with_tenant_and_kind_isolation(sales):
    for version in ("alpha", "beta", "gamma"):
        sales.publish_policy(policy(version), "operator:reviewer")
    sales.publish_knowledge(
        "beta", {"claims": {"access": "Approved positioning"}}, "operator:reviewer"
    )
    first = sales.library(kind="qualification", limit=2)
    assert [row["version"] for row in first["items"]] == ["alpha", "beta"]
    assert first["next_cursor"] == "beta"
    assert first["items"][0]["approved_by"] == "operator:reviewer"
    assert first["items"][0]["data"]["buying_case"] == "network_access"
    second = sales.library(kind="qualification", after=first["next_cursor"], limit=2)
    assert [row["version"] for row in second["items"]] == ["gamma"]
    assert second["next_cursor"] is None
    assert [row["version"] for row in sales.library(kind="knowledge")["items"]] == ["beta"]
    assert Sales("other-library-tenant").library(kind="qualification")["items"] == []


def test_library_selection_is_reviewed_and_preserves_integration_state(sales):
    sales.publish_policy(policy(), "operator:test")
    sales.publish_knowledge("v1", {"claims": {"access": "Approved positioning"}}, "operator:test")
    state = sales.settings_snapshot()
    sales.select_library(
        policy_versions={"network_access": "v1"},
        knowledge_version="v1",
        expected_revision=state["revision"],
        reason="Reviewed pilot sales library",
        actor="operator:reviewer",
    )
    current = sales.settings_snapshot()
    assert current["config"]["active_policy_versions"] == {"network_access": "v1"}
    assert current["config"]["sending_enabled"] is False
    with pytest.raises(Conflict, match="changed"):
        sales.select_library(
            policy_versions={},
            knowledge_version="",
            expected_revision=state["revision"],
            reason="Stale library selection review",
            actor="operator:reviewer",
        )
    assert sales.settings_snapshot() == current


def test_restoring_old_selection_does_not_restore_old_message_authority(sales):
    p = prepared(sales)
    executing = approved(sales, p)
    pending = draft(sales, p)
    sales.publish_knowledge("v2", {"claims": {"access": "Updated positioning"}}, "operator:test")
    sales.configure({"active_knowledge_version": "v2"}, "operator:test")
    sales.configure({"active_knowledge_version": "v1"}, "operator:test")
    with pytest.raises(Conflict, match="selection changed"):
        sales.validate_send(executing)
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status FROM operation_actions WHERE tenant_id=%s AND id=%s",
            (sales.tenant, pending),
        )
        assert cur.fetchone()["status"] == "cancelled"


def test_library_generation_cannot_be_supplied_by_a_config_caller(sales):
    with pytest.raises(Conflict, match="managed"):
        sales.configure({"library_revision": 42}, "operator:test")


@pytest.mark.parametrize("version", ["", " ", " v1", "a" * 81])
def test_published_versions_require_nonempty_bounded_labels(sales, version):
    with pytest.raises(ValueError, match="version"):
        sales.publish_knowledge(
            version, {"claims": {"access": "Approved positioning"}}, "operator:test"
        )

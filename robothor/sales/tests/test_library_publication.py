"""Publication records the exact human-reviewed packet without activating it."""

import pytest

from robothor.operations.store import Conflict


def packet():
    return {
        "kind": "qualification",
        "version": "pilot-1",
        "data": {
            "version": "pilot-1",
            "buying_case": "network_access",
            "required": ["prescribing"],
            "weights": {"prescribing": 100},
            "threshold": 80,
            "criteria_definitions": {"prescribing": "Explicit evidence of prescription services."},
        },
    }


def test_publication_binds_preview_content_and_audits_without_selecting(sales):
    from robothor.sales.library import preview

    reviewed = preview(packet())
    assert reviewed["packet"]["data"]["max_evidence_age_days"] == 90
    before = sales.settings_snapshot()
    sales.publish_library(
        reviewed["packet"],
        expected_hash=reviewed["content_hash"],
        reason="Reviewed initial qualification rules",
        actor="operator:reviewer",
    )
    assert sales.settings_snapshot() == before
    entry = sales.library(kind="qualification")["items"][0]
    assert entry["data"] == reviewed["packet"]["data"]
    assert entry["approved_by"] == "operator:reviewer"
    record = sales.library_record(kind="qualification", version="pilot-1")
    assert record["content_hash"] == reviewed["content_hash"]
    assert sales.library_record(kind="knowledge", version="pilot-1") is None
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT detail FROM operation_audit WHERE tenant_id=%s AND event='qualification.published'",
            (sales.tenant,),
        )
        audit = cur.fetchone()["detail"]
    assert audit["content_hash"] == reviewed["content_hash"]
    assert audit["reason"] == "Reviewed initial qualification rules"
    modified = {**reviewed["packet"], "data": {**reviewed["packet"]["data"], "threshold": 70}}
    with pytest.raises(Conflict, match="review"):
        sales.publish_library(
            modified,
            expected_hash=reviewed["content_hash"],
            reason="Reviewed initial qualification rules",
            actor="operator:reviewer",
        )


def test_publication_rejects_unreviewed_or_invalid_packets(sales):
    from robothor.sales.library import preview

    with pytest.raises(ValueError):
        preview({**packet(), "data": {**packet()["data"], "weights": {"prescribing": 80}}})
    with pytest.raises(ValueError):
        preview({**packet(), "version": "another"})
    with pytest.raises(ValueError):
        preview({**packet(), "data": {**packet()["data"], "criteria_definitions": {}}})
    for claims in ({}, {"empty": " "}, {"ambiguous": {"text": "nested claim"}}):
        with pytest.raises(ValueError):
            preview({"kind": "knowledge", "version": "claims-1", "data": {"claims": claims}})
    reviewed = preview(packet())
    with pytest.raises(Conflict, match="Human"):
        sales.publish_library(
            reviewed["packet"],
            expected_hash=reviewed["content_hash"],
            reason="Reviewed initial qualification rules",
            actor="agent:scout",
        )
    assert sales.library(kind="qualification")["items"] == []


def test_knowledge_publication_preserves_sources_and_refuses_version_overwrite(sales):
    from robothor.sales.library import preview

    proposal = {
        "kind": "knowledge",
        "version": "claims-1",
        "data": {
            "claims": {"workflow": "One connected ordering workflow."},
            "sources": [{"url": "https://example.com/services", "checked_at": "2026-09-19"}],
            "limitations": ["Availability must be checked for the specific account."],
        },
    }
    reviewed = preview(proposal)
    sales.publish_library(
        proposal,
        expected_hash=reviewed["content_hash"],
        reason="Reviewed dated business claims",
        actor="operator:reviewer",
    )
    assert sales.library(kind="knowledge")["items"][0]["data"] == proposal["data"]
    proposal["data"]["claims"]["workflow"] = "Different claim."
    changed = preview(proposal)
    with pytest.raises(Conflict, match="immutable"):
        sales.publish_library(
            proposal,
            expected_hash=changed["content_hash"],
            reason="Reviewed revised business claims",
            actor="operator:reviewer",
        )

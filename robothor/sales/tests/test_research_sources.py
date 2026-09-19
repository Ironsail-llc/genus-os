"""Citations require the same native child's actual successful source retrieval."""

from datetime import UTC, datetime

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.operations.store import Conflict
from robothor.sales.tests.test_research_fanout import fragment


def capture(*, ctx=None, result=None, tool="web_fetch"):
    from robothor.sales.research_sources import ResearchSources

    sources = ResearchSources("tenant-a", "research-worker")
    sources.observe(
        tool,
        {},
        result
        if result is not None
        else {
            "url": "https://clinic.example.com/services",
            "status": 200,
            "content": "Header\nPublic  business\n information\nFooter",
        },
        ctx or ToolContext(tenant_id="tenant-a", agent_id="research-worker", run_id="child"),
    )
    return sources


@pytest.mark.parametrize("tool", ["web_fetch", "web_render"])
def test_citations_are_bound_to_actual_source_and_engine_time_with_recoverable_receipt(tool):
    before = datetime.now(UTC)
    sources = capture(tool=tool)
    dossier, receipt = sources.attest("child", fragment("services"))
    assert before <= dossier.evidence[0].retrieved_at <= datetime.now(UTC)
    assert dossier.evidence[0].retrieved_at != fragment("services").evidence[0].retrieved_at
    assert receipt["run_id"] == "child"
    assert receipt["sources"][0]["content_hash"]
    restored, proof = sources.restore("child", dossier, receipt)
    assert restored == dossier
    assert proof == receipt


@pytest.mark.parametrize(
    "fault",
    [
        "no_fetch",
        "other_child",
        "tenant",
        "agent",
        "search",
        "error",
        "empty",
        "http_error",
        "url",
        "excerpt",
        "blank_excerpt",
    ],
)
def test_unretrieved_or_unmatched_citations_are_refused(fault):
    ctx = ToolContext(
        tenant_id="other" if fault == "tenant" else "tenant-a",
        agent_id="other" if fault == "agent" else "research-worker",
        run_id="child",
    )
    result = {
        "url": "https://clinic.example.com/services",
        "content": "Public business information",
        "status": 200,
    }
    if fault == "error":
        result["error"] = "Fetch failed"
    if fault == "empty":
        result["content"] = ""
    if fault == "http_error":
        result["status"] = 403
    sources = capture(
        ctx=ctx,
        result=result,
        tool="web_search" if fault in {"no_fetch", "search"} else "web_fetch",
    )
    dossier = fragment("services")
    if fault == "url":
        dossier.evidence[0].url = "https://other.example.com/services"
    if fault == "excerpt":
        dossier.evidence[0].excerpt = "Claim copied from internal background knowledge"
    if fault == "blank_excerpt":
        dossier.evidence[0].excerpt = "   "
    with pytest.raises(Conflict, match="retriev|citation"):
        sources.attest("other" if fault == "other_child" else "child", dossier)


@pytest.mark.parametrize(
    "fault", ["missing", "run", "tenant", "agent", "content", "time", "dossier"]
)
def test_recovery_cannot_accept_legacy_or_mismatched_source_proof(fault):
    sources = capture()
    dossier, proof = sources.attest("child", fragment("services"))
    if fault == "missing":
        proof = None
    elif fault == "run":
        proof["run_id"] = "other"
    elif fault == "tenant":
        proof["tenant_id"] = "other"
    elif fault == "agent":
        proof["agent_id"] = "other"
    elif fault == "content":
        proof["sources"][0]["content"] = "Invented"
    elif fault == "time":
        proof["sources"][0]["retrieved_at"] = "not-a-time"
    else:
        dossier.evidence[0].excerpt = "Invented"
    with pytest.raises(Conflict):
        sources.restore("child", dossier, proof)

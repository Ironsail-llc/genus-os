"""Native research selects captured passages; the engine materializes quotations."""

from copy import deepcopy

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_research_sources import capture


def selection(packet):
    return {
        "buying_case": "network_access",
        "evidence": [
            {
                "id": "business",
                "field": "business",
                "value": True,
                "source_ref": packet["source_ref"],
                "passage_ref": packet["passages"][0]["ref"],
                "confidence": "supported",
            }
        ],
        "criteria": {"business": ["business"]},
        "summary": "Public business information",
    }


def captured():
    from robothor.engine.tools.dispatch import ToolContext

    sources = capture()
    packet = sources.observe(
        "web_fetch",
        {},
        {
            "status": 200,
            "url": "https://clinic.example.com/about",
            "content": "A clinic.\n\nPublic business information.",
        },
        ToolContext(tenant_id="tenant-a", agent_id="research-worker", run_id="child"),
    )
    return sources, packet


def test_selected_passage_becomes_exact_crm_evidence_and_recovers():
    sources, packet = captured()
    dossier, proof = sources.attest_output("child", selection(packet))
    evidence = dossier.evidence[0]
    assert evidence.excerpt == packet["passages"][0]["text"]
    assert evidence.url == "https://clinic.example.com/about"
    assert evidence.retrieved_at.isoformat() == sources.runs["child"][-1]["retrieved_at"]
    assert "source_ref" not in dossier.model_dump()["evidence"][0]
    assert proof["version"] == 2
    assert proof["selection"] == selection(packet)
    restored, _ = sources.restore("child", dossier, proof)
    assert restored == dossier


def test_repeated_page_selection_keeps_its_own_capture_time():
    from robothor.engine.tools.dispatch import ToolContext

    sources, _ = captured()
    first = deepcopy(sources.runs["child"][-1])
    sources.runs["child"][-1]["retrieved_at"] = "2026-09-18T00:00:00+00:00"
    packet = sources.observe(
        "web_fetch",
        {},
        {"status": 200, "url": first["url"], "content": first["content"]},
        ToolContext(tenant_id="tenant-a", agent_id="research-worker", run_id="child"),
    )
    dossier, proof = sources.attest_output("child", selection(packet))
    assert dossier.evidence[0].retrieved_at.isoformat() == sources.runs["child"][-1]["retrieved_at"]
    assert sources.restore("child", dossier, proof)[0] == dossier


@pytest.mark.parametrize("fault", ["source", "passage", "other_child", "invented_quote"])
def test_invented_or_foreign_references_cannot_become_evidence(fault):
    from pydantic import ValidationError

    sources, packet = captured()
    request = selection(packet)
    if fault == "source":
        request["evidence"][0]["source_ref"] = "invented"
    elif fault == "passage":
        request["evidence"][0]["passage_ref"] = "p999"
    elif fault == "invented_quote":
        request["evidence"][0]["excerpt"] = "Invented quotation"
    with pytest.raises((Conflict, ValidationError)):
        sources.attest_output("other" if fault == "other_child" else "child", request)


@pytest.mark.parametrize("fault", ["selection", "content", "run", "source_ref"])
def test_recovery_rechecks_selection_against_bound_original_capture(fault):
    sources, packet = captured()
    dossier, proof = sources.attest_output("child", selection(packet))
    proof = deepcopy(proof)
    if fault == "selection":
        proof["selection"]["evidence"][0]["value"] = False
    elif fault == "content":
        proof["sources"][-1]["content"] = "Replacement"
    elif fault == "run":
        proof["run_id"] = "other"
    else:
        proof["selection"]["evidence"][0]["source_ref"] = "invented"
    with pytest.raises(Conflict):
        sources.restore("child", dossier, proof)


def test_passages_are_bounded_exact_substrings_and_deterministic():
    from robothor.sales.research_contract import passages

    text = ("Header\n\n" + "A long sentence with punctuation. " * 150 + "\n\nFooter").strip()
    rows = passages(text)
    assert rows == passages(text)
    assert len({r["ref"] for r in rows}) == len(rows)
    assert all(0 < len(r["text"]) <= 800 and r["text"] in text for r in rows)
    assert "".join(r["text"].replace(" ", "").replace("\n", "") for r in rows) == text.replace(
        " ", ""
    ).replace("\n", "")


def test_new_captures_do_not_join_business_paragraphs_to_neighboring_reviews():
    from robothor.engine.tools.dispatch import ToolContext

    sources = capture()
    packet = sources.observe(
        "web_fetch",
        {},
        {
            "status": 200,
            "url": "https://business.example.com/",
            "content": "# Example Business\n\nLocated in Springfield, Illinois.\n\nCustomer review: wonderful service!",
        },
        ToolContext(tenant_id="tenant-a", agent_id="research-worker", run_id="child"),
    )
    assert packet["kind"] == "captured_passages_v2"
    assert [p["text"] for p in packet["passages"]] == [
        "# Example Business",
        "Located in Springfield, Illinois.",
        "Customer review: wonderful service!",
    ]
    request = selection(packet)
    request["evidence"][0]["passage_ref"] = packet["passages"][1]["ref"]
    dossier, proof = sources.attest_output("child", request)
    assert dossier.evidence[0].excerpt == "Located in Springfield, Illinois."
    assert proof["sources"][-1]["passage_version"] == 2
    assert sources.restore("child", dossier, proof)[0] == dossier
    changed = deepcopy(proof)
    changed["sources"][-1]["passage_version"] = 1
    with pytest.raises(Conflict):
        sources.restore("child", dossier, changed)


def test_old_unversioned_capture_proofs_keep_original_passage_boundaries():
    sources, _ = captured()
    source = sources.runs["child"][-1]
    source.pop("passage_version", None)
    packet = sources.packet("child", source)
    assert packet["kind"] == "captured_passages_v1"
    assert packet["passages"][0]["text"] == "A clinic.\n\nPublic business information."
    dossier, proof = sources.attest_output("child", selection(packet))
    assert sources.restore("child", dossier, proof)[0] == dossier


def test_paragraph_passages_keep_long_text_bounded_and_unknown_versions_fail():
    from robothor.sales.research_contract import passages

    text = "Header\n\n" + "A long sentence. " * 100 + "\n \t\nFooter"
    rows = passages(text, version=2)
    assert all(0 < len(p["text"]) <= 800 and p["text"] in text for p in rows)
    assert rows[0]["text"] == "Header"
    assert rows[-1]["text"] == "Footer"
    assert " ".join(p["text"] for p in rows).split() == text.split()
    with pytest.raises(ValueError):
        passages(text, version=99)


def test_schema_repair_uses_final_format_but_citation_repair_can_resume_page_reads():
    import json
    from types import SimpleNamespace

    from robothor.engine.llm_client import LLMClient
    from robothor.engine.output_validation import request_output_repair
    from robothor.engine.tool_observation import observe_tool_result
    from robothor.engine.tools.dispatch import ToolContext

    sources = capture()
    text = ["Here is my final answer: not a JSON object"]
    session = SimpleNamespace(
        run=SimpleNamespace(id="child", tenant_id="tenant-a", agent_id="research-worker"),
        messages=[],
        get_final_text=lambda: text[0],
        record_error=lambda error: None,
    )
    tools = [{"type": "function", "function": {"name": "web_fetch", "parameters": {}}}]

    def request():
        return LLMClient._build_llm_kwargs("openrouter/example/model", [], tools, 100, 0.3)

    with sources.child_scope("services"):
        packet = observe_tool_result(
            "web_fetch",
            {},
            {
                "status": 200,
                "url": "https://clinic.example.com/about",
                "content": "A public business.",
            },
            ToolContext(tenant_id="tenant-a", agent_id="research-worker", run_id="child"),
        )
        assert "response_format" not in request()
        assert request_output_repair(session)
        assert request()["response_format"]["type"] == "json_schema"
        bad = selection(packet)
        bad["evidence"][0]["source_ref"] = "invented"
        text[0] = json.dumps(bad)
        assert request_output_repair(session)
        assert "response_format" not in request()
        text[0] = json.dumps(selection(packet))
        assert not request_output_repair(session)

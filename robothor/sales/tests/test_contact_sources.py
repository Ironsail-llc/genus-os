"""A public contact address must occur in an observed page, not a model guess."""

import json
from types import SimpleNamespace

import pytest

from robothor.operations.store import Conflict, digest
from robothor.sales.tests.test_runtime import RunnerStub


class ContactRunnerStub(RunnerStub):
    """A model response paired with an independent synthetic business-page read."""

    async def run(self, **kwargs):
        from robothor.sales.contact_sources import ContactSources

        result = await super().run(**kwargs)
        sources = ContactSources(kwargs["tenant_id"], kwargs["agent_id"])
        sources.observe(
            "web_fetch",
            {"url": "https://clinic.example.com/team"},
            {
                "url": "https://clinic.example.com/team",
                "status": 200,
                "content": "Business contact: Alice, Owner. Email alice@example.com",
            },
            SimpleNamespace(
                tenant_id=kwargs["tenant_id"], agent_id=kwargs["agent_id"], run_id=str(result.id)
            ),
        )
        _, proof = sources.attest(str(result.id), result.output_text)
        result.stage_provenance = {"contact_sources": proof}
        return result


def contact(**changes):
    return json.dumps(
        {
            "contacts": [
                {
                    "name": "Alice",
                    "role": "Practice manager",
                    "email": "alice@example.com",
                    "source_url": "https://clinic.example.com/team",
                    **changes,
                }
            ]
        }
    )


def observe(sources, **changes):
    sources.observe(
        "web_fetch",
        {"url": "https://clinic.example.com/team"},
        {
            "url": "https://clinic.example.com/team",
            "status": 200,
            "content": "Business team\n\nAlice, Practice manager: alice@example.com\n\nOther information.",
            **changes,
        },
        SimpleNamespace(tenant_id="test", agent_id="contacts", run_id="run-1"),
    )


def test_only_observed_address_and_source_can_be_enriched():
    from robothor.sales.contact_sources import ContactSources

    sources = ContactSources("test", "contacts")
    with pytest.raises(Conflict):
        sources.attest("run-1", contact())
    observe(sources)
    batch, proof = sources.attest("run-1", contact())
    assert batch.contacts[0].verification == "unknown"
    assert proof["output_hash"] == digest(batch.model_dump(mode="json"))
    assert proof["contacts"][0]["excerpt"] == "Alice, Practice manager: alice@example.com"
    assert "Other information" not in json.dumps(proof)
    for changes in (
        {"email": "guessed@example.com"},
        {"source_url": "https://invented.example.com/team"},
    ):
        with pytest.raises(Conflict):
            sources.attest("run-1", contact(**changes))


def test_address_at_sentence_end_is_not_confused_with_a_longer_domain():
    from robothor.sales.contact_sources import ContactSources

    sources = ContactSources("test", "contacts")
    observe(sources, content="Email our manager Alice at alice@example.com.")
    assert sources.attest("run-1", contact())[0].contacts[0].email == "alice@example.com"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 403},
        {"content": ""},
        {"content": "notalice@example.com"},
        {"content": "alice@example.com.other"},
        {"error": "failed"},
    ],
)
def test_failed_or_partial_address_matches_do_not_authorize_contact(changes):
    from robothor.sales.contact_sources import ContactSources

    sources = ContactSources("test", "contacts")
    observe(sources, **changes)
    with pytest.raises(Conflict):
        sources.attest("run-1", contact())


def test_empty_contact_search_needs_an_actual_page_and_cannot_use_another_run():
    from robothor.sales.contact_sources import ContactSources

    sources = ContactSources("test", "contacts")
    with pytest.raises(Conflict):
        sources.attest("run-1", '{"contacts": []}')
    observe(sources, content="Contact us using the form. No public email is listed.")
    assert sources.attest("run-1", '{"contacts": []}')[0].contacts == []
    with pytest.raises(Conflict):
        sources.attest("different-run", '{"contacts": []}')


@pytest.mark.asyncio
async def test_native_contacts_require_page_read_and_preserve_attestation(monkeypatch):
    from robothor.engine.models import AgentConfig
    from robothor.engine.required_tool import tool_choice
    from robothor.engine.response_schema import defers_tool_turns, response_format
    from robothor.engine.tool_observation import observe_tool_result
    from robothor.sales.runtime import NativeStageRunner

    config = AgentConfig(
        id="contacts", name="Contacts", tools_allowed=["web_fetch"], max_cost_usd=1
    )
    tools = [{"type": "function", "function": {"name": "web_fetch"}}]

    async def execute(**kwargs):
        assert tool_choice(tools)["function"]["name"] == "web_fetch"
        assert response_format() is None
        observe_tool_result(
            "web_fetch",
            {"url": "https://clinic.example.com/team"},
            {
                "url": "https://clinic.example.com/team",
                "status": 200,
                "content": "Alice, Practice manager: alice@example.com",
            },
            SimpleNamespace(tenant_id="test", agent_id="contacts", run_id="run-1"),
        )
        assert tool_choice(tools) == "auto"
        assert defers_tool_turns()
        return SimpleNamespace(
            id="run-1", status="completed", total_cost_usd=0, output_text=contact()
        )

    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *a: config)
    monkeypatch.setattr(
        "robothor.engine.tools.handlers.spawn.get_runner",
        lambda: SimpleNamespace(config=SimpleNamespace(manifest_dir="unused"), execute=execute),
    )
    result = await NativeStageRunner().run(
        agent_id="contacts",
        tenant_id="test",
        correlation_id="job",
        max_cost_usd=1,
        stage="contacts",
        message="{}",
    )
    assert result.stage_provenance["contact_sources"]["run_id"] == "run-1"
    assert not defers_tool_turns()


@pytest.mark.asyncio
async def test_cached_contacts_without_native_source_proof_cannot_buy_verification(sales):
    from robothor.sales.models import ContactBatch
    from robothor.sales.stages import ContactWorker
    from robothor.sales.tests.test_runtime import RunnerStub
    from robothor.sales.tests.test_service import researched

    p = researched(sales)
    sales.configure(
        {"enrichment_enabled": True, "agents": {"contacts": "contacts"}}, "operator:test"
    )
    job = sales.ops.claim("sales.contacts")
    sales.ops.checkpoint(
        job["id"],
        job["lease_token"],
        {
            "checkpoint_version": 1,
            "stage": "contacts",
            "run_id": "old-run",
            "context": json.loads(json.dumps(sales.context(p["id"]), default=str)),
            "output": ContactBatch.model_validate_json(contact()).model_dump(mode="json"),
        },
    )
    sales.ops.defer(job["id"], job["lease_token"], "restart fixture", delay_seconds=0)
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job["id"]),
        )
    runner = RunnerStub(json.loads(contact()))
    assert await ContactWorker(sales, runner).tick()
    assert not runner.calls
    assert sales.contacts(p["id"]) == []
    assert sales.ops.claim("sales.verify") is None


@pytest.mark.parametrize(
    "change", ["tenant_id", "agent_id", "run_id", "output_hash", "excerpt", "release"]
)
def test_contact_recovery_proof_is_bound_to_current_output_and_identity(change):
    from robothor.sales.contact_sources import ContactSources, validate_checkpoint

    sources = ContactSources("test", "contacts")
    observe(sources)
    batch, proof = sources.attest("run-1", contact())
    checkpoint = {
        "run_id": "run-1",
        "fleet_release_id": "a" * 64,
        "provenance": {"contact_sources": proof},
    }
    validate_checkpoint(checkpoint, batch, "test", "contacts", "a" * 64)
    if change == "excerpt":
        proof["contacts"][0]["excerpt"] = "A guessed address"
    elif change == "release":
        checkpoint["fleet_release_id"] = "b" * 64
    else:
        proof[change] = "other"
    with pytest.raises(Conflict):
        validate_checkpoint(checkpoint, batch, "test", "contacts", "a" * 64)


@pytest.mark.asyncio
async def test_contact_completion_retains_source_receipt_and_pause_blocks_commit(sales):
    from robothor.sales.stages import ContactWorker
    from robothor.sales.tests.test_service import researched

    p = researched(sales)
    sales.configure(
        {
            "enrichment_enabled": True,
            "agents": {"contacts": "contacts"},
            "monthly_limit_units": 5_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )

    class PauseDuringRun(ContactRunnerStub):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            sales.configure({"enrichment_enabled": False}, "operator:test")
            return result

    assert await ContactWorker(sales, PauseDuringRun(json.loads(contact()))).tick()
    assert sales.contacts(p["id"]) == []
    sales.configure({"enrichment_enabled": True}, "operator:test")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND kind='sales.contacts'",
            (sales.tenant,),
        )
        cur.execute(
            "SELECT id FROM operation_jobs WHERE tenant_id=%s AND kind='sales.contacts'",
            (sales.tenant,),
        )
        job_id = str(cur.fetchone()["id"])
    runner = ContactRunnerStub(json.loads(contact()))
    assert await ContactWorker(sales, runner).tick()
    assert runner.calls == []  # Recovery reuses valid proof without buying generation again.
    receipt = sales.ops.get_job(job_id)["result"]
    assert receipt["provenance"]["contact_sources"]["contacts"][0]["excerpt"].endswith(
        "alice@example.com"
    )
    assert sales.ops.claim("sales.verify") is not None

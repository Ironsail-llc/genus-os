"""Bounded research requests are durable tasks, not permission to send messages."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_queue import NOW, configure, scouts


def request_data(**changes):
    return {
        "request_key": "request-1",
        "title": "Find prescribing practices",
        "query": "US prescribing clinics in Florida",
        "buying_case": "network_access",
        "target_companies": 3,
        **changes,
    }


def test_request_is_idempotent_tenant_scoped_and_does_not_enable_any_switch(sales):
    from robothor.sales.requests import Requests

    configure(sales, research_enabled=False)
    requests = Requests(sales)
    with ThreadPoolExecutor(max_workers=3) as pool:
        created = list(
            pool.map(lambda _: requests.create(request_data(), "agent:coordinator"), range(3))
        )
    assert len({str(row["id"]) for row in created}) == 1
    assert not sales.settings()["research_enabled"]
    assert sales.ops.claim("sales.scout") is None
    with pytest.raises(Conflict):
        requests.create(request_data(query="Different criteria"), "agent:coordinator")
    from robothor.sales.service import Sales

    with pytest.raises(Conflict):
        Requests(Sales("other-tenant")).get(created[0]["id"])


def test_request_allocates_only_target_slots_and_pause_prevents_commit(sales):
    from robothor.sales.models import CandidateBatch
    from robothor.sales.queue import DiscoveryPlanner
    from robothor.sales.requests import Requests
    from robothor.sales.stages import ScoutWorker

    configure(sales, discovery_mode="requests")
    requests = Requests(sales)
    task = requests.create(request_data(), "operator:test")
    assert DiscoveryPlanner(sales, clock=lambda: NOW).plan()
    jobs = [j for j in scouts(sales) if j["payload"].get("request_id") == str(task["id"])]
    assert len(jobs) == 1 and jobs[0]["payload"]["max_companies"] == 3
    job = sales.ops.claim("sales.scout")
    output = CandidateBatch.model_validate(
        {
            "companies": [
                {
                    "name": "Example Clinic",
                    "website": "https://clinic.example.com",
                    "source_url": "https://directory.example.com",
                    "reason": "Prescribing services",
                }
            ]
        }
    )
    requests.change(
        task["id"], "paused", task["revision"], "operator:test", "Pause this request for review"
    )
    with pytest.raises(Conflict):
        ScoutWorker(sales).commit(job, {"max_companies": 3}, output, "run-1")
    assert sales.overview()["prospects"] == []
    paused = requests.get(task["id"])
    requests.change(
        task["id"], "active", paused["revision"], "operator:test", "Resume this reviewed request"
    )
    ScoutWorker(sales).commit(job, {"max_companies": 3}, output, "run-1")
    state = requests.get(task["id"])
    assert state["progress"]["discovered"] == 1
    assert state["progress"]["remaining"] == 2


def test_known_domains_do_not_consume_new_company_target(sales):
    from robothor.sales.requests import Requests

    configure(sales)
    old = sales.discover(
        "Existing", "https://existing.example.com", "https://directory.example.com"
    )
    task = Requests(sales).create(request_data(), "operator:test")
    same = sales.discover(
        "Existing",
        "https://existing.example.com",
        "https://directory.example.com",
        request_id=str(task["id"]),
    )
    assert same["id"] == old["id"]
    assert Requests(sales).get(task["id"])["progress"]["discovered"] == 0
    sales.discover(
        "New",
        "https://new.example.com",
        "https://directory.example.com",
        request_id=str(task["id"]),
    )
    assert Requests(sales).get(task["id"])["progress"]["discovered"] == 1


def test_paused_request_blocks_funded_work_for_its_members(sales):
    from robothor.sales.models import SalesSettings
    from robothor.sales.requests import Requests
    from robothor.sales.runtime import ResearchWorker

    configure(sales)
    requests = Requests(sales)
    task = requests.create(request_data(), "operator:test")
    sales.discover(
        "New",
        "https://new.example.com",
        "https://directory.example.com",
        request_id=str(task["id"]),
    )
    job = sales.ops.claim("sales.research")
    requests.change(
        task["id"], "paused", task["revision"], "operator:test", "Pause before model spending"
    )
    with pytest.raises(Conflict):
        ResearchWorker(sales)._reserve(SalesSettings.model_validate(sales.settings()), job)
    assert sales.overview()["budgets"] == []


def test_request_limits_and_review_revisions_are_enforced(sales):
    from robothor.sales.requests import Requests

    configure(sales)
    requests = Requests(sales)
    task = requests.create(request_data(target_companies=1), "operator:test")
    sales.discover(
        "One",
        "https://one.example.com",
        "https://directory.example.com",
        request_id=str(task["id"]),
    )
    with pytest.raises(Conflict):
        sales.discover(
            "Two",
            "https://two.example.com",
            "https://directory.example.com",
            request_id=str(task["id"]),
        )
    requests.change(
        task["id"], "cancelled", task["revision"], "operator:test", "Cancel the reviewed request"
    )
    with pytest.raises(Conflict):
        requests.change(
            task["id"], "active", task["revision"], "operator:test", "Stale resume must not work"
        )


def test_pause_prevents_an_already_generated_dossier_from_committing(sales):
    from robothor.sales.models import Dossier
    from robothor.sales.requests import Requests

    configure(sales)
    requests = Requests(sales)
    task = requests.create(request_data(), "operator:test")
    p = sales.discover(
        "New",
        "https://new.example.com",
        "https://directory.example.com",
        request_id=str(task["id"]),
    )
    requests.change(
        task["id"], "paused", task["revision"], "operator:test", "Pause while model was working"
    )
    with pytest.raises(Conflict):
        sales.research(p["id"], Dossier(buying_case="network_access"), expected_version=0)
    assert sales.get(p["id"])["version"] == 0


def test_request_research_context_keeps_the_selected_buying_case(sales):
    from robothor.sales.models import Dossier, QualificationPolicy
    from robothor.sales.requests import Requests

    configure(sales)
    sales.publish_policy(
        QualificationPolicy(
            version="v2",
            buying_case="other",
            required=["prescribing"],
            weights={"prescribing": 100},
            threshold=80,
        ),
        "operator:test",
    )
    sales.configure(
        {"active_policy_versions": {"network_access": "v1", "other": "v2"}}, "operator:test"
    )
    task = Requests(sales).create(request_data(), "operator:test")
    p = sales.discover(
        "New",
        "https://new.example.com",
        "https://directory.example.com",
        request_id=str(task["id"]),
    )
    context = sales.context(p["id"])
    assert [
        p["data"]["buying_case"] for p in context["policies"] if p["kind"] == "qualification"
    ] == ["network_access"]
    assert context["research_request"]["query"] == task["config"]["query"]
    with pytest.raises(Conflict):
        sales.research(p["id"], Dossier(buying_case="other"), expected_version=0)


def test_request_progress_explains_disabled_research_and_changed_policy(sales):
    from robothor.sales.requests import Requests

    configure(sales, research_enabled=False)
    requests = Requests(sales)
    task = requests.create(request_data(), "operator:test")
    assert requests.get(task["id"])["progress"]["phase"] == "waiting_for_research"
    sales.configure({"active_policy_versions": {}}, "operator:test")
    assert requests.get(task["id"])["progress"]["phase"] == "policy_changed"


def test_workspace_intake_context_contains_only_reviewed_task_configuration(sales):
    from robothor.sales.requests import Requests

    configure(sales, discovery_mode="requests")
    context = Requests(sales).workspace()
    assert context["buying_cases"] == [{"id": "network_access", "policy_version": "v1"}]
    assert context["discovery_mode"] == "requests"
    assert context["daily_company_limit"] == 20
    assert not any(key in context for key in ("senders", "api_key", "business_sources"))

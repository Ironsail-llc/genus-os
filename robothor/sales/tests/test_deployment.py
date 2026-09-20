"""Durable fleet transitions never infer runtime readiness from artifact validity."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from robothor.templates.tests.test_fleet_release import source as source_fixture
from robothor.templates.tests.test_fleet_release import spec

source = source_fixture


@pytest.fixture
def deployment(sales, source, tmp_path, request):
    from robothor.sales.deployment import DeploymentCoordinator
    from robothor.templates.fleet_release import build_release
    from robothor.templates.fleet_store import stage_release

    with sales.ops.transaction() as cur:
        cur.execute(
            (Path(__file__).parents[3] / "crm/migrations/130_sales_deployments.sql").read_text()
        )
    (source / "docs/workflows/process.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "process",
                "steps": [
                    {
                        "id": "tick",
                        "type": "tool",
                        "tool_name": "sales_process_queue",
                        "tool_args": {"stage": "research"},
                    }
                ],
            }
        )
    )
    (source / "config/settings.yaml").write_text(
        yaml.safe_dump(
            {
                "agents": {"research": "ticket-router"},
                "workflow_bindings": {"research": "process"},
                "email_provider": getattr(request, "param", "instantly"),
            }
        )
    )
    built = build_release(
        source, tmp_path / "candidate", spec(sales_settings="config/settings.yaml")
    )
    stage_release(tmp_path / "candidate", tmp_path, expected_digest=built["release_id"])
    coordinator = DeploymentCoordinator(sales, tmp_path)
    return coordinator, built["release_id"]


class RuntimeEvidence:
    """Explicit test double; production must supply the native runtime verifier."""

    def verify(self, transition, *, restoring=False):
        return {
            "transition_id": str(transition["id"]),
            "release_id": transition["source_release_id"]
            if restoring
            else transition["target_release_id"],
            "runtime_generation": "test-generation",
            "restoring": restoring,
        }


def prepare(deployment):
    coordinator, release_id = deployment
    return coordinator.prepare(
        release_id,
        expected_revision=coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Install the reviewed fleet release",
    )


@pytest.mark.asyncio
async def test_preparation_survives_new_controller_and_blocks_native_queue(
    deployment, sales, monkeypatch
):
    from robothor.operations.store import Conflict
    from robothor.sales import queue
    from robothor.sales.deployment import DeploymentCoordinator

    record = prepare(deployment)
    restarted = DeploymentCoordinator(sales, deployment[0].workspace)
    assert restarted.status()["pending"]["id"] == record["id"]
    worker = AsyncMock(return_value=True)
    monkeypatch.setattr(queue, "StopWorker", lambda _: SimpleNamespace(tick=worker))
    with pytest.raises(Conflict, match="deployment"):
        await queue.QueueDriver(sales).tick("stop", "anything")
    worker.assert_not_awaited()
    assert sales.settings().get("fleet_release_id") is None
    restarted.abort(
        record["id"],
        RuntimeEvidence(),
        actor="operator:test",
        reason="Restore the prior runtime after review",
    )
    assert restarted.status()["pending"] is None


def test_commit_requires_runtime_evidence_and_atomically_selects_paused_config(deployment, sales):
    from robothor.operations.store import Conflict

    coordinator, release_id = deployment
    record = prepare(deployment)
    with pytest.raises(Conflict, match="runtime"):
        coordinator.commit(record["id"], None, actor="operator:test")
    assert coordinator.status()["pending"] is not None
    coordinator.commit(record["id"], RuntimeEvidence(), actor="operator:test")
    assert coordinator.status()["pending"] is None
    assert sales.settings()["fleet_release_id"] == release_id
    assert sales.settings()["agents"] == {"research": "ticket-router"}
    assert sales.settings()["workflow_bindings"] == {"research": "process"}
    assert sales.settings()["research_enabled"] is False


def test_operator_change_invalidates_prepared_settings_and_abort_preserves_it(deployment, sales):
    from robothor.operations.store import Conflict

    coordinator, _ = deployment
    record = prepare(deployment)
    sales.configure({"research_enabled": False}, "operator:test")
    with pytest.raises(Conflict, match="settings"):
        coordinator.commit(record["id"], RuntimeEvidence(), actor="operator:test")
    coordinator.abort(
        record["id"],
        RuntimeEvidence(),
        actor="operator:test",
        reason="Keep the operator's newer pause",
    )
    assert sales.settings()["research_enabled"] is False


@pytest.mark.parametrize("busy", ["job", "action", "effect", "gmail_effect"])
def test_prepare_refuses_unfinished_work_even_when_the_advisory_gate_is_free(
    deployment, sales, busy
):
    from robothor.operations.effects import Effects
    from robothor.operations.store import Conflict

    if busy == "job":
        sales.ops.enqueue("sales.research", "active", {})
        sales.ops.claim("sales.research")
    elif busy == "action":
        action = sales.ops.propose("sales.email", "active", {"body": "test"})
        sales.ops.decide(action, True, "operator:test")
        sales.ops.claim_action()
    else:
        kind = "gmail.send" if busy == "gmail_effect" else "pipedrive.organization"
        Effects(sales.tenant)._begin(kind, "active", {})
    with pytest.raises(Conflict, match="unfinished"):
        prepare(deployment)
    assert deployment[0].status()["pending"] is None


def test_stale_revision_and_parallel_preparations_cannot_replace_pending_transition(deployment):
    from robothor.operations.store import Conflict

    coordinator, release_id = deployment
    revision = coordinator.status()["settings_revision"]
    with pytest.raises(Conflict, match="settings"):
        coordinator.prepare(
            release_id,
            expected_revision=revision + 1,
            actor="operator:test",
            reason="Install the reviewed fleet release",
        )
    record = prepare(deployment)
    with pytest.raises(Conflict):
        prepare(deployment)
    assert coordinator.status()["pending"]["id"] == record["id"]


def test_rollback_uses_the_same_preparation_boundary_and_restores_previous_selection(
    deployment, sales
):
    coordinator, _ = deployment
    original = sales.settings()
    record = prepare(deployment)
    coordinator.commit(record["id"], RuntimeEvidence(), actor="operator:test")
    rollback = coordinator.prepare_rollback(
        record["id"],
        expected_revision=coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Roll back the runtime configuration",
    )
    assert coordinator.status()["pending"]["id"] == rollback["id"]
    coordinator.commit(rollback["id"], RuntimeEvidence(), actor="operator:test")
    assert sales.settings()["fleet_release_id"] == original.get("fleet_release_id")
    assert sales.settings()["research_enabled"] is False


def test_settings_aba_cannot_authorize_stale_commit(deployment, sales):
    from robothor.operations.store import Conflict

    record = prepare(deployment)
    sales.configure({"research_enabled": False}, "operator:test")
    sales.configure({"research_enabled": True}, "operator:test")
    with pytest.raises(Conflict, match="settings"):
        deployment[0].commit(record["id"], RuntimeEvidence(), actor="operator:test")


def test_failed_commit_rolls_back_both_selection_and_transition(deployment, sales, monkeypatch):
    record = prepare(deployment)
    previous = sales.settings()

    def fail(*args, **kwargs):
        raise RuntimeError("Simulated audit storage failure")

    monkeypatch.setattr(sales.ops, "audit", fail)
    with pytest.raises(RuntimeError):
        deployment[0].commit(record["id"], RuntimeEvidence(), actor="operator:test")
    assert sales.settings() == previous
    assert deployment[0].status()["pending"]["id"] == record["id"]


def test_runtime_failure_never_clears_pending_deployment(deployment):
    from robothor.operations.store import Conflict

    record = prepare(deployment)
    for restoring in (False, True):
        with pytest.raises(Conflict, match="runtime"):
            if restoring:
                deployment[0].abort(
                    record["id"], None, actor="operator:test", reason="Restore the previous runtime"
                )
            else:
                deployment[0].commit(record["id"], None, actor="operator:test")
        assert deployment[0].status()["pending"] is not None


def test_managed_selection_and_bindings_cannot_bypass_the_coordinator(deployment, sales):
    from robothor.operations.store import Conflict

    with pytest.raises(Conflict, match="coordinator"):
        sales.configure({"fleet_release_id": deployment[1]}, "operator:test")
    record = prepare(deployment)
    with pytest.raises(Conflict, match="coordinator"):
        sales.configure({"agents": {"research": "different-agent"}}, "operator:test")
    deployment[0].commit(record["id"], RuntimeEvidence(), actor="operator:test")
    with pytest.raises(Conflict, match="coordinator"):
        sales.configure({"workflow_bindings": {}}, "operator:test")
    sales.configure({"research_enabled": False}, "operator:test")


def test_artifact_drift_after_preparation_cannot_commit(deployment):
    from robothor.templates.fleet_release import ReleaseError
    from robothor.templates.fleet_store import staged_release_path

    coordinator, release_id = deployment
    record = prepare(deployment)
    (staged_release_path(coordinator.workspace, release_id) / "brain/WORKER.md").write_text(
        "Changed after preparation"
    )
    with pytest.raises(ReleaseError):
        coordinator.commit(record["id"], RuntimeEvidence(), actor="operator:test")
    assert coordinator.status()["pending"] is not None


def test_wrong_runtime_receipt_and_agent_actor_are_refused(deployment):
    from robothor.operations.store import Conflict

    coordinator, _ = deployment
    record = prepare(deployment)

    class WrongRuntime:
        def verify(self, *args, **kwargs):
            return {
                "transition_id": "wrong",
                "release_id": None,
                "runtime_generation": "x",
                "restoring": False,
            }

    with pytest.raises(Conflict, match="runtime"):
        coordinator.commit(record["id"], WrongRuntime(), actor="operator:test")
    with pytest.raises(Conflict, match="Human"):
        coordinator.commit(record["id"], RuntimeEvidence(), actor="agent:test")


def test_runtime_verifier_cannot_mutate_the_configuration_being_committed(deployment, sales):
    record = prepare(deployment)

    class MutatingRuntime(RuntimeEvidence):
        def verify(self, transition, **kwargs):
            transition["target_config"]["sending_enabled"] = True
            return super().verify(transition, **kwargs)

    deployment[0].commit(record["id"], MutatingRuntime(), actor="operator:test")
    assert sales.settings()["sending_enabled"] is False


def test_stale_historical_transition_cannot_be_rolled_back_after_a_later_cycle(deployment):
    from robothor.operations.store import Conflict

    coordinator, _ = deployment
    first = prepare(deployment)
    coordinator.commit(first["id"], RuntimeEvidence(), actor="operator:test")
    undo = coordinator.prepare_rollback(
        first["id"],
        expected_revision=coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Restore prior selection",
    )
    coordinator.commit(undo["id"], RuntimeEvidence(), actor="operator:test")
    second = prepare(deployment)
    coordinator.commit(second["id"], RuntimeEvidence(), actor="operator:test")
    with pytest.raises(Conflict, match="latest"):
        coordinator.prepare_rollback(
            first["id"],
            expected_revision=coordinator.status()["settings_revision"],
            actor="operator:test",
            reason="A stale rollback request",
        )


def test_other_tenant_cannot_read_or_commit_transition(deployment):
    from robothor.operations.store import Conflict
    from robothor.sales.deployment import DeploymentCoordinator
    from robothor.sales.service import Sales

    record = prepare(deployment)
    other = DeploymentCoordinator(Sales("test-other-deployment-tenant"), deployment[0].workspace)
    assert other.status()["pending"] is None
    with pytest.raises(Conflict):
        other.commit(record["id"], RuntimeEvidence(), actor="operator:test")


@pytest.mark.parametrize("deployment", ["none"], indirect=True)
def test_email_provider_selection_is_deployed_locked_and_rolled_back(deployment, sales):
    from robothor.operations.store import Conflict

    coordinator, _ = deployment
    record = prepare(deployment)
    assert record["target_config"]["email_provider"] == "none"
    coordinator.commit(record["id"], RuntimeEvidence(), actor="operator:test")
    assert sales.settings()["email_provider"] == "none"
    with pytest.raises(Conflict, match="coordinator"):
        sales.configure({"email_provider": "instantly"}, "operator:test")
    # Older deployment snapshots predate this field; rollback must use its
    # default -- which is now "none". A snapshot that never recorded a provider
    # rolls back to no provider, not to whichever vendor happened to be the
    # default when it was taken.
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_deployments SET previous_config=previous_config-'email_provider' WHERE id=%s",
            (str(record["id"]),),
        )
    rollback = coordinator.prepare_rollback(
        record["id"],
        expected_revision=coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Restore the previous email selection",
    )
    assert rollback["target_config"]["email_provider"] == "none"

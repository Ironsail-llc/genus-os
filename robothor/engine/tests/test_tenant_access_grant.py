"""Who a run may reach beyond its own tenant, decided once at run start.

``attach_tenant_access`` sets ``run.accessible_tenant_ids``. The rules:

* a sub-agent run and a benchmark run reach only their own tenant, even when
  they inherit an owner's user from the run that spawned them;
* a run with a verified human (and not a system trigger) reaches what that
  human's role reaches (``resolve_accessible_tenants``: owner and admin get the
  descendants);
* a run with no verified human -- a cron, a hook, an event -- reaches the
  descendants ONLY when both the tenant (``crm_tenants.child_data_access``)
  and the agent's manifest (``v2.child_tenant_access``) say so. Its manifest
  role (``service_role``) is not a human role and grants nothing here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.models import TriggerType
from robothor.identity import IdentityContext

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
EVERYTHING = (PARENT, CHILD_A, CHILD_B)

OWNER = IdentityContext(
    tenant_id=PARENT, channel="webchat", identifier="alice", verified=True, role="owner"
)
UNVERIFIED = IdentityContext(
    tenant_id=PARENT, channel="telegram", identifier="alice", verified=False, role="owner"
)


def _resolve(tenant_id: str, user_role: str | None = None, **_: Any) -> tuple[str, ...]:
    return EVERYTHING if user_role in ("owner", "admin") else (tenant_id,)


def _run(**kw: Any) -> SimpleNamespace:
    base = {
        "agent_id": "main",
        "user_role": "owner",
        "parent_run_id": None,
        "nesting_depth": 0,
        "trigger_type": TriggerType.WEBCHAT,
        "is_benchmark": False,
        "accessible_tenant_ids": (),
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def grant():
    state = SimpleNamespace(tenant_flag=False, opt_in=False)
    with (
        patch(
            "robothor.engine.permissions.resolve_accessible_tenants",
            side_effect=_resolve,
        ),
        patch(
            "robothor.crm.dal.tenant_child_data_access",
            side_effect=lambda tenant_id: state.tenant_flag,
        ),
        patch(
            "robothor.engine.run_identity._agent_opts_in",
            side_effect=lambda agent_id: state.opt_in,
        ),
    ):
        yield state


def _attach(run: Any, identity: Any = OWNER) -> tuple[str, ...]:
    from robothor.engine.run_identity import attach_tenant_access

    asyncio.run(attach_tenant_access(run, PARENT, identity))
    return tuple(run.accessible_tenant_ids)


def test_a_verified_owner_reaches_the_descendants(grant):
    assert _attach(_run()) == EVERYTHING


def test_a_verified_member_reaches_only_its_own_tenant(grant):
    member = IdentityContext(
        tenant_id=PARENT, channel="webchat", identifier="bob", verified=True, role="member"
    )
    assert _attach(_run(user_role="member"), member) == (PARENT,)


def test_a_sub_agent_inheriting_an_owner_reaches_only_its_own_tenant(grant):
    assert _attach(_run(parent_run_id="parent-run", nesting_depth=1)) == (PARENT,)


def test_a_sub_agent_of_an_untracked_parent_reaches_only_its_own_tenant(grant):
    assert _attach(_run(trigger_type=TriggerType.SUB_AGENT, nesting_depth=1)) == (PARENT,)


def test_a_benchmark_run_reaches_only_its_own_tenant(grant):
    assert _attach(_run(is_benchmark=True)) == (PARENT,)


def test_an_unverified_identity_is_not_a_human_grant(grant):
    assert _attach(_run(), UNVERIFIED) == (PARENT,)


def test_an_unattended_run_without_the_grant_sees_only_its_own_tenant(grant):
    # A manifest role of "owner" is a service role, not a person.
    run = _run(trigger_type=TriggerType.CRON, user_role="owner")
    assert _attach(run, None) == (PARENT,)


def test_the_tenant_flag_alone_grants_nothing(grant):
    grant.tenant_flag = True
    assert _attach(_run(trigger_type=TriggerType.CRON, user_role="service"), None) == (PARENT,)


def test_the_manifest_opt_in_alone_grants_nothing(grant):
    grant.opt_in = True
    assert _attach(_run(trigger_type=TriggerType.CRON, user_role="service"), None) == (PARENT,)


def test_both_grants_give_an_unattended_run_the_descendants(grant):
    grant.tenant_flag = grant.opt_in = True
    assert _attach(_run(trigger_type=TriggerType.CRON, user_role="service"), None) == EVERYTHING


def test_both_grants_still_never_reach_a_sub_agent(grant):
    grant.tenant_flag = grant.opt_in = True
    run = _run(trigger_type=TriggerType.SUB_AGENT, nesting_depth=1, user_role="service")
    assert _attach(run, None) == (PARENT,)


def test_a_verified_human_on_a_system_trigger_is_unattended(grant):
    # A workflow run carries no human in the loop, whoever it is attributed to.
    assert _attach(_run(trigger_type=TriggerType.WORKFLOW), OWNER) == (PARENT,)


# ── Reach follows the verified identity, not only the run's role ───────────


def _verified(role: str, tenant_id: str = PARENT) -> IdentityContext:
    return IdentityContext(
        tenant_id=tenant_id, channel="webchat", identifier="bob", verified=True, role=role
    )


def test_a_verified_member_on_a_run_carrying_owner_reaches_only_its_own_tenant(grant):
    assert _attach(_run(user_role="owner"), _verified("member")) == (PARENT,)


def test_a_verified_owner_on_a_run_carrying_member_reaches_only_its_own_tenant(grant):
    assert _attach(_run(user_role="member"), _verified("owner")) == (PARENT,)


def test_a_verified_identity_of_another_tenant_reaches_only_the_runs_tenant(grant):
    assert _attach(_run(), _verified("owner", tenant_id="acme-elsewhere")) == (PARENT,)


def test_a_verified_admin_on_an_owner_run_still_reaches_the_descendants(grant):
    assert _attach(_run(user_role="owner"), _verified("admin")) == EVERYTHING


def test_an_owner_acting_in_a_child_reaches_only_from_that_child_down(grant):
    from robothor.engine.run_identity import attach_tenant_access

    seen: list[str] = []

    def resolve(tenant_id: str, user_role: str | None = None, **_: Any) -> tuple[str, ...]:
        seen.append(tenant_id)
        return (tenant_id,)

    run = _run()
    with patch("robothor.engine.permissions.resolve_accessible_tenants", side_effect=resolve):
        asyncio.run(attach_tenant_access(run, CHILD_A, _verified("owner", tenant_id=CHILD_A)))
    assert seen == [CHILD_A]
    assert run.accessible_tenant_ids == (CHILD_A,)


#: Triggers a person starts. Without a verified human on them, nobody is
#: vouched for: the unattended grant is for scheduled and system work only.
HUMAN_TRIGGERS = (
    TriggerType.WEBCHAT,
    TriggerType.TELEGRAM,
    TriggerType.SLACK,
    TriggerType.CHANNEL,
    TriggerType.MANUAL,
    TriggerType.IDE,
    TriggerType.WEBHOOK,
)

#: Triggers with no person behind them that the unattended grant covers.
GRANT_TRIGGERS = (
    TriggerType.CRON,
    TriggerType.HOOK,
    TriggerType.EVENT,
    TriggerType.WORKFLOW,
    TriggerType.CHANNEL_EVENT,
)


@pytest.mark.parametrize("trigger", HUMAN_TRIGGERS)
@pytest.mark.parametrize("identity", [None, UNVERIFIED], ids=["no-identity", "unverified"])
def test_an_unidentified_person_never_gets_the_unattended_grant(grant, trigger, identity):
    grant.tenant_flag = grant.opt_in = True
    run = _run(trigger_type=trigger, user_role="viewer")
    assert _attach(run, identity) == (PARENT,)


@pytest.mark.parametrize("trigger", GRANT_TRIGGERS)
def test_both_grants_reach_the_descendants_on_every_system_trigger(grant, trigger):
    grant.tenant_flag = grant.opt_in = True
    run = _run(trigger_type=trigger, user_role="service")
    assert _attach(run, None) == EVERYTHING


def test_a_heartbeat_with_both_grants_reaches_the_descendants(grant):
    grant.tenant_flag = grant.opt_in = True
    run = _run(trigger_type=TriggerType.CRON, trigger_detail="heartbeat:main", user_role="service")
    assert _attach(run, None) == EVERYTHING


def test_a_federated_run_never_gets_the_unattended_grant(grant):
    # A request from a peer instance is not this tenant's scheduled work.
    grant.tenant_flag = grant.opt_in = True
    assert _attach(_run(trigger_type=TriggerType.FEDERATION, user_role="service"), None) == (
        PARENT,
    )


def test_a_repair_continuation_reaches_only_its_own_tenant(grant):
    # Started by the engine after a deployment verifies, with nobody present:
    # the owner identity it carries is attribution, not someone at the keyboard.
    grant.tenant_flag = grant.opt_in = True
    run = _run(trigger_type=TriggerType.MANUAL, trigger_detail="repair-resume:job-1")
    assert _attach(run, OWNER) == (PARENT,)


def test_a_repair_continuation_is_confined_for_every_crossing():
    from robothor.engine.run_identity import confined_to_own_tenant

    run = _run(trigger_type=TriggerType.MANUAL, trigger_detail="repair-resume:job-1")
    assert confined_to_own_tenant(run)
    assert not confined_to_own_tenant(_run(trigger_type=TriggerType.MANUAL, trigger_detail="x"))


def test_a_failed_lookup_restricts_to_the_own_tenant():
    from robothor.engine.run_identity import attach_tenant_access

    run = _run()
    with patch(
        "robothor.engine.permissions.resolve_accessible_tenants",
        side_effect=RuntimeError("db down"),
    ):
        asyncio.run(attach_tenant_access(run, PARENT, OWNER))
    assert run.accessible_tenant_ids == (PARENT,)


def test_attribute_run_passes_the_effective_identity():
    from robothor.engine import run_attribution

    session = SimpleNamespace(run=_run(), identity=None)
    seen: dict[str, Any] = {}

    async def fake_attach(run: Any, tenant_id: str, identity: Any = None) -> None:
        seen["identity"] = identity

    with (
        patch.object(run_attribution, "attach_tenant_access", fake_attach),
        patch.object(run_attribution, "stamp_capability_tier", MagicMock(return_value=None)),
        patch.object(run_attribution, "link_run_person", MagicMock(side_effect=_noop)),
    ):
        asyncio.run(
            run_attribution.attribute_run(
                session,
                None,
                OWNER,
                TriggerType.WEBCHAT,
                None,
                PARENT,  # type: ignore[arg-type]
            )
        )
    assert seen["identity"] is OWNER


async def _noop(*_: Any, **__: Any) -> None:
    return None


# ── The manifest key ───────────────────────────────────────────────────────


def _manifest(**v2: Any) -> dict[str, Any]:
    return {"id": "probe", "name": "Probe", "model": {"primary": "probe-model"}, "v2": v2}


def test_the_manifest_key_is_read_from_v2():
    from robothor.engine.config import manifest_to_agent_config

    assert manifest_to_agent_config(_manifest(child_tenant_access=True)).child_tenant_access
    assert not manifest_to_agent_config(_manifest()).child_tenant_access


def test_the_manifest_key_is_a_known_v2_key():
    from robothor.engine.config_schema import validate_manifest

    warnings = validate_manifest({"id": "probe", "v2": {"child_tenant_access": True}})
    assert not [w for w in warnings if "child_tenant_access" in w]


def test_the_manifest_key_must_be_a_boolean():
    from robothor.engine.manifest_schema import validate

    issues = validate({"id": "probe", "v2": {"child_tenant_access": "yes"}})
    assert [i for i in issues if i.path == "v2.child_tenant_access" and i.code == "wrong_type"]


# ── The tenant flag ────────────────────────────────────────────────────────


def _conn_returning(row: Any) -> MagicMock:
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


@pytest.mark.parametrize(("row", "expected"), [((True,), True), ((False,), False), (None, False)])
def test_the_tenant_flag_reads_child_data_access(row, expected):
    from robothor.crm import dal

    with patch.object(dal, "get_connection", return_value=_conn_returning(row)):
        assert dal.tenant_child_data_access("acme-parent") is expected

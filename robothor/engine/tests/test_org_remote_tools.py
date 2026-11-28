"""``org_tools``, ``org_tool_read``, ``org_tool_write``: a parent's agent using a child's tool.

A child company's plugins (its connectors) are not installed in the parent.
These three built-ins reach them through the bridge (``POST
/api/org/remote/call``), which forwards to the child's own engine. They take
``tenant`` in their OWN schema: the engine's generic ``tenant`` interception
(``cross_tenant.admit``) leaves them alone, and the handler admits the call
itself, in this order:

1. the shape of ``tenant``,
2. a live, top-level, non-benchmark run,
3. a target this run may reach that is not its own tenant,
4. for a write, an attended run by the verified owner,
5. ``dry_run`` a real boolean.

Every outcome writes one ``agent.cross_tenant_call`` row under the home
tenant, with ``details.remote_tool``. The bridge is faked at the HTTP layer
(``httpx.MockTransport``), so the request the handler builds is the one
asserted.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from robothor.engine.models import TriggerType
from robothor.identity import IdentityContext
from robothor.tenancy.remote_contract import (
    ERR_ATTESTATION_MISSING,
    ERR_OUTCOME_UNKNOWN,
    ERR_WRITE_FORBIDDEN,
    HOME_TIMEOUT_S,
    SCOPE_ORG_REMOTE,
)

PARENT, CHILD_A, CHILD_B = "acme-parent", "acme-child-a", "acme-child-b"
REACH = (PARENT, CHILD_A, CHILD_B)
ALICE = "11111111-1111-1111-1111-111111111111"
KEY = "test-signing-key-at-least-32-bytes-long-xyz"
BRIDGE = "http://bridge.test"
TOOLS = ("org_tools", "org_tool_read", "org_tool_write")

OWNER = IdentityContext(
    tenant_id=PARENT,
    channel="webchat",
    identifier=ALICE,
    verified=True,
    role="owner",
    user_account_id=ALICE,
)
ADMIN = IdentityContext(
    tenant_id=PARENT, channel="webchat", identifier="bob", verified=True, role="admin"
)


def _session(**run: Any) -> SimpleNamespace:
    base = {
        "parent_run_id": None,
        "nesting_depth": 0,
        "trigger_type": TriggerType.WEBCHAT,
        "is_benchmark": False,
    }
    base.update(run)
    return SimpleNamespace(run=SimpleNamespace(**base))


class _Bridge:
    """The bridge, at the HTTP layer: records each request, answers what it is told."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: tuple[int, Any] | None = None
        self.per_tenant: dict[str, tuple[int, Any]] = {}
        self.raise_exc: Exception | None = None
        self.observers: list[Any] = []  # called with each request, before it is answered

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for observe in self.observers:
            observe(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        tenant = request.url.params.get("tenant") or self.body(request).get("tenant")
        if tenant in self.per_tenant:
            status, body = self.per_tenant[tenant]
        elif self.answer is not None:
            status, body = self.answer
        elif request.url.path.endswith("/catalogue"):
            status, body = (
                200,
                {
                    "tenant_id": tenant,
                    "plugins": [{"name": "example-plugin", "version": "1.0.0", "enabled": True}],
                    "tools": [{"name": "example_read", "access": "read", "description": "d"}],
                    "checks": [],
                },
            )
        else:
            sent = self.body(request)
            outcome = "dry_run" if sent.get("dry_run") else "ok"
            status, body = (
                200,
                {
                    "outcome": outcome,
                    "request_id": sent.get("request_id"),
                    "tool": sent.get("tool"),
                    "result": {"rows": 3},
                    "would": None,
                },
            )
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    @staticmethod
    def body(request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content) if request.content else {}

    @property
    def calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/api/org/remote/call"]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    from robothor.auth import tokens
    from robothor.engine.tools.handlers.org_remote import HANDLERS
    from robothor.engine.tools.service_client import reset_bridge_token_cache
    from robothor.settings import reset_settings

    tokens.reset_signing_key_cache()
    reset_bridge_token_cache()
    reset_settings()
    bridge = _Bridge()
    transport = httpx.MockTransport(bridge.handle)
    registry = MagicMock()
    registry.get_adapter_route.return_value = None
    state = SimpleNamespace(bridge=bridge, session=_session())
    with (
        patch("robothor.flags.store.resolve", side_effect=lambda name: None),
        patch("robothor.engine.permissions.check_tool_permission", MagicMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._runtime_denial", AsyncMock(return_value=None)),
        patch("robothor.engine.tools.dispatch._get_handlers", return_value=dict(HANDLERS)),
        patch("robothor.engine.tools.get_registry", return_value=registry),
        patch("robothor.engine.tools.dispatch._audit_tool_call"),
        patch(
            "robothor.engine.session_registry.lookup",
            side_effect=lambda run_id: state.session,
        ),
        patch(
            "robothor.services.get_service_url",
            side_effect=lambda name, path="": f"{BRIDGE}{path}",
        ),
        patch(
            "robothor.http_clients.shared_async_client",
            side_effect=lambda name, **kw: httpx.AsyncClient(transport=transport),
        ),
        patch("robothor.engine.tools.cross_tenant.audit_cross_tenant") as audit,
    ):
        state.audit = audit
        yield state
    tokens.reset_signing_key_cache()
    reset_bridge_token_cache()
    reset_settings()


def _call(name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import _execute_tool

    base: dict[str, Any] = {
        "agent_id": "main",
        "run_id": "run-1",
        "tenant_id": PARENT,
        "user_id": ALICE,
        "user_role": "owner",
        "accessible_tenant_ids": REACH,
        "identity": OWNER,
    }
    base.update(kw)
    return asyncio.run(_execute_tool(name, args, **base))


def _unattended(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return _call(name, args, identity=None, user_id="service:main", user_role="service")


def _read(**over: Any) -> dict[str, Any]:
    return {"tenant": CHILD_A, "tool": "example_read", "args": {"q": "x"}, **over}


def _write(**over: Any) -> dict[str, Any]:
    return {"tenant": CHILD_A, "tool": "example_write", "args": {"id": "r-1"}, **over}


def _refused(result: dict[str, Any], env: Any) -> str:
    assert result.get("refused") is True, result
    assert env.bridge.requests == [], "a refused call must send nothing"
    return str(result["error"])


def _rows(env: Any) -> list[dict[str, Any]]:
    return [c.kwargs for c in env.audit.call_args_list]


# ── The generic tenant interception leaves these tools alone ────────────────


@pytest.mark.parametrize("name", TOOLS)
def test_the_engines_tenant_interception_does_not_claim_them(env, name):
    from robothor.engine.tools import cross_tenant
    from robothor.engine.tools.dispatch import ToolContext

    ctx = ToolContext(agent_id="main", run_id="run-1", tenant_id=PARENT)
    assert cross_tenant.admit(name, {"tenant": CHILD_A, "dry_run": True}, ctx) is None


def test_a_read_runs_in_the_home_tenant_and_reaches_the_child_through_the_bridge(env):
    from robothor.db.connection import current_tenant_scope

    seen: list[Any] = []
    env.bridge.observers.append(lambda request: seen.append(current_tenant_scope()))
    result = _call("org_tool_read", _read())

    assert "error" not in result, result
    assert result["result"] == {"rows": 3}
    assert seen == [None], "never inside the child's database scope"


# ── What reaches the bridge ─────────────────────────────────────────────────


def test_a_read_sends_the_contract_body_with_a_scoped_engine_token(env):
    from robothor.auth.tokens import decode_token

    _call("org_tool_read", _read())

    (request,) = env.bridge.calls
    assert str(request.url) == f"{BRIDGE}/api/org/remote/call"
    sent = env.bridge.body(request)
    assert sent == {
        "tenant": CHILD_A,
        "tool": "example_read",
        "args": {"q": "x"},
        "access": "read",
        "dry_run": False,
        "request_id": None,
        "requested_by": ALICE,
        "agent_id": "main",
    }
    claims = decode_token(
        request.headers["authorization"].removeprefix("Bearer "), expected_audience="genus-bridge"
    )
    assert claims["tid"] == PARENT
    assert SCOPE_ORG_REMOTE in claims["scope"].split()
    assert claims["agent_id"] == "main"


def test_the_request_carries_the_home_timeout(env):
    seen: list[Any] = []
    env.bridge.observers.append(lambda request: seen.append(request.extensions.get("timeout")))
    _call("org_tool_read", _read())

    assert seen and seen[0]["read"] == HOME_TIMEOUT_S


def test_an_unattended_read_names_no_person(env):
    env.session = _session(trigger_type=TriggerType.CRON)

    result = _unattended("org_tool_read", _read())

    assert "error" not in result, result
    assert env.bridge.body(env.bridge.calls[0])["requested_by"] is None


def test_a_person_without_an_account_id_is_not_sent_as_one(env):
    # A verified chat-app identity linked to no account: nobody to name.
    unlinked = IdentityContext(
        tenant_id=PARENT, channel="telegram", identifier="4242", verified=True, role="admin"
    )
    _call("org_tool_read", _read(), identity=unlinked, user_id="bob", user_role="admin")

    assert env.bridge.body(env.bridge.calls[0])["requested_by"] is None


def test_the_catalogue_of_one_company_and_one_tool(env):
    _call("org_tools", {"tenant": CHILD_A})
    _call("org_tools", {"tenant": CHILD_A, "tool": "example_read"})

    params = [dict(r.url.params) for r in env.bridge.requests]
    assert [r.url.path for r in env.bridge.requests] == ["/api/org/remote/catalogue"] * 2
    assert params == [{"tenant": CHILD_A}, {"tenant": CHILD_A, "tool": "example_read"}]


def test_the_catalogue_without_a_tenant_asks_every_reachable_company(env):
    result = _call("org_tools", {})

    asked = sorted(r.url.params["tenant"] for r in env.bridge.requests)
    assert asked == [CHILD_A, CHILD_B]
    assert [t["tenant_id"] for t in result["tenants"]] == [CHILD_A, CHILD_B]
    assert all(t["status"] == "ok" for t in result["tenants"])


def test_one_company_down_in_the_catalogue_is_that_entry_only(env):
    env.bridge.per_tenant[CHILD_B] = (502, {"error": "engine unavailable"})

    result = _call("org_tools", {})

    by_id = {t["tenant_id"]: t for t in result["tenants"]}
    assert by_id[CHILD_A]["status"] == "ok"
    assert by_id[CHILD_B]["status"] == "error"


def test_the_catalogue_with_no_reach_says_so_and_asks_nobody(env):
    result = _call("org_tools", {}, accessible_tenant_ids=(PARENT,))

    assert result["tenants"] == []
    assert result["note"]
    assert env.bridge.requests == []


# ── Admission, in order ─────────────────────────────────────────────────────


def test_a_write_is_sent_as_asked(env):
    _call("org_tool_write", _write())
    _call("org_tool_write", _write(dry_run=True))

    assert [env.bridge.body(r)["dry_run"] for r in env.bridge.calls] == [False, True]
    assert all(env.bridge.body(r)["access"] == "write" for r in env.bridge.calls)


@pytest.mark.parametrize("bad", [7, "x" * 129, "bad\ntenant", ["a"]])
def test_a_malformed_tenant_is_answered_as_unreachable(env, bad):
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    assert _refused(_call("org_tool_read", _read(tenant=bad)), env) == UNREACHABLE


@pytest.mark.parametrize("name", ["org_tool_read", "org_tool_write"])
def test_a_call_without_a_tenant_is_refused(env, name):
    _refused(_call(name, {"tool": "example_read"}), env)


@pytest.mark.parametrize("name", TOOLS)
def test_a_sub_agent_is_refused(env, name):
    env.session = _session(parent_run_id="parent-run", nesting_depth=1)

    _refused(_call(name, _read()), env)


def test_a_benchmark_run_is_refused(env):
    _refused(_call("org_tool_read", _read(), is_benchmark=True), env)


def test_a_call_outside_a_live_run_is_refused(env):
    env.session = None

    _refused(_call("org_tool_read", _read()), env)


def test_an_unreachable_and_a_nonexistent_tenant_get_the_same_sentence(env):
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    unreachable = _call(
        "org_tool_read", _read(tenant=CHILD_B), accessible_tenant_ids=(PARENT, CHILD_A)
    )
    missing = _call("org_tool_read", _read(tenant="no-such-tenant"))

    assert _refused(unreachable, env) == _refused(missing, env) == UNREACHABLE


def test_the_own_tenant_is_refused_with_a_pointer_to_the_tool_itself(env):
    result = _call("org_tool_read", _read(tenant=PARENT))

    message = _refused(result, env)
    assert "your own" in message.lower()


def test_a_write_needs_the_verified_owner(env):
    result = _call("org_tool_write", _write(), identity=ADMIN, user_id="bob", user_role="admin")

    assert "owner" in _refused(result, env).lower()
    assert result["error_code"] == ERR_WRITE_FORBIDDEN


def test_an_unattended_run_never_writes(env):
    env.session = _session(trigger_type=TriggerType.CRON)

    result = _unattended("org_tool_write", _write())

    _refused(result, env)
    assert result["error_code"] == ERR_WRITE_FORBIDDEN


def test_an_owner_identity_on_a_scheduled_run_still_does_not_write(env):
    env.session = _session(trigger_type=TriggerType.CRON)

    _refused(_call("org_tool_write", _write()), env)


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_dry_run_must_be_a_real_boolean(env, bad):
    _refused(_call("org_tool_write", _write(dry_run=bad)), env)


def test_the_owner_check_comes_before_the_dry_run_check(env):
    result = _call(
        "org_tool_write",
        _write(dry_run="yes"),
        identity=ADMIN,
        user_id="bob",
        user_role="admin",
    )

    assert "owner" in _refused(result, env).lower()


@pytest.mark.parametrize(("field", "value"), [("tool", ""), ("tool", 5), ("args", ["x"])])
def test_a_malformed_tool_or_args_is_refused(env, field, value):
    _refused(_call("org_tool_read", _read(**{field: value})), env)


# ── What comes back ─────────────────────────────────────────────────────────


def test_a_target_refusal_keeps_its_code_and_message(env):
    env.bridge.answer = (
        403,
        {"error": ERR_ATTESTATION_MISSING, "message": "Needs an agreement.", "attestation": "baa"},
    )

    result = _call("org_tool_read", _read())

    assert result["error_code"] == ERR_ATTESTATION_MISSING
    assert "Needs an agreement." in result["error"]
    assert result["attestation"] == "baa"


def test_a_tool_that_reported_an_error_is_an_error(env):
    env.bridge.answer = (
        200,
        {"outcome": "error", "request_id": None, "tool": "example_read", "result": {"error": "x"}},
    )

    result = _call("org_tool_read", _read())

    assert result["error"]
    assert result["result"] == {"error": "x"}


def test_a_write_whose_outcome_the_target_cannot_tell_is_outcome_unknown(env):
    env.bridge.answer = (409, {"error": ERR_OUTCOME_UNKNOWN, "message": "In flight."})

    result = _call("org_tool_write", _write())

    assert result["outcome_unknown"] is True


def _unknown_answer() -> tuple[int, dict[str, Any]]:
    return (
        200,
        {
            "outcome": "error",
            "request_id": "eff-1",
            "tool": "example_write",
            "result": {"error": "the connection dropped after the request was sent"},
            "would": None,
            "outcome_unknown": True,
        },
    )


def test_a_write_the_child_cannot_account_for_says_it_may_have_changed(env):
    env.bridge.answer = _unknown_answer()

    result = _call("org_tool_write", _write())

    assert result["outcome_unknown"] is True
    assert result["retryable"] is False
    assert result["error_code"] == ERR_OUTCOME_UNKNOWN
    assert "may or may not" in result["error"]
    assert "connection dropped" in result["error"]
    (row,) = _rows(env)
    assert row["outcome"] not in ("denied", "ok")


def test_an_unknown_write_is_fenced_in_the_effect_ledger(env, ledger):
    env.bridge.answer = _unknown_answer()

    with (
        patch("robothor.engine.runtime.effect_results.record", return_value=True) as record,
        patch("robothor.engine.runtime.effects.finish", return_value=True) as finish,
    ):
        _call("org_tool_write", _write())

    record.assert_not_called()
    assert finish.call_args.kwargs["uncertain"] is True


def test_a_write_that_timed_out_on_the_way_is_outcome_unknown(env):
    env.bridge.raise_exc = httpx.ReadTimeout("slow")

    result = _call("org_tool_write", _write())

    assert result["outcome_unknown"] is True


def test_a_write_the_bridge_never_received_is_known_not_sent(env):
    env.bridge.raise_exc = httpx.ConnectError("refused")

    result = _call("org_tool_write", _write())

    assert result["error"]
    assert result.get("outcome_unknown") is not True


def test_a_read_that_timed_out_is_a_retryable_error(env):
    env.bridge.raise_exc = httpx.ReadTimeout("slow")

    result = _call("org_tool_read", _read())

    assert result["error"]
    assert result.get("outcome_unknown") is not True


# ── Audit: one row per outcome ──────────────────────────────────────────────


def test_an_ok_read_is_audited_once_with_the_remote_tool(env):
    _call("org_tool_read", _read())

    (row,) = _rows(env)
    assert (row["home_tenant"], row["target_tenant"]) == (PARENT, CHILD_A)
    assert (row["tool"], row["outcome"], row["remote_tool"]) == (
        "org_tool_read",
        "ok",
        "example_read",
    )
    assert row["user_id"] == ALICE


def test_a_refusal_is_audited_as_denied(env):
    _call("org_tool_write", _write(), identity=ADMIN, user_id="bob", user_role="admin")

    (row,) = _rows(env)
    assert (row["tool"], row["outcome"], row["remote_tool"]) == (
        "org_tool_write",
        "denied",
        "example_write",
    )


def test_a_target_refusal_is_audited_as_denied_with_its_code(env):
    env.bridge.answer = (403, {"error": ERR_ATTESTATION_MISSING, "message": "No."})

    _call("org_tool_read", _read())

    (row,) = _rows(env)
    assert row["outcome"] == "denied"
    assert ERR_ATTESTATION_MISSING in row["reason"]


def test_an_error_is_audited_as_an_error(env):
    env.bridge.raise_exc = httpx.ConnectError("refused")

    _call("org_tool_read", _read())

    (row,) = _rows(env)
    assert row["outcome"] == "error"


def test_a_dry_run_is_audited_as_a_dry_run(env):
    _call("org_tool_write", _write(dry_run=True))

    (row,) = _rows(env)
    assert row["dry_run"] is True


def test_the_catalogue_is_audited_once_per_company(env):
    _call("org_tools", {})

    assert sorted(r["target_tenant"] for r in _rows(env)) == [CHILD_A, CHILD_B]
    assert {r["tool"] for r in _rows(env)} == {"org_tools"}


def test_a_refused_catalogue_is_audited(env):
    _call("org_tools", {"tenant": CHILD_B}, accessible_tenant_ids=(PARENT, CHILD_A))

    (row,) = _rows(env)
    assert row["outcome"] == "denied"


# ── The effect ledger ───────────────────────────────────────────────────────


@pytest.fixture
def ledger():
    from robothor.engine.runtime.contracts import ExecutionContext
    from robothor.engine.runtime.current import active_context

    reserved: list[dict[str, Any]] = []
    state = SimpleNamespace(record={"state": "prepared", "id": "eff-1"}, reserved=reserved)

    async def _reserve(context, name, args, ctx):
        reserved.append({"name": name, "args": dict(args)})
        return state.record

    token = active_context.set(ExecutionContext(PARENT, ALICE, "request"))
    with (
        patch("robothor.engine.runtime.effect_dispatch._reserve", side_effect=_reserve),
        patch("robothor.engine.runtime.effects.mark_dispatched", return_value=True),
        patch("robothor.engine.runtime.effects.finish", return_value=True),
        patch("robothor.engine.runtime.effect_results.record", return_value=True),
    ):
        yield state
    active_context.reset(token)


def test_a_write_is_journalled_and_sends_its_effect_id(env, ledger):
    _call("org_tool_write", _write())

    assert [r["name"] for r in ledger.reserved] == ["org_tool_write"]
    assert env.bridge.body(env.bridge.calls[0])["request_id"] == "eff-1"


def test_a_read_is_not_journalled(env, ledger):
    _call("org_tool_read", _read())

    assert ledger.reserved == []


def test_a_replay_from_the_ledger_does_not_call_again_and_is_audited(env, ledger):
    ledger.record = {
        "state": "confirmed",
        "id": "eff-1",
        "resolution": {"result": {"outcome": "ok", "result": {"rows": 3}}},
    }

    result = _call("org_tool_write", _write())

    assert env.bridge.requests == []
    assert result["recovered"] is True
    (row,) = _rows(env)
    assert (row["tool"], row["outcome"], row["remote_tool"]) == (
        "org_tool_write",
        "replay",
        "example_write",
    )


_WITHHELD = {"reason": "too_large", "bytes": 300000, "max_bytes": 262144}


def _withheld_answer() -> tuple[int, dict[str, Any]]:
    return (
        200,
        {
            "outcome": "ok",
            "request_id": "eff-1",
            "tool": "example_write",
            "result": None,
            "would": None,
            "result_withheld": dict(_WITHHELD),
        },
    )


def test_a_write_whose_result_was_too_large_is_reported_as_done(env):
    env.bridge.answer = _withheld_answer()

    result = _call("org_tool_write", _write())

    assert "error" not in result
    assert result["outcome"] == "ok"
    assert result["result"] is None
    assert result["result_withheld"] == _WITHHELD
    assert "too large" in result["note"]
    assert "outcome_unknown" not in result
    (row,) = _rows(env)
    assert row["outcome"] == "ok"


def test_a_too_large_write_is_recorded_so_a_retry_replays_it(env, ledger):
    env.bridge.answer = _withheld_answer()
    recorded: list[dict[str, Any]] = []

    def _record(context, effect_id, run_id, result):
        recorded.append(result)
        ledger.record = {
            "state": "finished",
            "id": effect_id,
            "resolution": {"source": "tool_response", "result": result},
        }
        return True

    with (
        patch("robothor.engine.runtime.effect_results.record", side_effect=_record),
        patch("robothor.engine.runtime.effects.finish", return_value=True) as finish,
    ):
        first = _call("org_tool_write", _write())
        second = _call("org_tool_write", _write())

    assert first["outcome"] == "ok"
    assert len(recorded) == 1
    finish.assert_not_called()  # done and cached, never uncertain
    assert len(env.bridge.calls) == 1
    assert second["recovered"] is True
    assert second["result_withheld"] == _WITHHELD


# ── Registration ────────────────────────────────────────────────────────────


def test_registered_with_schemas_that_declare_tenant():
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas

    schemas = get_engine_schemas()
    for name in TOOLS:
        assert name in builtin_handlers()
        props = schemas[name]["function"]["parameters"]["properties"]
        assert "tenant" in props and "tool" in props
    assert schemas["org_tool_read"]["function"]["parameters"]["required"] == ["tenant", "tool"]
    assert "dry_run" in schemas["org_tool_write"]["function"]["parameters"]["properties"]
    assert "dry_run" not in schemas["org_tool_read"]["function"]["parameters"]["properties"]


def test_the_two_reads_are_read_only_and_none_is_core():
    from robothor.engine.tools.constants import CORE_TOOLS, READONLY_TOOLS

    for name in ("org_tools", "org_tool_read"):
        assert name in READONLY_TOOLS
    assert "org_tool_write" not in READONLY_TOOLS
    # Offered up front only to a run that reaches a child
    # (test_org_remote_tools_discoverable.py).
    assert not set(TOOLS) & CORE_TOOLS


def test_not_on_the_generic_cross_tenant_allowlists():
    from robothor.engine.tools.constants import CROSS_TENANT_READS, CROSS_TENANT_WRITES

    assert not set(TOOLS) & (CROSS_TENANT_READS | CROSS_TENANT_WRITES)


def test_every_result_is_wrapped_as_untrusted_content():
    from robothor.engine.session import EXTERNAL_DATA_TOOLS

    assert set(TOOLS) <= EXTERNAL_DATA_TOOLS


def test_the_two_call_tools_outlast_the_home_timeout():
    from robothor.engine.tool_timeouts import resolve_tool_timeout

    for name in ("org_tool_read", "org_tool_write"):
        assert resolve_tool_timeout(name, 120) > HOME_TIMEOUT_S


def test_withheld_from_benchmark_runs():
    from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS

    assert set(TOOLS) <= _BENCHMARK_EXCLUDED_TOOLS


def test_each_has_a_search_hint():
    from robothor.engine.tools.keywords import TOOL_HINTS

    for name in TOOLS:
        assert TOOL_HINTS[name].keywords


def test_a_bridge_refusal_says_what_to_fix_and_is_not_a_target_code(env):
    sentence = (
        "agent 'main' is listed in the bridge's capabilities manifest "
        '(agent_capabilities.json) without "POST /api/org/remote/call" in its '
        "bridge_endpoints: add them and restart the bridge"
    )
    env.bridge.answer = (403, {"error": sentence})

    result = _call("org_tool_read", _read())

    assert sentence in result["error"]
    assert result["error"].startswith("The bridge refused the call")
    assert result["error_code"] is None


# ── A write the home side cannot read as a clean refusal is unknown ─────────


@pytest.mark.parametrize(
    "answer",
    [
        (500, {"error": "internal_error", "message": "the call failed"}),
        (500, b"Internal Server Error"),
        (200, b"<html>not json</html>"),
        (200, ["not", "an", "object"]),
        (200, {"outcome": "something_new", "result": {}}),
        (418, {"error": "teapot"}),
        (503, {"detail": "upstream"}),
        (413, {"error": "remote_too_large", "message": "too big"}),
    ],
    ids=lambda a: f"{a[0]}-{type(a[1]).__name__}",
)
def test_a_write_answer_that_is_not_a_clean_refusal_is_unknown(env, answer):
    env.bridge.answer = answer

    result = _call("org_tool_write", _write())

    assert result["outcome_unknown"] is True, result
    assert result["retryable"] is False
    (row,) = _rows(env)
    assert row["outcome"] == "unknown"


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"error": "remote_write_forbidden", "message": "Owners only."}),
        (403, {"error": "remote_not_permitted", "message": "No."}),
        (
            403,
            {"error": "remote_attestation_missing", "message": "Needs baa.", "attestation": "baa"},
        ),
        (404, {"error": "remote_tool_unknown", "message": "No such tool."}),
        (404, {"error": "remote_not_permitted", "message": "Unreachable."}),
        (409, {"error": "remote_access_mismatch", "message": "Read only."}),
        (409, {"error": "remote_request_mismatch", "message": "Other call."}),
        (422, {"error": "remote_bad_arguments", "message": "Bad."}),
        (401, {"error": "token expired"}),  # the bridge's own gate: nothing was sent
        (403, {"error": "scope missing"}),
    ],
    ids=lambda a: f"{a[0]}-{a[1].get('error')}",
)
def test_a_clean_refusal_of_a_write_is_not_unknown(env, answer):
    env.bridge.answer = answer

    result = _call("org_tool_write", _write())

    assert "outcome_unknown" not in result
    (row,) = _rows(env)
    assert row["outcome"] == "denied"


def test_a_500_on_a_write_is_fenced_in_the_effect_ledger(env, ledger):
    env.bridge.answer = (500, b"Internal Server Error")

    with (
        patch("robothor.engine.runtime.effect_results.record", return_value=True) as record,
        patch("robothor.engine.runtime.effects.finish", return_value=True) as finish,
    ):
        _call("org_tool_write", _write())

    record.assert_not_called()
    assert finish.call_args.kwargs["uncertain"] is True


def test_a_500_on_a_read_is_a_plain_error(env):
    env.bridge.answer = (500, b"Internal Server Error")

    result = _call("org_tool_read", _read())

    assert result["error"]
    assert "outcome_unknown" not in result


# ── The home fence covers one call, never the whole tool ─────────────────────


def _write_args(**over: Any) -> dict[str, Any]:
    return {"tenant": CHILD_A, "tool": "example_write", "args": {"id": 7, "note": "x"}, **over}


def test_org_tool_write_fences_by_tenant_tool_and_argument_digest():
    from robothor.engine.runtime import effect_tools
    from robothor.tenancy.remote_contract import args_digest

    target = effect_tools.target_of("org_tool_write", _write_args())

    assert target == {
        "tenant": CHILD_A,
        "tool": "example_write",
        "args_digest": args_digest({"id": 7, "note": "x"}),
    }
    assert effect_tools.fences_whole_tool("org_tool_write") is False
    key = effect_tools.target_key("org_tool_write", target)
    assert key is not None
    for other in (
        _write_args(tenant=CHILD_B),
        _write_args(tool="example_other"),
        _write_args(args={"id": 8, "note": "x"}),
    ):
        assert (
            effect_tools.target_key(
                "org_tool_write", effect_tools.target_of("org_tool_write", other)
            )
            != key
        )


def test_the_stored_target_holds_the_digest_never_the_arguments():
    from robothor.engine.runtime import effect_tools

    secret = "patient-name-Alice-Example"
    stored = effect_tools.stored(
        "org_tool_write",
        effect_tools.target_of("org_tool_write", _write_args(args={"name": secret})),
    )

    assert secret not in repr(stored)
    assert set(stored) == {"tenant", "tool", "args_digest"}


def test_a_dry_run_write_bypasses_the_effect_ledger(env, ledger):
    _call("org_tool_write", _write(dry_run=True))

    assert ledger.reserved == []
    assert env.bridge.body(env.bridge.calls[0])["dry_run"] is True


def test_a_plain_error_from_the_child_tool_is_not_unknown(env):
    env.bridge.answer = (
        200,
        {
            "outcome": "error",
            "request_id": "eff-1",
            "tool": "example_write",
            "result": {"error": "slot already taken"},
            "would": None,
        },
    )

    result = _call("org_tool_write", _write())

    assert result["error"]
    assert "outcome_unknown" not in result


# ── The home audit never copies the child's own text ────────────────────────


def test_a_tool_reported_error_is_audited_without_its_text(env):
    env.bridge.answer = (
        200,
        {
            "outcome": "error",
            "request_id": "eff-1",
            "tool": "example_write",
            "result": {"error": "no slot for patient-name-Alice-Example"},
            "would": None,
        },
    )

    result = _call("org_tool_write", _write())

    assert "patient-name-Alice-Example" in result["error"]  # the model still reads it
    (row,) = _rows(env)
    assert "patient-name-Alice-Example" not in row["reason"]
    assert row["reason"]


def test_an_unreadable_answer_is_audited_without_its_text(env):
    env.bridge.answer = (500, {"error": "boom", "message": "patient-name-Alice-Example leaked"})

    _call("org_tool_write", _write())

    (row,) = _rows(env)
    assert "patient-name-Alice-Example" not in row["reason"]


def test_a_targets_own_refusal_sentence_is_kept_in_the_audit(env):
    env.bridge.answer = (
        403,
        {"error": "remote_attestation_missing", "message": "Needs an agreement."},
    )

    _call("org_tool_write", _write())

    (row,) = _rows(env)
    assert "Needs an agreement." in row["reason"]


# ── A call interrupted on the way still leaves its audit row ────────────────


def test_a_write_interrupted_by_an_unexpected_error_is_audited_as_maybe_reached(env):
    from robothor.engine.tools.handlers import org_remote

    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    with patch.object(org_remote, "_send", broken):
        result = _call("org_tool_write", _write())

    assert result["error"]
    (row,) = _rows(env)
    assert row["outcome"] == "unknown"
    assert "may have reached" in row["reason"]


def test_a_cancelled_write_is_audited_as_maybe_reached(env):
    from robothor.engine.tools.handlers import org_remote

    async def cancelled(*a: Any, **k: Any) -> Any:
        raise asyncio.CancelledError

    with patch.object(org_remote, "_send", cancelled), pytest.raises(asyncio.CancelledError):
        _call("org_tool_write", _write())

    (row,) = _rows(env)
    assert row["outcome"] == "unknown"


def test_an_interrupted_read_is_audited_as_an_error(env):
    from robothor.engine.tools.handlers import org_remote

    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    with patch.object(org_remote, "_send", broken):
        _call("org_tool_read", _read())

    (row,) = _rows(env)
    assert row["outcome"] == "error"


# ── dry_run where it does not belong ─────────────────────────────────────────


def test_dry_run_inside_a_writes_args_is_refused_before_sending(env):
    result = _call("org_tool_write", _write(args={"id": 7, "dry_run": True}))

    message = _refused(result, env)
    assert "top level" in message
    assert env.bridge.requests == []


def test_a_read_with_dry_run_is_refused_before_sending(env):
    result = _call("org_tool_read", _read(dry_run=True))

    _refused(result, env)
    assert env.bridge.requests == []

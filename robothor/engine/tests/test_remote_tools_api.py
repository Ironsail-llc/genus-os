"""A parent tenant's agent runs a declared tool in this tenant, through the bridge.

The target engine's three routes under ``/api/admin/remote-tools``. The bridge
is the only caller; it forwards what the parent's engine asked for, and this
engine believes none of it beyond what it re-derives: the plugin's
declaration, the person's standing (``acting_role``) or the parent's grant
(``parent_agent_grant``), the attestations the declaration requires, the
arguments against the tool's own schema. A write needs an owner and a request
id; a finished one is replayed, an unfinished one is never run twice. The call
itself goes through the normal registry dispatch, so RBAC applies with the
derived role, and every call leaves one audit row under this tenant that holds
a digest of the arguments and never the arguments or the result.

The plugin, the accounts, the hierarchy, the permission rows and the request
ledger are fakes; everything between the route and the plugin's handler is the
real code.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from robothor.auth.deps import AuthContext

# The real evaluator, taken before the engine suite's autouse fixture swaps in
# its seeded stand-in: these tests read permission rows through it.
from robothor.engine.permissions import check_tool_permission as _real_check_tool_permission
from robothor.tenancy import remote_contract as rc

CHILD = "acme-child-a"  # this engine's tenant
PARENT = "acme-parent"
OTHER = "acme-other"  # not this tenant's ancestor

ALICE = "4d4d4d4d-1111-4222-8333-0000000000a1"  # parent owner
BOB = "4d4d4d4d-1111-4222-8333-0000000000b2"  # parent admin
CAROL = "4d4d4d4d-1111-4222-8333-0000000000c3"  # parent member

SECRET = "patient-name-Alice-Example"  # an argument value that must never be audited
RESULT_SECRET = "result-value-never-audited"

BRIDGE = AuthContext(user_id=rc.BRIDGE_SERVICE_ID, tenant_id=CHILD, role="service", typ="service")


# ── the plugin ───────────────────────────────────────────────────────────────


class _Dist:
    name = "acme-tools"
    version = "1.2.3"
    metadata = {"Name": "acme-tools"}
    files = ()

    def read_text(self, filename: str) -> str | None:
        # A payload that declares `remote` must carry the manifest mirror.
        return _MANIFEST if filename == "genus-plugin.yaml" else None


_MANIFEST = (
    "name: acme-tools\n"
    "contract_version: 1\n"
    "handlers: [acme_lookup, acme_update, acme_big, acme_export, acme_risky, acme_slow,"
    " acme_fails, acme_plain]\n"
    "schemas: [acme_lookup, acme_update]\n"
    "remote_tools:\n"
    "  read: [acme_lookup, acme_big, acme_slow, acme_fails]\n"
    "  write: [acme_update, acme_export, acme_risky]\n"
)


class _EP:
    def __init__(self, group: str, payload: dict[str, Any], name: str = "acme") -> None:
        self.group = group
        self.name = name
        self.value = f"acme_tools:{group}"
        self.dist = _Dist()
        self._payload = payload

    def load(self) -> dict[str, Any]:
        return self._payload


class _Handlers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.delay = 0.0
        self.risky_result: Any = {"done": True}
        self.lookup_result: Any = None

    async def lookup(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_lookup", args, ctx))
        if self.lookup_result is not None:
            return dict(self.lookup_result)
        return {"record": {"id": 7, "note": RESULT_SECRET}}

    async def update(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_update", args, ctx))
        return {"updated": True, "note": RESULT_SECRET}

    async def big(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_big", args, ctx))
        return {"blob": "x" * (rc.REMOTE_RESULT_MAX_BYTES + 10)}

    async def big_write(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_export", args, ctx))
        return {"exported": True, "blob": "x" * (rc.REMOTE_RESULT_MAX_BYTES + 10)}

    async def risky(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        """A write whose answer each test sets: a value, or an exception to raise."""
        self.calls.append(("acme_risky", args, ctx))
        outcome = self.risky_result
        if outcome == "hang":
            await asyncio.sleep(5)
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    async def slow(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_slow", args, ctx))
        await asyncio.sleep(5)
        return {"late": True}

    async def fails(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_fails", args, ctx))
        return {"error": "the upstream said no"}

    async def plain(self, args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
        self.calls.append(("acme_plain", args, ctx))
        return {}


def _schema(name: str, description: str, properties: dict[str, Any], required=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


LONG_DESCRIPTION = "Look a record up by its query. " + "More detail. " * 40


def _entry_points(handlers: _Handlers) -> list[_EP]:
    from robothor.doctor.model import Check, fail, ok

    async def connected(ctx: Any) -> Any:
        return ok("connected to the upstream")

    async def leaky(ctx: Any) -> Any:
        return fail(
            "token sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 at /srv/acme/conf.yaml"
        )

    tools = {
        "genus_contract_version": "1.0",
        "handlers": {
            "acme_lookup": handlers.lookup,
            "acme_update": handlers.update,
            "acme_big": handlers.big,
            "acme_export": handlers.big_write,
            "acme_risky": handlers.risky,
            "acme_slow": handlers.slow,
            "acme_fails": handlers.fails,
            "acme_plain": handlers.plain,
        },
        "read_only": ["acme_lookup", "acme_big", "acme_slow", "acme_fails", "acme_plain"],
        "remote": {
            "acme_lookup": {"access": "read", "notes": "Use it to find one record."},
            "acme_update": {"access": "write", "attestations": ["baa"]},
            "acme_big": {"access": "read"},
            "acme_export": {"access": "write"},
            "acme_risky": {"access": "write"},
            "acme_slow": {"access": "read"},
            "acme_fails": {"access": "read"},
        },
    }
    schemas = {
        "genus_contract_version": "1.0",
        "schemas": {
            "acme_lookup": _schema(
                "acme_lookup", LONG_DESCRIPTION, {"query": {"type": "string"}}, ["query"]
            ),
            "acme_update": _schema(
                "acme_update",
                "Change a record.",
                {"id": {"type": "integer"}, "note": {"type": "string"}},
                ["id"],
            ),
        },
    }
    checks = {
        "genus_contract_version": "1.0",
        "checks": {
            "acme_tools.connection": Check(
                id="acme_tools.connection",
                title="Upstream reachable",
                category="plugins",
                severity="required",
                run=connected,
            ),
            "acme_tools.credentials": Check(
                id="acme_tools.credentials",
                title="Credentials shaped",
                category="plugins",
                severity="recommended",
                run=leaky,
            ),
        },
    }
    return [
        _EP("genus.tools", tools),
        _EP("genus.schemas", schemas),
        _EP("genus.doctor", checks),
    ]


# ── the world around it ──────────────────────────────────────────────────────


class _Writer:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> None:
        self.rows.append(kwargs)


class _Ledger:
    """The request ledger's three operations, in memory."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.claims: list[tuple[str, str]] = []

    def claim(
        self, tenant_id: str, request_id: str, tool: str = "", args_digest: str = ""
    ) -> dict[str, Any] | None:
        key = (tenant_id, request_id)
        self.claims.append(key)
        if key in self.rows:
            return dict(self.rows[key])
        self.rows[key] = {
            "state": "running",
            "result": None,
            "tool": tool,
            "args_digest": args_digest,
        }
        return None

    def finish(self, tenant_id: str, request_id: str, result: dict[str, Any]) -> None:
        self.rows[(tenant_id, request_id)] = {
            **self.rows.get((tenant_id, request_id), {}),
            "state": "done",
            "result": result,
        }

    def release(self, tenant_id: str, request_id: str) -> None:
        self.rows.pop((tenant_id, request_id), None)


@pytest.fixture
def world(monkeypatch) -> Any:
    from robothor.engine import remote_tools
    from robothor.engine.tools import registry as tool_registry
    from robothor.plugins import loader

    handlers = _Handlers()
    eps = _entry_points(handlers)
    monkeypatch.setattr(loader, "_discover", lambda: list(eps))
    monkeypatch.setattr(
        "robothor.doctor.registry._discover", lambda: [e for e in eps if e.group == "genus.doctor"]
    )
    loader.reload_plugins()
    tool_registry.get_registry()._refresh_plugin_schemas_if_stale()

    accounts = {
        ALICE: {"id": ALICE, "tenant_id": PARENT, "role": "owner", "status": "active"},
        BOB: {"id": BOB, "tenant_id": PARENT, "role": "admin", "status": "active"},
        CAROL: {"id": CAROL, "tenant_id": PARENT, "role": "member", "status": "active"},
    }
    tree = {PARENT: (PARENT, CHILD), OTHER: (OTHER,), CHILD: (CHILD,)}
    state: dict[str, Any] = {"grant": {PARENT: True}}
    # (pattern, access, tenant) per role, as role_permissions holds them.
    rules: dict[str, list[tuple[str, str, str]]] = {
        "owner": [("*", "allow", "__default__")],
        "admin": [("*", "allow", "__default__")],
        rc.ROLE_PARENT_AGENT: [],
    }

    def accessible(tenant_id: str, role: str | None = None, **kw: Any) -> tuple[str, ...]:
        return tree.get(tenant_id, (tenant_id,)) if role in ("owner", "admin") else (tenant_id,)

    def cached_rows(key: tuple[str, str, str], sql: str, params: tuple[Any, ...]) -> list[Any]:
        table, _tenant, who = key
        if table == "user_permissions":
            return []
        return list(rules.get(who, []))

    writer = _Writer()
    ledger = _Ledger()
    monkeypatch.setattr("robothor.auth.accounts.get_account_by_id", lambda uid: accounts.get(uid))
    monkeypatch.setattr("robothor.engine.permissions.resolve_accessible_tenants", accessible)
    monkeypatch.setattr(
        "robothor.engine.permissions.check_tool_permission", _real_check_tool_permission
    )
    monkeypatch.setattr("robothor.engine.permissions._cached_rows", cached_rows)
    monkeypatch.setattr(
        "robothor.crm.dal.tenant_child_data_access", lambda t: bool(state["grant"].get(t))
    )
    monkeypatch.setattr("robothor.engine.event_writer.get_event_writer", lambda: writer)
    monkeypatch.setattr("robothor.goals.runtime.admit_tool", lambda *a, **k: None)
    monkeypatch.setattr(remote_tools, "claim_request", ledger.claim)
    monkeypatch.setattr(remote_tools, "finish_request", ledger.finish)
    monkeypatch.setattr(remote_tools, "release_request", ledger.release)
    remote_tools.reset_caches()
    yield type(
        "World",
        (),
        {
            "handlers": handlers,
            "state": state,
            "rules": rules,
            "writer": writer,
            "ledger": ledger,
        },
    )
    remote_tools.reset_caches()
    loader.reload_plugins()


def _app(auth: AuthContext = BRIDGE) -> FastAPI:
    from robothor.engine import admin_remote_tools

    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    admin_remote_tools.register(app)
    return app


async def _request(method: str, path: str, auth: AuthContext = BRIDGE, **kw: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(auth))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kw)


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tool": "acme_lookup",
        "args": {"query": SECRET},
        "access": "read",
        "dry_run": False,
        "request_id": None,
        "home_tenant": PARENT,
        "requested_by": ALICE,
        "agent_id": "main",
        "attestations": [],
    }
    body.update(over)
    return body


def _write(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tool": "acme_update",
        "args": {"id": 7, "note": SECRET},
        "access": "write",
        "request_id": "req-1",
        "attestations": ["baa"],
    }
    return _body(**{**base, **over})


async def _call(auth: AuthContext = BRIDGE, **body: Any) -> httpx.Response:
    return await _request("POST", "/api/admin/remote-tools/call", auth, json=body)


def _refusal(answer: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert answer.status_code == status, answer.text
    payload = answer.json()
    assert payload["error"] == code
    assert isinstance(payload["message"], str) and payload["message"]
    return payload


def _audits(world: Any) -> list[dict[str, Any]]:
    return [row for row in world.writer.rows if row.get("event_type") == rc.EVENT_REMOTE_TOOL_CALL]


# ── the caller ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "auth",
    [
        AuthContext(user_id="genus-other", tenant_id=CHILD, role="service", typ="service"),
        AuthContext(user_id=ALICE, tenant_id=CHILD, role="owner", typ="user"),
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/remote-tools"),
        ("GET", "/api/admin/remote-tools/acme_lookup"),
        ("POST", "/api/admin/remote-tools/call"),
    ],
)
async def test_only_the_bridge_may_call(world, auth, method, path) -> None:
    kw = {"json": _body()} if method == "POST" else {}
    answer = await _request(method, path, auth, **kw)

    _refusal(answer, 403, rc.ERR_CALLER_FORBIDDEN)
    assert world.handlers.calls == []


# ── a write ──────────────────────────────────────────────────────────────────


async def test_a_write_runs(world) -> None:
    answer = await _call(**_write())

    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["outcome"] == rc.OUTCOME_OK
    assert payload["result"]["updated"] is True
    assert payload["request_id"] == "req-1" and payload["tool"] == "acme_update"
    assert payload["would"] is None
    assert [c[0] for c in world.handlers.calls] == ["acme_update"]


async def test_a_write_asked_as_a_dry_run_runs_nothing_and_is_audited(world) -> None:
    answer = await _call(**_write(dry_run=True))

    assert answer.json()["outcome"] == rc.OUTCOME_DRY_RUN
    assert world.handlers.calls == []
    [row] = _audits(world)
    assert row["details"]["outcome"] == rc.OUTCOME_DRY_RUN
    assert row["details"]["dry_run"] is True


# ── the declaration ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["acme_plain", "no_such_tool", "exec"])
async def test_an_undeclared_tool_is_unknown(world, tool) -> None:
    _refusal(await _call(**_body(tool=tool)), 404, rc.ERR_TOOL_UNKNOWN)
    _refusal(await _request("GET", f"/api/admin/remote-tools/{tool}"), 404, rc.ERR_TOOL_UNKNOWN)
    assert world.handlers.calls == []


async def test_an_access_that_differs_from_the_declaration_is_refused(world) -> None:
    _refusal(await _call(**_body(access="write", request_id="r")), 409, rc.ERR_ACCESS_MISMATCH)
    _refusal(
        await _call(**_body(tool="acme_update", access="read", attestations=["baa"])),
        409,
        rc.ERR_ACCESS_MISMATCH,
    )
    assert world.handlers.calls == []


# ── who is asking ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("person", [ALICE, BOB])
async def test_a_parent_owner_or_admin_may_read(world, person) -> None:
    answer = await _call(**_body(requested_by=person))

    assert answer.status_code == 200, answer.text
    [(_tool, _args, ctx)] = world.handlers.calls
    assert ctx.user_id == person
    assert ctx.user_role == {ALICE: "owner", BOB: "admin"}[person]
    assert ctx.tenant_id == CHILD
    assert tuple(ctx.accessible_tenant_ids) == (CHILD,)
    assert ctx.agent_id == f"remote:{PARENT}:main"


@pytest.mark.parametrize(
    ("person", "home"),
    [
        (CAROL, PARENT),  # a member of the parent
        (ALICE, OTHER),  # claims a home that is not hers
        ("not-a-uuid", PARENT),
        ("4d4d4d4d-1111-4222-8333-0000000000ff", PARENT),  # nobody
    ],
)
async def test_anyone_else_is_not_permitted(world, person, home) -> None:
    _refusal(await _call(**_body(requested_by=person, home_tenant=home)), 403, rc.ERR_NOT_PERMITTED)
    assert world.handlers.calls == []


async def test_this_tenant_naming_itself_as_home_is_not_permitted(world) -> None:
    world.state["grant"][CHILD] = True
    _refusal(await _call(**_body(requested_by=None, home_tenant=CHILD)), 403, rc.ERR_NOT_PERMITTED)


async def test_only_an_owner_may_write(world) -> None:
    _refusal(await _call(**_write(requested_by=BOB)), 403, rc.ERR_WRITE_FORBIDDEN)
    assert world.handlers.calls == []


async def test_a_person_less_write_is_refused(world) -> None:
    _refusal(await _call(**_write(requested_by=None)), 403, rc.ERR_WRITE_FORBIDDEN)
    assert world.handlers.calls == []


async def test_a_person_less_read_needs_the_parents_grant(world) -> None:
    world.state["grant"][PARENT] = False
    world.rules[rc.ROLE_PARENT_AGENT] = [("acme_lookup", "allow", CHILD)]

    _refusal(await _call(**_body(requested_by=None)), 403, rc.ERR_NOT_PERMITTED)
    assert world.handlers.calls == []


async def test_a_person_less_read_is_denied_by_rbac_without_a_grant_row(world) -> None:
    answer = await _call(**_body(requested_by=None))

    _refusal(answer, 403, rc.ERR_NOT_PERMITTED)
    assert world.handlers.calls == []


async def test_a_person_less_read_runs_as_parent_agent_with_a_grant_row(world) -> None:
    world.rules[rc.ROLE_PARENT_AGENT] = [("acme_lookup", "allow", CHILD)]

    answer = await _call(**_body(requested_by=None))

    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == rc.OUTCOME_OK
    [(_tool, _args, ctx)] = world.handlers.calls
    assert ctx.user_role == rc.ROLE_PARENT_AGENT
    assert ctx.user_id == ""


async def test_a_grant_row_for_one_tool_does_not_open_another(world) -> None:
    world.rules[rc.ROLE_PARENT_AGENT] = [("acme_lookup", "allow", CHILD)]

    _refusal(
        await _call(**_body(tool="acme_fails", args={}, requested_by=None)),
        403,
        rc.ERR_NOT_PERMITTED,
    )


# ── attestations and arguments ───────────────────────────────────────────────


async def test_a_missing_attestation_is_named(world) -> None:
    payload = _refusal(
        await _call(**_write(attestations=["other"])), 403, rc.ERR_ATTESTATION_MISSING
    )

    assert payload["attestation"] == "baa"
    assert world.handlers.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {},  # the required argument is missing
        {"query": 5},  # wrong type
        {"query": "x", "tenant": PARENT},  # not a parameter of the tool
        "not-an-object",
    ],
)
async def test_bad_arguments_are_refused(world, args) -> None:
    _refusal(await _call(**_body(args=args)), 422, rc.ERR_BAD_ARGUMENTS)
    assert world.handlers.calls == []


async def test_a_write_needs_a_request_id(world) -> None:
    _refusal(await _call(**_write(request_id=None)), 422, rc.ERR_BAD_ARGUMENTS)
    assert world.handlers.calls == []


# ── write dedupe ─────────────────────────────────────────────────────────────


async def test_a_finished_write_is_replayed_not_run_again(world) -> None:
    first = await _call(**_write())
    second = await _call(**_write())

    assert first.json()["outcome"] == rc.OUTCOME_OK
    assert second.status_code == 200
    assert second.json()["outcome"] == rc.OUTCOME_REPLAYED
    assert second.json()["result"] == first.json()["result"]
    assert [c[0] for c in world.handlers.calls] == ["acme_update"]


async def test_a_write_in_flight_is_never_run_twice(world) -> None:
    world.ledger.rows[(CHILD, "req-1")] = {"state": "running", "result": None}

    _refusal(await _call(**_write()), 409, rc.ERR_OUTCOME_UNKNOWN)
    assert world.handlers.calls == []


async def test_an_unreadable_ledger_is_an_unknown_outcome(world, monkeypatch) -> None:
    from robothor.engine import remote_tools

    def broken(*a: Any) -> Any:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(remote_tools, "claim_request", broken)

    _refusal(await _call(**_write()), 409, rc.ERR_OUTCOME_UNKNOWN)
    assert world.handlers.calls == []


async def test_a_same_request_id_in_another_tenant_is_not_a_replay(world) -> None:
    world.ledger.rows[(OTHER, "req-1")] = {"state": "done", "result": {"status": 200, "body": {}}}

    assert (await _call(**_write())).json()["outcome"] == rc.OUTCOME_OK


async def test_a_replay_with_other_arguments_is_refused_not_answered(world) -> None:
    first = await _call(**_write())
    other = await _call(**_write(args={"id": 8, "note": "different"}))

    assert first.json()["outcome"] == rc.OUTCOME_OK
    payload = _refusal(other, 409, rc.ERR_REQUEST_MISMATCH)
    assert RESULT_SECRET not in repr(payload)
    assert [c[0] for c in world.handlers.calls] == ["acme_update"]


async def test_a_replay_for_another_tool_is_refused_not_answered(world) -> None:
    await _call(**_write())
    other = await _call(**_write(tool="acme_export", args={}, attestations=[]))

    payload = _refusal(other, 409, rc.ERR_REQUEST_MISMATCH)
    assert RESULT_SECRET not in repr(payload)
    assert [c[0] for c in world.handlers.calls] == ["acme_update"]


async def test_the_ledger_row_records_the_tool_and_the_argument_digest(world) -> None:
    await _call(**_write())

    row = world.ledger.rows[(CHILD, "req-1")]
    assert row["tool"] == "acme_update"
    assert row["args_digest"] == rc.args_digest({"id": 7, "note": SECRET})


async def test_a_read_ignores_its_request_id(world) -> None:
    world.ledger.rows[(CHILD, "req-1")] = {
        "state": "done",
        "result": {"status": 200, "body": {"outcome": "ok", "result": {"other": "call"}}},
        "tool": "acme_lookup",
        "args_digest": rc.args_digest({"query": SECRET}),
    }

    answer = await _call(**_body(request_id="req-1"))

    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == rc.OUTCOME_OK
    assert answer.json()["request_id"] is None
    assert answer.json()["result"] == {"record": {"id": 7, "note": RESULT_SECRET}}
    assert world.ledger.claims == []
    [row] = _audits(world)
    assert row["details"]["request_id"] is None


async def test_an_rbac_denied_read_never_releases_a_write_claim(world) -> None:
    world.ledger.rows[(CHILD, "req-9")] = {"state": "running", "result": None}

    answer = await _call(**_body(requested_by=None, request_id="req-9"))

    _refusal(answer, 403, rc.ERR_NOT_PERMITTED)
    assert world.ledger.rows[(CHILD, "req-9")]["state"] == "running"


async def test_an_rbac_denied_write_is_released_so_a_retry_can_run(world) -> None:
    world.rules["owner"] = [("acme_update", "deny", CHILD), ("*", "allow", "__default__")]

    _refusal(await _call(**_write()), 403, rc.ERR_NOT_PERMITTED)
    assert world.ledger.rows == {}


# ── results ──────────────────────────────────────────────────────────────────


async def test_a_tool_error_is_an_error_outcome(world) -> None:
    answer = await _call(**_body(tool="acme_fails", args={}))

    assert answer.status_code == 200
    assert answer.json()["outcome"] == rc.OUTCOME_ERROR
    assert answer.json()["result"]["error"] == "the upstream said no"


async def test_a_result_over_the_cap_is_refused_not_truncated(world) -> None:
    answer = await _call(**_body(tool="acme_big", args={}))

    _refusal(answer, 413, rc.ERR_TOO_LARGE)
    assert "blob" not in answer.text


async def test_a_write_over_the_cap_ran_and_says_so_with_its_result_withheld(world) -> None:
    answer = await _call(**_write(tool="acme_export", args={}, attestations=[]))

    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["outcome"] == rc.OUTCOME_OK
    assert payload["result"] is None
    withheld = payload["result_withheld"]
    assert withheld["reason"] == "too_large"
    assert withheld["max_bytes"] == rc.REMOTE_RESULT_MAX_BYTES
    assert withheld["bytes"] > rc.REMOTE_RESULT_MAX_BYTES
    assert "blob" not in answer.text
    assert [c[0] for c in world.handlers.calls] == ["acme_export"]


async def test_a_write_over_the_cap_replays_the_withheld_answer_and_never_reruns(world) -> None:
    first = await _call(**_write(tool="acme_export", args={}, attestations=[]))
    second = await _call(**_write(tool="acme_export", args={}, attestations=[]))

    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == rc.OUTCOME_REPLAYED
    assert second.json()["result"] is None
    assert second.json()["result_withheld"] == first.json()["result_withheld"]
    assert [c[0] for c in world.handlers.calls] == ["acme_export"]


async def test_a_call_that_runs_out_of_time_says_so(world, monkeypatch) -> None:
    from robothor.engine import remote_tools

    monkeypatch.setattr(remote_tools, "TIMEOUT_S", 0.05)

    _refusal(await _call(**_body(tool="acme_slow", args={})), 504, rc.ERR_TIMEOUT)


# ── the audit row ────────────────────────────────────────────────────────────


async def test_every_call_leaves_one_row_with_a_digest_and_nothing_else(world) -> None:
    await _call(**_write())

    [row] = _audits(world)
    assert row["category"] == "agent"
    assert row["action"] == "acme_update"
    assert row["actor"] == f"remote:{PARENT}:main"
    assert row["tenant_id"] == CHILD
    assert set(row["details"]) == {
        "home_tenant",
        "user_id",
        "agent_id",
        "tool",
        "access",
        "outcome",
        "dry_run",
        "request_id",
        "args_digest",
    }
    assert row["details"] == {
        "home_tenant": PARENT,
        "user_id": ALICE,
        "agent_id": "main",
        "tool": "acme_update",
        "access": "write",
        "outcome": rc.OUTCOME_OK,
        "dry_run": False,
        "request_id": "req-1",
        "args_digest": rc.args_digest({"id": 7, "note": SECRET}),
    }
    assert SECRET not in repr(row) and RESULT_SECRET not in repr(row)


async def test_a_refusal_is_audited_too(world) -> None:
    await _call(**_body(requested_by=CAROL))

    [row] = _audits(world)
    assert row["details"]["outcome"] == rc.ERR_NOT_PERMITTED
    assert row["tenant_id"] == CHILD
    assert SECRET not in repr(row)


# ── the catalogue ────────────────────────────────────────────────────────────


async def test_the_catalogue_lists_the_declared_tools_plugins_and_checks(world) -> None:
    answer = await _request("GET", "/api/admin/remote-tools")

    assert answer.status_code == 200, answer.text
    payload = answer.json()
    assert payload["tenant_id"] == CHILD
    assert "mode" not in payload
    assert payload["as_of"].endswith("Z")
    assert {"name": "acme-tools", "version": "1.2.3", "enabled": True} in payload["plugins"]
    tools = {tool["name"]: tool for tool in payload["tools"]}
    assert set(tools) == {
        "acme_lookup",
        "acme_update",
        "acme_big",
        "acme_export",
        "acme_risky",
        "acme_slow",
        "acme_fails",
    }
    assert tools["acme_lookup"] == {
        "name": "acme_lookup",
        "plugin": "acme-tools",
        "access": "read",
        "attestations": [],
        "description": LONG_DESCRIPTION[: rc.DESCRIPTION_MAX_CHARS],
    }
    assert tools["acme_update"]["attestations"] == ["baa"]
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["acme_tools.connection"]["status"] == "ok"
    assert checks["acme_tools.credentials"]["status"] == "warn"
    detail = checks["acme_tools.credentials"]["detail"]
    assert "sk-ant" not in detail and "/srv/acme" not in detail


async def test_the_checks_are_cached(world, monkeypatch) -> None:
    from robothor.engine import remote_tools

    runs: list[int] = []
    real = remote_tools._run_checks

    async def counted() -> list[dict[str, Any]]:
        runs.append(1)
        return await real()

    monkeypatch.setattr(remote_tools, "_run_checks", counted)
    await _request("GET", "/api/admin/remote-tools")
    await _request("GET", "/api/admin/remote-tools")

    assert len(runs) == 1


async def test_describe_returns_the_whole_declaration_and_schema(world) -> None:
    answer = await _request("GET", "/api/admin/remote-tools/acme_lookup")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "name": "acme_lookup",
        "plugin": "acme-tools",
        "access": "read",
        "attestations": [],
        "notes": "Use it to find one record.",
        "description": LONG_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }


# ── the parent's grant ───────────────────────────────────────────────────────


def test_parent_agent_grant_needs_a_descendant_and_the_flag(world) -> None:
    from robothor.auth.acting import parent_agent_grant

    assert parent_agent_grant(PARENT, CHILD) is True
    assert parent_agent_grant(OTHER, CHILD) is False  # not a descendant
    assert parent_agent_grant(CHILD, CHILD) is False  # itself
    assert parent_agent_grant("", CHILD) is False
    world.state["grant"][PARENT] = False
    assert parent_agent_grant(PARENT, CHILD) is False


def test_parent_agent_grant_refuses_when_it_cannot_read(world, monkeypatch) -> None:
    from robothor.auth.acting import parent_agent_grant

    def broken(tenant_id: str) -> bool:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr("robothor.crm.dal.tenant_child_data_access", broken)

    assert parent_agent_grant(PARENT, CHILD) is False


async def test_the_plugin_checks_run_inside_the_checks_budget(world, monkeypatch) -> None:
    from robothor.doctor import runner

    budgets: list[float] = []
    real = runner.run

    async def run(ctx: Any, **kw: Any) -> Any:
        budgets.append(ctx.total_timeout_s)
        return await real(ctx, **kw)

    monkeypatch.setattr(runner, "run", run)

    assert (await _request("GET", "/api/admin/remote-tools")).status_code == 200
    assert budgets == [rc.CHECKS_BUDGET_S]


# ── a write whose outcome is unknown, and a write a control refused ─────────


def _risky(**over: Any) -> dict[str, Any]:
    return _write(tool="acme_risky", args={}, attestations=[], **over)


@pytest.mark.parametrize(
    "outcome",
    [
        # The tool says so itself, as the effect ledger reads it.
        {"error": "the connection dropped after the request was sent", "outcome_unknown": True},
        # A transport failure the engine marks (response_failure) for a write.
        {"error": "backing service unreachable: ReadTimeout", "outcome_unknown": True},
        # A crash the dispatcher caught.
        {"error": "RuntimeError: boom", "tool_crashed": True},
        # The tool raised: the dispatcher reports a crash.
        ConnectionError("connection reset by peer"),
    ],
)
async def test_a_write_whose_outcome_is_unknown_says_so_and_is_never_replayed(
    world, outcome
) -> None:
    world.handlers.risky_result = outcome

    first = await _call(**_risky())

    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["outcome"] == rc.OUTCOME_ERROR
    assert payload["outcome_unknown"] is True
    assert payload["result"]["error"]
    assert world.ledger.rows[(CHILD, "req-1")]["state"] == "running"
    [row] = _audits(world)
    assert row["details"]["outcome"] == rc.ERR_OUTCOME_UNKNOWN

    world.handlers.risky_result = {"done": True}
    again = await _call(**_risky())

    _refusal(again, 409, rc.ERR_OUTCOME_UNKNOWN)
    assert [c[0] for c in world.handlers.calls] == ["acme_risky"]


async def test_a_write_the_tool_refused_by_its_own_control_is_released(world) -> None:
    from robothor.engine.refusal import refusal

    world.handlers.risky_result = refusal(
        "writes are stopped", gate="flag", name="ACME_KILL", unlock="clear the kill file"
    )

    first = await _call(**_risky())

    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == rc.OUTCOME_ERROR
    assert "outcome_unknown" not in first.json()
    assert (CHILD, "req-1") not in world.ledger.rows

    world.handlers.risky_result = {"done": True}
    again = await _call(**_risky())

    assert again.json()["outcome"] == rc.OUTCOME_OK
    assert [c[0] for c in world.handlers.calls] == ["acme_risky", "acme_risky"]


async def test_a_successful_write_is_still_stored_and_replayed(world) -> None:
    await _call(**_risky())
    again = await _call(**_risky())

    assert again.json()["outcome"] == rc.OUTCOME_REPLAYED
    assert [c[0] for c in world.handlers.calls] == ["acme_risky"]


# ── the catalogue's detail scrubber ──────────────────────────────────────────


def test_check_details_keep_only_a_urls_scheme_host_and_port() -> None:
    from robothor.engine.remote_tools import _clean

    text = _clean(
        "token from https://svc-user:pa55word@api.example:8443/v1/token?key=abc123#frag "
        "kept at /srv/acme/conf.yaml"
    )

    assert "https://api.example:8443" in text
    assert "pa55word" not in text and "svc-user" not in text
    assert "abc123" not in text and "key=" not in text and "frag" not in text
    assert "/v1/token" not in text
    assert "/srv/acme" not in text and "<path>" in text
    assert "http:/<path>" not in text


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        # A chat webhook: the last path segment is the credential.
        (
            "https://hooks.chat.example/services/T0000/B0000/webhooksecretvalue",
            "webhooksecretvalue",
        ),
        # An id and a token in the path.
        ("https://chat.example/api/webhooks/1234567/webhooktokenvalue", "webhooktokenvalue"),
        # A bot API: the token rides in the first segment.
        ("https://api.bot.example/bot1234567:bottokenvalue/getMe", "bottokenvalue"),
    ],
)
def test_check_details_never_keep_a_credential_carried_in_a_urls_path(url, secret) -> None:
    from robothor.engine.remote_tools import _clean

    text = _clean(f"could not post to {url}: 404")

    assert secret not in text
    assert "1234567" not in text
    assert text.startswith("could not post to https://") and text.endswith(": 404")


def test_check_details_scrub_a_windows_path() -> None:
    from robothor.engine.remote_tools import _clean

    assert "acme" not in _clean(r"config at C:\Users\acme\conf.yaml")


# ── a result that is not plain JSON ──────────────────────────────────────────


def _awkward() -> dict[str, Any]:
    import datetime
    import decimal
    import uuid

    return {
        "at": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "amount": decimal.Decimal("12.50"),
        "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "ratio": float("nan"),
        "limit": float("inf"),
    }


async def test_a_write_with_a_non_json_result_answers_once_and_replays(world) -> None:
    world.handlers.risky_result = _awkward()

    first = await _call(**_risky())
    again = await _call(**_risky())

    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["outcome"] == rc.OUTCOME_OK
    assert payload["result"]["amount"] == "12.50"
    assert payload["result"]["at"].startswith("2026-01-02")
    assert payload["result"]["id"] == "12345678-1234-5678-1234-567812345678"
    assert isinstance(payload["result"]["ratio"], str)
    assert isinstance(payload["result"]["limit"], str)
    assert again.status_code == 200, again.text
    assert again.json()["outcome"] == rc.OUTCOME_REPLAYED
    assert again.json()["result"] == payload["result"]
    assert [c[0] for c in world.handlers.calls] == ["acme_risky"]
    assert world.ledger.rows[(CHILD, "req-1")]["state"] == "done"


async def test_a_read_with_a_non_json_result_answers(world) -> None:
    world.handlers.lookup_result = {"record": _awkward()}

    answer = await _call(**_body())

    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == rc.OUTCOME_OK
    assert answer.json()["result"]["record"]["amount"] == "12.50"


async def test_a_plain_error_from_a_write_is_a_completed_call_stored_and_replayed(world) -> None:
    world.handlers.risky_result = {"error": "slot already taken"}

    first = await _call(**_risky())
    again = await _call(**_risky())

    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == rc.OUTCOME_ERROR
    assert "outcome_unknown" not in first.json()
    assert world.ledger.rows[(CHILD, "req-1")]["state"] == "done"
    assert again.json()["outcome"] == rc.OUTCOME_REPLAYED
    assert again.json()["result"] == {"error": "slot already taken"}
    assert [c[0] for c in world.handlers.calls] == ["acme_risky"]
    [row, _replay] = _audits(world)
    assert row["details"]["outcome"] == rc.OUTCOME_ERROR


async def test_a_write_that_runs_out_of_time_is_never_run_again(world, monkeypatch) -> None:
    from robothor.engine import remote_tools

    monkeypatch.setattr(remote_tools, "TIMEOUT_S", 0.05)
    world.handlers.risky_result = "hang"

    _refusal(await _call(**_risky()), 504, rc.ERR_TIMEOUT)
    world.handlers.risky_result = {"done": True}
    _refusal(await _call(**_risky()), 409, rc.ERR_OUTCOME_UNKNOWN)
    assert [c[0] for c in world.handlers.calls] == ["acme_risky"]


async def test_an_internal_failure_is_audited_and_keeps_a_writes_claim(world, monkeypatch) -> None:
    from robothor.engine import remote_tools

    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(remote_tools, "_dispatch", broken)

    answer = await _call(**_risky())

    assert answer.status_code == 500
    assert answer.json()["error"] == "internal_error"
    [row] = _audits(world)
    assert row["details"]["outcome"] == "internal_error"
    assert world.ledger.rows[(CHILD, "req-1")]["state"] == "running"


# ── dry_run where it does not belong ─────────────────────────────────────────


async def test_dry_run_inside_a_writes_arguments_is_refused_not_run(world) -> None:
    answer = await _call(**_write(tool="acme_risky", args={"dry_run": True}, attestations=[]))

    payload = _refusal(answer, 422, rc.ERR_BAD_ARGUMENTS)
    assert "top level" in payload["message"]
    assert world.handlers.calls == []
    assert world.ledger.rows == {}


async def test_a_read_asked_as_a_dry_run_is_refused(world) -> None:
    _refusal(await _call(**_body(dry_run=True)), 422, rc.ERR_BAD_ARGUMENTS)
    assert world.handlers.calls == []


# ── a declared name that an adapter has taken ───────────────────────────────


async def test_a_declared_tool_routed_to_an_adapter_is_unknown(world, monkeypatch) -> None:
    from robothor.engine.tools.registry import get_registry

    monkeypatch.setitem(get_registry()._adapter_routes, "acme_lookup", "some-mcp-server")

    _refusal(await _call(**_body()), 404, rc.ERR_TOOL_UNKNOWN)
    _refusal(await _request("GET", "/api/admin/remote-tools/acme_lookup"), 404, rc.ERR_TOOL_UNKNOWN)
    listed = (await _request("GET", "/api/admin/remote-tools")).json()
    assert "acme_lookup" not in {tool["name"] for tool in listed["tools"]}
    assert world.handlers.calls == []

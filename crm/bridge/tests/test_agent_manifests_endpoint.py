"""Building an agent from the browser.

Before this router the only way to add an agent was an ssh session, an editor
and ``systemctl restart``. The routes here are the operator-gated, audited
front door to the same files, and what these tests hold down is everything that
makes a form-driven manifest write safe:

* the operator gate on every route, GETs included — this surface enumerates the
  fleet and rewrites what it runs;
* a hand-written key survives an edit, because a form that knows twenty paths
  must not be able to erase the other two hundred;
* no response, error or audit detail carries a filesystem path (rules 1 and 2);
* a validation failure leaves the manifest directory byte-identical, temp files
  included — a torn manifest is an agent that will not load at all;
* every write is followed by an engine reconcile, and a reconcile that fails is
  REPORTED rather than rolled back or swallowed.

Every test builds its fleet in ``tmp_path``. ``docs/agents/`` in this repo is
gitignored instance data (rule 11) and is empty on a fresh checkout, so a test
that read the real one would be both a leak and a flake.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

RECONCILED = {
    "added": ["demo-agent"],
    "replaced": [],
    "pruned": [],
    "blocked": {},
    "clean": True,
}

EXISTING = {
    "id": "demo-agent",
    "name": "Demo Agent",
    "description": "Does the demo work",
    "version": "2026-09-01",
    "department": "operations",
    "model": {"primary": "openrouter/example/demo-model", "temperature": 0.1},
    "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    "delivery": {"mode": "none"},
    "tools_allowed": ["exec", "read_file", "write_file"],
    "instruction_file": "brain/DEMO_AGENT.md",
    "v2": {"custom_plugin_key": 7},
}

ENGINE_TOOLS = {
    "tools": ["exec", "read_file", "write_file", "search_memory", "list_my_tasks"],
    "count": 5,
}


class FakeEngine:
    """Stands in for the engine's ``/api/admin`` surface, recording every call.

    Recorded rather than merely stubbed: the point of this router is that a
    manifest write is only half the act, so "did it reconcile" is an assertion
    and not an implementation detail.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reconcile_status = 200
        self.reconcile_body: dict = dict(RECONCILED)
        self.trigger_body: dict = {"status": "triggered", "agent_id": "demo-agent"}

    async def __call__(self, method, path, *, json=None, timeout=30):
        self.calls.append((method, path))
        if path == "/api/admin/tools":
            return 200, ENGINE_TOOLS
        if path == "/api/admin/scheduler/reconcile":
            return self.reconcile_status, self.reconcile_body
        if path.endswith("/trigger"):
            return 200, self.trigger_body
        raise AssertionError(f"unexpected engine call: {method} {path}")

    @property
    def reconciles(self) -> int:
        return sum(1 for _, path in self.calls if path == "/api/admin/scheduler/reconcile")


@pytest.fixture
def fake_engine():
    engine = FakeEngine()
    with patch("routers.agent_manifests.engine_request", new=engine):
        yield engine


#: The canonical schema, which `manifest_checks.validate_agent` loads from
#: ``<repo_root>/docs/agents/schema.yaml`` — and ``repo_root`` here is the
#: WORKSPACE. Without it, check A falls back to `required_fields={"id","name"}`
#: with an empty department enum, so the strictest check in the set ran against
#: almost nothing and the tests below could not have told the difference.
CANONICAL_SCHEMA = (
    Path(__file__).resolve().parents[3] / "robothor" / "engine" / "schema" / "agent_manifest.yaml"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace. Never the operator's real one — rule 11."""
    manifests = tmp_path / "docs" / "agents"
    manifests.mkdir(parents=True)
    (tmp_path / "brain").mkdir()
    # Shaped like a real instance: `genus init` puts the schema here, and an
    # appliance without it silently degrades check A — worth reproducing rather
    # than papering over, so a test that depends on the enum actually gets it.
    (manifests / "schema.yaml").write_bytes(CANONICAL_SCHEMA.read_bytes())
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
    return tmp_path


@pytest.fixture
def manifest_dir(workspace):
    return workspace / "docs" / "agents"


@pytest.fixture
def seeded(manifest_dir):
    """One healthy agent on disk, with a hand-written key the form never sees."""
    (manifest_dir / "demo-agent.yaml").write_text(yaml.safe_dump(EXISTING, sort_keys=False))
    (Path(manifest_dir).parent.parent / "brain" / "DEMO_AGENT.md").write_text(
        "# Demo Agent\n\n## Your Role\n\nDemo.\n\n## Tasks\n\n1. Work.\n\n## Output\n\nA line.\n"
    )
    return manifest_dir / "demo-agent.yaml"


@pytest.fixture
def client(controls_client_as_operator):
    return controls_client_as_operator


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _create_body(**overrides) -> dict:
    body = {
        "name": "Invoice Watcher",
        "description": "Watches invoices",
        "instructions": "Read the inbox and file every invoice.",
        "department": "operations",
        "model": "openrouter/example/demo-model",
        "cron": "0 8 * * *",
        "timezone": "UTC",
        "tools_allowed": ["exec", "read_file", "write_file"],
    }
    body.update(overrides)
    return body


# ─── Containment ─────────────────────────────────────────────────────


class TestNoTestReachesARealWorkspace:
    """The forgetful case, which is the only one that matters.

    These routes resolve their write paths from
    ``EngineConfig.from_env().workspace``, which falls back to ``~/robothor``
    when ROBOTHOR_WORKSPACE is unset — a developer's live fleet. On 2026-09-12 a
    test that resolved a workspace from the environment overwrote ten of the
    operator's agent manifests with their template versions, which is precisely
    the pair of files ``POST /api/agent-manifests`` writes.

    The per-test ``workspace`` fixture protects the tests that remember it.
    ``tests/conftest_workspace_containment.contained_workspace`` is autouse and
    protects the ones that do not, and checks a sentinel manifest byte-for-byte
    after every test in this package so the redirect is proven rather than
    assumed.

    Every assertion here runs BEFORE the request, deliberately: a test that
    probed the escape first and checked afterwards would have to perform the
    write to find out, and on an unguarded checkout that write is the incident.
    """

    def test_the_containment_fixture_is_active_in_this_package(self):
        from tests.conftest_workspace_containment import assert_contained

        assert_contained()

    def test_a_route_that_forgets_the_workspace_fixture_stays_inside_it(self, client, fake_engine):
        """No ``workspace`` fixture on this test, on purpose."""
        from tests.conftest_workspace_containment import assert_contained

        contained = assert_contained()

        response = client.post("/api/agent-manifests", json=_create_body(name="Probe Agent"))

        assert response.status_code == 201, response.text
        assert (contained / "docs" / "agents" / "probe-agent.yaml").is_file()
        assert (contained / "brain" / "PROBE_AGENT.md").is_file()

    def test_no_router_can_reach_a_real_engine_however_it_imports_the_client(
        self, _controls_auth_key
    ):
        """The engine guard is bound to the SINK, not to three attribute names.

        Its first version patched ``routers.agent_manifests.engine_request`` and
        two ``reconcile_engine_schedules``. Every router imports those by value,
        so ``routers.providers`` and ``setup.py``'s two function-local imports
        were never covered, and a router added tomorrow would be unguarded by
        construction — the exact failure the fixture claims to replace.

        This calls the shared client directly, the way a router nobody has
        written yet would, and asserts it cannot reach anything.
        """
        import asyncio

        from routers._engine_client import engine_request

        status, body = asyncio.run(engine_request("GET", "/api/admin/models"))

        assert status == 502
        assert body == {"error": "engine unavailable"}

    @pytest.mark.parametrize("entry", ["async_request", "async_send", "sync_request", "sync_send"])
    def test_every_httpx_entry_point_is_covered_not_just_async_request(self, entry):
        """The guard patched `AsyncClient.request` only, while its assertion
        said "a unit suite must not dial anything".

        `AsyncClient.send`, and the SYNC `httpx.Client`, both went straight to
        the network. Not hypothetical: `templates/hub_client.py` uses the sync
        client, and `POST /api/installed-agents/install` — a route this PR made
        async — reaches it.
        """
        import asyncio

        import httpx
        import pytest as _pytest

        url = "http://example.com/probe"

        async def _async_request() -> None:
            async with httpx.AsyncClient() as client:
                await client.get(url)

        async def _async_send() -> None:
            async with httpx.AsyncClient() as client:
                await client.send(client.build_request("GET", url))

        def _sync_request() -> None:
            with httpx.Client() as client:
                client.get(url)

        def _sync_send() -> None:
            with httpx.Client() as client:
                client.send(client.build_request("GET", url))

        call = {
            "async_request": lambda: asyncio.run(_async_request()),
            "async_send": lambda: asyncio.run(_async_send()),
            "sync_request": _sync_request,
            "sync_send": _sync_send,
        }[entry]

        with _pytest.raises(AssertionError, match="real httpx transport"):
            call()

    def test_a_real_transport_to_loopback_is_refused_not_quietly_allowed(self):
        """``127.0.0.1`` is not a safe default here — on a developer's box it is
        their own engine on :18800. The guard asks what the TRANSPORT is, not
        what the host looks like."""
        import asyncio

        import httpx
        import pytest as _pytest

        async def _dial() -> None:
            async with httpx.AsyncClient() as client:
                await client.get("http://127.0.0.1:18800/api/admin/models")

        with _pytest.raises(AssertionError, match="real httpx transport"):
            asyncio.run(_dial())

    def test_home_is_redirected_too_so_the_fallback_cannot_bite(self):
        """``default_workspace_root`` falls back to ``Path.home()/robothor``
        when the variable is absent, so pinning only ROBOTHOR_WORKSPACE would
        leave a helper that clears it pointed straight back at the real one."""
        from pathlib import Path as _Path

        from tests.conftest_workspace_containment import assert_contained

        contained = assert_contained()
        assert _Path.home() == contained


# ─── The gate ────────────────────────────────────────────────────────


class TestEveryRouteIsOperatorGated:
    def test_every_route_calls_require_operator_including_the_gets(self):
        """A GET here enumerates the fleet, its models and its delivery
        targets. That is appliance state, not member data."""
        import inspect

        from routers import agent_manifests

        ungated = [
            f"{sorted(route.methods)} {route.path}"
            for route in agent_manifests.router.routes
            if "require_operator(" not in inspect.getsource(route.endpoint)
        ]
        assert not ungated, f"routes without the operator gate: {ungated}"

    def test_a_viewer_session_cannot_list(self, controls_client_as_viewer, workspace):
        assert controls_client_as_viewer.get("/api/agent-manifests").status_code == 403

    def test_a_viewer_session_cannot_create(self, controls_client_as_viewer, workspace):
        response = controls_client_as_viewer.post("/api/agent-manifests", json=_create_body())
        assert response.status_code == 403

    def test_a_service_token_cannot_create(self, controls_client_as_service, workspace):
        response = controls_client_as_service.post("/api/agent-manifests", json=_create_body())
        assert response.status_code == 403


# ─── Reading ─────────────────────────────────────────────────────────


class TestListing:
    def test_it_lists_the_fleet_and_names_what_will_not_load(
        self, client, manifest_dir, seeded, fake_engine
    ):
        (manifest_dir / "broken.yaml").write_text("id: broken\nname: [unclosed\n")

        body = client.get("/api/agent-manifests").json()

        assert [row["id"] for row in body["agents"]] == ["demo-agent"]
        assert body["agents"][0]["cron"] == "0 9 * * *"
        assert body["agents"][0]["enabled"] is True
        assert body["broken"] == [
            {"id": "broken", "filename": "broken.yaml", "error_type": "YAMLError"}
        ]

    def test_a_broken_manifest_is_not_a_404(self, client, manifest_dir, fake_engine):
        """ "Absent" and "unreadable" have different fixes. Telling the operator
        their agent does not exist while the file sits there with a typo in it
        is the confusion the scan machinery exists to remove."""
        (manifest_dir / "broken.yaml").write_text("id: broken\nname: [unclosed\n")

        response = client.get("/api/agent-manifests/broken")

        assert response.status_code == 200
        body = response.json()
        assert body["manifest"] is None
        assert body["validation"]["ok"] is False

    def test_an_absent_agent_is_a_404(self, client, workspace, fake_engine):
        assert client.get("/api/agent-manifests/no-such-agent").status_code == 404

    def test_it_returns_the_instructions_alongside_the_document(self, client, seeded, fake_engine):
        body = client.get("/api/agent-manifests/demo-agent").json()

        assert body["manifest"]["id"] == "demo-agent"
        assert "## Your Role" in body["instructions"]
        assert "id: demo-agent" in body["yaml"]


# ─── Which agents the chat may address ───────────────────────────────


class TestChattable:
    """Whether the Helm's chat switcher may offer an agent, and why.

    The Helm talks to another agent by sending ``session_key:
    "agent:<id>:primary"`` with a chat request; the engine's
    ``_effective_session_key`` does the rest for every role. But a session key
    is only a conversation if the agent HOLDS one between runs, which is what
    ``schedule.session_target: persistent`` means. An ``isolated`` worker gets a
    fresh session per run, so a message sent to one would be answered by a
    stranger who forgets it immediately — that is not a chat, and offering it in
    a switcher would be the appliance promising something it cannot do.

    The one agent that is chattable regardless is the configured default. Its
    manifest is free to say ``isolated`` (the shipped main agent's schedule
    block is about its heartbeat, not about the operator's conversation) while
    the engine still pins ``main_session_key`` for it. The id is read from
    ``EngineConfig``, never spelled "main" here: an instance that set
    ``ROBOTHOR_DEFAULT_CHAT_AGENT`` would otherwise lose the ability to chat
    with the only agent it chats with.
    """

    def _write(self, manifest_dir, agent_id: str, **schedule) -> None:
        document = dict(EXISTING)
        document["id"] = agent_id
        document["name"] = agent_id.replace("-", " ").title()
        document["schedule"] = {"cron": "0 9 * * *", "timezone": "UTC", **schedule}
        (manifest_dir / f"{agent_id}.yaml").write_text(yaml.safe_dump(document, sort_keys=False))

    def _rows(self, client) -> dict:
        body = client.get("/api/agent-manifests").json()
        return {row["id"]: row for row in body["agents"]}

    def test_a_persistent_agent_is_chattable_and_says_so(self, client, manifest_dir, fake_engine):
        self._write(manifest_dir, "helper", session_target="persistent")

        row = self._rows(client)["helper"]

        assert row["session_target"] == "persistent"
        assert row["chattable"] is True

    def test_an_isolated_worker_is_not_chattable(self, client, manifest_dir, fake_engine):
        self._write(manifest_dir, "worker", session_target="isolated")

        row = self._rows(client)["worker"]

        assert row["session_target"] == "isolated"
        assert row["chattable"] is False

    def test_a_manifest_with_no_session_target_reports_an_empty_one(
        self, client, seeded, fake_engine
    ):
        """``EXISTING``'s schedule block has no ``session_target``. The row
        reports what the manifest SAYS, empty included — inventing ``isolated``
        here would put the engine's default in a field an operator reads as the
        manifest's own words."""
        row = self._rows(client)["demo-agent"]

        assert row["session_target"] == ""
        assert row["chattable"] is False

    def test_the_configured_default_agent_is_chattable_however_it_schedules(
        self, client, manifest_dir, fake_engine, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_DEFAULT_CHAT_AGENT", "concierge")
        self._write(manifest_dir, "concierge", session_target="isolated")

        row = self._rows(client)["concierge"]

        assert row["chattable"] is True

    def test_the_default_agent_is_not_hardcoded_as_main(
        self, client, manifest_dir, fake_engine, monkeypatch
    ):
        """With the default moved off "main", the agent literally named ``main``
        loses the exemption — only the CONFIGURED default keeps it."""
        monkeypatch.setenv("ROBOTHOR_DEFAULT_CHAT_AGENT", "concierge")
        self._write(manifest_dir, "main", session_target="isolated")
        self._write(manifest_dir, "concierge", session_target="isolated")

        rows = self._rows(client)

        assert rows["main"]["chattable"] is False
        assert rows["concierge"]["chattable"] is True

    def test_the_listing_names_the_default_agent(
        self, client, manifest_dir, fake_engine, monkeypatch
    ):
        """The browser cannot know which id is the default, and it has to: the
        switcher defaults to that agent and sends NO session key for it, which
        is the whole reason the owner's shared main session survives this."""
        monkeypatch.setenv("ROBOTHOR_DEFAULT_CHAT_AGENT", "concierge")
        self._write(manifest_dir, "concierge", session_target="isolated")

        body = client.get("/api/agent-manifests").json()

        assert body["default_agent"] == "concierge"

    def test_the_default_agent_is_resolved_once_for_the_whole_listing(
        self, client, manifest_dir, fake_engine
    ):
        """Twenty agents must not mean twenty ``EngineConfig.from_env()`` calls
        — that builder reads the environment and touches the filesystem."""
        for index in range(5):
            self._write(manifest_dir, f"agent-{index}", session_target="persistent")

        from routers import agent_manifests

        with patch.object(agent_manifests, "_default_chat_agent", return_value="main") as resolved:
            client.get("/api/agent-manifests")

        assert resolved.call_count == 1


# ─── Validation ──────────────────────────────────────────────────────


class TestValidate:
    def test_validate_always_answers_200(self, client, workspace, fake_engine):
        """The verdict is the payload. A 422 would make "this manifest is
        invalid" and "your request was malformed" the same answer, which is
        unusable for a form that validates as the operator types."""
        response = client.post("/api/agent-manifests/validate", json={"manifest": {"id": "x"}})

        assert response.status_code == 200
        assert response.json()["ok"] is False

    def test_a_good_manifest_validates(self, client, workspace, fake_engine):
        body = client.post("/api/agent-manifests/validate", json={"manifest": EXISTING}).json()

        assert body["ok"] is True, body["errors"]

    def test_unparseable_yaml_is_reported_not_raised(self, client, workspace, fake_engine):
        body = client.post(
            "/api/agent-manifests/validate", json={"yaml": "id: x\nname: [unclosed"}
        ).json()

        assert body["ok"] is False
        assert body["errors"][0]["code"] == "unparseable"

    def test_an_unregistered_tool_is_refused_using_the_engines_own_list(
        self, client, workspace, fake_engine
    ):
        candidate = {**EXISTING, "tools_allowed": ["exec", "no_such_tool"]}

        body = client.post("/api/agent-manifests/validate", json={"manifest": candidate}).json()

        assert body["ok"] is False
        assert ("GET", "/api/admin/tools") in fake_engine.calls
        assert any("no_such_tool" in issue["message"] for issue in body["errors"])

    def test_an_unreachable_engine_warns_instead_of_blocking(self, client, workspace):
        """An operator must be able to edit an agent while the engine is down —
        that is precisely when they want to."""

        async def _dead(method, path, *, json=None, timeout=30):
            return 502, {"error": "engine unavailable"}

        with patch("routers.agent_manifests.engine_request", new=_dead):
            body = client.post("/api/agent-manifests/validate", json={"manifest": EXISTING}).json()

        assert body["ok"] is True
        assert any(issue["code"] == "tools_unverified" for issue in body["warnings"])

    def test_a_bad_cron_is_caught_by_the_scheduler_itself(self, client, workspace, fake_engine):
        candidate = {**EXISTING, "schedule": {"cron": "not a cron", "timezone": "UTC"}}

        body = client.post("/api/agent-manifests/validate", json={"manifest": candidate}).json()

        assert body["ok"] is False
        assert any(issue["code"] == "bad_cron" for issue in body["errors"])


# ─── Wrong-typed manifests ───────────────────────────────────────────


#: Manifests whose VALUES are the wrong shape, not merely wrong. Each one made
#: a validator raise before the guard: `x not in {...}` on an unhashable, a
#: `.get()` on a list, `re.match` on an int. They are the manifests an operator
#: most needs the editor for, so a 500 here is worse than a 404 — it hides the
#: file rather than reporting it.
WRONG_TYPED = {
    "sandbox_is_a_mapping": {"v2": {"sandbox": {"enabled": True}}},
    "sandbox_is_a_list": {"v2": {"sandbox": ["local"]}},
    "guardrail_is_a_dict": {"v2": {"guardrails": [{"name": "x"}]}},
    "difficulty_is_a_dict": {"v2": {"difficulty_class": {"tier": 1}}},
    "session_target_is_a_dict": {"schedule": {"session_target": {"mode": "isolated"}}},
    "schedule_is_a_list": {"schedule": ["0 9 * * *"]},
    "delivery_is_a_list": {"delivery": ["announce"]},
    "model_is_a_string": {"model": "openrouter/example/demo-model"},
    "id_is_an_int": {"id": 7},
    "name_is_a_list": {"name": ["Demo", "Agent"]},
    "tools_allowed_is_a_string": {"tools_allowed": "exec"},
    "changelog_is_a_string": {"changelog": "initial"},
    "v2_is_a_list": {"v2": ["sandbox"]},
}


def _wrong_typed(key: str) -> dict:
    return {**EXISTING, **WRONG_TYPED[key]}


class TestAWrongTypedManifestIsReportedNotRaised:
    """``manifest_schema.validate`` says "Never raises". It is not true of an
    arbitrary document, and this PR is the first caller to hand it an HTTP body.

    The guard is at the call sites rather than only in the validator because
    the promise is load-bearing here in a way it was not when every caller was
    the manifest loader: a crash on read means the operator cannot SEE the file
    that is broken, let alone repair it.
    """

    @pytest.mark.parametrize("case", sorted(WRONG_TYPED))
    def test_validate_is_still_always_200(self, client, workspace, fake_engine, case):
        response = client.post(
            "/api/agent-manifests/validate", json={"manifest": _wrong_typed(case)}
        )

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False

    @pytest.mark.parametrize("case", sorted(WRONG_TYPED))
    def test_the_fleet_list_survives_a_wrong_typed_manifest(
        self, client, manifest_dir, fake_engine, case
    ):
        """The landing page of the whole feature.

        ``_summary`` reads three blocks with ``document.get(k) or {}``, and a
        TRUTHY non-mapping sails through that and into ``.get()``. The list
        route maps it over every manifest in one generator, so one bad file
        took the entire response with it — and the route's own docstring
        promises the opposite: "including the ones that will not load".

        This column is the reason the round-1 fix stopped one function short:
        the class covered validate, GET, PATCH and disable, and never the list.
        """
        (manifest_dir / "demo-agent.yaml").write_text(
            yaml.safe_dump(_wrong_typed(case), sort_keys=False)
        )

        response = client.get("/api/agent-manifests")

        assert response.status_code == 200, response.text

    def test_one_unreadable_manifest_does_not_hide_the_healthy_agents(
        self, client, manifest_dir, seeded, fake_engine
    ):
        """The property that makes the route worth calling at all. A fleet of
        twenty with one bad file must list nineteen, not none."""
        (manifest_dir / "healthy-agent.yaml").write_text(
            yaml.safe_dump({**EXISTING, "id": "healthy-agent", "name": "Healthy"}, sort_keys=False)
        )
        (manifest_dir / "demo-agent.yaml").write_text(
            yaml.safe_dump({**EXISTING, "model": "not-a-mapping"}, sort_keys=False)
        )

        body = client.get("/api/agent-manifests").json()

        listed = {row["id"] for row in body["agents"]}
        assert "healthy-agent" in listed, body
        # The bad one is named as broken rather than silently dropped — the
        # operator has to be able to find the file that needs fixing.
        assert "demo-agent" in listed | {row["id"] for row in body["broken"]}, body

    @pytest.mark.parametrize("case", sorted(WRONG_TYPED))
    def test_a_wrong_typed_manifest_on_disk_can_still_be_read(
        self, client, manifest_dir, fake_engine, case
    ):
        """The repair path. 200 with the verdict, per the route's own contract —
        never a 500, and never a 404 that says the agent does not exist."""
        (manifest_dir / "demo-agent.yaml").write_text(
            yaml.safe_dump(_wrong_typed(case), sort_keys=False)
        )

        response = client.get("/api/agent-manifests/demo-agent")

        assert response.status_code == 200, response.text
        assert response.json()["validation"]["ok"] is False

    @pytest.mark.parametrize("case", sorted(WRONG_TYPED))
    def test_a_wrong_typed_manifest_can_still_be_silenced(
        self, client, manifest_dir, fake_engine, case
    ):
        """Disable is the stop control. It must work on the manifests that are
        already wrong — those are the ones an operator wants stopped."""
        (manifest_dir / "demo-agent.yaml").write_text(
            yaml.safe_dump(_wrong_typed(case), sort_keys=False)
        )

        response = client.post("/api/agent-manifests/demo-agent/disable")

        assert response.status_code < 500, response.text

    @pytest.mark.parametrize("case", sorted(WRONG_TYPED))
    def test_a_patch_of_a_wrong_typed_manifest_is_a_refusal_not_a_crash(
        self, client, manifest_dir, fake_engine, case
    ):
        (manifest_dir / "demo-agent.yaml").write_text(
            yaml.safe_dump(_wrong_typed(case), sort_keys=False)
        )

        response = client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed"})

        assert response.status_code < 500, response.text

    def test_the_refusal_names_no_manifest_value(self, client, workspace, fake_engine):
        """An exception's text can carry the offending value verbatim, and this
        body reaches a browser. Only the type comes out."""
        secret = "a-value-that-must-not-be-echoed"
        candidate = {**EXISTING, "v2": {"sandbox": {secret: True}}}

        body = client.post("/api/agent-manifests/validate", json={"manifest": candidate}).json()

        assert body["ok"] is False
        assert secret not in json.dumps(body)

    def test_a_wrongly_shaped_value_is_named_at_its_own_path(self, client, workspace, fake_engine):
        """The precise finding, not just "something went wrong". The validator
        itself now reports the wrong type rather than raising, so the operator
        is told WHICH field to fix; `not_validatable` is only the backstop for a
        check that raises anyway (see the next test)."""
        candidate = {**EXISTING, "v2": {"sandbox": {"enabled": True}}}

        body = client.post("/api/agent-manifests/validate", json={"manifest": candidate}).json()

        sandbox = [issue for issue in body["errors"] if issue["path"] == "v2.sandbox"]
        assert sandbox, body["errors"]
        assert sandbox[0]["code"] == "wrong_type"

    def test_a_validator_that_raises_anyway_becomes_a_finding(self, client, workspace, fake_engine):
        """The backstop, probed rather than assumed — this codebase has shipped
        six controls that were green and inert. The value-level guards cover
        what is known today; this covers the check nobody has written yet."""
        from routers import _manifest_validation

        def _explode(*args, **kwargs):
            raise RuntimeError("a future check echoed a-secret-value")

        with patch.object(_manifest_validation, "check_issues", _explode):
            response = client.post("/api/agent-manifests/validate", json={"manifest": EXISTING})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert any(issue["code"] == "not_validatable" for issue in body["errors"]), body
        assert "a-secret-value" not in json.dumps(body), "the raise text leaked"


class TestOneProblemIsOneFinding:
    """`CheckResult.faults` must not guess which checks enumerate.

    Round 3 defaulted `faults` from `details`, which is right for the seven
    checks whose details are one string per problem and wrong for the two whose
    details are prose. A default that guesses right seven times out of nine is
    what silently breaks the tenth:

    * **K** carries its list in the MESSAGE (`Missing basic I/O tools: [...]`)
      and a sentence of advice in `details` — so the default threw the list away
      and reported only the advice.
    * **E** carries its explanation in the message and two lines of CONTEXT in
      `details` — so one problem became two findings and the explanation was
      lost from both.

    The default is now opt-in: a check says `faults=` when its details
    enumerate, and otherwise one result is one finding carrying its whole
    message. A future prose-details check cannot repeat this, because nothing
    infers anything any more.
    """

    def _findings(self, client, manifest: dict, bucket: str, check: str) -> list[dict]:
        body = client.post("/api/agent-manifests/validate", json={"manifest": manifest}).json()
        return [issue for issue in body[bucket] if issue["path"] == f"check.{check}"]

    def test_check_k_keeps_the_list_of_missing_tools(self, client, workspace, fake_engine):
        """Only `write_file` is missing, and the assertion is the EXACT computed
        fragment — both halves of that matter.

        K's `details` is generic advice naming all three basic tools, so this
        test has been written wrong twice already. `"exec" in message and
        "write_file" in message` passed with the computed list discarded,
        because the advice says both words. Splitting it into `"Missing basic
        I/O tools" in message` plus `"write_file" in message` passed too: the
        prefix survives on its own if the `: {sorted(missing)}` interpolation is
        dropped, and the advice still supplies `write_file`.

        Only the whole computed fragment pins it, because only that requires the
        prefix, the list, AND the list being right. Mutation-proved: removing
        the interpolation from `check_basic_io_tools` turns this red.
        """
        candidate = {**EXISTING, "tools_allowed": ["exec", "read_file"]}

        found = self._findings(client, candidate, "warnings", "K")

        assert len(found) == 1, found
        message = found[0]["message"]
        assert "Missing basic I/O tools: ['write_file']" in message, message

    def test_check_e_is_one_finding_that_explains_itself(self, client, workspace, fake_engine):
        candidate = {
            **EXISTING,
            "status_file": "brain/memory/demo-agent-status.md",
            "tools_allowed": ["read_file"],
        }

        found = self._findings(client, candidate, "errors", "E")

        assert len(found) == 1, found
        assert "status_file but no write tools" in found[0]["message"], found[0]

    def test_an_enumerating_check_still_reports_one_finding_per_item(
        self, client, workspace, fake_engine
    ):
        """The counter-case. Making the default conservative must not undo
        round 3 — D still has to split, or the partial-repair fix is gone."""
        candidate = {**EXISTING, "tools_allowed": ["bad_a", "bad_b", "exec"]}

        found = self._findings(client, candidate, "errors", "D")

        assert len(found) == 2, found
        rendered = json.dumps(found)
        assert "bad_a" in rendered and "bad_b" in rendered

    def test_a_multi_issue_schema_check_still_splits(self, client, workspace, fake_engine):
        """Check A's details genuinely are one string per problem, so it keeps
        the per-item behaviour it needs for the same reason D does."""
        candidate = {**EXISTING, "id": "Not-Kebab", "department": "not-a-department"}

        found = self._findings(client, candidate, "errors", "A")

        assert len(found) >= 2, found
        rendered = json.dumps(found)
        assert "kebab-case" in rendered and "department" in rendered


# ─── Creating ────────────────────────────────────────────────────────


class TestCreate:
    def test_it_writes_a_manifest_and_an_instruction_file(
        self, client, manifest_dir, workspace, fake_engine
    ):
        response = client.post("/api/agent-manifests", json=_create_body())

        assert response.status_code == 201, response.json()
        assert response.json()["id"] == "invoice-watcher"
        document = _read(manifest_dir / "invoice-watcher.yaml")
        assert document["id"] == "invoice-watcher"
        assert document["schedule"]["cron"] == "0 8 * * *"
        assert (workspace / "brain" / "INVOICE_WATCHER.md").is_file()

    def test_it_never_writes_a_model_id_the_instance_did_not_choose(
        self, client, manifest_dir, workspace, fake_engine
    ):
        """The scaffold template hardcodes a model and a fallback. Writing
        either would point a new agent at a provider this appliance may hold no
        key for at all — so the only ids that may land are the one the operator
        chose and the one the fleet's own ``_defaults.yaml`` already names."""
        template = (
            Path(__file__).resolve().parents[3] / "templates" / "agent-manifest.yaml"
        ).read_text()
        hardcoded = "openrouter/moonshotai/kimi-k2.5"
        assert hardcoded in template, "the template stopped hardcoding a model — update this test"
        (manifest_dir / "_defaults.yaml").write_text(
            "model:\n  primary: openrouter/example/fleet-model\n"
        )

        response = client.post("/api/agent-manifests", json=_create_body(model=None))

        assert response.status_code == 201, response.json()
        written = (manifest_dir / "invoice-watcher.yaml").read_text()
        assert hardcoded not in written
        assert "gemini/gemini-2.5-pro" not in written
        assert "openrouter/example/fleet-model" in written

    def test_it_conflicts_on_an_existing_id(self, client, seeded, fake_engine):
        response = client.post("/api/agent-manifests", json=_create_body(name="Demo Agent"))

        assert response.status_code == 409

    def test_a_create_that_loses_the_race_conflicts_instead_of_overwriting(
        self, client, manifest_dir, fake_engine
    ):
        """The ``.exists()`` check is a courtesy; ``O_EXCL`` is the decision.

        Two concurrent creates of the same id both clear the check, and
        ``os.replace`` overwrites without complaint — so the second silently
        destroyed the first agent, with the history ring as the only record.
        The file appearing between the check and the write is simulated here
        rather than raced, because a race that only sometimes reproduces is a
        test that only sometimes tests anything.
        """
        from routers import agent_manifests

        real_dump = agent_manifests._dump
        rival = {**EXISTING, "id": "invoice-watcher", "name": "Rival"}

        def _another_writer_gets_there_first(document):
            (manifest_dir / "invoice-watcher.yaml").write_text(
                yaml.safe_dump(rival, sort_keys=False)
            )
            return real_dump(document)

        with patch.object(agent_manifests, "_dump", _another_writer_gets_there_first):
            response = client.post("/api/agent-manifests", json=_create_body())

        assert response.status_code == 409, response.text
        assert _read(manifest_dir / "invoice-watcher.yaml")["name"] == "Rival"
        assert not list(manifest_dir.glob(".*.tmp"))

    def test_operator_text_cannot_inject_yaml_into_the_scaffold(
        self, client, manifest_dir, fake_engine
    ):
        """The template is YAML with BARE placeholders (``name: {AGENT_NAME}``).
        Substituting operator text into it and parsing the result would let a
        browser write a different document than the one it asked for — or fail
        the parse and call a legitimate name a server error."""
        hostile = 'Ops: Bot #1 & "co"'
        nasty_description = "line one\nid: other-agent\ndelivery:\n  mode: announce"

        response = client.post(
            "/api/agent-manifests",
            json=_create_body(id="ops-bot", name=hostile, description=nasty_description),
        )

        assert response.status_code == 201, response.json()
        document = _read(manifest_dir / "ops-bot.yaml")
        assert document["id"] == "ops-bot"
        assert document["name"] == hostile
        assert document["description"] == nasty_description
        assert document["delivery"]["mode"] == "none"
        assert not (manifest_dir / "other-agent.yaml").exists()

    @pytest.mark.parametrize("agent_id", ["../x", "..%2Fx", "a/b", "A", "", "docs/agents/x"])
    def test_an_explicit_traversal_id_is_refused(
        self, client, manifest_dir, workspace, fake_engine, agent_id
    ):
        """An id the caller typed is taken literally, never slugified: quietly
        turning ``../x`` into ``x`` would answer 201 to a request to escape the
        manifest directory."""
        before = sorted(p.name for p in manifest_dir.iterdir())

        response = client.post("/api/agent-manifests", json=_create_body(id=agent_id))

        assert response.status_code == 422, response.json()
        assert sorted(p.name for p in manifest_dir.iterdir()) == before
        assert not list(workspace.glob("*.yaml"))
        assert not list(workspace.parent.glob("*.yaml"))

    @pytest.mark.parametrize("name", ["", "   ", "!!!", "---"])
    def test_a_name_that_reduces_to_no_id_is_refused(
        self, client, manifest_dir, workspace, fake_engine, name
    ):
        before = sorted(p.name for p in manifest_dir.iterdir())

        response = client.post("/api/agent-manifests", json=_create_body(name=name))

        assert response.status_code == 422, response.json()
        assert sorted(p.name for p in manifest_dir.iterdir()) == before

    def test_a_display_name_with_punctuation_becomes_a_kebab_id(
        self, client, manifest_dir, fake_engine
    ):
        response = client.post("/api/agent-manifests", json=_create_body(name="Acme / Ops Bot"))

        assert response.json()["id"] == "acme-ops-bot"
        assert (manifest_dir / "acme-ops-bot.yaml").is_file()

    def test_a_validation_failure_leaves_no_file_and_no_temp_residue(
        self, client, manifest_dir, fake_engine
    ):
        before = sorted(p.name for p in manifest_dir.iterdir())

        response = client.post(
            "/api/agent-manifests", json=_create_body(cron="every other tuesday")
        )

        assert response.status_code == 422
        assert sorted(p.name for p in manifest_dir.iterdir()) == before
        assert not list(manifest_dir.glob(".*.tmp"))

    def test_the_422_carries_the_validators_own_codes(self, client, manifest_dir, fake_engine):
        response = client.post(
            "/api/agent-manifests", json=_create_body(cron="every other tuesday")
        )

        detail = response.json()["detail"]
        assert any(issue["code"] == "bad_cron" for issue in detail["errors"]), detail

    def test_the_rendered_instructions_keep_the_contract_sections(
        self, client, workspace, fake_engine
    ):
        """``docs/agents/INSTRUCTION_CONTRACT.md`` requires four headings. An
        agent whose instructions are only the operator's free text loses every
        one of them."""
        client.post("/api/agent-manifests", json=_create_body(instructions=""))

        text = (workspace / "brain" / "INVOICE_WATCHER.md").read_text()
        for heading in ("# Invoice Watcher", "## Your Role", "## Tasks", "## Output"):
            assert heading in text

    def test_the_operators_own_words_land_under_their_role(self, client, workspace, fake_engine):
        client.post("/api/agent-manifests", json=_create_body(instructions="Only touch invoices."))

        text = (workspace / "brain" / "INVOICE_WATCHER.md").read_text()
        role = text.split("## Your Role", 1)[1].split("## Tasks", 1)[0]
        assert "Only touch invoices." in role

    def test_it_calls_the_engine_reconcile(self, client, workspace, fake_engine):
        body = client.post("/api/agent-manifests", json=_create_body()).json()

        assert ("POST", "/api/admin/scheduler/reconcile") in fake_engine.calls
        assert body["reconcile"]["applied"] is True
        assert body["reconcile"]["added"] == ["demo-agent"]

    def test_a_failed_reconcile_is_reported_and_does_not_undo_the_write(
        self, client, manifest_dir, fake_engine
    ):
        """The file on disk is the source of truth and the watchdog reconciles
        it within five minutes anyway. What must not happen is the operator
        being told the agent is live when it is not."""
        fake_engine.reconcile_status = 503
        fake_engine.reconcile_body = {"error": "scheduler not running"}

        body = client.post("/api/agent-manifests", json=_create_body()).json()

        assert (manifest_dir / "invoice-watcher.yaml").is_file()
        assert body["reconcile"]["applied"] is False
        assert body["reconcile"]["error"] == "scheduler not running"

    def test_it_audits_the_create(self, client, workspace, fake_engine):
        with patch("routers.agent_manifests.audited") as audit:
            client.post("/api/agent-manifests", json=_create_body())

        assert audit.call_args.args[1] == "helm.agent_manifest.create"
        assert audit.call_args.kwargs["action"] == "invoice-watcher"


# ─── Editing ─────────────────────────────────────────────────────────


class TestPatch:
    def test_it_preserves_an_unknown_hand_written_key(self, client, seeded, fake_engine):
        """The form knows twenty paths out of a schema with hundreds. A
        ``dict.update`` of the blocks it does know would take a hand-written
        ``model.temperature`` with it."""
        client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed Agent"})

        document = _read(seeded)
        assert document["name"] == "Renamed Agent"
        assert document["v2"]["custom_plugin_key"] == 7
        assert document["model"]["temperature"] == 0.1
        assert document["model"]["primary"] == "openrouter/example/demo-model"

    def test_a_replaced_list_does_not_union(self, client, seeded, fake_engine):
        """``resolver.deep_merge`` unions lists, which is right for inheritance
        and wrong for a form: an operator who removes a tool expects it gone."""
        client.patch(
            "/api/agent-manifests/demo-agent",
            json={"tools_allowed": ["exec", "read_file", "write_file", "search_memory"]},
        )
        client.patch("/api/agent-manifests/demo-agent", json={"tools_allowed": ["exec"]})

        assert _read(seeded)["tools_allowed"] == ["exec"]

    def test_it_bumps_the_version_and_appends_one_changelog_entry(
        self, client, seeded, fake_engine
    ):
        from datetime import UTC, datetime

        client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed Agent"})

        document = _read(seeded)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert document["version"] == today
        assert document["changelog"][-1]["date"] == today
        assert document["changelog"][-1]["change"]

    def test_an_absent_agent_is_a_404(self, client, workspace, fake_engine):
        response = client.patch("/api/agent-manifests/no-such-agent", json={"name": "X"})
        assert response.status_code == 404

    def test_a_patch_that_would_not_validate_leaves_the_file_alone(
        self, client, seeded, fake_engine
    ):
        before = seeded.read_text()

        response = client.patch("/api/agent-manifests/demo-agent", json={"cron": "nope"})

        assert response.status_code == 422
        assert seeded.read_text() == before
        assert not list(seeded.parent.glob(".*.tmp"))

    def test_it_reconciles_after_the_write(self, client, seeded, fake_engine):
        client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed Agent"})

        assert fake_engine.reconciles == 1

    def test_the_history_ring_keeps_five(self, client, seeded, fake_engine):
        for n in range(6):
            response = client.patch(
                "/api/agent-manifests/demo-agent", json={"description": f"Revision {n}"}
            )
            assert response.status_code == 200, response.json()

        history = sorted((seeded.parent / ".history" / "demo-agent").glob("*.yaml"))
        assert len(history) == 5

    def test_the_history_dir_is_invisible_to_the_engines_loader(self, client, seeded, fake_engine):
        """``load_manifest_dir`` globs one level, non-recursively — which is
        what makes a snapshot ring safe to keep inside docs/agents/."""
        from robothor.engine.config import load_manifest_dir

        client.patch("/api/agent-manifests/demo-agent", json={"description": "Revised"})

        scan = load_manifest_dir(seeded.parent)
        assert [m["id"] for m in scan.manifests] == ["demo-agent"]
        assert scan.failures == ()


class TestAnAlreadyBrokenAgentCanStillBeRepairedAndSilenced:
    """A save must not BREAK a manifest. It must not be gated on one that is
    already broken, which is a different promise and the opposite outcome.

    `_apply_patch` ran the full validator on the merged document, so a manifest
    that already failed any check could not be turned off — and `disable` is the
    stop control. Worse, the tool half of the check reads the LIVE engine's
    registry, so uninstalling one plugin made every agent that named its tools
    simultaneously un-editable and un-silenceable. `DELETE` does not validate,
    so retiring the agent was the only remedy the Helm offered.

    The rule now: refuse only the errors THIS EDIT introduced.
    """

    @pytest.fixture
    def broken_tool(self, manifest_dir, seeded):
        """On disk: an agent naming a tool the engine no longer registers."""
        document = {**EXISTING, "tools_allowed": ["exec", "a_retired_plugin_tool"]}
        seeded.write_text(yaml.safe_dump(document, sort_keys=False))
        return seeded

    @pytest.fixture
    def broken_cron(self, manifest_dir, seeded):
        document = {**EXISTING, "schedule": {"cron": "not a cron", "timezone": "UTC"}}
        seeded.write_text(yaml.safe_dump(document, sort_keys=False))
        return seeded

    def test_disable_silences_an_agent_with_an_unregistered_tool(
        self, client, broken_tool, fake_engine
    ):
        response = client.post("/api/agent-manifests/demo-agent/disable")

        assert response.status_code == 200, response.text
        assert _read(broken_tool)["schedule"]["enabled"] is False

    def test_disable_silences_an_agent_with_a_bad_cron(self, client, broken_cron, fake_engine):
        response = client.post("/api/agent-manifests/demo-agent/disable")

        assert response.status_code == 200, response.text
        assert _read(broken_cron)["schedule"]["enabled"] is False

    def test_the_pre_existing_error_still_comes_back_as_a_warning(
        self, client, broken_tool, fake_engine
    ):
        """Allowed through is not the same as unreported: the operator has to
        be told the agent is still broken, or the Helm has quietly blessed it."""
        body = client.post("/api/agent-manifests/demo-agent/disable").json()

        rendered = json.dumps(body)
        assert "a_retired_plugin_tool" in rendered, body

    def test_the_carried_faults_are_named_under_their_own_key(
        self, client, broken_tool, fake_engine
    ):
        """`pre_existing` is in the response, not just in the verdict dict.

        It was computed and dropped on the floor — the string appeared nowhere
        in crm/, docs/ or app/ outside its own assignment. A UI that wants to
        say "saved, but still broken" should not have to diff `warnings`
        against a previous call to work out which of them used to be errors.
        """
        body = client.post("/api/agent-manifests/demo-agent/disable").json()

        assert "pre_existing" in body, body
        assert any("a_retired_plugin_tool" in issue["message"] for issue in body["pre_existing"])

    def test_a_healthy_agent_carries_nothing(self, client, seeded, fake_engine):
        """The counter-case: the key is empty when there is nothing to carry,
        so its presence means something."""
        body = client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed"}).json()

        assert body["pre_existing"] == [], body

    def test_a_patch_that_repairs_the_manifest_is_accepted(self, client, broken_cron, fake_engine):
        """The repair path. Editing the very field that is wrong must work."""
        response = client.patch("/api/agent-manifests/demo-agent", json={"cron": "0 9 * * *"})

        assert response.status_code == 200, response.text
        assert _read(broken_cron)["schedule"]["cron"] == "0 9 * * *"

    def test_a_patch_of_an_unrelated_field_on_a_broken_agent_is_accepted(
        self, client, broken_tool, fake_engine
    ):
        response = client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed"})

        assert response.status_code == 200, response.text
        assert _read(broken_tool)["name"] == "Renamed"

    def test_an_edit_that_introduces_an_error_is_still_refused(self, client, seeded, fake_engine):
        """The counter-case, and the whole reason the gate exists. Without it
        this class would be a description of removing the validation."""
        response = client.patch("/api/agent-manifests/demo-agent", json={"cron": "not a cron"})

        assert response.status_code == 422, response.text
        assert any(issue["code"] == "bad_cron" for issue in response.json()["detail"]["errors"])
        assert _read(seeded)["schedule"]["cron"] == "0 9 * * *", "the file was written anyway"

    def test_an_edit_that_introduces_a_second_error_is_refused_on_that_one_only(
        self, client, broken_tool, fake_engine
    ):
        """A manifest with one pre-existing fault does not become a free pass
        for the next one — a fault of a DIFFERENT kind."""
        response = client.patch("/api/agent-manifests/demo-agent", json={"cron": "not a cron"})

        assert response.status_code == 422, response.text
        codes = {issue["code"] for issue in response.json()["detail"]["errors"]}
        assert "bad_cron" in codes
        assert "check_d" not in codes, "the pre-existing tool error was re-reported as new"

    def test_a_second_fault_under_the_same_check_is_also_refused(
        self, client, broken_tool, fake_engine
    ):
        """The narrower case, and the one that made the claim false.

        Every `manifest_checks` finding collapses to one `(check.D, check_d)`
        pair, so keying identity on `(path, code)` made a pre-existing
        unregistered tool a free pass for a second unregistered tool — the
        check fires once either way and the edit looked like it introduced
        nothing. The test that certified the claim used two DIFFERENT checks,
        so it was narrower than the sentence it protected.
        """
        before = _read(broken_tool)["tools_allowed"]

        response = client.patch(
            "/api/agent-manifests/demo-agent",
            json={"tools_allowed": [*before, "a_second_missing_tool"]},
        )

        assert response.status_code == 422, response.text
        rendered = json.dumps(response.json()["detail"]["errors"])
        assert "a_second_missing_tool" in rendered, response.text
        assert _read(broken_tool)["tools_allowed"] == before, (
            "the second bad tool was written to disk"
        )

    @pytest.fixture
    def three_bad_tools(self, manifest_dir, seeded):
        """The shape a plugin uninstall leaves behind: several names invalid
        at once."""
        document = {**EXISTING, "tools_allowed": ["bad_a", "bad_b", "bad_c", "exec"]}
        seeded.write_text(yaml.safe_dump(document, sort_keys=False))
        return seeded

    def test_a_partial_repair_is_accepted(self, client, three_bad_tools, fake_engine):
        """Removing one of three bad tool names is a REPAIR, not a new fault.

        Keying a finding's identity on its message caught the addition
        direction and broke the removal one: `manifest_checks` collapses N bad
        items into one result whose message enumerates them, so dropping `bad_c`
        changed the message, the residual had no match in the before-verdict,
        and the edit was refused — with a 422 naming `bad_a`, which was already
        on disk, as an error the operator had just introduced.

        The normal case is a plugin uninstall invalidating five names at once;
        this forced all five into a single PATCH or none at all.
        """
        response = client.patch(
            "/api/agent-manifests/demo-agent",
            json={"tools_allowed": ["bad_a", "bad_b", "exec"]},
        )

        assert response.status_code == 200, response.text
        assert _read(three_bad_tools)["tools_allowed"] == ["bad_a", "bad_b", "exec"]

    def test_the_residual_of_a_partial_repair_is_reported_as_pre_existing(
        self, client, three_bad_tools, fake_engine
    ):
        """Accepted is not the same as clean: what is left must still be named,
        and named as something that was already there."""
        body = client.patch(
            "/api/agent-manifests/demo-agent",
            json={"tools_allowed": ["bad_a", "bad_b", "exec"]},
        ).json()

        carried = json.dumps(body["pre_existing"])
        assert "bad_a" in carried and "bad_b" in carried, body
        assert "bad_c" not in carried, "a fault the edit actually fixed is still reported"

    def test_repairing_down_to_one_is_accepted_too(self, client, three_bad_tools, fake_engine):
        response = client.patch(
            "/api/agent-manifests/demo-agent", json={"tools_allowed": ["bad_a", "exec"]}
        )

        assert response.status_code == 200, response.text

    def test_a_full_repair_is_accepted(self, client, three_bad_tools, fake_engine):
        response = client.patch(
            "/api/agent-manifests/demo-agent",
            json={"tools_allowed": ["exec", "read_file", "write_file"]},
        )

        assert response.status_code == 200, response.text

    def test_a_repair_that_also_introduces_a_new_item_is_still_refused(
        self, client, three_bad_tools, fake_engine
    ):
        """The counter-case that keeps the whole class honest: dropping one bad
        name must not buy the right to add a different one."""
        response = client.patch(
            "/api/agent-manifests/demo-agent",
            json={"tools_allowed": ["bad_a", "bad_b", "a_brand_new_bad_tool", "exec"]},
        )

        assert response.status_code == 422, response.text
        rendered = json.dumps(response.json()["detail"]["errors"])
        assert "a_brand_new_bad_tool" in rendered
        assert "bad_a" not in rendered, "a fault that was already on disk was reported as new"
        assert _read(three_bad_tools)["tools_allowed"] == ["bad_a", "bad_b", "bad_c", "exec"]

    def test_the_unchanged_pre_existing_fault_is_still_not_refused(
        self, client, broken_tool, fake_engine
    ):
        """The counter-case for the above. Tightening identity must not turn
        every pre-existing fault back into a lockout — that was I1."""
        response = client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed"})

        assert response.status_code == 200, response.text


class TestEnableDisable:
    def test_disable_writes_the_flag_and_enable_restores_it(self, client, seeded, fake_engine):
        client.post("/api/agent-manifests/demo-agent/disable")
        assert _read(seeded)["schedule"]["enabled"] is False

        client.post("/api/agent-manifests/demo-agent/enable")
        assert _read(seeded)["schedule"]["enabled"] is True

    def test_enable_is_never_a_no_op(self, client, seeded, fake_engine):
        """The key starts absent, and absent means enabled. A route that
        skipped the write would report success while producing no document
        that says so — and the fleet view would keep showing whatever it
        showed before."""
        assert "enabled" not in _read(seeded)["schedule"]

        client.post("/api/agent-manifests/demo-agent/enable")

        assert _read(seeded)["schedule"]["enabled"] is True

    def test_disable_reconciles_so_the_job_actually_goes(self, client, seeded, fake_engine):
        client.post("/api/agent-manifests/demo-agent/disable")

        assert fake_engine.reconciles == 1


# ─── Retiring ────────────────────────────────────────────────────────


class TestRetire:
    def test_it_requires_the_confirmation_to_be_the_id(self, client, seeded, fake_engine):
        for confirm in ("", "yes", "true", "demo agent"):
            response = client.request(
                "DELETE", "/api/agent-manifests/demo-agent", json={"confirm": confirm}
            )
            assert response.status_code == 422, confirm
        assert seeded.is_file()

    def test_it_moves_the_manifest_instead_of_unlinking_it(self, client, seeded, fake_engine):
        response = client.request(
            "DELETE", "/api/agent-manifests/demo-agent", json={"confirm": "demo-agent"}
        )

        assert response.status_code == 200
        assert not seeded.exists()
        assert (seeded.parent / "retired" / "demo-agent.yaml").is_file()

    def test_the_loader_no_longer_sees_the_agent(self, client, seeded, fake_engine):
        from robothor.engine.config import load_manifest_dir

        client.request("DELETE", "/api/agent-manifests/demo-agent", json={"confirm": "demo-agent"})

        scan = load_manifest_dir(seeded.parent)
        assert scan.manifests == ()
        assert scan.failures == ()

    def test_the_instruction_file_stays(self, client, seeded, workspace, fake_engine):
        """It is the operator's own writing, and re-creating the agent with the
        same id picks it back up."""
        client.request("DELETE", "/api/agent-manifests/demo-agent", json={"confirm": "demo-agent"})

        assert (workspace / "brain" / "DEMO_AGENT.md").is_file()

    def test_it_snapshots_before_moving(self, client, seeded, fake_engine):
        client.request("DELETE", "/api/agent-manifests/demo-agent", json={"confirm": "demo-agent"})

        assert list((seeded.parent / ".history" / "demo-agent").glob("*.yaml"))

    def test_it_reconciles(self, client, seeded, fake_engine):
        client.request("DELETE", "/api/agent-manifests/demo-agent", json={"confirm": "demo-agent"})

        assert fake_engine.reconciles == 1


# ─── Firing one run ──────────────────────────────────────────────────


class TestRunNow:
    def test_it_proxies_to_the_engines_trigger_route(self, client, seeded, fake_engine):
        response = client.post("/api/agent-manifests/demo-agent/run")

        assert response.status_code == 200
        assert ("POST", "/api/agents/demo-agent/trigger") in fake_engine.calls

    def test_an_engine_refusal_is_a_502_and_not_a_fake_success(self, client, seeded, fake_engine):
        """The engine's trigger route answers 200 with an ``error`` key for an
        agent it cannot load, so the status code alone is not the verdict."""
        fake_engine.trigger_body = {"error": "Agent not found: demo-agent"}

        response = client.post("/api/agent-manifests/demo-agent/run")

        assert response.status_code == 502

    def test_an_unknown_agent_is_a_404_and_never_reaches_the_engine(
        self, client, workspace, fake_engine
    ):
        """Every sibling route answers 404 for an id that is not an agent here.
        This one proxied it and returned the engine's 502, which reads as "the
        engine is down" — a different problem with a different fix."""
        response = client.post("/api/agent-manifests/no-such-agent/run")

        assert response.status_code == 404
        assert fake_engine.calls == [], "an unknown id reached the engine"


class TestAMisconfiguredWorkspaceIsARefusalNotACrash:
    """Brief decision 4: a symlinked workspace directory is an operator's
    deployment mistake, and it should read as one.

    ``safety.py`` refuses to write through a symlinked root — that part was
    always right. What was missing is that ``_manifest_root`` was the one path
    helper with no handler, so the refusal surfaced as an unhandled 500 while a
    symlinked ``brain/`` correctly gave a 422 naming the rule.
    """

    @pytest.fixture
    def symlinked_agents_dir(self, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "brain").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (workspace / "docs" / "agents").symlink_to(elsewhere, target_is_directory=True)
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
        monkeypatch.delenv("ROBOTHOR_MANIFEST_DIR", raising=False)
        return elsewhere

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "/api/agent-manifests/demo-agent", None),
            ("post", "/api/agent-manifests", "create"),
            ("patch", "/api/agent-manifests/demo-agent", {"name": "X"}),
            ("post", "/api/agent-manifests/demo-agent/disable", None),
            ("post", "/api/agent-manifests/demo-agent/run", None),
        ],
    )
    def test_a_symlinked_manifest_dir_is_a_422(
        self, client, symlinked_agents_dir, fake_engine, method, path, body
    ):
        payload = _create_body() if body == "create" else body
        call = getattr(client, method)
        response = call(path, json=payload) if payload is not None else call(path)

        assert response.status_code == 422, response.text
        assert "symlink" in response.text.lower()
        assert not list(symlinked_agents_dir.iterdir()), "a write went through the link"


# ─── No caller-supplied id reaches an engine URL ─────────────────────


HOSTILE_IDS = [
    "../x",
    "..",
    "a/b",
    "http://evil.example.com",
    "https://evil.example.com/api/admin/models",
    "a?b=c",
    "a#b",
    "A",
    "",
    "a" * 65,
    "x\ny",
]


class TestNoHostileIdReachesTheEngine:
    """``/run`` is the only place in the appliance where a caller chooses part
    of an engine URL.

    ``validate_identifier`` fullmatches ``[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?``
    BEFORE the path is built, the surviving value is then percent-encoded, and
    ``_engine_client._checked_path`` refuses anything that is not a literal
    ``/api/...`` path at the sink. Three locks; this asserts the first one
    fires, and that nothing is dialled when it does.
    """

    @pytest.mark.parametrize("agent_id", HOSTILE_IDS)
    def test_a_url_shaped_id_is_422_before_any_engine_call(
        self, client, seeded, fake_engine, agent_id
    ):
        from urllib.parse import quote

        # Encoded so the value survives routing and reaches the handler: an
        # unencoded "../x" is normalised away by the client before the request
        # is ever sent, which would make this test pass for the wrong reason.
        response = client.post(f"/api/agent-manifests/{quote(agent_id, safe='')}/run")

        assert response.status_code in (404, 422), response.text
        assert fake_engine.calls == [], f"{agent_id!r} reached the engine"

    @pytest.mark.parametrize(
        "agent_id", [i for i in HOSTILE_IDS if i and "/" not in i and i not in {".", ".."}]
    )
    def test_a_hostile_id_that_reaches_the_handler_is_a_422(
        self, client, seeded, fake_engine, agent_id
    ):
        """These ids survive routing — no slash and no dot segment — so they
        prove the HANDLER's gate rather than the router's path normalisation.
        Same parameter on all five routes."""
        from urllib.parse import quote

        encoded = quote(agent_id, safe="")
        statuses = {
            "GET": client.get(f"/api/agent-manifests/{encoded}").status_code,
            "PATCH": client.patch(
                f"/api/agent-manifests/{encoded}", json={"name": "X"}
            ).status_code,
            "enable": client.post(f"/api/agent-manifests/{encoded}/enable").status_code,
            "disable": client.post(f"/api/agent-manifests/{encoded}/disable").status_code,
            "DELETE": client.request(
                "DELETE", f"/api/agent-manifests/{encoded}", json={"confirm": agent_id}
            ).status_code,
        }

        assert statuses == dict.fromkeys(statuses, 422)
        assert fake_engine.calls == []

    @pytest.mark.parametrize("agent_id", [i for i in HOSTILE_IDS if i])
    def test_the_same_ids_are_refused_on_every_id_bearing_route(
        self, client, seeded, fake_engine, agent_id
    ):
        """The gate is on the id, not on one route. GET/PATCH/DELETE/enable/
        disable all take the same path parameter, and all of them can reach a
        filesystem path or the engine with it."""
        from urllib.parse import quote

        encoded = quote(agent_id, safe="")
        responses = {
            "GET": client.get(f"/api/agent-manifests/{encoded}"),
            "PATCH": client.patch(f"/api/agent-manifests/{encoded}", json={"name": "X"}),
            "enable": client.post(f"/api/agent-manifests/{encoded}/enable"),
            "disable": client.post(f"/api/agent-manifests/{encoded}/disable"),
            "DELETE": client.request(
                "DELETE", f"/api/agent-manifests/{encoded}", json={"confirm": agent_id}
            ),
        }

        # 422 when the value reaches the handler and `_safe_id` refuses it; 404
        # when it carries a slash, because the client normalises the path away
        # before the router ever sees this route. Both are refusals that happen
        # before anything is read, written or dialled — which is the property.
        # A 200 or a 5xx anywhere here would not be.
        refused = {name: r.status_code for name, r in responses.items()}
        assert all(status in (404, 422) for status in refused.values()), refused
        assert fake_engine.calls == []

    def test_a_valid_id_still_reaches_the_engine(self, client, seeded, fake_engine):
        """The counter-case. A guard that refused everything would make every
        assertion above vacuous."""
        response = client.post("/api/agent-manifests/demo-agent/run")

        assert response.status_code == 200
        assert ("POST", "/api/agents/demo-agent/trigger") in fake_engine.calls

    def test_the_path_is_percent_encoded_on_the_way_out(self, client, seeded, fake_engine):
        """``validate_identifier`` already leaves nothing to encode, which is
        the point: the second lock costs nothing and holds if the first is ever
        loosened."""
        import inspect

        from routers import agent_manifests

        source = inspect.getsource(agent_manifests.run_manifest)
        assert "quote(agent_id" in source, "the run proxy stopped encoding the id"


# ─── Rules 1 and 2 ───────────────────────────────────────────────────


class TestNoResponseDescribesTheBox:
    def _bodies(self, client, manifest_dir, workspace):
        (manifest_dir / "broken.yaml").write_text("id: broken\nname: [unclosed\n")
        yield client.get("/api/agent-manifests").json()
        yield client.get("/api/agent-manifests/demo-agent").json()
        yield client.get("/api/agent-manifests/broken").json()
        yield client.post("/api/agent-manifests/validate", json={"manifest": EXISTING}).json()
        yield client.post(
            "/api/agent-manifests", json=_create_body(cron="every other tuesday")
        ).json()
        yield client.patch("/api/agent-manifests/demo-agent", json={"cron": "nope"}).json()
        yield client.post("/api/agent-manifests/demo-agent/disable").json()

    def test_no_response_carries_an_absolute_path(
        self, client, manifest_dir, workspace, seeded, fake_engine
    ):
        needle = str(workspace)
        for body in self._bodies(client, manifest_dir, workspace):
            rendered = json.dumps(body, default=str)
            assert needle not in rendered, f"a workspace path leaked: {rendered[:200]}"
            assert "/tmp/" not in rendered, f"a filesystem path leaked: {rendered[:200]}"

    def test_an_audit_detail_is_identifiers_only(self, client, seeded, fake_engine, workspace):
        with patch("routers.agent_manifests.audited") as audit:
            client.patch("/api/agent-manifests/demo-agent", json={"name": "Renamed Agent"})

        rendered = json.dumps(audit.call_args.kwargs, default=str)
        assert str(workspace) not in rendered
        assert ".yaml" not in rendered

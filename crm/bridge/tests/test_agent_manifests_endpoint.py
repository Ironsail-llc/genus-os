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


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace. Never the operator's real one — rule 11."""
    manifests = tmp_path / "docs" / "agents"
    manifests.mkdir(parents=True)
    (tmp_path / "brain").mkdir()
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

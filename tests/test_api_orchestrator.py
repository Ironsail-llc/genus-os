"""Tests for robothor.api.orchestrator — FastAPI app models and configuration."""

import time

from robothor.api.orchestrator import (
    ChatChoice,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    IngestRequest,
    QueryRequest,
    VisionEnrollRequest,
    VisionLookRequest,
    VisionModeRequest,
    app,
)

# ─── Pydantic Models ─────────────────────────────────────────────────


class TestChatMessage:
    def test_defaults(self):
        msg = ChatMessage(content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"

    def test_custom_role(self):
        msg = ChatMessage(role="assistant", content="hi")
        assert msg.role == "assistant"


class TestChatRequest:
    def test_defaults(self):
        req = ChatRequest(messages=[ChatMessage(content="hi")])
        assert req.model == "default"
        assert req.stream is False
        assert req.use_memory is True
        assert req.use_web is True
        assert req.profile is None
        assert req.temperature is None
        assert req.max_tokens is None

    def test_custom(self):
        req = ChatRequest(
            model="test-model",
            messages=[ChatMessage(content="hi")],
            profile="research",
            stream=True,
            temperature=0.5,
        )
        assert req.model == "test-model"
        assert req.profile == "research"
        assert req.stream is True
        assert req.temperature == 0.5


class TestChatResponse:
    def test_structure(self):
        resp = ChatResponse(
            id="chatcmpl-test",
            created=int(time.time()),
            choices=[ChatChoice(message=ChatMessage(role="assistant", content="response"))],
        )
        assert resp.id == "chatcmpl-test"
        assert resp.object == "chat.completion"
        assert len(resp.choices) == 1
        assert resp.choices[0].finish_reason == "stop"
        assert resp.usage.total_tokens == 0
        assert resp.rag_metadata is None


class TestQueryRequest:
    def test_required_field(self):
        req = QueryRequest(question="What is the meaning of life?")
        assert req.question == "What is the meaning of life?"
        assert req.profile is None
        assert req.memory_limit is None
        assert req.web_limit is None


class TestIngestRequest:
    def test_defaults(self):
        req = IngestRequest(content="some content")
        assert req.source_channel == "api"
        assert req.content_type == "conversation"
        assert req.metadata is None

    def test_custom(self):
        req = IngestRequest(
            content="email body",
            source_channel="email",
            content_type="email",
            metadata={"from": "test@example.com"},
        )
        assert req.source_channel == "email"
        assert req.metadata["from"] == "test@example.com"


class TestVisionModels:
    def test_look_request_default(self):
        req = VisionLookRequest()
        assert "Describe" in req.prompt

    def test_enroll_request(self):
        req = VisionEnrollRequest(name="Alice")
        assert req.name == "Alice"

    def test_mode_request(self):
        req = VisionModeRequest(mode="armed")
        assert req.mode == "armed"


# ─── FastAPI App ──────────────────────────────────────────────────────


class TestAppConfig:
    def test_app_title(self):
        assert "Genus OS" in app.title

    def test_app_has_routes(self):
        route_paths = [r.path for r in app.routes]
        assert "/health" in route_paths
        assert "/live" in route_paths
        assert "/ready" in route_paths
        assert "/query" in route_paths
        assert "/v1/chat/completions" in route_paths
        assert "/v1/models" in route_paths
        assert "/profiles" in route_paths
        assert "/stats" in route_paths
        assert "/ingest" in route_paths

    def test_app_has_vision_routes(self):
        route_paths = [r.path for r in app.routes]
        assert "/vision/look" in route_paths
        assert "/vision/detect" in route_paths
        assert "/vision/identify" in route_paths
        assert "/vision/status" in route_paths
        assert "/vision/enroll" in route_paths
        assert "/vision/mode" in route_paths

    def test_cors_middleware(self):
        # CORS middleware should be configured
        assert len(app.user_middleware) >= 1


# ─── Readiness ───────────────────────────────────────────────────────


class TestReadinessGatesOnALocalModelOnlyWhenTheFleetUsesOne:
    """The compose stack could never come up on a fresh machine because of this.

    The orchestrator's `/ready` demanded a generation model in Ollama. The
    stack's Ollama container starts empty, and the wizard's model-pull step ran
    after `up` — so `docker compose up` exited 1 with "container
    robothor-orchestrator is unhealthy" and the bridge and dashboard never
    started at all. An instance whose agents all run in the cloud does not need
    a local model to be ready, and one whose chain names an `ollama` model
    still does.
    """

    def _readiness(self, monkeypatch, *, chain, available):
        import asyncio

        from robothor.api import orchestrator

        monkeypatch.setattr(orchestrator, "fleet_uses_ollama", lambda: bool(chain))

        async def _available(*_args, **_kwargs):
            return available

        monkeypatch.setattr("robothor.llm.ollama.check_model_available", _available)

        async def _db():
            return "ok"

        # Strict setattr, no `raising=False`: an earlier revision stubbed a name
        # (`_database_check`) that `readiness()` never read, so the real check
        # ran, opened a real connection, and every expectation of 200 came back
        # 503 on any host without Postgres -- which is every CI unit runner.
        # Patching a name that does not exist must fail loudly, not silently.
        monkeypatch.setattr(orchestrator, "_check_database", _db)
        response = asyncio.run(orchestrator.readiness())
        import json

        return json.loads(bytes(response.body).decode()), response.status_code

    def test_a_cloud_only_fleet_is_ready_without_ollama(self, monkeypatch):
        body, status = self._readiness(monkeypatch, chain=False, available=False)

        assert status == 200
        assert "generation_model" not in body["checks"]
        assert "skipped" in body["generation_model"]

    def test_a_fleet_that_routes_through_ollama_still_requires_the_model(self, monkeypatch):
        body, status = self._readiness(monkeypatch, chain=True, available=False)

        assert status == 503
        assert body["checks"]["generation_model"].startswith("error:")

    def test_a_fleet_that_routes_through_ollama_passes_when_the_model_is_there(self, monkeypatch):
        body, status = self._readiness(monkeypatch, chain=True, available=True)

        assert status == 200
        assert body["checks"]["generation_model"] == "ok"

    def test_the_database_stub_is_the_check_that_actually_runs(self, monkeypatch):
        """The readiness tests must not need a database to say 200.

        `readiness()` built its database probe as a local closure, so there was
        no name a test could replace: the stub above was inert and the endpoint
        dialled Postgres for real. Detonating `get_connection` proves the stub
        is now the code path -- if the seam regresses to a closure, this test
        raises instead of quietly passing on whatever host happens to have a
        database.
        """
        import robothor.db.connection as db_connection

        def _explode(*_args, **_kwargs):
            raise AssertionError("readiness opened a real database connection")

        monkeypatch.setattr(db_connection, "get_connection", _explode)

        body, status = self._readiness(monkeypatch, chain=False, available=False)

        assert status == 200
        assert body["checks"]["database"] == "ok"


class TestTheFleetChainPredicate:
    def test_an_ollama_chat_primary_counts(self, tmp_path, monkeypatch):
        from robothor.engine.config import fleet_uses_ollama

        (tmp_path / "_defaults.yaml").write_text(
            "model:\n  primary: ollama_chat/qwen3:8b\n  fallbacks: []\n", encoding="utf-8"
        )
        monkeypatch.setenv("ROBOTHOR_MANIFEST_DIR", str(tmp_path))

        assert fleet_uses_ollama(tmp_path) is True

    def test_a_cloud_only_chain_does_not(self, tmp_path):
        from robothor.engine.config import fleet_uses_ollama

        (tmp_path / "_defaults.yaml").write_text(
            "model:\n  primary: openrouter/openai/gpt-5.4\n"
            "  fallbacks: [openrouter/anthropic/claude-sonnet-4.6]\n",
            encoding="utf-8",
        )

        assert fleet_uses_ollama(tmp_path) is False

    def test_a_local_last_resort_model_counts_even_though_no_manifest_names_it(
        self, tmp_path, monkeypatch
    ):
        """`_with_last_resort` appends it to every chain, so it IS in the chain."""
        from robothor.engine.config import fleet_uses_ollama

        (tmp_path / "_defaults.yaml").write_text(
            "model:\n  primary: openrouter/openai/gpt-5.4\n", encoding="utf-8"
        )
        monkeypatch.setenv("ROBOTHOR_LAST_RESORT_MODEL", "ollama_chat/qwen3.8:27b")

        assert fleet_uses_ollama(tmp_path) is True

    def test_an_unreadable_fleet_reads_as_using_ollama(self, tmp_path):
        """Unknown is not "unused": probing costs one loopback request, and
        reporting ready on an instance that cannot generate costs an outage."""
        from robothor.engine.config import fleet_uses_ollama

        assert fleet_uses_ollama(tmp_path / "nowhere") is True

    def test_dispatch_and_readiness_read_the_same_last_resort_model(self, tmp_path, monkeypatch):
        """One declared name, one reader.

        `_with_last_resort` (dispatch) read the raw environment while
        `fleet_model_chain` (readiness) read the declared
        `ProviderSettings.last_resort_model`. The two disagree for exactly the
        instance `genus init` produces -- the name lands in `config.yaml`, not
        in the environment -- so readiness would gate on a local model that
        dispatch was never going to dial, or skip one that it was.
        """
        from robothor.engine.config import _with_last_resort, fleet_model_chain

        (tmp_path / ".robothor").mkdir()
        (tmp_path / ".robothor" / "config.yaml").write_text(
            "settings:\n  providers:\n    last_resort_model: ollama_chat/qwen3:8b\n",
            encoding="utf-8",
        )
        (tmp_path / "_defaults.yaml").write_text(
            "model:\n  primary: openrouter/openai/gpt-5.4\n", encoding="utf-8"
        )
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ROBOTHOR_LAST_RESORT_MODEL", raising=False)

        assert fleet_model_chain(tmp_path) == [
            "openrouter/openai/gpt-5.4",
            "ollama_chat/qwen3:8b",
        ]
        assert _with_last_resort("openrouter/openai/gpt-5.4", []) == ["ollama_chat/qwen3:8b"]

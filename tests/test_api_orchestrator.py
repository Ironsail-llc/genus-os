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
        assert "Robothor" in app.title

    def test_app_has_routes(self):
        route_paths = [r.path for r in app.routes]
        assert "/health" in route_paths
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

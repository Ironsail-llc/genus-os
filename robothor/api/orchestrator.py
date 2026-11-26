"""
RAG Orchestrator — FastAPI service tying together all pipeline components.

Architecture:
  Query -> Classify -> Select RAG Profile -> Parallel Retrieve (memory + web)
  -> Merge -> Rerank -> Context Inject -> Generate
  -> Return response with citations

Exposes an OpenAI-compatible /v1/chat/completions endpoint.

Start:
  robothor serve
  # or
  uvicorn robothor.api.orchestrator:app --host 0.0.0.0 --port 9099
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from robothor import __version__
from robothor.engine.config import fleet_uses_ollama
from robothor.rag.pipeline import run_pipeline
from robothor.rag.profiles import RAG_PROFILES

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────


def _default_vision_url() -> str:
    try:
        from robothor.config import get_config

        return get_config().vision_url
    except Exception:
        return "http://127.0.0.1:8600"


VISION_SERVICE_URL = os.environ.get("VISION_SERVICE_URL") or _default_vision_url()

# ─── FastAPI App ──────────────────────────────────────────────────────


def _harden_at_import() -> None:
    """Make this process's /proc entries root-only, at import time.

    Called at module scope because uvicorn owns the entry point: there is no
    function of ours that runs first. Never raises — an import that dies takes
    the service with it, and this is a mitigation, not a dependency.
    """
    try:
        from robothor.engine.process_hardening import harden_process

        harden_process()
    except Exception as e:  # noqa: BLE001 - a hardening step must never block a boot
        logger.warning("Process hardening skipped: %s: %s", type(e).__name__, e)


app = FastAPI(
    title="Genus OS RAG Orchestrator",
    description="Hybrid RAG pipeline with memory search, web search, reranking, and LLM generation.",
    version="1.0.0",
)

# At IMPORT, not on a startup event. uvicorn imports this module to find `app`,
# and the process is the operator's uid from that moment — an agent's `exec`
# child could read its environment out of /proc before any startup hook fired.
# The orchestrator is started as `python -m uvicorn robothor.api.orchestrator:app`,
# so there is no entry function of ours to put this in.
_harden_at_import()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic Models ─────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


class ChatRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model: str = "default"
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    # Custom extensions
    profile: str | None = Field(
        None, description="RAG profile: fast, general, research, expert, heavy, code"
    )
    use_memory: bool = Field(True, description="Search memory database")
    use_web: bool = Field(True, description="Search the web")


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str = "default"
    choices: list[ChatChoice]
    usage: UsageInfo = UsageInfo()
    rag_metadata: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    """Simple query request for the /query endpoint."""

    question: str
    profile: str | None = None
    memory_limit: int | None = None
    web_limit: int | None = None


class IngestRequest(BaseModel):
    """Request body for the /ingest endpoint."""

    content: str
    source_channel: str = "api"
    content_type: str = "conversation"
    metadata: dict[str, Any] | None = None


class VisionLookRequest(BaseModel):
    prompt: str = Field(
        default="Describe what you see in this image in detail.", description="What to analyze"
    )


class VisionEnrollRequest(BaseModel):
    name: str = Field(..., description="Name of the person to enroll")


class VisionModeRequest(BaseModel):
    mode: str = Field(..., description="Vision mode: disarmed, basic, or armed")


class VisionEnrollFromImageRequest(BaseModel):
    name: str = Field(..., description="Name of the person to enroll")
    image_paths: list[str] = Field(..., description="List of image file paths")


class VisionSuppressionRequest(BaseModel):
    suppressed: bool = Field(..., description="True to suppress alerts, False to enable")


# ─── Acting tenant ───────────────────────────────────────────────────


#: A JSON Web Token: three base64url segments. Anything else in a bearer is
#: not one of our tokens -- an OpenAI-compatible client (Open WebUI, LiteLLM,
#: the openai SDK) always sends ``Bearer <api key>`` and will not start without
#: a key, so operators give it a placeholder.
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


async def acting_tenant(request: Request, *, scope: str) -> str:
    """The tenant a memory endpoint acts as, from the caller.

    1. A bearer JWT is verified (bridge audience: the dashboard forwards the
       session's bridge token, and services send a minted service token) and its
       tenant is the tenant. Then, as on the bridge: an invalid token is a 401;
       a token without ``scope`` (``bridge:read`` to read, ``bridge:write`` to
       ingest) is a 403; a session that no longer stands
       (``robothor.auth.session_standing.refusal``) is refused; an
       ``X-Tenant-Id`` that contradicts the token is a 403.
    2. Without a token -- and a bearer that is not a JWT, such as an OpenAI
       client's placeholder key, is no token --
       :func:`robothor.tenancy.acting.resolve_acting_tenant` decides: the
       default tenant on a single-tenant deployment, otherwise a 400 refusal
       rather than another tenant's memory.

    A bare ``X-Tenant-Id`` never selects a tenant. Without a token it may only
    restate what step 2 resolves to; naming any other tenant is a 401. An
    unauthenticated caller of this port could only ever reach the default
    tenant, and honouring the header would hand it every tenant by name.

    The handler must then run its memory calls inside ``tenant_scope`` of the
    returned tenant: this one process serves every tenant, and under row-level
    security a connection bound to the process's own tenant cannot see or write
    another's rows.
    """
    from robothor.auth import session_standing
    from robothor.auth.deps import bearer_from_header, verify_token
    from robothor.auth.tokens import TokenError
    from robothor.tenancy.acting import TenantRequiredError, resolve_acting_tenant

    header = (request.headers.get("x-tenant-id") or "").strip()
    authorization = request.headers.get("authorization")
    token = bearer_from_header(authorization) if authorization else None
    if authorization and token is None:
        raise HTTPException(status_code=401, detail="invalid authorization header")
    if token and _JWT_SHAPE.fullmatch(token):
        try:
            auth = verify_token(token, expected_audience="genus-bridge")
        except TokenError as error:
            raise HTTPException(status_code=401, detail="invalid or expired token") from error
        if not auth.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"token lacks {scope}")
        refused = await asyncio.to_thread(session_standing.refusal, auth)
        if refused is not None:
            raise HTTPException(status_code=refused[0], detail=refused[1])
        if header and header != auth.tenant_id:
            raise HTTPException(status_code=403, detail="tenant not authorized")
        return auth.tenant_id

    token_required = HTTPException(
        status_code=401,
        detail="a bearer token is required to act as a named tenant; "
        "X-Tenant-Id alone does not select one",
    )
    try:
        resolved = await asyncio.to_thread(
            resolve_acting_tenant, "", surface=f"orchestrator {request.url.path}"
        )
    except TenantRequiredError as refused_tenant:
        if header:
            raise token_required from refused_tenant
        raise HTTPException(
            status_code=400, detail=f"{refused_tenant} Send a bearer token for the tenant."
        ) from refused_tenant
    if header and header != resolved:
        raise token_required
    return resolved


async def reading_tenant(request: Request) -> str:
    """:func:`acting_tenant` for an endpoint that reads memory."""
    return await acting_tenant(request, scope="bridge:read")


async def writing_tenant(request: Request) -> str:
    """:func:`acting_tenant` for an endpoint that writes memory."""
    return await acting_tenant(request, scope="bridge:write")


# ─── Core Endpoints ──────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check — reports status of all components."""
    from robothor.llm.ollama import check_model_available
    from robothor.rag.reranker import check_reranker_available
    from robothor.rag.web_search import check_searxng_available

    model_ok = await check_model_available()
    reranker_ok = await check_reranker_available()
    searxng_ok = await check_searxng_available()

    return {
        "status": "ok" if model_ok else "degraded",
        "components": {
            "generation_model": {"available": model_ok},
            "reranker": {"available": reranker_ok},
            "web_search": {"available": searxng_ok},
            "memory_db": {"available": True},
        },
        # Optional dependencies sit beside `components`, not in it: consumers
        # (the dashboard among them) read any component with available=false
        # as a failing check, and vision is not required for this service.
        "optional": {"vision": dict(_vision_state)},
    }


@app.get("/live")
@app.get("/liveness")
async def liveness() -> dict[str, Any]:
    """Process-only liveness; ``/liveness`` remains a compatibility alias."""
    from robothor.health_contract import liveness_response

    return liveness_response("orchestrator", __version__)


async def _check_database() -> str:
    """Round-trip one trivial query through a pooled connection.

    Module level rather than a closure inside :func:`readiness` on purpose: a
    closure has no name a test can replace, so the readiness tests could only
    stub something `readiness()` never read and then dialled a real Postgres --
    passing on a developer box and failing on every runner without a database.
    A probe that reaches out over the network needs a seam.
    """
    import asyncio

    from robothor.db.connection import get_connection

    def _query() -> None:
        with get_connection() as conn:
            conn.cursor().execute("SELECT 1")

    await asyncio.to_thread(_query)
    return "ok"


async def _check_generation_model() -> str:
    """Whether the local Ollama holds a model the fleet could generate with."""
    from robothor.llm.ollama import check_model_available

    if not await check_model_available():
        raise RuntimeError("generation model unavailable")
    return "ok"


@app.get("/ready")
async def readiness() -> Any:
    """Report whether the database and required generation model are usable."""
    from fastapi.responses import JSONResponse

    from robothor.health_contract import readiness_response

    checks: dict[str, Any] = {"database": _check_database}
    details: dict[str, Any] = {}

    # Only an instance that actually routes a tier through Ollama needs a
    # local model to be ready. Gating unconditionally is how the compose stack
    # became unstartable on a fresh machine: the Ollama container comes up
    # empty, the wizard pulls into it only AFTER `up`, and bridge and dashboard
    # both wait on this service being healthy -- so `docker compose up` exited
    # 1 and nothing else ever started. The readiness contract has no "skipped",
    # so the reason goes in the body rather than in `checks`.
    if fleet_uses_ollama():
        checks["generation_model"] = _check_generation_model
    else:
        details["generation_model"] = (
            "skipped: no local generation model configured "
            "(no model in the fleet's chain routes through Ollama)"
        )

    body, status = await readiness_response("orchestrator", __version__, checks, details)
    return JSONResponse(body, status_code=status)


@app.post("/query")
async def query_endpoint(req: QueryRequest, tenant_id: str = Depends(reading_tenant)) -> Any:
    """Simple RAG query endpoint, searching the caller's tenant's memory."""
    from robothor.db.connection import tenant_scope

    with tenant_scope(tenant_id):
        result = await run_pipeline(query=req.question, profile=req.profile, tenant_id=tenant_id)
    return result


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, tenant_id: str = Depends(reading_tenant)) -> Any:
    """OpenAI-compatible chat completions endpoint.

    Supports the standard OpenAI API format so tools like Open WebUI,
    LiteLLM, and other OpenAI-compatible clients can connect directly.
    """
    last_user = ""
    for m in reversed(req.messages):
        if m.role == "user":
            last_user = m.content
            break

    if not last_user:
        raise HTTPException(status_code=400, detail="No user message found")

    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    from robothor.db.connection import tenant_scope

    with tenant_scope(tenant_id):
        result = await run_pipeline(
            query=last_user,
            profile=req.profile,
            messages=messages,
            tenant_id=tenant_id,
        )

    response = ChatResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created=int(time.time()),
        model=req.model,
        choices=[
            ChatChoice(
                message=ChatMessage(role="assistant", content=result["answer"]),
            )
        ],
        rag_metadata={
            "profile": result["profile"],
            "memories_found": result["memories_found"],
            "web_results_found": result["web_results_found"],
            "timing": result["timing"],
            "sources": result["sources"],
        },
    )

    if req.stream:

        async def stream_response() -> AsyncGenerator[str, None]:
            chunk = {
                "id": response.id,
                "object": "chat.completion.chunk",
                "created": response.created,
                "model": response.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": result["answer"]},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_response(), media_type="text/event-stream")

    return response


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """OpenAI-compatible model listing."""
    return {
        "object": "list",
        "data": [
            {
                "id": "default",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local",
            }
        ],
    }


@app.get("/profiles")
async def list_profiles() -> Any:
    """List available RAG profiles."""
    return RAG_PROFILES


@app.get("/stats")
async def stats(tenant_id: str = Depends(reading_tenant)) -> dict[str, Any]:
    """Memory system statistics for the caller's tenant."""
    from robothor.db.connection import tenant_scope
    from robothor.memory.facts import get_memory_stats

    # to_thread copies the context, so the worker's connections bind to the scope.
    with tenant_scope(tenant_id):
        return await asyncio.to_thread(get_memory_stats, tenant_id=tenant_id)


@app.post("/ingest")
async def ingest_endpoint(req: IngestRequest, tenant_id: str = Depends(writing_tenant)) -> Any:
    """Ingest content from any channel, into the caller's tenant.

    The channel the caller names is a claim: what this route stores is never
    trusted above external content.
    """
    from robothor.db.connection import tenant_scope
    from robothor.memory.graph_schema import Origin
    from robothor.memory.ingestion import ingest_content

    try:
        with tenant_scope(tenant_id):
            result = await ingest_content(
                content=req.content,
                source_channel=req.source_channel,
                content_type=req.content_type,
                metadata=req.metadata,
                tenant_id=tenant_id,
                origin_cap=Origin.EXTERNAL,
            )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ─── Vision Proxy Endpoints ──────────────────────────────────────────


def _vision_url(path: str) -> str:
    return f"{VISION_SERVICE_URL}{path}"


#: What the last proxied vision call found. ``available`` is None until a call
#: has been made; /health reports it so a dead vision service is visible.
_vision_state: dict[str, Any] = {"available": None, "last_error": None}


def _vision_reached() -> None:
    _vision_state.update(available=True, last_error=None)


def _vision_unreachable(path: str, exc: Exception) -> None:
    """Record and log a failed vision call. WARNING on the first, DEBUG while it stays down."""
    error = f"{path}: {type(exc).__name__}: {exc}"
    level = logging.DEBUG if _vision_state["available"] is False else logging.WARNING
    logger.log(level, "Vision service unreachable at %s", error)
    _vision_state.update(available=False, last_error=error)


@app.post("/vision/look")
async def vision_look(req: VisionLookRequest | None = None) -> Any:
    """Proxy to vision service — capture and analyze a snapshot."""
    if req is None:
        req = VisionLookRequest()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_vision_url("/look"), json={"prompt": req.prompt})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Vision service error: {e}") from e


@app.post("/vision/detect")
async def vision_detect() -> Any:
    """Proxy to vision service — run object detection."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_vision_url("/detections"))
            if resp.status_code == 200:
                _vision_reached()
                return resp.json()
    except Exception as e:  # noqa: BLE001 - the route answers "not available" instead
        _vision_unreachable("/detections", e)
    raise HTTPException(status_code=503, detail="Vision service not available")


@app.post("/vision/identify")
async def vision_identify() -> Any:
    """Proxy to vision service — face identification."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_vision_url("/identifications"))
            if resp.status_code == 200:
                _vision_reached()
                return resp.json()
    except Exception as e:  # noqa: BLE001 - the route answers "not available" instead
        _vision_unreachable("/identifications", e)
    raise HTTPException(status_code=503, detail="Vision service not available")


@app.get("/vision/status")
async def vision_status() -> Any:
    """Get the vision service status."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_vision_url("/health"))
            if resp.status_code == 200:
                _vision_reached()
                return resp.json()
    except Exception as e:  # noqa: BLE001 - the route answers "not available" instead
        _vision_unreachable("/health", e)
    return {"running": False, "people_present": [], "last_detection": None}


@app.post("/vision/enroll")
async def vision_enroll(req: VisionEnrollRequest) -> Any:
    """Proxy to vision service — enroll a face for recognition."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_vision_url("/enroll"), json={"name": req.name})
            if resp.status_code == 200:
                return resp.json()
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Vision service not running") from e


@app.get("/vision/mode")
async def vision_mode_get() -> Any:
    """Get the current vision mode."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_vision_url("/mode"))
            if resp.status_code == 200:
                _vision_reached()
                return resp.json()
    except Exception as e:  # noqa: BLE001 - the route answers "not available" instead
        _vision_unreachable("/mode", e)
    return {"mode": "unknown", "error": "Vision service not reachable"}


@app.post("/vision/mode")
async def vision_mode_set(req: VisionModeRequest) -> Any:
    """Switch the vision mode (disarmed, basic, armed, disabled)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_vision_url("/mode"), json={"mode": req.mode})
            if resp.status_code == 200:
                return resp.json()
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Vision service not reachable") from e


@app.post("/vision/enroll-from-image")
async def vision_enroll_from_image(req: VisionEnrollFromImageRequest) -> Any:
    """Enroll a face from saved image files (snapshots, photos)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                _vision_url("/enroll-from-image"),
                json={"name": req.name, "image_paths": req.image_paths},
            )
            if resp.status_code == 200:
                return resp.json()
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Vision service not running") from e


@app.get("/vision/enrolled")
async def vision_enrolled() -> Any:
    """List all enrolled faces."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_vision_url("/enrolled"))
            if resp.status_code == 200:
                _vision_reached()
                return resp.json()
    except Exception as e:  # noqa: BLE001 - the route answers "not available" instead
        _vision_unreachable("/enrolled", e)
    return {"enrolled_faces": [], "count": 0, "error": "Vision service not reachable"}


@app.post("/vision/unenroll")
async def vision_unenroll(req: VisionEnrollRequest) -> Any:
    """Remove a person from face enrollment."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(_vision_url("/unenroll"), json={"name": req.name})
            if resp.status_code == 200:
                return resp.json()
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Vision service not running") from e

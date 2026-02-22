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

import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from robothor.rag.pipeline import run_pipeline
from robothor.rag.profiles import RAG_PROFILES

# ─── Configuration ────────────────────────────────────────────────────

VISION_SERVICE_URL = os.environ.get("VISION_SERVICE_URL", "http://localhost:8600")

# ─── FastAPI App ──────────────────────────────────────────────────────

app = FastAPI(
    title="Robothor RAG Orchestrator",
    description="Hybrid RAG pipeline with memory search, web search, reranking, and LLM generation.",
    version="1.0.0",
)

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
    profile: str | None = Field(None, description="RAG profile: fast, general, research, expert, heavy, code")
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
    prompt: str = Field(default="Describe what you see in this image in detail.", description="What to analyze")


class VisionEnrollRequest(BaseModel):
    name: str = Field(..., description="Name of the person to enroll")


class VisionModeRequest(BaseModel):
    mode: str = Field(..., description="Vision mode: disarmed, basic, or armed")


# ─── Core Endpoints ──────────────────────────────────────────────────


@app.get("/health")
async def health():
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
    }


@app.post("/query")
async def query_endpoint(req: QueryRequest):
    """Simple RAG query endpoint."""
    result = await run_pipeline(query=req.question, profile=req.profile)
    return result


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
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

    result = await run_pipeline(
        query=last_user,
        profile=req.profile,
        messages=messages,
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
        async def stream_response():
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
async def list_models():
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
async def list_profiles():
    """List available RAG profiles."""
    return RAG_PROFILES


@app.get("/stats")
async def stats():
    """Memory system statistics."""
    from robothor.memory.tiers import get_memory_stats
    return get_memory_stats()


@app.post("/ingest")
async def ingest_endpoint(req: IngestRequest):
    """Ingest content from any channel."""
    from robothor.memory.ingestion import ingest_content

    try:
        result = await ingest_content(
            content=req.content,
            source_channel=req.source_channel,
            content_type=req.content_type,
            metadata=req.metadata,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ─── Vision Proxy Endpoints ──────────────────────────────────────────


def _vision_url(path: str) -> str:
    return f"{VISION_SERVICE_URL}{path}"


@app.post("/vision/look")
async def vision_look(req: VisionLookRequest | None = None):
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
async def vision_detect():
    """Proxy to vision service — run object detection."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_vision_url("/detections"))
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="Vision service not available")


@app.post("/vision/identify")
async def vision_identify():
    """Proxy to vision service — face identification."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_vision_url("/identifications"))
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="Vision service not available")


@app.get("/vision/status")
async def vision_status():
    """Get the vision service status."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_vision_url("/health"))
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {"running": False, "people_present": [], "last_detection": None}


@app.post("/vision/enroll")
async def vision_enroll(req: VisionEnrollRequest):
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
async def vision_mode_get():
    """Get the current vision mode."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_vision_url("/mode"))
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {"mode": "unknown", "error": "Vision service not reachable"}


@app.post("/vision/mode")
async def vision_mode_set(req: VisionModeRequest):
    """Switch the vision mode (disarmed, basic, armed)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_vision_url("/mode"), json={"mode": req.mode})
            if resp.status_code == 200:
                return resp.json()
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Vision service not reachable") from e

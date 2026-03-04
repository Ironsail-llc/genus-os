#!/usr/bin/env python3
"""
RAG Orchestrator — FastAPI service tying together all pipeline components.

Architecture:
  Query → Classify → Select RAG Profile → Parallel Retrieve (memory + web)
  → Merge → Rerank → Context Inject → Generate via Qwen3-80B
  → Return response with citations

Exposes an OpenAI-compatible /v1/chat/completions endpoint.

Start:
  cd /home/philip/robothor/brain/memory_system
  source venv/bin/activate
  uvicorn orchestrator:app --host 0.0.0.0 --port 9099
"""

import asyncio
import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from reranker import check_reranker_available, rerank_with_fallback
from web_search import (
    check_searxng_available,
    search_web,
    web_results_to_memory_format,
)

from robothor.llm.ollama import (
    GENERATION_MODEL,
    analyze_image,
    chat,
    check_model_available,
    detect_generation_model,
    generate,
)
from robothor.memory.facts import search_facts_compat as search_all_memory

# ============== RAG Profiles ==============

RAG_PROFILES = {
    "fast": {
        "description": "Quick answers, minimal retrieval",
        "memory_limit": 5,
        "web_limit": 3,
        "rerank_top_k": 5,
        "temperature": 0.6,
        "max_tokens": 1024,
        "use_reranker": True,
        "use_web": True,
    },
    "general": {
        "description": "Balanced retrieval and generation",
        "memory_limit": 15,
        "web_limit": 5,
        "rerank_top_k": 10,
        "temperature": 0.7,
        "max_tokens": 4096,
        "use_reranker": True,
        "use_web": True,
    },
    "research": {
        "description": "Deep retrieval, more context, thorough answers",
        "memory_limit": 30,
        "web_limit": 15,
        "rerank_top_k": 15,
        "temperature": 0.5,
        "max_tokens": 8192,
        "use_reranker": True,
        "use_web": True,
    },
    "expert": {
        "description": "Expert-level deep analysis with extensive retrieval",
        "memory_limit": 25,
        "web_limit": 50,
        "rerank_top_k": 25,
        "temperature": 0.45,
        "max_tokens": 8192,
        "use_reranker": True,
        "use_web": True,
    },
    "heavy": {
        "description": "Maximum retrieval, broadest context window",
        "memory_limit": 30,
        "web_limit": 100,
        "rerank_top_k": 30,
        "temperature": 0.5,
        "max_tokens": 8192,
        "use_reranker": True,
        "use_web": True,
    },
    "code": {
        "description": "Code-focused, precise generation",
        "memory_limit": 15,
        "web_limit": 10,
        "rerank_top_k": 10,
        "temperature": 0.6,
        "max_tokens": 4096,
        "use_reranker": True,
        "use_web": True,
    },
}


# ============== Query Classification ==============

CLASSIFICATION_RULES = {
    "code": [
        "code",
        "function",
        "class",
        "def ",
        "import ",
        "error",
        "bug",
        "traceback",
        "syntax",
        "compile",
        "debug",
        "python",
        "javascript",
        "typescript",
        "rust",
        "bash",
        "script",
        "api",
        "endpoint",
        "database",
        "sql",
        "query",
        "docker",
        "git",
        "deploy",
    ],
    "research": [
        "explain in detail",
        "how does",
        "why does",
        "compare",
        "difference between",
        "pros and cons",
        "research",
        "paper",
        "study",
        "analysis",
        "deep dive",
        "architecture",
        "design",
        "theory",
        "concept",
        "history of",
        "explain how",
        "explain why",
    ],
    "expert": [
        "expert",
        "comprehensive",
        "thorough analysis",
        "in depth",
        "detailed breakdown",
        "technical deep dive",
        "evaluate",
        "critical analysis",
        "systematic review",
        "benchmark",
    ],
    "heavy": [
        "everything about",
        "all information",
        "exhaustive",
        "complete overview",
        "full report",
        "extensive search",
        "gather all",
        "maximum detail",
        "leave nothing out",
    ],
    "fast": [
        "what time",
        "weather",
        "quick",
        "simple",
        "yes or no",
        "one word",
        "briefly",
        "tldr",
        "summary",
        "short answer",
    ],
}


def classify_query(query: str) -> str:
    """Classify a query into a RAG profile.

    Args:
        query: The user's query text.

    Returns:
        Profile name: 'fast', 'code', 'research', 'expert', 'heavy', or 'general'.
    """
    query_lower = query.lower()

    scores = {profile: 0 for profile in CLASSIFICATION_RULES}
    for profile, keywords in CLASSIFICATION_RULES.items():
        for kw in keywords:
            if kw in query_lower:
                scores[profile] += 1

    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best
    return "general"


# ============== Context Formatting ==============

SYSTEM_PROMPT = """You are Robothor, an AI assistant with access to a personal memory system and web search.
You have been given relevant context from memory and/or web search below.
Use this context to answer the user's question accurately.
If the context doesn't contain relevant information, say so and answer from general knowledge.
Cite your sources when applicable — reference memory entries or web URLs."""


def format_merged_context(
    memory_results: list[dict],
    web_results: list[dict],
    max_chars: int = 40000,
) -> str:
    """Format merged memory + web results into a context block.

    Args:
        memory_results: Reranked memory search results.
        web_results: Web search results (already in memory format or raw).
        max_chars: Maximum total characters for context.

    Returns:
        Formatted context string.
    """
    parts = []
    total = 0

    # Memory results first
    for i, r in enumerate(memory_results, 1):
        content = r.get("content", "")
        tier = r.get("tier", "unknown")
        sim = r.get("similarity", 0)
        rerank = r.get("rerank_score", None)
        ctype = r.get("content_type", "unknown")

        score_str = f"sim={sim:.3f}"
        rerank_rel = r.get("rerank_relevant", None)
        if rerank_rel is not None:
            score_str += f", relevant={rerank_rel}"
        elif rerank is not None:
            score_str += f", rerank={rerank:.3f}"

        entry = f"[Memory {i}] ({tier}, {ctype}, {score_str})\n{content}"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    # Web results
    for i, r in enumerate(web_results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")

        entry = f"[Web {i}] {title}\nURL: {url}\n{content}"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    if not parts:
        return "No relevant context found."

    return "\n\n---\n\n".join(parts)


# ============== Core Pipeline ==============


async def run_pipeline(
    query: str,
    profile: str | None = None,
    messages: list[dict] | None = None,
) -> dict:
    """Run the full RAG pipeline.

    Steps:
      1. Classify query → select profile
      2. Parallel retrieval (memory + web)
      3. Merge results
      4. Rerank
      5. Inject context into prompt
      6. Generate via Qwen3-80B

    Args:
        query: The user's question.
        profile: Optional profile override ('fast', 'general', 'research', 'code').
        messages: Optional chat history for multi-turn.

    Returns:
        Dict with 'answer', 'profile', 'sources', 'timing'.
    """
    t0 = time.time()

    # Step 1: Classify
    selected_profile = profile or classify_query(query)
    p = RAG_PROFILES.get(selected_profile, RAG_PROFILES["general"])

    # Step 2: Parallel retrieval
    retrieval_tasks = []

    # Memory search (always)
    retrieval_tasks.append(
        asyncio.to_thread(
            search_all_memory,
            query,
            limit=p["memory_limit"],
        )
    )

    # Web search (if enabled)
    async def _no_web():
        return []

    if p["use_web"]:
        retrieval_tasks.append(search_web(query, limit=p["web_limit"]))
    else:
        retrieval_tasks.append(_no_web())

    # Run in parallel
    results = await asyncio.gather(*retrieval_tasks, return_exceptions=True)

    memory_results = results[0] if not isinstance(results[0], Exception) else []
    raw_web_results = (
        results[1] if len(results) > 1 and not isinstance(results[1], Exception) else []
    )

    t_retrieval = time.time() - t0

    # Step 3: Merge — convert web results to memory format for reranking
    web_as_memory = web_results_to_memory_format(raw_web_results) if raw_web_results else []
    all_results = memory_results + web_as_memory

    # Step 4: Rerank
    t_rerank_start = time.time()
    if p["use_reranker"] and len(all_results) > p["rerank_top_k"]:
        reranked = await rerank_with_fallback(query, all_results, top_k=p["rerank_top_k"])
    else:
        reranked = all_results[: p["rerank_top_k"]]
    t_rerank = time.time() - t_rerank_start

    # Separate back into memory and web for formatting
    reranked_memory = [r for r in reranked if r.get("tier") != "web"]
    reranked_web = [r for r in reranked if r.get("tier") == "web"]

    # Step 5: Format context
    context = format_merged_context(
        reranked_memory, raw_web_results if not reranked_web else reranked_web
    )

    # Step 6: Generate
    t_gen_start = time.time()

    if messages:
        # Multi-turn: inject context as system message
        system_msg = f"{SYSTEM_PROMPT}\n\n## Retrieved Context\n{context}"
        chat_messages = [{"role": "system", "content": system_msg}] + messages
        answer = await chat(
            messages=chat_messages,
            temperature=p["temperature"],
            max_tokens=p["max_tokens"],
        )
    else:
        # Single query
        augmented_prompt = (
            f"## Retrieved Context\n{context}\n\n"
            f"## Question\n{query}\n\n"
            f"Answer using the context above. Cite sources when applicable."
        )
        answer = await generate(
            prompt=augmented_prompt,
            system=SYSTEM_PROMPT,
            temperature=p["temperature"],
            max_tokens=p["max_tokens"],
        )

    t_gen = time.time() - t_gen_start
    t_total = time.time() - t0

    return {
        "answer": answer,
        "profile": selected_profile,
        "query": query,
        "memories_found": len(memory_results),
        "web_results_found": len(raw_web_results),
        "reranked_count": len(reranked),
        "timing": {
            "retrieval_ms": round(t_retrieval * 1000),
            "rerank_ms": round(t_rerank * 1000),
            "generation_ms": round(t_gen * 1000),
            "total_ms": round(t_total * 1000),
        },
        "sources": {
            "memory": [
                {
                    "tier": r.get("tier"),
                    "type": r.get("content_type"),
                    "similarity": round(r.get("similarity", 0), 4),
                    "rerank_relevant": r.get("rerank_relevant"),
                    "preview": r.get("content", "")[:100],
                }
                for r in reranked_memory[:5]
            ],
            "web": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", r.get("metadata", {}).get("url", "")),
                }
                for r in (raw_web_results if not reranked_web else reranked_web)[:5]
            ],
        },
    }


# ============== FastAPI App ==============

app = FastAPI(
    title="Robothor RAG Orchestrator",
    description="Hybrid RAG pipeline with memory search, web search, reranking, and Qwen3-80B generation.",
    version="1.0.0",
)


@app.on_event("startup")
async def startup_detect_model():
    """Auto-detect the generation model on startup."""
    detected = await detect_generation_model()
    if detected:
        print(f"Auto-detected generation model: {detected}")
    else:
        print("WARNING: No generation model detected. Pull one: ollama pull qwen3:80b")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic Models ---


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


class ChatRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model: str = "qwen3-80b"
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    # Custom extensions
    profile: str | None = Field(
        None, description="RAG profile override: fast, general, research, expert, heavy, code"
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
    model: str = "qwen3-80b"
    choices: list[ChatChoice]
    usage: UsageInfo = UsageInfo()
    # Custom extensions
    rag_metadata: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    """Simple query request for the /query endpoint."""

    question: str
    profile: str | None = None
    memory_limit: int | None = None
    web_limit: int | None = None


# --- Endpoints ---


@app.get("/health")
async def health():
    """Health check — reports status of all components."""
    model_ok = await check_model_available()
    reranker_ok = await check_reranker_available()
    searxng_ok = await check_searxng_available()

    return {
        "status": "ok" if model_ok else "degraded",
        "components": {
            "generation_model": {"available": model_ok, "model": GENERATION_MODEL},
            "reranker": {"available": reranker_ok},
            "web_search": {"available": searxng_ok},
            "memory_db": {"available": True},  # If we got here, DB is fine
        },
    }


@app.post("/query")
async def query_endpoint(req: QueryRequest):
    """Simple RAG query endpoint.

    Takes a question, searches memory + web, reranks, generates an answer.
    """
    result = await run_pipeline(
        query=req.question,
        profile=req.profile,
    )
    return result


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    """OpenAI-compatible chat completions endpoint.

    Supports the standard OpenAI API format so tools like Open WebUI,
    LiteLLM, and other OpenAI-compatible clients can connect directly.
    """
    # Extract the last user message for search
    last_user = ""
    for m in reversed(req.messages):
        if m.role == "user":
            last_user = m.content
            break

    if not last_user:
        raise HTTPException(status_code=400, detail="No user message found")

    # Build messages list
    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    # Run pipeline
    result = await run_pipeline(
        query=last_user,
        profile=req.profile,
        messages=messages,
    )

    # Format as OpenAI-compatible response
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
        # Streaming mode — SSE format
        async def stream_response():
            # For now, send the full response as a single chunk
            # (true streaming would require streaming from Ollama)
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

        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
        )

    return response


@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible model listing."""
    return {
        "object": "list",
        "data": [
            {
                "id": "qwen3-80b",
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
    from robothor.memory.facts import get_memory_stats

    return get_memory_stats()


# --- Ingestion Endpoint ---


class IngestRequest(BaseModel):
    """Request body for the /ingest endpoint."""

    content: str
    source_channel: str = "api"
    content_type: str = "conversation"
    metadata: dict[str, Any] | None = None


@app.post("/ingest")
async def ingest_endpoint(req: IngestRequest):
    """Ingest content from any channel.

    Extracts facts, runs conflict resolution, and stores in memory.
    """
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
        raise HTTPException(status_code=400, detail=str(e))


# ============== Vision Endpoints ==============

import base64
import subprocess
from pathlib import Path

SNAPSHOT_DIR = Path("/home/philip/robothor/brain/memory/snapshots")
RTSP_URL = "rtsp://localhost:8554/webcam"
VISION_SERVICE_URL = "http://localhost:8600"


async def capture_snapshot(save_path: str | None = None) -> str:
    """Capture a snapshot from the RTSP webcam.

    Args:
        save_path: Optional path to save the snapshot. Defaults to /tmp/webcam-snapshot.jpg.

    Returns:
        Path to the saved snapshot file.
    """
    if save_path is None:
        save_path = "/tmp/webcam-snapshot.jpg"

    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            RTSP_URL,
            "-frames:v",
            "1",
            "-update",
            "1",
            "-y",
            save_path,
        ],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg snapshot failed: {result.stderr.decode()[:200]}")
    return save_path


def save_snapshot_dated(source_path: str) -> str:
    """Copy a snapshot to the date-organized snapshot directory.

    Args:
        source_path: Path to the source snapshot file.

    Returns:
        Path to the saved snapshot in the dated directory.
    """
    from datetime import datetime

    now = datetime.now()
    day_dir = SNAPSHOT_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    dest = day_dir / now.strftime("%H%M%S.jpg")
    import shutil

    shutil.copy2(source_path, dest)
    return str(dest)


def image_to_base64(path: str) -> str:
    """Read an image file and return its base64-encoded contents."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


class VisionLookRequest(BaseModel):
    prompt: str = Field(
        "Describe what you see in this image in detail.", description="What to analyze"
    )


class VisionEnrollRequest(BaseModel):
    name: str = Field(..., description="Name of the person to enroll")


class VisionModeRequest(BaseModel):
    mode: str = Field(..., description="Vision mode: disarmed, basic, or armed")


@app.post("/vision/look")
async def vision_look(req: VisionLookRequest = None):
    """Capture a snapshot and analyze it with the vision LLM.

    Returns a rich scene description from llama3.2-vision.
    """
    if req is None:
        req = VisionLookRequest()

    snap_path = await capture_snapshot()
    img_b64 = image_to_base64(snap_path)
    saved = save_snapshot_dated(snap_path)

    description = await analyze_image(
        image_base64=img_b64,
        prompt=req.prompt,
        system="You are Robothor's vision system. Describe what you see clearly and concisely. Note any people, objects, and notable details.",
    )

    return {
        "description": description,
        "snapshot_path": saved,
        "prompt": req.prompt,
    }


@app.post("/vision/detect")
async def vision_detect():
    """Capture a snapshot and run YOLO object detection.

    Returns a list of detected objects with bounding boxes and confidence.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{VISION_SERVICE_URL}/detections")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass

    # Fallback: capture and run detection inline if vision service is down
    snap_path = await capture_snapshot()
    try:
        from ultralytics import YOLO

        model = YOLO("yolov8n.pt")
        results = model(snap_path, verbose=False)
        detections = []
        for r in results:
            for box in r.boxes:
                detections.append(
                    {
                        "class": r.names[int(box.cls[0])],
                        "confidence": round(float(box.conf[0]), 3),
                        "bbox": [round(float(x), 1) for x in box.xyxy[0].tolist()],
                    }
                )
        return {"detections": detections, "source": "inline"}
    except ImportError:
        raise HTTPException(
            status_code=503, detail="Vision service not running and ultralytics not available"
        )


@app.post("/vision/identify")
async def vision_identify():
    """Capture a snapshot and identify any faces against enrolled people.

    Returns a list of identified and unknown faces.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{VISION_SERVICE_URL}/identifications")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass

    raise HTTPException(
        status_code=503,
        detail="Vision service not running — face identification requires the background service",
    )


@app.get("/vision/status")
async def vision_status():
    """Get the status of the vision service.

    Reports whether the vision service is running, who's currently present,
    and the last detection timestamp.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE_URL}/health")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass

    return {
        "running": False,
        "people_present": [],
        "last_detection": None,
        "message": "Vision service not reachable",
    }


@app.post("/vision/enroll")
async def vision_enroll(req: VisionEnrollRequest):
    """Capture face embeddings and enroll a person for recognition.

    Takes multiple snapshots, extracts face embeddings via InsightFace,
    averages them, and stores in the entity graph.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{VISION_SERVICE_URL}/enroll",
                json={"name": req.name},
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                error_detail = resp.json().get("detail", resp.text)
                raise HTTPException(status_code=resp.status_code, detail=error_detail)
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Vision service not running — face enrollment requires the background service",
        )


@app.get("/vision/mode")
async def vision_mode_get():
    """Get the current vision service mode."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VISION_SERVICE_URL}/mode")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {"mode": "unknown", "error": "Vision service not reachable"}


@app.post("/vision/mode")
async def vision_mode_set(req: VisionModeRequest):
    """Switch the vision service mode.

    Modes:
      disarmed — Camera connected, no processing
      basic    — Motion detection only (cheap, fast)
      armed    — Full pipeline: YOLO + face ID + VLM escalation
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{VISION_SERVICE_URL}/mode",
                json={"mode": req.mode},
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                error_detail = resp.json().get("error", resp.text)
                raise HTTPException(status_code=resp.status_code, detail=error_detail)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Vision service not reachable")


# ============== Main ==============

if __name__ == "__main__":
    import uvicorn

    print("Starting Robothor RAG Orchestrator on port 9099...")
    print("Endpoints:")
    print("  GET  /health              — Component health check")
    print("  POST /query               — Simple RAG query")
    print("  POST /v1/chat/completions — OpenAI-compatible chat")
    print("  GET  /v1/models           — List models")
    print("  GET  /profiles            — List RAG profiles")
    print("  GET  /stats               — Memory statistics")
    print("  POST /ingest              — Ingest content from any channel")

    uvicorn.run(app, host="0.0.0.0", port=9099)

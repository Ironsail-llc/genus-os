"""Every model endpoint a fresh Genus OS install touches, answered deterministically.

The acceptance gate proves the *install* works, not that a model is clever, and
a gate that needed a real provider key would be a gate nobody could run. So
this serves two surfaces:

* **OpenAI-compatible** (`/v1/chat/completions`, `/v1/models`) — what litellm
  reaches for the configured cloud provider, and therefore what `genus init`'s
  one-token provider probe and `genus run` hit. Point litellm at it with
  ``OPENROUTER_API_BASE``.
* **Ollama** (`/api/tags`, `/api/show`, `/api/embed`, `/api/embeddings`,
  `/api/chat`, `/api/generate`, `/api/pull`) — the surface the platform touches
  at boot. Point the platform at it with ``ROBOTHOR_OLLAMA_URL``.

Two of those are load-bearing in ways that are invisible until a fresh box:
the orchestrator's `/ready` calls ``check_model_available()``, which reads
``/api/tags`` and matches the generation model by name, and every memory write
embeds through ``/api/embed``, whose vectors must be 1024-dimensional because
`infra/migrations/001_init.sql` declares ``embedding vector(1024)``. A mock
that answered 768 would pass every HTTP assertion and fail the first insert.

Answers are deterministic: "pong" for chat, a fixed ``noop`` call when tools
are supplied, and a hash-seeded unit vector per text so the same input always
embeds to the same place (and never to the zero vector, whose cosine distance
is NaN).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

#: `infra/migrations/001_init.sql` declares `embedding vector(1024)`.
EMBEDDING_DIMENSIONS = 1024

#: The one sentence every chat surface answers with.
REPLY = "pong"

#: Matches `robothor.llm.ollama.GENERATION_MODEL` so `check_model_available()`
#: — the orchestrator's `/ready` generation check — finds it.
CHAT_MODEL = "qwen3:8b"

#: Matches `robothor.llm.ollama._embedding_model()`'s default.
EMBEDDING_MODEL = "qwen3-embedding:0.6b"

#: The reranker the `models` init step pulls alongside the embedder.
RERANKER_MODEL = "Qwen3-Reranker-0.6B:F16"

MODELS: dict[str, list[str]] = {
    CHAT_MODEL: ["completion", "tools"],
    EMBEDDING_MODEL: ["embedding"],
    RERANKER_MODEL: ["embedding"],
}

app = FastAPI(title="genus install-gate mock model server")


def embedding_for(text: str) -> list[float]:
    """A deterministic unit vector for ``text``, 1024-dimensional."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [((digest[i % len(digest)] + i) % 251) / 251.0 - 0.5 for i in range(EMBEDDING_DIMENSIONS)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


def _texts(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(entry) for entry in payload]
    return [str(payload if payload is not None else "")]


def _tag(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "model": name,
        "size": 1,
        "digest": hashlib.sha256(name.encode("utf-8")).hexdigest(),
        "modified_at": "2026-01-01T00:00:00Z",
        "capabilities": MODELS[name],
        "details": {"family": "qwen3", "parameter_size": "0.6B", "quantization_level": "Q4_0"},
    }


# --------------------------------------------------------------------------
# OpenAI-compatible surface
# --------------------------------------------------------------------------


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": "genus-install-gate"}
                for name in MODELS
            ],
        }
    )


def _completion(model: str, tools: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": None if tools else REPLY}
    finish = "stop"
    if tools:
        message["tool_calls"] = [
            {
                "id": "call_noop",
                "type": "function",
                "function": {"name": "noop", "arguments": "{}"},
            }
        ]
        finish = "tool_calls"
    return {
        "id": "chatcmpl-installgate",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    model = str(body.get("model") or CHAT_MODEL)
    full = _completion(model, body.get("tools"))

    if not body.get("stream"):
        return JSONResponse(full)

    choice = full["choices"][0]["message"]

    async def stream() -> Any:
        first: dict[str, Any] = {"role": "assistant"}
        if choice.get("tool_calls"):
            first["tool_calls"] = [{"index": 0, **choice["tool_calls"][0]}]
        else:
            first["content"] = REPLY
        for delta in (first, {}):
            chunk = {
                "id": full["id"],
                "object": "chat.completion.chunk",
                "created": full["created"],
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": None if delta else full["choices"][0]["finish_reason"],
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Ollama surface
# --------------------------------------------------------------------------


@app.get("/api/tags")
async def tags() -> JSONResponse:
    return JSONResponse({"models": [_tag(name) for name in MODELS]})


@app.get("/api/version")
async def version() -> JSONResponse:
    return JSONResponse({"version": "0.0.0-install-gate"})


@app.post("/api/show")
async def show(request: Request) -> JSONResponse:
    body = await request.json()
    name = str(body.get("model") or body.get("name") or CHAT_MODEL)
    capabilities = MODELS.get(name, ["completion", "tools"])
    return JSONResponse(
        {
            "capabilities": capabilities,
            "details": {"family": "qwen3", "parameter_size": "0.6B"},
            "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 32768},
        }
    )


@app.post("/api/pull")
async def pull(request: Request) -> StreamingResponse:
    body = await request.json()
    name = str(body.get("model") or body.get("name") or CHAT_MODEL)

    async def stream() -> Any:
        for status in ("pulling manifest", f"pulling {name}", "success"):
            yield json.dumps({"status": status}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/embed")
async def embed(request: Request) -> JSONResponse:
    body = await request.json()
    texts = _texts(body.get("input"))
    return JSONResponse(
        {
            "model": str(body.get("model") or EMBEDDING_MODEL),
            "embeddings": [embedding_for(text) for text in texts],
        }
    )


@app.post("/api/embeddings")
async def embeddings_legacy(request: Request) -> JSONResponse:
    body = await request.json()
    return JSONResponse({"embedding": embedding_for(str(body.get("prompt") or ""))})


def _ollama_message() -> dict[str, Any]:
    return {"role": "assistant", "content": REPLY}


@app.post("/api/chat")
async def ollama_chat(request: Request) -> Any:
    body = await request.json()
    model = str(body.get("model") or CHAT_MODEL)
    done = {
        "model": model,
        "created_at": "2026-01-01T00:00:00Z",
        "message": _ollama_message(),
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1,
        "eval_count": 1,
    }
    if not body.get("stream"):
        return JSONResponse(done)

    async def stream() -> Any:
        yield json.dumps({**done, "done": False, "done_reason": None}) + "\n"
        yield json.dumps({**done, "message": {"role": "assistant", "content": ""}}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/generate")
async def ollama_generate(request: Request) -> Any:
    body = await request.json()
    model = str(body.get("model") or CHAT_MODEL)
    done = {
        "model": model,
        "created_at": "2026-01-01T00:00:00Z",
        "response": REPLY,
        "done": True,
        "done_reason": "stop",
    }
    if not body.get("stream"):
        return JSONResponse(done)

    async def stream() -> Any:
        yield json.dumps({**done, "done": False, "done_reason": None}) + "\n"
        yield json.dumps({**done, "response": ""}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/ready")
@app.get("/health")
async def ready() -> JSONResponse:
    return JSONResponse({"status": "ok"})

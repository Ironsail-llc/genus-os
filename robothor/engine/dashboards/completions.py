"""Authenticated, Engine-owned completions for the Next.js dashboard.

The dashboard is a presentation tier.  It must never receive model-provider
credentials or choose provider request parameters.  This narrowly-scoped BFF
endpoint keeps those capabilities inside the Engine authentication boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from robothor.engine.auth import request_context
from robothor.engine.llm_client import llm_call

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-completions"])

DEFAULT_DASHBOARD_MODEL = "openrouter/google/gemini-2.5-flash-lite"
#: The whole chain's time, per purpose. The dashboard's client gives up on a
#: triage after 20s and a render after 130s for the WHOLE request, so the
#: chain shares one deadline inside that window, and each model gets an equal
#: slice of it: a first model that hangs must leave time for the next.
TRIAGE_BUDGET_SECONDS = 15
RENDER_BUDGET_SECONDS = 120
MAX_SYSTEM_PROMPT_CHARS = 12_000
MAX_USER_PROMPT_CHARS = 40_000
MAX_REQUEST_BYTES = 64 * 1024
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class DashboardCompletionRequest(BaseModel):
    """Strict request accepted from the authenticated dashboard BFF."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    purpose: Literal["triage", "render"]
    system_prompt: str
    user_prompt: str

    @field_validator("system_prompt")
    @classmethod
    def _bounded_system_prompt(cls, value: str) -> str:
        if not value or len(value) > MAX_SYSTEM_PROMPT_CHARS:
            raise ValueError("system prompt is outside the allowed bounds")
        return value

    @field_validator("user_prompt")
    @classmethod
    def _bounded_user_prompt(cls, value: str) -> str:
        if not value or len(value) > MAX_USER_PROMPT_CHARS:
            raise ValueError("user prompt is outside the allowed bounds")
        return value


class DashboardCompletionResponse(BaseModel):
    """Provider-neutral response; no routing metadata crosses the boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str


def dashboard_model_chain(manifest_dir: Path | None = None) -> list[str]:
    """The models the canvas tries, in order; the first that answers wins.

    ``DASHBOARD_MODEL`` (an operator override) first, then the fleet defaults'
    ``model.canvas`` (the Providers page's "Canvas model"), then the fleet's
    own ``model.primary``, then the instance's model of last resort. No
    provider prefix is added: an id is dialled as written, so the canvas
    works on whichever provider the instance holds a key for. Only when
    nothing at all is configured does the historical default apply.

    The defaults are read through the same cached loader the admin defaults
    routes use, so a ``PUT /api/admin/defaults`` or a reload is seen here
    without a restart.
    """
    from robothor.engine import config as engine_config

    override = os.environ.get("DASHBOARD_MODEL", "").strip()
    if override and not _MODEL_ID_RE.fullmatch(override):
        logger.error("DASHBOARD_MODEL is not a valid model identifier")
        raise RuntimeError("invalid dashboard model configuration")

    directory = manifest_dir or engine_config.EngineConfig.from_env().manifest_dir
    block = engine_config._load_defaults(Path(directory)).get("model") or {}
    if not isinstance(block, dict):
        block = {}

    candidates = [
        override,
        block.get("canvas"),
        block.get("primary"),
        engine_config.last_resort_model(),
    ]
    chain: list[str] = []
    for candidate in candidates:
        model = str(candidate).strip() if candidate else ""
        if not model or model in chain:
            continue
        if not _MODEL_ID_RE.fullmatch(model):
            logger.warning("Skipping an invalid model id in the dashboard model chain")
            continue
        chain.append(model)
    return chain or [DEFAULT_DASHBOARD_MODEL]


def _completion_content(response: Any) -> str:
    """Extract only text content from a LiteLLM response."""

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError) as error:
        raise ValueError("completion response did not contain content") from error
    if not isinstance(content, str) or not content.strip():
        raise ValueError("completion response did not contain text")
    return content


async def _validated_payload(request: Request) -> DashboardCompletionRequest:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=400, detail="invalid dashboard completion request")
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="invalid dashboard completion request"
            ) from error

    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=400, detail="invalid dashboard completion request")
    try:
        payload = json.loads(body)
        return DashboardCompletionRequest.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        # Do not echo prompts back in validation responses.
        raise HTTPException(
            status_code=400, detail="invalid dashboard completion request"
        ) from error


@router.post("/completions", response_model=DashboardCompletionResponse)
async def dashboard_completion(request: Request) -> DashboardCompletionResponse:
    """Run one bounded completion using server-controlled provider settings."""

    # The Engine middleware performs signature, audience, tenant and
    # ``engine:chat`` scope checks.  Requiring its installed context here also
    # prevents this router from ever being reused without that boundary.
    request_context(request)
    payload = await _validated_payload(request)

    if payload.purpose == "triage":
        max_tokens = 256
        json_mode = True
        temperature = 0.1
        budget: float = TRIAGE_BUDGET_SECONDS
    else:
        max_tokens = 4096
        json_mode = False
        temperature = 0.3
        budget = RENDER_BUDGET_SECONDS

    try:
        chain = dashboard_model_chain()
        call = llm_call(
            [
                {"role": "system", "content": payload.system_prompt},
                {"role": "user", "content": payload.user_prompt},
            ],
            model=chain,
            temperature=temperature,
            json_mode=json_mode,
            timeout=budget / len(chain),
            max_retries=1,
            max_tokens=max_tokens,
        )
        # One deadline for the whole chain, so nothing is still dialling a
        # provider after the client has stopped waiting.
        response = await asyncio.wait_for(call, timeout=budget)
        return DashboardCompletionResponse(content=_completion_content(response))
    except Exception as error:
        # Provider response bodies can contain request details and must not be
        # reflected to the dashboard.  The exception class is enough to
        # correlate this generic response with Engine-side telemetry.
        logger.warning(
            "Dashboard completion failed (purpose=%s, error_type=%s)",
            payload.purpose,
            type(error).__name__,
        )
        raise HTTPException(status_code=503, detail="dashboard completion unavailable") from error

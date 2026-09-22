"""Observe benchmark HTTP payload configuration without retaining prompts or headers."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from typing import Any


def digest(value: Any) -> Any:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class ProviderCapture:
    def __init__(self) -> None:
        self.requests: ContextVar[list[dict[str, Any]] | None] = ContextVar(
            "screening_provider_requests", default=None
        )

    def start(self) -> Any:
        return self.requests.set([])

    def finish(self, token: Any) -> Any:
        captured = self.requests.get()
        self.requests.reset(token)
        return captured

    async def record(self, request: Any) -> None:
        captured = self.requests.get()
        if captured is None:
            raise ValueError("provider request outside a screening sample")
        body = json.loads(request.content)
        captured.append(
            {
                "model": body["model"],
                "prompt_hash": digest(body.get("messages", [])),
                "tools_hash": digest(body.get("tools", [])),
                "model_settings": {
                    key: value
                    for key, value in body.items()
                    if key not in {"model", "messages", "tools"}
                },
            }
        )

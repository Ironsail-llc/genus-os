"""Observe benchmark HTTP payload configuration without retaining prompts or headers."""

import hashlib
import json
from contextvars import ContextVar


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class ProviderCapture:
    def __init__(self):
        self.requests = ContextVar("screening_provider_requests", default=None)

    def start(self):
        return self.requests.set([])

    def finish(self, token):
        captured = self.requests.get()
        self.requests.reset(token)
        return captured

    async def record(self, request):
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

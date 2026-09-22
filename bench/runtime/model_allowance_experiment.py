"""Process-local experiment; never installs a production timeout policy."""

from __future__ import annotations

from typing import Any


def install(
    monkeypatch: Any,
    seconds: Any,
    *,
    isolate_short_timeout_health: Any = False,
    cloud_only: Any = False,
) -> Any:
    from robothor.engine import llm_client

    original_timeout = llm_client._per_call_timeout

    def limited(model: Any) -> Any:
        return not (cloud_only and llm_client.uses_ollama_timeout(model))

    monkeypatch.setattr(
        llm_client,
        "_per_call_timeout",
        lambda model, override: (
            min(seconds, original_timeout(model, override))
            if limited(model)
            else original_timeout(model, override)
        ),
    )
    if isolate_short_timeout_health:
        original_blame = llm_client._blame_model

        def blame(breaker: Any, error: Any, model: Any, attempt_timeout: Any) -> Any:
            # This is a test allowance, not the provider's ordinary timeout.
            # Other failures still use the native health and alert policy.
            if not (limited(model) and isinstance(error, TimeoutError)):
                original_blame(breaker, error, model, attempt_timeout)

        monkeypatch.setattr(llm_client, "_blame_model", blame)

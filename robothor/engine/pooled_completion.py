"""``litellm.acompletion`` with the credential pool wired in.

The pool only helps callers that consult it. On 2026-08-27 the engine's pool
retired a capped OpenRouter key correctly and the 403 storm continued anyway,
because eight other modules called ``litellm.acompletion`` directly and let
the SDK resolve ``OPENROUTER_API_KEY`` from the environment. Each kept
hammering a credential the pool had already given up on, none could rotate to
a spare, and none contributed to the exhaustion signal.

Drop-in: replace ``litellm.acompletion(...)`` with ``acompletion(...)``.
Behaviour is unchanged for any provider that is not pooled — the key is
simply not injected and litellm resolves the environment exactly as before.

Not a policy layer. It injects a credential and retires a rejected one; it
does not retry, fall back, or decide anything about models. Callers keep
their own control flow.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def acompletion(*, model: str, **kwargs: Any) -> Any:
    """Call the provider using the pool's current credential for ``model``.

    On a credential rejection the key is retired for the whole process before
    the exception propagates, so the caller's next attempt — and every other
    caller's next call — rotates instead of re-dialling a corpse.
    """
    import litellm

    from robothor.engine.key_pool import api_key_for_model

    key = api_key_for_model(model)
    if key and "api_key" not in kwargs:
        kwargs["api_key"] = key
    try:
        return await litellm.acompletion(model=model, **kwargs)
    except Exception as exc:
        if key:
            _retire_if_credential_failure(model, key, exc)
        raise


def _retire_if_credential_failure(model: str, key: str, exc: Exception) -> None:
    """Take the key out of rotation when the provider rejected the CREDENTIAL.

    Deliberately narrow. A model-specific refusal (a moderated or
    region-blocked model answering 403) must not retire a healthy key for
    every other model on it — the distinction ``llm_client.is_auth_failure``
    draws, and the reason it is 401-only.
    """
    try:
        from robothor.engine.key_pool import Retirement, retire_for_model
        from robothor.engine.llm_client import (
            is_auth_failure,
            is_credit_exhausted,
            is_periodic_quota_exhausted,
        )

        if is_credit_exhausted(exc):
            reason = (
                Retirement.QUOTA_EXHAUSTED_PERIODIC
                if is_periodic_quota_exhausted(exc)
                else Retirement.CREDIT_EXHAUSTED
            )
        elif is_auth_failure(exc):
            reason = Retirement.AUTH_FAILED
        else:
            return
        retire_for_model(model, key, reason)
    except Exception:  # noqa: BLE001 — never convert a call failure into a new one
        logger.debug("could not classify a credential failure for %s", model)

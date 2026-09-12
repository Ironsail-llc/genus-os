"""Prove the instance can reach a model before writing a configuration for it.

The failure this exists to prevent: an install that finishes green, writes a
provider into config.yaml, and only discovers on the operator's first message
that the key was pasted with a trailing newline. So the wizard makes a real
one-token completion, and a failed probe blocks the run.

The credential is the delicate part. It has not been stored anywhere yet, and
the only safe way to test it is to hand it to that ONE call —
``llm_client.llm_call(api_key=...)`` — rather than to ``os.environ``, which
would publish it to every thread and every subprocess for the life of the
process. Nothing in this module writes to the environment; the tests assert it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.init.context import InitContext

__all__ = [
    "DetectedProvider",
    "PROBE_TIMEOUT_S",
    "ProbeResult",
    "detect_codex_login",
    "detect_ollama_tool_models",
    "detect_provider_keys",
    "models_for_provider",
    "probe_model",
    "record_unprobed",
]

#: The cheapest question that still proves the whole path works: a model id,
#: a credential, a network route, and a provider willing to answer.
PROBE_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "ping"}]

#: Seconds the probe waits. Longer than the doctor's 5s budget on purpose —
#: this is a one-off during an install, a cold provider can take a while, and
#: an operator who has just pasted a key would rather wait than retype it.
PROBE_TIMEOUT_S = 30.0

#: How many Ollama models to interrogate for tool support. A box with a large
#: library would otherwise turn one wizard step into a hundred HTTP calls.
MAX_OLLAMA_MODELS = 40


@dataclass(frozen=True)
class ProbeResult:
    """What the probe learned. ``probed`` separates "it works" from "we did
    not look" — an ``--offline`` run records a choice, and a report that
    called that a successful test would be lying to the next reader."""

    ok: bool
    provider: str
    model: str
    detail: str
    probed: bool


@dataclass(frozen=True)
class DetectedProvider:
    """A provider this box already has a credential for. No key material."""

    id: str
    label: str
    env_var: str
    default_model: str
    slots: tuple[str, ...]


def _provider_for_model(model: str) -> str:
    """The provider id a model id belongs to, or "" when we do not know.

    Derived from the ``key_pool`` catalogue rather than from a parallel list:
    a hand-maintained mapping beside the thing it describes is the drift that
    produced three separate "hardcoded names" defects on this instance.
    """
    from robothor.engine.key_pool import PROVIDERS

    for spec in PROVIDERS:
        if model.startswith(spec.model_prefix):
            return spec.id
    return ""


def models_for_provider(provider_id: str) -> list[str]:
    """Models the engine already knows limits and pricing for, for this provider.

    From ``model_registry._MODEL_REGISTRY`` rather than a list typed into the
    wizard: a model offered here that the registry does not carry gets a
    guessed context window and no price, and the operator's first surprise is
    a truncated conversation or an uncapped bill.
    """
    from robothor.engine.key_pool import provider_by_id
    from robothor.engine.model_registry import _MODEL_REGISTRY

    spec = provider_by_id(provider_id)
    if spec is None:
        return []
    known = sorted(name for name in _MODEL_REGISTRY if name.startswith(spec.model_prefix))
    if spec.default_model in known:
        known.remove(spec.default_model)
        known.insert(0, spec.default_model)
    elif spec.default_model:
        known.insert(0, spec.default_model)
    return known


def _redact(text: str, secret: str | None) -> str:
    """Never let a credential reach a report, a log or a terminal."""
    if secret:
        return text.replace(secret, "***")
    return text


def probe_model(
    model: str,
    *,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT_S,
    llm_call: Callable[..., Any] | None = None,
) -> ProbeResult:
    """Ask ``model`` for one token with ``api_key``, for this call only.

    Never raises: a probe that threw would take down the wizard on exactly the
    box the wizard exists to set up. Every outcome comes back as a
    :class:`ProbeResult` whose ``detail`` is a sentence an operator can act on.
    """
    provider = _provider_for_model(model)
    call = llm_call
    if call is None:  # pragma: no cover - the real client, exercised live
        from robothor.engine.llm_client import llm_call as real_call

        call = real_call
    dial = call

    async def _run() -> Any:
        return await dial(
            PROBE_MESSAGES,
            model=model,
            max_tokens=1,
            temperature=0.0,
            timeout=timeout,
            max_retries=1,
            api_key=api_key,
        )

    try:
        answer = asyncio.run(_run())
    except TimeoutError:
        return ProbeResult(
            ok=False,
            provider=provider,
            model=model,
            detail=f"no answer in {timeout:.0f}s",
            probed=True,
        )
    except Exception as exc:  # noqa: BLE001 - the failure IS the answer here
        return ProbeResult(
            ok=False,
            provider=provider,
            model=model,
            detail=_redact(f"{type(exc).__name__}: {exc}", api_key),
            probed=True,
        )

    if answer is None:
        return ProbeResult(
            ok=False,
            provider=provider,
            model=model,
            detail="the provider returned no completion",
            probed=True,
        )
    return ProbeResult(
        ok=True,
        provider=provider,
        model=model,
        detail="answered a 1-token completion",
        probed=True,
    )


def record_unprobed(provider: str, model: str) -> ProbeResult:
    """Accept a provider choice without testing it, and say so out loud."""
    return ProbeResult(
        ok=True,
        provider=provider,
        model=model,
        detail="recorded without a probe (--offline); nothing has tested this credential",
        probed=False,
    )


def detect_provider_keys() -> list[DetectedProvider]:
    """Providers this box already has a usable credential for.

    Through ``key_pool.provider_slots``, which reports fingerprints rather than
    keys, so a detection summary can be printed, logged and pasted into a bug
    report without disclosing anything.
    """
    from robothor.engine.key_pool import PROVIDERS, provider_slots

    detected: list[DetectedProvider] = []
    for spec in PROVIDERS:
        try:
            slots = provider_slots(spec.id)
        except Exception:  # noqa: BLE001 - an unreadable vault is not a crash
            slots = []
        if not slots:
            continue
        detected.append(
            DetectedProvider(
                id=spec.id,
                label=spec.label,
                env_var=spec.env_var,
                default_model=spec.default_model,
                slots=tuple(slot.fingerprint for slot in slots),
            )
        )
    return detected


def detect_ollama_tool_models(ctx: InitContext) -> list[str]:
    """Local models that can actually call tools, per Ollama's own report.

    ``/api/tags`` lists what is pulled; only ``/api/show`` knows whether a
    model can call tools. The distinction is not cosmetic — this instance has
    already shipped agents pinned to local models that could not tool-call, and
    an agent that cannot call a tool cannot do anything the fleet asks of it.
    """
    import json

    base = str(ctx.settings.ollama.url).rstrip("/")
    tags = ctx.http("GET", f"{base}/api/tags")
    if not tags.ok:
        return []
    try:
        listed = json.loads(tags.body).get("models", [])
    except Exception:  # noqa: BLE001 - a non-JSON answer is "nothing detected"
        return []

    capable: list[str] = []
    for entry in listed[:MAX_OLLAMA_MODELS]:
        name = entry.get("name") or entry.get("model") or ""
        if not name:
            continue
        shown = ctx.http("POST", f"{base}/api/show", body={"model": name})
        if not shown.ok:
            continue
        try:
            capabilities = json.loads(shown.body).get("capabilities", [])
        except Exception:  # noqa: BLE001
            continue
        if "tools" in capabilities:
            capable.append(name)
    return capable


def detect_codex_login(codex_home: Path | str) -> str:
    """The Codex credential file, if this account has signed in. Else "".

    ``robothor/cli/codex.py`` has no login-state helper to reuse — its only
    helper strips an API key out of the environment — so the check is the file
    Codex itself writes. Returning the path rather than a bool means the
    detection summary can say WHICH file it found.
    """
    auth = Path(codex_home).expanduser() / "auth.json"
    try:
        if auth.is_file():
            return str(auth)
    except OSError:  # pragma: no cover - an unreadable home is "not signed in"
        return ""
    return ""

"""Can this instance actually call a model?

The expensive lesson behind ``provider.completion``: the container built for
the first harness measurement was configured, started, passed every health
probe, and could not make a single LLM call -- a missing ``orjson`` broke
litellm's import, and nothing anywhere asked the one question that would have
caught it. A key being CONFIGURED and a key WORKING are different facts, and
only the second one means the fleet runs.

Nothing in this module puts key material in a result. ``provider.keys`` reports
fingerprints (``key_pool.key_fingerprint``, an HMAC, not a truncation) and slot
states; ``provider.completion`` reports a latency and an error class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

#: One token, and a prompt whose answer nobody reads. The cheapest call that
#: still proves the whole path: credential resolution, litellm import, network,
#: the provider's auth, and a response body.
_PROBE_PROMPT = "ping"


def _configured() -> list[tuple[str, str, list[str]]]:
    """``(provider id, label, slot descriptions)`` for every configured provider."""
    from robothor.engine import key_pool

    found: list[tuple[str, str, list[str]]] = []
    for spec in key_pool.PROVIDERS:
        slots = key_pool.provider_slots(spec.id)
        if slots:
            found.append(
                (
                    spec.id,
                    spec.label,
                    [f"{slot.source}/{slot.state} {slot.fingerprint}" for slot in slots],
                )
            )
    return found


async def _keys(ctx: DoctorContext) -> Result:
    """At least one provider credential resolves, from the vault or the
    environment.

    Nothing runs without one: every agent turn is an LLM call. A failure here
    is usually a credential written to the vault that the engine has not
    reloaded, or one exported into a shell but not into the unit environment.
    Fingerprints are reported so two instances can be compared without either
    one disclosing a key.
    """
    try:
        found = await ctx.run_blocking(_configured)
    except Exception as exc:  # noqa: BLE001 - a broken pool is a failure, not a crash
        return fail(f"cannot read the credential pool: {type(exc).__name__}")
    if not found:
        return fail(
            "no provider credential is configured — set one with "
            "'genus config set OPENROUTER_API_KEY ...' or from the Helm's Providers page"
        )
    described = "; ".join(f"{label}: {', '.join(slots)}" for _provider_id, label, slots in found)
    return ok(described)


def _fleet_model() -> str:
    """The model the fleet actually dials, read from the manifests.

    Manifests are the source of truth for models (design decision D5), so this
    reads ``_defaults.yaml`` through the same loader the engine uses rather
    than guessing from the provider catalogue.
    """
    from robothor.engine import config as engine_config

    manifest_dir = engine_config.EngineConfig.from_env().manifest_dir
    defaults = engine_config._load_defaults(manifest_dir)
    primary = (defaults.get("model") or {}).get("primary")
    if primary:
        return str(primary)

    from robothor.engine import key_pool

    for spec in key_pool.PROVIDERS:
        if key_pool.resolve_keys(spec.id):
            return spec.default_model
    return ""


async def _completion(ctx: DoctorContext) -> Result:
    """One real one-token completion through the fleet's default model.

    This is the only check that proves the instance can do its job. A pass
    means credentials, litellm, the network and the provider all work
    together; a fail names the error class (``auth``, ``rate_limit``,
    ``network``, ``model``) and never echoes the response. Skipped under
    ``--offline`` and when no credential is configured -- there is nothing to
    test, and a red line for an absent key would duplicate ``provider.keys``.
    """
    if ctx.offline:
        return skip("--offline: no upstream call was made")

    try:
        model = await ctx.run_blocking(_fleet_model)
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest is manifests.schema's job
        return fail(f"cannot resolve the fleet's default model: {type(exc).__name__}")
    if not model:
        return skip("no provider credential and no fleet default model to test")

    from robothor.engine import llm_client

    try:
        await llm_client.llm_call(
            [{"role": "user", "content": _PROBE_PROMPT}],
            model=model,
            max_tokens=1,
            temperature=0.0,
            timeout=ctx.timeout_s,
            max_retries=1,
        )
    except Exception as exc:  # noqa: BLE001 - the verdict is the point
        from robothor.engine.admin_providers import _classify

        return fail(f"{model} did not answer: {_classify(exc)} ({type(exc).__name__})")
    return ok(f"{model} answered a 1-token completion")


async def _ollama(ctx: DoctorContext) -> Result:
    """The local Ollama server answers, for the offline tier and embeddings.

    Recommended, not required: an instance that runs entirely on a cloud
    provider needs no Ollama. But memory writes embed through it, so on a box
    that HAS it configured a failure here means memory search quietly degrades
    rather than errors -- which is why it is checked rather than assumed.
    """
    base = (
        ctx.settings.ollama.url or f"http://{ctx.settings.ollama.host}:{ctx.settings.ollama.port}"
    )
    if not base:
        return skip("no Ollama endpoint is configured")
    response = await ctx.run_blocking(ctx.fetch, f"{base.rstrip('/')}/api/tags")
    if response.ok:
        return ok(f"{base} answered {response.status}")
    reported = response.error or f"HTTP {response.status}"
    return fail(f"{base} did not answer: {reported}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="provider.keys",
        title="A provider credential resolves",
        category="models",
        severity="required",
        run=_keys,
    ),
    Check(
        id="provider.completion",
        title="The fleet's default model answers",
        category="models",
        severity="required",
        run=_completion,
    ),
    Check(
        id="ollama.reachable",
        title="Ollama is reachable",
        category="models",
        severity="recommended",
        run=_ollama,
    ),
)

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

from typing import TYPE_CHECKING, Any

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
    spent = await ctx.run_blocking(_spent_pools)
    if spent:
        # A failure, not a note. A retired pool is a configured, unshadowed,
        # completely unusable credential — three green columns and a stopped
        # fleet (2026-08-27, 48 hours). The remedy is named because the reload
        # is the ONLY thing that clears a cooldown early: raising the cap at
        # the provider changes nothing in the engine's memory.
        return fail(
            f"{'; '.join(spent)}. Top up or raise the limit, then run "
            f"`genus secrets reload`. Configured: {described}"
        )
    return ok(described)


def _spent_pools() -> list[str]:
    """Providers the RUNNING engine has no credential left for.

    Asked of the engine over its control API, because pools live in ITS memory:
    a doctor answering from its own process reports "active" for a key the
    fleet has not been able to use for six hours. An engine that does not
    answer contributes nothing — this check's job is credentials, and
    ``services.*`` already owns "is the daemon up".
    """
    from robothor.engine_control import control_request

    try:
        payload = control_request("GET", "/api/admin/providers")
    except Exception:  # noqa: BLE001 - a silent daemon is not a credential fault
        return []
    spent: list[str] = []
    for provider in payload.get("providers") or []:
        slots = provider.get("slots") or []
        if not slots or any(slot.get("state") not in ("capped", "revoked") for slot in slots):
            continue
        reasons = {str(slot.get("reason") or slot.get("state")) for slot in slots}
        returns = [
            slot.get("returns_in_s")
            for slot in slots
            if isinstance(slot.get("returns_in_s"), (int, float))
        ]
        window = f", retried in {min(returns) / 3600:.1f}h" if returns else ""
        spent.append(
            f"{provider.get('id')}: key exhausted — every credential for "
            f"{provider.get('env_var')} is retired ({', '.join(sorted(reasons))}{window})"
        )
    return spent


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
    ``--offline``, and skipped when no credential resolves at all -- there is
    nothing to dial, and a second red line for an absent key would tell the
    operator what ``provider.keys`` has already told them while hiding which
    of the two is the cause.
    """
    if ctx.offline:
        return skip("--offline: no upstream call was made")

    try:
        configured = await ctx.run_blocking(_configured)
    except Exception as exc:  # noqa: BLE001 - provider.keys owns that failure
        return skip(f"the credential pool could not be read ({type(exc).__name__})")
    if not configured:
        # provider.keys has already said this, as a required failure. Saying it
        # again here tells the operator the same thing twice and hides which of
        # the two is the cause.
        return skip("no provider credential configured — see provider.keys")

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


#: The settings that say WHERE Ollama is. Any one of them coming from the
#: environment or config.yaml is an operator stating that Ollama should exist.
_OLLAMA_ENDPOINT_VARS = ("ROBOTHOR_OLLAMA_URL", "ROBOTHOR_OLLAMA_HOST", "ROBOTHOR_OLLAMA_PORT")


def _endpoint_configured() -> bool:
    """Did anyone actually configure an Ollama endpoint?

    Asked of ``settings.provenance``, which knows which layer supplied a value,
    rather than by comparing against the declared default. The structural
    comparison could not tell a deliberate ``ROBOTHOR_OLLAMA_HOST=127.0.0.1``
    -- the default value, typed out on purpose -- from no setting at all, and
    every Ollama field carries a default, so "is it set?" is always yes.
    """
    from robothor.settings import provenance
    from robothor.settings.registry import field_index

    index = field_index()
    for name in _OLLAMA_ENDPOINT_VARS:
        record = index.get(name)
        if record is None:  # pragma: no cover - the registry declares all three
            continue
        _value, source, _detail = provenance.resolve(record)
        if source != provenance.SOURCE_DEFAULT:
            return True
    return False


def _fleet_models() -> list[str]:
    """Every model the fleet's default chain names, primary and fallbacks.

    Manifests are the source of truth for models (design decision D5), so this
    reads the same ``_defaults.yaml`` the engine loads rather than guessing
    from the provider catalogue. The orchestrator's readiness probe asks the
    same question, so the implementation is shared rather than copied -- two
    answers to "does this instance use Ollama" is how they come to disagree.
    """
    from robothor.engine.config import fleet_model_chain

    return fleet_model_chain()


def _ollama_in_use() -> tuple[bool, str]:
    """``(does this instance use Ollama, why)``.

    Two independent signals, either of which is enough. Someone configured an
    endpoint, or the fleet's own model chain routes a tier through Ollama --
    which is what the offline tier IS. Deliberately not "an embedding model is
    declared": one always is, so that signal is always true and would make the
    skip unreachable, which is this defect in the other direction.
    """
    if _endpoint_configured():
        return True, "an Ollama endpoint is configured"
    try:
        models = _fleet_models()
    except Exception:  # noqa: BLE001 - manifests.schema owns an unreadable fleet
        # Unknown is not "unused". Probing costs one loopback request.
        return True, "the fleet's model chain could not be read"
    ollama_models = [model for model in models if "ollama" in model.lower()]
    if ollama_models:
        return True, f"the fleet routes {len(ollama_models)} model(s) through Ollama"
    return False, "no model in the fleet's chain routes through Ollama"


async def _ollama(ctx: DoctorContext) -> Result:
    """The local Ollama server answers, for the offline tier and embeddings.

    Recommended, not required: an instance that runs entirely on a cloud
    provider needs no Ollama. But memory writes embed through it and the offline
    tier runs on it, so on a box that HAS it a failure here means memory search
    and the fallback tier quietly degrade rather than error -- which is why it
    is probed rather than assumed.

    "Has it" is decided BEFORE the probe, from two signals: an endpoint someone
    configured (asked of the settings provenance, not compared against a
    default), or an Ollama model in the fleet's own chain. An instance with
    neither is cloud-only and the check skips without dialling. An instance with
    either gets a real verdict -- and a failed probe is reported as a FAILURE,
    never as a skip. The earlier version called a completed, failed probe "not
    configured", which is not what ``skip`` means in this package ("a check that
    could not run") and left the Helm banner green through an embedding outage
    on the commonest install: every Ollama setting on its default, and the fleet
    using it anyway.
    """
    ollama = ctx.settings.ollama
    base = ollama.base_url
    in_use, why = await ctx.run_blocking(_ollama_in_use)
    if not in_use:
        return skip(f"{why} and no endpoint is configured — this instance does not use Ollama")
    if not base:
        return skip("no Ollama endpoint is configured")

    response = await ctx.run_blocking(ctx.fetch, f"{base.rstrip('/')}/api/tags")
    if response.ok:
        return ok(f"{base} answered {response.status}")
    reported = response.error or f"HTTP {response.status}"
    return fail(f"{base} did not answer: {reported} ({why})")


def _local_models() -> list[str]:
    """Every ``ollama_chat/`` model the fleet's own chain names."""
    try:
        return [model for model in _fleet_models() if model.startswith("ollama_chat/")]
    except Exception:  # noqa: BLE001 - manifests.schema owns an unreadable fleet
        return []


def _server_windows(body: str) -> dict[str, int]:
    """``{model name: the context it was trained for}`` from ``/api/tags``.

    The number matters as much as the name. The engine sends ``num_ctx`` from
    its OWN registry, so a registry entry above the model's context is a
    request the server cannot honour and silently trims — the failure mode that
    produced an error with no mention of length in it (2026-09-16).
    """
    import json

    try:
        payload = json.loads(body or "{}")
    except ValueError:
        return {}
    windows: dict[str, int] = {}
    for entry in payload.get("models") or []:
        name = str(entry.get("name") or "")
        window = (entry.get("details") or {}).get("context_length")
        if name:
            windows[name] = int(window) if isinstance(window, int) else 0
    return windows


def _carried_as(name: str, windows: dict[str, int]) -> str | None:
    """The server's own spelling of ``name``, or None if it does not have it."""
    for have in windows:
        if have == name or have.startswith(f"{name}:"):
            return have
    return None


async def _local_fallback_ready(ctx: DoctorContext) -> Result:
    """The last fallback can be reached AND can hold a conversation.

    Required on an instance whose chain ends on Ollama, because that tier is
    the whole answer to "the cloud key is spent" — and on 2026-09-16 it was up,
    was answering, and the run still died, because three numbers nobody
    compared disagreed: the model's own context, the registry window the engine
    sends as ``num_ctx``, and the point compaction fires.
    """
    from robothor.engine.context_fit import fit_for

    models = await ctx.run_blocking(_local_models)
    if not models:
        return skip("this instance has no local fallback in its model chain")
    base = (ctx.settings.ollama.base_url or "").rstrip("/")
    if not base:
        return fail("a local fallback is configured but no Ollama endpoint is")

    response = await ctx.run_blocking(ctx.fetch, f"{base}/api/tags")
    if not response.ok:
        return fail(
            f"the local fallback's server at {base} did not answer "
            f"({response.error or f'HTTP {response.status}'}) — the fleet has nothing "
            "left when a cloud credential is spent"
        )
    windows = _server_windows(response.body)

    problems: list[str] = []
    healthy: list[str] = []
    for model in models:
        name = model.split("/", 1)[1]
        carried = _carried_as(name, windows)
        if carried is None:
            problems.append(f"{model} is in the chain but not on the server — `ollama pull {name}`")
            continue
        fit = fit_for(model)
        server_window = windows.get(carried) or 0
        if server_window and fit.window > server_window:
            problems.append(
                f"{model}: the engine asks for num_ctx={fit.window:,} and the model holds "
                f"{server_window:,} — the server trims the conversation and answers with a "
                "structural error"
            )
            continue
        if fit.threshold + fit.reserved_output > fit.window:
            problems.append(
                f"{model}: compaction fires at {fit.threshold:,} tokens and the answer needs "
                f"{fit.reserved_output:,}, which overflows its {fit.window:,}-token window"
            )
            continue
        healthy.append(f"{model} ({fit.window:,} ctx, compacts at {fit.threshold:,})")
    if problems:
        return fail("; ".join(problems))
    return ok("; ".join(healthy))


#: A conversation this many times the model's window, so the probe is testing
#: the shrink and not the estimate's rounding.
_PROBE_OVERSHOOT = 1.5

#: Below this budget the probe cannot finish a real generation, and a check
#: that reports a timeout as a failure of the thing it is probing is worse
#: than one that did not run.
_PROBE_MIN_TIMEOUT_S = 60.0


async def _local_fallback_probe(ctx: DoctorContext) -> Result:
    """Positive control: an oversized conversation COMPACTS instead of failing.

    Opt-in (``--only models.local_fallback_probe``) because it spends a real
    generation on a slow local model. It is here because every inert-control
    incident on this instance was found the same way — by firing a real
    violation at a guard and watching nothing happen. The guard under test is
    the one that was missing: a conversation longer than ``num_ctx`` used to
    reach the server intact and come back as ``no user query found in
    messages``.
    """
    from robothor.engine.context_fit import fit_for
    from robothor.engine.llm_client import LLMClient

    models = await ctx.run_blocking(_local_models)
    if not models:
        return skip("this instance has no local fallback in its model chain")
    if ctx.offline:
        return skip("offline: the probe runs a real generation")
    if ctx.timeout_s < _PROBE_MIN_TIMEOUT_S:
        return skip(
            f"the probe needs a real generation — re-run with "
            f"`--timeout {int(_PROBE_MIN_TIMEOUT_S * 2)}`"
        )

    model = models[-1]
    fit = fit_for(model)
    filler = "the quick brown fox jumps over the lazy dog. " * 200
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are terse. Answer in one short sentence."},
        {"role": "user", "content": "Summarise what you were told."},
    ]
    turns = int(fit.window * _PROBE_OVERSHOOT * 4 / len(filler)) + 1
    for index in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"probe-{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"probe-{index}", "content": filler})
    messages.append({"role": "user", "content": "In one sentence: are you still here?"})

    try:
        response = await LLMClient()._call_llm(messages, [model], [], broken_models=set())
    except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
        return fail(f"{model} raised {type(exc).__name__} on an oversized conversation: {exc}")
    if response is None:
        return fail(
            f"{model} could not answer a conversation {_PROBE_OVERSHOOT}x its "
            f"{fit.window:,}-token window — the shrink did not save the call"
        )
    from robothor.engine.context import estimate_tokens

    return ok(
        f"{model} answered a conversation {_PROBE_OVERSHOOT}x its window; "
        f"the engine sent ~{estimate_tokens(messages):,} tokens of a "
        f"{fit.window:,}-token budget"
    )


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
    Check(
        id="models.local_fallback_ready",
        title="The local fallback can hold a conversation",
        category="models",
        # Required, and only on an instance that HAS one (it skips otherwise).
        # The tier exists for the hours when no cloud credential works; a
        # degraded one is discovered during exactly those hours.
        severity="required",
        run=_local_fallback_ready,
    ),
    Check(
        id="models.local_fallback_probe",
        title="An oversized conversation compacts instead of failing",
        category="models",
        severity="recommended",
        run=_local_fallback_probe,
        opt_in=True,
    ),
)

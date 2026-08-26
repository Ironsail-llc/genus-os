"""Model Registry — accurate context windows and pricing for all engine models.

Provides model-aware output token limits and pre-flight context checks
so the engine adapts to each model's capabilities instead of hardcoding.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache

from robothor.engine.sanitize import sanitize_log

logger = logging.getLogger(__name__)

# Default thinking budget for models that support extended thinking
THINKING_BUDGET_TOKENS = 10_000


@dataclass(frozen=True)
class ModelLimits:
    """Token limits and pricing for a single model."""

    max_input_tokens: int
    max_output_tokens: int
    default_output_tokens: int  # what we request by default
    input_cost_per_token: float
    output_cost_per_token: float
    cache_write_cost_per_token: float = 0.0  # prompt caching: write (Anthropic: 1.25x input)
    cache_read_cost_per_token: float = 0.0  # prompt caching: read (Anthropic: 0.1x input)
    supports_thinking: bool = False
    ttft_hint_ms: int = 3000  # estimated p50 time-to-first-token (ms) for interactive routing
    # Curated override for the cache_control capability (PR 4). None means
    # "no override — derive it" (see supports_cache_control()); True/False
    # forces the answer regardless of the OpenRouter blanket-exclusion or
    # litellm's catalog, for cases we've specifically verified either way.
    supports_cache_control: bool | None = None


# ─── Registry ────────────────────────────────────────────────────────

_MODEL_REGISTRY: dict[str, ModelLimits] = {
    # Codex via local ChatGPT/Codex subscription auth. Costs are intentionally
    # zero in API-token accounting because usage is governed by Codex plan quota.
    "codex/gpt-5.5": ModelLimits(
        max_input_tokens=1_050_000,
        max_output_tokens=128_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        supports_thinking=True,
        ttft_hint_ms=2500,
    ),
    "codex/gpt-5.4": ModelLimits(
        max_input_tokens=1_050_000,
        max_output_tokens=128_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        supports_thinking=True,
        ttft_hint_ms=2500,
    ),
    "codex/gpt-5.3-codex": ModelLimits(
        max_input_tokens=1_050_000,
        max_output_tokens=128_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        supports_thinking=True,
        ttft_hint_ms=2500,
    ),
    # Claude Sonnet 4.6 via OpenRouter
    "openrouter/anthropic/claude-sonnet-4.6": ModelLimits(
        max_input_tokens=1_000_000,
        max_output_tokens=128_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.000_003,  # $3/M
        output_cost_per_token=0.000_015,  # $15/M
        cache_write_cost_per_token=0.000_003_75,  # $3.75/M (1.25x input)
        cache_read_cost_per_token=0.000_000_3,  # $0.30/M (0.1x input)
        supports_thinking=True,
        ttft_hint_ms=1500,  # Anthropic via OpenRouter — fast
    ),
    # Ox Alpha - stealth model on OpenRouter. Specs read from the live
    # OpenRouter catalog on 2026-08-21, not estimated: context 1,048,576,
    # max_completion 131,072, pricing prompt/completion both "0" while in
    # stealth. Costs are therefore genuinely zero rather than unknown --
    # revisit when it exits stealth and starts billing, because an
    # unregistered or stale-priced model silently accounts at $0 and hides
    # real spend.
    "openrouter/stealth/ox-alpha": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=131_072,
        default_output_tokens=16_384,
        # $0 is CORRECT, not a gap: verified against OpenRouter's live models
        # API on 2026-08-24 (pricing.prompt=0, pricing.completion=0 — cloaked
        # stealth preview). Re-verify when the model de-cloaks; a stealth
        # model gaining a price while this reads 0 is the 07-07 dated-slug
        # cost bug all over again.
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        supports_thinking=False,
        ttft_hint_ms=1500,
    ),
    # Local offline tier — Qwen 3.8 27B on the system Ollama (>= 0.32, upgraded
    # in place 2026-08-24; 0.17.7 could not read this model's manifest).
    # Last-resort fallback when OpenRouter is down. Dense 27.3B, 262k ctx,
    # native tool use (verified through litellm on 2026-08-24: correct
    # create_task call, 1.9s warm / ~34s cold-load). Costs zero API dollars.
    #
    # DELIBERATELY on the default port (11434): ollama_chat/* in litellm routes
    # to http://localhost:11434 unless api_base is passed, and nothing in the
    # engine passes one. An earlier design ran a second server on :11435 and
    # silently sent every request to a model that did not exist on :11434 —
    # keep the tier where the client actually looks.
    "ollama_chat/qwen3.8:27b": ModelLimits(
        max_input_tokens=262_144,
        max_output_tokens=32_768,
        default_output_tokens=8_192,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        supports_thinking=False,
        ttft_hint_ms=9000,  # dense 27B on GB10 — slow, by design for disaster tier
    ),
    # Local Qwen3 8B — pf-watchdog's configured primary. Pulled 2026-08-24;
    # before that the manifest named a model no local server carried.
    "ollama_chat/qwen3:8b": ModelLimits(
        max_input_tokens=40_960,
        max_output_tokens=16_384,
        default_output_tokens=4_096,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        supports_thinking=False,
        ttft_hint_ms=4000,
    ),
    # GLM-5 via OpenRouter
    "openrouter/z-ai/glm-5": ModelLimits(
        max_input_tokens=204_800,
        max_output_tokens=65_536,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_8,  # $0.80/M
        output_cost_per_token=0.000_002_56,  # $2.56/M
        ttft_hint_ms=4000,  # Variable via OpenRouter
    ),
    # MiMo-V2-Pro via OpenRouter (superseded by V2.5 — kept for fallback reference)
    "openrouter/xiaomi/mimo-v2-pro": ModelLimits(
        max_input_tokens=1_000_000,
        max_output_tokens=65_536,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_001,  # $1/M
        output_cost_per_token=0.000_003,  # $3/M
        ttft_hint_ms=3000,
    ),
    # MiMo-V2.5 via OpenRouter — fleet-wide primary (2026-07-07)
    "openrouter/xiaomi/mimo-v2.5": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_105,  # $0.105/M
        output_cost_per_token=0.000_000_28,  # $0.28/M
        ttft_hint_ms=3000,
    ),
    # MiMo-V2.5-Pro via OpenRouter — fleet fallback (escalation from v2.5)
    "openrouter/xiaomi/mimo-v2.5-pro": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_435,  # $0.435/M
        output_cost_per_token=0.000_000_87,  # $0.87/M
        cache_read_cost_per_token=0.000_000_2,  # $0.20/M
        ttft_hint_ms=3000,
    ),
    # DeepSeek V4 Pro via OpenRouter — main's primary (2026-04-24)
    # 1.6T MoE / 49B active, 1M ctx, native tool use. Direct provider only
    # (data policy must allow it; io-net is rate-limited on shared pool).
    "openrouter/deepseek/deepseek-v4-pro": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=384_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.000_001_74,  # $1.74/M
        output_cost_per_token=0.000_003_48,  # $3.48/M
        cache_read_cost_per_token=0.000_000_145,  # $0.145/M
        ttft_hint_ms=1500,  # DeepSeek-direct p50 ~145ms
    ),
    # DeepSeek V4 Flash via OpenRouter — the fleet's single cloud fallback
    # (2026-08-26). Cheaper than the mimo-v2.5 primary it backs up
    # ($0.080/$0.159 vs $0.140/$0.280), same 1M context, native tool use.
    #
    # What it does and does not cover: it is a DIFFERENT MODEL on the SAME
    # OpenRouter credential, so it protects against mimo failing specifically
    # — a 400, a moderation block, that model being down — and NOT against the
    # key capping, which is the outage that actually happened this morning and
    # took a three-deep OpenRouter chain down with it. The on-device tier the
    # engine appends to every chain is the only defence against that.
    "openrouter/deepseek/deepseek-v4-flash": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        default_output_tokens=16_384,
        input_cost_per_token=0.000_000_08,  # $0.080/M
        output_cost_per_token=0.000_000_159,  # $0.159/M
        ttft_hint_ms=900,
    ),
    # Gemini 2.5 Flash
    "gemini/gemini-2.5-flash": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_535,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_15,  # $0.15/M
        output_cost_per_token=0.000_000_6,  # $0.60/M
        ttft_hint_ms=1000,  # Google direct — fast
    ),
    # MiniMax M2.5 via OpenRouter
    "openrouter/minimax/minimax-m2.5": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=131_072,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_5,  # $0.50/M
        output_cost_per_token=0.000_002,  # $2/M
        ttft_hint_ms=3000,
    ),
    # Claude Opus 4.7 via OpenRouter — released 2026-04-16
    "openrouter/anthropic/claude-opus-4.7": ModelLimits(
        max_input_tokens=1_000_000,
        max_output_tokens=128_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.000_005,  # $5/M
        output_cost_per_token=0.000_025,  # $25/M
        cache_write_cost_per_token=0.000_006_25,  # $6.25/M (1.25x input)
        cache_read_cost_per_token=0.000_000_5,  # $0.50/M (0.1x input)
        supports_thinking=True,
        ttft_hint_ms=3000,
    ),
    # Gemini 3.1 Pro Preview via OpenRouter
    "openrouter/google/gemini-3.1-pro-preview": ModelLimits(
        max_input_tokens=1_000_000,
        max_output_tokens=65_536,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_002,  # $2/M
        output_cost_per_token=0.000_012,  # $12/M
        ttft_hint_ms=2500,
    ),
    # GPT-5.4 via OpenRouter
    "openrouter/openai/gpt-5.4": ModelLimits(
        max_input_tokens=1_050_000,
        max_output_tokens=128_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.000_002_5,  # $2.50/M
        output_cost_per_token=0.000_015,  # $15/M
        ttft_hint_ms=2000,
    ),
    # Gemini 2.5 Pro
    "gemini/gemini-2.5-pro": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_535,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_001_25,  # $1.25/M
        output_cost_per_token=0.000_01,  # $10/M
        ttft_hint_ms=2500,  # Google direct — moderate
    ),
}

# Conservative fallback for unknown models
_FALLBACK = ModelLimits(
    max_input_tokens=128_000,
    max_output_tokens=8_192,
    default_output_tokens=8_192,
    input_cost_per_token=0.000_001,
    output_cost_per_token=0.000_003,
)

# Cache of ModelLimits derived from litellm's bundled catalog (G6), keyed by
# model id. None means "litellm has no entry" — don't re-probe every call.
_catalog_cache: dict[str, ModelLimits | None] = {}


def _from_litellm_catalog(model_id: str) -> ModelLimits | None:
    """Build ModelLimits from litellm's bundled model catalog, or None.

    litellm ships ``model_prices_and_context_window.json`` (context windows +
    pricing for hundreds of models) — effectively a Models.dev-style catalog
    with no network dependency. We consult it for models not in our curated
    ``_MODEL_REGISTRY`` so unknown models get accurate limits instead of the
    flat 128K/8K fallback.
    """
    if model_id in _catalog_cache:
        return _catalog_cache[model_id]
    result: ModelLimits | None = None
    try:
        import litellm

        info = litellm.get_model_info(model_id)
        if info:
            max_in = int(info.get("max_input_tokens") or info.get("max_tokens") or 0)
            max_out = int(info.get("max_output_tokens") or 0)
            if max_in > 0:
                default_out = min(16_384, max_out) if max_out else 8_192
                result = ModelLimits(
                    max_input_tokens=max_in,
                    max_output_tokens=max_out or default_out,
                    default_output_tokens=default_out,
                    input_cost_per_token=float(info.get("input_cost_per_token") or 0.0),
                    output_cost_per_token=float(info.get("output_cost_per_token") or 0.0),
                    cache_read_cost_per_token=float(info.get("cache_read_input_token_cost") or 0.0),
                    supports_thinking=bool(info.get("supports_reasoning", False)),
                )
    except Exception as e:  # noqa: BLE001 — litellm raises for unknown models
        logger.debug("litellm catalog lookup failed for '%s': %s", sanitize_log(model_id), e)
    _catalog_cache[model_id] = result
    return result


@lru_cache(maxsize=256)
def _dynamic_model_limits(model_id: str) -> ModelLimits | None:
    """Look up limits from litellm's model catalog (cached). None if unknown.

    Lets the engine size models that aren't in the hand-maintained
    ``_MODEL_REGISTRY`` instead of silently using a wrong 128K fallback.
    """
    try:
        import litellm

        info = litellm.get_model_info(model_id)
    except Exception:
        return None
    if not info:
        return None
    max_in = info.get("max_input_tokens") or info.get("max_tokens")
    if not max_in:
        return None
    max_out = int(info.get("max_output_tokens") or 8_192)
    return ModelLimits(
        max_input_tokens=int(max_in),
        max_output_tokens=max_out,
        default_output_tokens=min(max_out, 8_192),
        input_cost_per_token=float(info.get("input_cost_per_token") or 0.0),
        output_cost_per_token=float(info.get("output_cost_per_token") or 0.0),
    )


_DATED_SLUG_RE = re.compile(r"-2\d{7}$")  # e.g. xiaomi/mimo-v2.5-20260422

# Routing prefixes we add to reach a provider. The provider never echoes them
# back, so they must come off before two ids can be compared.
_ROUTE_PREFIXES = ("openrouter/", "litellm_proxy/", "ollama_chat/", "ollama/")


def canonical_model_id(model_id: str) -> str:
    """Normalize a model id so request and response forms compare equal.

    ``openrouter/xiaomi/mimo-v2.5`` (what a manifest configures) and
    ``xiaomi/mimo-v2.5-20260422`` (what the provider reports back, and what
    ``agent_runs.model_used`` therefore stores) name the same model. Strip the
    routing prefix and the dated release suffix and lowercase. Anything that
    compares a configured model id against a recorded one must go through
    here, or every healthy run looks like a fallback.

    Args:
        model_id: A model id in either request or response form.

    Returns:
        The lowercased id with routing prefix and dated slug removed.
    """
    canonical = (model_id or "").strip().lower()
    for prefix in _ROUTE_PREFIXES:
        if canonical.startswith(prefix):
            canonical = canonical[len(prefix) :]
            break
    return _DATED_SLUG_RE.sub("", canonical)


def _registry_candidates(model_id: str) -> list[str]:
    """Aliases under which a model id may appear in ``_MODEL_REGISTRY``.

    Provider responses differ from request ids two ways: OpenRouter appends a
    dated release suffix (``xiaomi/mimo-v2.5-20260422``) and drops our
    ``openrouter/`` routing prefix. Cost fallback and context sizing look up
    the *response* id, so both variants must resolve to the curated entry.
    """
    candidates = [model_id]
    stripped = _DATED_SLUG_RE.sub("", model_id)
    if stripped != model_id:
        candidates.append(stripped)
    candidates.extend(
        f"openrouter/{c}" for c in list(candidates) if not c.startswith("openrouter/") and "/" in c
    )
    return candidates


def get_model_limits(model_id: str) -> ModelLimits:
    """Look up model limits.

    Order: curated ``_MODEL_REGISTRY`` (authoritative — carries Genus-specific
    facts like codex $0 / cache pins) → litellm's bundled catalog when
    catalog-backed mode (Rip 17 / G6) is on → conservative fallback. The curated
    registry always wins so our hand-tuned pricing and thinking flags stand.
    """
    for candidate in _registry_candidates(model_id):
        limits = _MODEL_REGISTRY.get(candidate)
        if limits:
            return limits

    dynamic = _dynamic_model_limits(model_id)
    if dynamic is not None:
        return dynamic

    logger.warning(
        "Unknown model '%s' — not in the static registry or litellm catalog; "
        "using conservative %dK fallback. Add it to model_registry._MODEL_REGISTRY.",
        sanitize_log(model_id),
        _FALLBACK.max_input_tokens // 1000,
    )
    return _FALLBACK


def supports_cache_control(model_id: str) -> bool:
    """Whether ``model_id`` should get Anthropic-style prompt-cache content
    blocks — the ``cache_control`` conversion in ``LLMClient._build_llm_kwargs``.

    Replaces the historical ``model.startswith("anthropic/")`` gate with a
    catalog-driven capability lookup so caching follows fleet model changes
    instead of a string prefix.

    Order:
    1. Curated ``_MODEL_REGISTRY`` override (``ModelLimits.supports_cache_control``,
       when explicitly set) — always wins, including for OpenRouter ids.
    2. OpenRouter-prefixed models default to False. OpenRouter proxies models
       through its OpenAI-compatible completion path, which chokes on mixed
       text/``cache_control`` content blocks and breaks tool_use/tool_result
       pairing — this holds even for ``openrouter/anthropic/*``, where
       litellm's catalog reports the underlying Claude model supports prompt
       caching, but OpenRouter's transport doesn't accept our content-block
       format for it. (Historically documented at the old gate's call site in
       ``llm_client.py``; this is now the single source of truth.)
    3. litellm's bundled ``supports_prompt_caching`` — safe against unmapped
       custom providers (e.g. ``codex/*``, a subscription provider litellm
       doesn't catalog; it reports False rather than raising).
    4. Default False.

    Deliberately NO ``anthropic/``-prefix fallback: litellm returns False
    both for models explicitly marked unsupported and for ids it simply
    hasn't mapped, and the two are indistinguishable from the return value —
    a prefix fallback that treats False as "catalog lag, assume True" would
    also flip genuinely unsupported models to True, making this an
    untrustworthy capability oracle. If a newly-released direct-Anthropic
    fleet model lags litellm's catalog, pin it with a curated
    ``supports_cache_control=True`` override in ``_MODEL_REGISTRY``.
    """
    limits = _MODEL_REGISTRY.get(model_id)
    if limits is not None and limits.supports_cache_control is not None:
        return limits.supports_cache_control

    if model_id.startswith("openrouter/"):
        return False

    return _cache_control_from_litellm(model_id)


@lru_cache(maxsize=256)
def _cache_control_from_litellm(model_id: str) -> bool:
    """litellm's ``supports_prompt_caching`` lookup, cached and exception-safe."""
    try:
        from litellm.utils import supports_prompt_caching

        return bool(supports_prompt_caching(model=model_id))
    except Exception as e:  # noqa: BLE001 — litellm may raise for unusual ids
        logger.debug(
            "supports_prompt_caching lookup failed for '%s': %s",
            sanitize_log(model_id),
            e,
        )
        return False


def register_pricing_with_litellm() -> None:
    """Seed litellm's cost table so ``completion_cost`` prices our models.

    When catalog-backed mode (Rip 17 / G6) is on, registers EVERY model in the
    curated ``_MODEL_REGISTRY`` from that single source — ending the historical
    drift where a separate hand-maintained dict in runner.py held divergent
    prices. When off, registers the legacy two-model dict to preserve exact
    prior behavior until the flag is flipped.
    """
    import litellm

    from robothor.engine.feature_flags import catalog_backed_models_enabled

    if catalog_backed_models_enabled():
        litellm.register_model(
            {
                model_id: {
                    "max_tokens": limits.max_input_tokens,
                    "input_cost_per_token": limits.input_cost_per_token,
                    "output_cost_per_token": limits.output_cost_per_token,
                }
                for model_id, limits in _MODEL_REGISTRY.items()
                # codex is subscription-billed ($0) — leave it out of litellm pricing.
                if not model_id.startswith("codex/")
            }
        )
    else:
        # Legacy block (verbatim prior behavior) — kept until Rip 17 is enabled.
        litellm.register_model(
            {
                "openrouter/xiaomi/mimo-v2-pro": {
                    "max_tokens": 1000000,
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000003,
                },
                "openrouter/anthropic/claude-sonnet-4.6": {
                    "max_tokens": 200000,
                    "input_cost_per_token": 0.000003,
                    "output_cost_per_token": 0.000015,
                },
            }
        )


# Reasoning-effort → thinking-token budget. Promotes the single global
# THINKING_BUDGET_TOKENS into a per-agent setting (AgentConfig.reasoning_effort).
_REASONING_BUDGETS: dict[str, int] = {
    "low": 2_000,
    "medium": THINKING_BUDGET_TOKENS,  # 10_000 — preserves the prior default
    "high": 24_000,
    "max": 48_000,
}


def reasoning_budget_tokens(effort: str) -> int:
    """Thinking-token budget for a reasoning-effort level (default medium)."""
    return _REASONING_BUDGETS.get((effort or "medium").strip().lower(), THINKING_BUDGET_TOKENS)


# Per-run reasoning effort. A ContextVar so concurrent runs (each its own asyncio
# task) don't race; set by the runner at run start, read when building thinking kwargs.
_reasoning_effort_ctx: ContextVar[str] = ContextVar("reasoning_effort", default="medium")


def set_reasoning_effort(effort: str) -> None:
    """Set the reasoning effort for the current run's task context."""
    _reasoning_effort_ctx.set(effort or "medium")


def current_thinking_budget() -> int:
    """Thinking-token budget for the current run's reasoning effort."""
    return reasoning_budget_tokens(_reasoning_effort_ctx.get())


def compute_token_budget(model_id: str, max_iterations: int) -> int:
    """Compute estimated token budget for observability/tracking.

    This is NOT enforced as a hard limit. Used for:
    - Soft warnings when approaching 80% usage
    - Post-run analytics (budget_exhausted flag)
    - Sub-agent budget cascade (tracking only)

    Returns 0 (unlimited) if max_iterations is 0.
    """
    if max_iterations <= 0:
        return 0
    limits = get_model_limits(model_id)
    return limits.max_input_tokens * max_iterations


def get_output_tokens(model_id: str, estimated_input_tokens: int = 0) -> int:
    """Calculate the output token limit for a model given estimated input.

    Returns min(default_output, max_output, remaining_window) so the
    output request never overflows the context window.
    """
    limits = get_model_limits(model_id)

    # Remaining window after input
    remaining = limits.max_input_tokens - estimated_input_tokens
    if remaining <= 0:
        # Context is already full — request minimum to get a response
        return min(1024, limits.max_output_tokens)

    return min(limits.default_output_tokens, limits.max_output_tokens, remaining)

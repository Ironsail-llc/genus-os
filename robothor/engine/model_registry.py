"""Model Registry — accurate context windows and pricing for all engine models.

Provides model-aware output token limits and pre-flight context checks
so the engine adapts to each model's capabilities instead of hardcoding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelLimits:
    """Token limits and pricing for a single model."""

    max_input_tokens: int
    max_output_tokens: int
    default_output_tokens: int  # what we request by default
    input_cost_per_token: float
    output_cost_per_token: float


# ─── Registry ────────────────────────────────────────────────────────

_MODEL_REGISTRY: dict[str, ModelLimits] = {
    # Claude Sonnet 4.6 via OpenRouter
    "openrouter/anthropic/claude-sonnet-4-6": ModelLimits(
        max_input_tokens=200_000,
        max_output_tokens=64_000,
        default_output_tokens=16_384,
        input_cost_per_token=0.000_003,  # $3/M
        output_cost_per_token=0.000_015,  # $15/M
    ),
    # Kimi K2.5 via OpenRouter
    "openrouter/moonshotai/kimi-k2.5": ModelLimits(
        max_input_tokens=262_144,
        max_output_tokens=262_144,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_6,  # $0.60/M
        output_cost_per_token=0.000_002_4,  # $2.40/M
    ),
    # Gemini 2.5 Flash
    "gemini/gemini-2.5-flash": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_535,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_15,  # $0.15/M
        output_cost_per_token=0.000_000_6,  # $0.60/M
    ),
    # MiniMax M2.5 via OpenRouter
    "openrouter/minimax/minimax-m2.5": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=131_072,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_000_5,  # $0.50/M
        output_cost_per_token=0.000_002,  # $2/M
    ),
    # Gemini 2.5 Pro
    "gemini/gemini-2.5-pro": ModelLimits(
        max_input_tokens=1_048_576,
        max_output_tokens=65_535,
        default_output_tokens=8_192,
        input_cost_per_token=0.000_001_25,  # $1.25/M
        output_cost_per_token=0.000_01,  # $10/M
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


def get_model_limits(model_id: str) -> ModelLimits:
    """Look up model limits. Returns conservative fallback for unknown models."""
    limits = _MODEL_REGISTRY.get(model_id)
    if limits:
        return limits
    logger.debug("Unknown model '%s', using fallback limits", model_id)
    return _FALLBACK


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

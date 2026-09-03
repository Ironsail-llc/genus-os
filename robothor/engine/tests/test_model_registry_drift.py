"""The hand-maintained model table must not drift from the catalog unnoticed.

`_MODEL_REGISTRY` is 19 entries maintained by hand, and it WINS over the
litellm catalog in `get_model_limits`. It is there for a good reason — the
catalog carries no per-token cost for most OpenRouter routes, and cost
accounting needs one — but a hand-maintained table beside an authoritative one
is precisely the shape that produced #329/#330/#331, where three bug reports
turned out to be one hand-maintained name list drifting from what was actually
registered.

Measured 2026-08-27, three entries disagree with the catalog on
`max_input_tokens`. None currently causes a failure, and the reasons differ
per entry — so this is a RATCHET, not a hard equality check:

* a known disagreement is listed below WITH its reason, and stays green
* a NEW disagreement fails, and someone decides which source is right

Failing on every disagreement would be wrong: the catalog is not always
correct for OpenRouter routes, and an entry deliberately pinned against a bad
catalog value would make the suite permanently red.
"""

from __future__ import annotations

import pytest

from robothor.engine.model_registry import _MODEL_REGISTRY, _from_litellm_catalog

#: model -> why the hand-maintained value differs from the catalog.
#: Adding an entry here is a decision, and the reason is the point.
KNOWN_DRIFT: dict[str, str] = {
    "openrouter/z-ai/glm-5": (
        "registry 204,800 vs catalog 202,752 — a 1% difference, and compaction "
        "fires at the 80,000 absolute budget long before either binds."
    ),
    "openrouter/minimax/minimax-m2.5": (
        "registry 1,048,576 vs catalog 196,608. The registry value is the "
        "optimistic one; it is inert because compaction fires at 80,000, but "
        "if ROBOTHOR_COMPACTION_TRIGGER_TOKENS is ever raised or disabled this "
        "is the first entry that would overflow."
    ),
    "openrouter/google/gemini-3.1-pro-preview": (
        "registry 1,000,000 vs catalog 1,048,576 — we compact 5% early on a "
        "model the fleet has not used in the last 7 days."
    ),
}


def _catalog_input(model_id: str) -> int | None:
    entry = _from_litellm_catalog(model_id)
    return getattr(entry, "max_input_tokens", None) if entry else None


def _comparable() -> list[tuple[str, int, int]]:
    rows = []
    for model_id, limits in _MODEL_REGISTRY.items():
        catalog = _catalog_input(model_id)
        if catalog:
            rows.append((model_id, limits.max_input_tokens, catalog))
    return rows


def test_the_catalog_is_reachable_at_all():
    """If the lookup silently returns None for everything, every assertion
    below passes vacuously — which is how a drift check becomes decoration."""
    assert _comparable(), "no registry entry could be compared against the catalog"


def test_no_unexplained_drift_from_the_catalog():
    surprises = {
        model_id: f"registry {ours:,} vs catalog {theirs:,}"
        for model_id, ours, theirs in _comparable()
        if ours != theirs and model_id not in KNOWN_DRIFT
    }
    assert not surprises, (
        "the hand-maintained table disagrees with the catalog on entries "
        f"nobody has ruled on: {surprises}. Decide which is right, then either "
        "correct the registry or add the model to KNOWN_DRIFT with the reason."
    )


def test_known_drift_is_still_actually_drifting():
    """A ratchet that keeps stale exemptions stops being one. When an entry is
    corrected, its exemption has to go with it."""
    stale = {
        model_id
        for model_id, ours, theirs in _comparable()
        if ours == theirs and model_id in KNOWN_DRIFT
    }
    assert not stale, f"these now agree with the catalog — drop them from KNOWN_DRIFT: {stale}"


@pytest.mark.parametrize("model_id", sorted(KNOWN_DRIFT))
def test_every_exemption_carries_a_reason(model_id):
    assert len(KNOWN_DRIFT[model_id]) > 40, "an exemption without a reason is a silenced test"


def test_a_registry_entry_never_claims_more_output_than_input():
    """`get_output_tokens` returns min(default, max_output, remaining). An entry
    whose output ceiling exceeds its whole input window is incoherent, and the
    min would hide it."""
    for model_id, limits in _MODEL_REGISTRY.items():
        assert limits.max_output_tokens <= limits.max_input_tokens, model_id


def test_every_entry_has_a_default_below_its_ceiling():
    for model_id, limits in _MODEL_REGISTRY.items():
        assert limits.default_output_tokens <= limits.max_output_tokens, model_id


#: What the fleet actually ran in the 7 days to 2026-08-27.
LIVE_MODELS = [
    "openrouter/xiaomi/mimo-v2.5",
    "openrouter/xiaomi/mimo-v2.5-pro",
    "openrouter/stealth/ox-alpha",
    "ollama_chat/qwen3.8:27b",
    "openrouter/deepseek/deepseek-v4-pro",
]


@pytest.mark.parametrize("model_id", LIVE_MODELS)
def test_a_model_the_fleet_runs_has_its_own_entry(model_id):
    """Falling through to catalog defaults costs the tempo hint and the cost
    fields, so budgets are sized for the wrong model and spend is estimated
    rather than known.

    Deliberately NOT asserting a non-zero price: `openrouter/stealth/ox-alpha`
    is a cloaked preview whose price really is zero — verified against
    OpenRouter's live models API on 2026-08-24 — and `ollama_chat/*` is local.
    Asserting "every model costs something" would have flagged both, which is
    how a check trains people to delete it.
    """
    from robothor.engine.model_registry import canonical_model_id

    assert canonical_model_id(model_id) in _MODEL_REGISTRY or model_id in _MODEL_REGISTRY


@pytest.mark.parametrize("model_id", LIVE_MODELS)
def test_a_live_model_has_a_tempo_hint(model_id):
    """`tempo_factor` scales every manifest budget by the model's time to first
    token. A missing hint silently sizes a local 27B model's watchdog against
    cloud latency."""
    from robothor.engine.model_registry import get_model_limits

    assert get_model_limits(model_id).ttft_hint_ms > 0, model_id

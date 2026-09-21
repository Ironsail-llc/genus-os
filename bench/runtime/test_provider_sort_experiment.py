from copy import deepcopy

import pytest

from bench.runtime.provider_sort_experiment import apply


def test_preference_preserves_all_provider_constraints_without_mutating_input():
    original = {
        "extra_body": {
            "provider": {
                "only": ["allowed"],
                "allow_fallbacks": False,
                "max_price": {"prompt": 1},
                "data_collection": "deny",
            }
        },
        "temperature": 0.5,
    }
    before = deepcopy(original)
    result = apply("openrouter/test/model", original, "throughput")
    assert original == before
    provider = result["extra_body"]["provider"]
    assert provider.pop("sort") == "throughput"
    assert result == before


@pytest.mark.parametrize("field,value", [("order", ["preferred"]), ("sort", "latency")])
def test_existing_provider_preferences_are_not_overridden(field, value):
    with pytest.raises(ValueError, match="must not override"):
        apply("openrouter/test/model", {"extra_body": {"provider": {field: value}}}, "throughput")


def test_local_model_is_unchanged_and_invalid_preference_rejected():
    original = {"timeout": 600}
    assert apply("ollama_chat/model", original, "throughput") is original
    with pytest.raises(ValueError):
        apply("openrouter/test/model", {}, "unknown")

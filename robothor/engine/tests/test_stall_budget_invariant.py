"""A stall budget shorter than the call it measures is guaranteed to misfire.

2026-08-27. Six manifests carried stall budgets of 120 or 180 seconds, each
with a changelog line recording that the number was tuned against a CLOUD
model. When the OpenRouter weekly cap sent the fleet to the local tier -- where
the engine itself allows a single call 600s (LLM_REQUEST_TIMEOUT_OLLAMA) --
every one of them became shorter than the call it was supposed to be measuring.
The watchdog killed 33 healthy runs in a day.

That is not a tuning mistake, it is an INVARIANT violation: a run's stall
budget must exceed the per-call allowance for the models in its chain, or the
watchdog is guaranteed to fire on a call the LLM layer still considers healthy.

Hand-auditing the manifests found six. It missed two more (devops-analyst and
email-briefing, both 300s, one of them actively failing at the time). That miss
is the reason this check exists: nobody should have to read YAML by eye.

The comparison is against the SCALED budget, not the raw manifest number,
because tier-aware budgets (watchdog_budgets.py) already multiply it by the
chain's slowest model. That distinction matters and is easy to get wrong: a raw
120 is a scaled 360 and still misfires, but a raw 300 is a scaled 900 and is
genuinely safe. The two 300s were dangerous THIS MORNING, before scaling
existed; they are not dangerous now. A guard forced to flag all eight would be
warning about values that are currently correct, so it flags six — every value
unsafe under current semantics — and TestScalingChangedTheAnswer pins why.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from robothor.engine.config_schema import validate_manifest

LOCAL = "ollama_chat/qwen3.8:27b"
CLOUD = "openrouter/xiaomi/mimo-v2.5"


def _manifest(**schedule):
    return {
        "id": "probe",
        "model": {"primary": CLOUD, "fallbacks": [LOCAL]},
        "schedule": schedule,
    }


def _stall_warnings(warnings):
    return [w for w in warnings if "stall" in w.lower()]


class TestTheInvariantIsChecked:
    def test_a_cloud_calibrated_budget_is_flagged(self):
        """120 scales to 360 on the local tier, still under the 600s allowance."""
        w = _stall_warnings(validate_manifest(_manifest(stall_timeout_seconds=120)))
        assert w, "the exact shape that killed 33 runs was accepted silently"
        assert "600" in w[0], "the warning should name the allowance it violates"

    def test_an_early_stall_budget_is_checked_too(self):
        """main's heartbeat failed on early_stall, not stall."""
        w = _stall_warnings(validate_manifest(_manifest(early_stall_timeout_seconds=120)))
        assert w

    def test_a_budget_that_survives_scaling_is_accepted(self):
        """300 -> 900 scaled, comfortably past the allowance."""
        assert not _stall_warnings(validate_manifest(_manifest(stall_timeout_seconds=300)))

    def test_a_disabled_budget_is_not_flagged(self):
        """0 means the watchdog is off. `_defaults.yaml` sets this fleet-wide;
        warning on it would bury the real findings in noise."""
        assert not _stall_warnings(validate_manifest(_manifest(stall_timeout_seconds=0)))

    def test_a_cloud_only_chain_is_judged_against_the_cloud_allowance(self):
        """Not every instance has a local tier; the check must not invent one."""
        m = {
            "id": "probe",
            "model": {"primary": CLOUD, "fallbacks": []},
            "schedule": {"stall_timeout_seconds": 60},
        }
        assert _stall_warnings(validate_manifest(m)), "60s < the 120s cloud allowance"

    def test_the_heartbeat_and_worker_blocks_are_checked(self):
        """main's failure was in its HEARTBEAT block, not the top-level one --
        the same place the 2026-08-23 manifest incident hid."""
        m = _manifest()
        m["heartbeat"] = {"early_stall_timeout_seconds": 120}
        assert _stall_warnings(validate_manifest(m))


class TestTheWholeFleetSatisfiesIt:
    """Acceptance is all eight manifests, not the six found by eye."""

    def test_no_live_manifest_violates_the_invariant(self):
        agents_dir = Path.home() / "robothor" / "docs" / "agents"
        if not agents_dir.is_dir():
            pytest.skip("instance manifests not present (platform-only checkout)")
        offenders = []
        for f in sorted(agents_dir.glob("*.yaml")):
            if f.name.startswith("_") or ".bak" in f.name:
                continue
            try:
                data = yaml.safe_load(f.read_text()) or {}
            except Exception:
                continue
            if not isinstance(data, dict) or not data.get("id"):
                continue
            data.setdefault("model", {"primary": CLOUD, "fallbacks": [LOCAL]})
            offenders.extend(f"{f.name}: {w}" for w in _stall_warnings(validate_manifest(data)))
        assert not offenders, "manifests still violating the stall invariant:\n" + "\n".join(
            offenders
        )


class TestScalingChangedTheAnswer:
    """Why the guard flags six of the eight, not all eight.

    Landing tier-aware budgets moved the line. Recording it here so a future
    reader does not "fix" the guard to flag a value that is now correct.
    """

    def test_three_hundred_would_have_been_unsafe_unscaled(self):
        """The state this morning: the raw number was what the watchdog enforced."""
        from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT_OLLAMA

        assert LLM_REQUEST_TIMEOUT_OLLAMA > 300  # would misfire

    def test_three_hundred_is_safe_once_the_chain_is_scaled(self):
        from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT_OLLAMA
        from robothor.engine.model_registry import chain_tempo_factor

        scaled = 300 * chain_tempo_factor([CLOUD, LOCAL])
        assert scaled > LLM_REQUEST_TIMEOUT_OLLAMA
        assert not _stall_warnings(validate_manifest(_manifest(stall_timeout_seconds=300)))

    def test_one_twenty_is_still_unsafe_even_scaled(self):
        """The six the guard does flag."""
        from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT_OLLAMA
        from robothor.engine.model_registry import chain_tempo_factor

        assert 120 * chain_tempo_factor([CLOUD, LOCAL]) < LLM_REQUEST_TIMEOUT_OLLAMA
        assert _stall_warnings(validate_manifest(_manifest(stall_timeout_seconds=120)))

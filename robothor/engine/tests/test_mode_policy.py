"""Each mode carries its own economics. Neither may borrow the other's.

2026-08-27. Both local models are registered ``input_cost_per_token=0.0``, so
every monetary governor in the engine sees unlimited local inference as free --
while local inference is precisely what caused three prior thermal hard-cuts.
"Free" was never true; local is paid in heat, resident memory and inference
slots, in a currency no governor could read.

That is the whole argument for splitting the ruleset. These tests assert the
split in *both* directions, because a policy object that silently applies the
wrong economics is worse than no policy object at all:

* the monetary governor is inert in LOCAL -- there is no money to spend;
* the thermal/slot governor is inert in CLOUD -- someone else's datacentre is
  not our heat budget.

``TestCloudReproducesTodaysBehaviour`` is the regression guard that lets this
ship: if CLOUD policy is not today's constants exactly, this is a rewrite
wearing an abstraction's clothes.
"""

from robothor.engine.execution_mode import ExecutionMode
from robothor.engine.host_profile import DEFAULT, PROBED, HostProfile, Reading
from robothor.engine.mode_policy import ModePolicy, policy_for


def _profile(slots: int = 2, thermal: bool = True) -> HostProfile:
    return HostProfile(
        accelerator=Reading("cuda", PROBED),
        total_memory_gb=Reading(64.0, PROBED),
        available_memory_gb=Reading(32.0, PROBED),
        inference_slots=Reading(slots, PROBED),
        thermal_sensors=Reading(thermal, PROBED),
    )


class TestCloudReproducesTodaysBehaviour:
    """The abstraction must be behaviour-neutral in CLOUD or it cannot ship."""

    def test_cloud_keeps_the_configured_concurrency(self):
        p = policy_for(ExecutionMode.CLOUD, _profile(slots=2), cloud_max_concurrent=3)
        assert p.max_concurrent_runs == 3
        assert p.reserved_interactive_slots == 0

    def test_cloud_does_not_scale_time_budgets(self):
        p = policy_for(ExecutionMode.CLOUD, _profile())
        assert p.time_budget_multiplier == 1.0

    def test_cloud_uses_the_cloud_request_timeout(self):
        from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT

        p = policy_for(ExecutionMode.CLOUD, _profile())
        assert p.request_timeout_seconds == LLM_REQUEST_TIMEOUT

    def test_cloud_ignores_the_host_profile_for_concurrency(self):
        """A 1-slot laptop must not throttle cloud fan-out."""
        p = policy_for(ExecutionMode.CLOUD, _profile(slots=1), cloud_max_concurrent=8)
        assert p.max_concurrent_runs == 8


class TestLocalIsBoundByTheDevice:
    def test_concurrency_tracks_the_profile_not_a_constant(self):
        assert policy_for(ExecutionMode.LOCAL, _profile(slots=4)).max_concurrent_runs == 4
        assert policy_for(ExecutionMode.LOCAL, _profile(slots=2)).max_concurrent_runs == 2

    def test_one_slot_is_held_for_an_interactive_turn(self):
        p = policy_for(ExecutionMode.LOCAL, _profile(slots=2))
        assert p.reserved_interactive_slots == 1
        assert p.background_slots == 1

    def test_a_single_slot_device_still_yields_a_usable_policy(self):
        """A laptop with one slot must not end up with zero background capacity
        AND zero interactive capacity -- that is a deadlock, not a policy."""
        p = policy_for(ExecutionMode.LOCAL, _profile(slots=1, thermal=False))
        assert p.max_concurrent_runs >= 1
        assert p.background_slots >= 1

    def test_an_unknown_slot_count_falls_back_conservatively(self):
        profile = HostProfile(
            accelerator=Reading(None, DEFAULT),
            total_memory_gb=Reading(None, DEFAULT),
            available_memory_gb=Reading(None, DEFAULT),
            inference_slots=Reading(None, DEFAULT),
            thermal_sensors=Reading(False, PROBED),
        )
        p = policy_for(ExecutionMode.LOCAL, profile)
        assert p.max_concurrent_runs >= 1

    def test_local_absorbs_backpressure_rather_than_failing_fast(self):
        from robothor.engine.llm_client import LOCAL_CAPACITY_RETRIES

        p = policy_for(ExecutionMode.LOCAL, _profile())
        assert p.capacity_retries == LOCAL_CAPACITY_RETRIES
        assert p.capacity_retries > policy_for(ExecutionMode.CLOUD, _profile()).capacity_retries

    def test_local_uses_the_local_request_allowance(self):
        from robothor.engine.llm_client import LLM_REQUEST_TIMEOUT_OLLAMA

        p = policy_for(ExecutionMode.LOCAL, _profile())
        assert p.request_timeout_seconds == LLM_REQUEST_TIMEOUT_OLLAMA


class TestNeitherModeBorrowsTheOthersEconomics:
    def test_the_monetary_governor_is_inert_in_local(self):
        """There is no money to spend. A cost cap here would be theatre."""
        assert policy_for(ExecutionMode.LOCAL, _profile()).monetary_governor is False

    def test_the_monetary_governor_is_live_in_cloud(self):
        assert policy_for(ExecutionMode.CLOUD, _profile()).monetary_governor is True

    def test_the_thermal_governor_is_inert_in_cloud(self):
        """Someone else's datacentre is not our heat budget."""
        assert policy_for(ExecutionMode.CLOUD, _profile(thermal=True)).thermal_governor is False

    def test_the_thermal_governor_is_live_in_local_when_readable(self):
        assert policy_for(ExecutionMode.LOCAL, _profile(thermal=True)).thermal_governor is True

    def test_the_thermal_governor_stays_off_when_nothing_can_be_read(self):
        """Unknown is not zero: no sensor means skip, not 'assume cool'."""
        assert policy_for(ExecutionMode.LOCAL, _profile(thermal=False)).thermal_governor is False


class TestPolicyIsPure:
    def test_policy_is_a_value_not_a_service(self):
        a = policy_for(ExecutionMode.LOCAL, _profile())
        b = policy_for(ExecutionMode.LOCAL, _profile())
        assert a == b
        assert isinstance(a, ModePolicy)

    def test_the_policy_explains_itself(self):
        d = policy_for(ExecutionMode.LOCAL, _profile()).describe()
        assert d["mode"] == "local"
        assert d["monetary_governor"] is False

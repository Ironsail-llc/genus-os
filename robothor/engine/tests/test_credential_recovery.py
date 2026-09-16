"""Getting a credential back, which nothing could do without a restart.

2026-09-16: the operator raised the cap at the provider and nothing changed.
A key retired for a calendar quota sits out six hours in the engine's memory,
and neither a top-up nor a raised limit is visible to it. The pool had no way
to say how long was left, the reload had no way to say what came back, and
there was no command to run.
"""

from __future__ import annotations

import pytest

from robothor.engine import key_pool
from robothor.engine.key_pool import KeyPool, Retirement, SlotStatus


class _Clock:
    """A clock the test moves, because a cooldown is measured, not waited."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class TestThePoolSaysHowLongIsLeft:
    def test_a_key_in_rotation_carries_no_timings(self):
        pool = KeyPool(["sk-one"])
        status = pool.status()[0]
        assert status.available
        assert status.retired_for_s is None and status.returns_in_s is None

    def test_a_quota_retirement_reports_elapsed_and_remaining(self):
        clock = _Clock()
        pool = KeyPool(["sk-one"], clock=clock)
        pool.retire("sk-one", Retirement.QUOTA_EXHAUSTED_PERIODIC)
        clock.now += 2 * 3600

        status = pool.status()[0]

        assert not status.available
        assert status.reason is Retirement.QUOTA_EXHAUSTED_PERIODIC
        assert status.retired_for_s == pytest.approx(2 * 3600)
        # Six hours, less the two that have passed.
        assert status.returns_in_s == pytest.approx(4 * 3600)

    def test_a_rejected_key_never_returns_on_its_own(self):
        pool = KeyPool(["sk-one"])
        pool.retire("sk-one", Retirement.AUTH_FAILED)
        status = pool.status()[0]
        assert not status.available
        assert status.returns_in_s is None, "a revoked key has no return time to promise"

    def test_an_elapsed_cooldown_reads_as_back_in_rotation(self):
        """`_available` is what clears the record, so it is consulted first."""
        clock = _Clock()
        pool = KeyPool(["sk-one"], clock=clock)
        pool.retire("sk-one", Retirement.CREDIT_EXHAUSTED)
        clock.now += key_pool.CREDIT_COOLDOWN_SECONDS + 1

        assert pool.status()[0].available


class TestTheReloadSaysWhatCameBack:
    @pytest.fixture(autouse=True)
    def _no_vault(self, monkeypatch):
        monkeypatch.setattr(key_pool, "refresh_vault_snapshot", dict)
        monkeypatch.setattr(key_pool, "_SHARED", {"OPENROUTER_API_KEY": KeyPool(["sk-one"])})

    def test_a_retired_credential_is_named_as_restored(self, monkeypatch):
        monkeypatch.setattr(
            key_pool,
            "provider_slots",
            lambda provider_id: (
                [
                    SlotStatus(
                        position=1,
                        source="env",
                        fingerprint="sha256:ab12cd34",
                        state="capped",
                        reason=Retirement.QUOTA_EXHAUSTED_PERIODIC,
                    )
                ]
                if provider_id == "openrouter"
                else []
            ),
        )

        result = key_pool.reload_provider_keys()

        assert result.restored == ["sha256:ab12cd34"]
        assert key_pool._SHARED == {}, "the pools must be dropped, or the old key is re-dialled"

    def test_a_healthy_pool_restores_nothing(self, monkeypatch):
        monkeypatch.setattr(
            key_pool,
            "provider_slots",
            lambda provider_id: [
                SlotStatus(position=1, source="env", fingerprint="sha256:ab12cd34", state="active")
            ],
        )

        assert key_pool.reload_provider_keys().restored == []

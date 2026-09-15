"""The one governed flag every step-efficiency control hangs off.

Five of twelve WildClawBench Code tasks and one Productivity task spent their
whole budget and scored zero, while every Code task that finished scored
0.9-1.0. The controls that answer that (pace-aware deadline notes, the
repeat-call guard, bounded tool timeouts, a check-in that actually fires) all
change what the model sees mid-run, so they share one rung rather than four:
an operator promoting "step efficiency" moves one switch and gets a coherent
behaviour, not a partial one.

Default ``observe``, not ``off``, for the same reason the honesty suite
defaults that way: a control nobody runs measures nothing, and observe only
logs what enforce would have done.
"""

from __future__ import annotations

import pytest


class TestTheLadder:
    def test_the_default_is_observe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.engine import feature_flags as ff

        monkeypatch.delenv("ROBOTHOR_STEP_EFFICIENCY_MODE", raising=False)
        monkeypatch.setattr(ff, "_resolve_raw", lambda name, default="": default)
        assert ff.step_efficiency_mode() == "observe"

    @pytest.mark.parametrize("rung", ["off", "observe", "enforce"])
    def test_each_rung_is_honoured(self, monkeypatch: pytest.MonkeyPatch, rung: str) -> None:
        from robothor.engine import feature_flags as ff

        monkeypatch.setattr(ff, "_resolve_raw", lambda name, default="", _r=rung: _r)
        assert ff.step_efficiency_mode() == rung

    def test_an_unknown_value_falls_back_to_observe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo must not silently promote a control to enforce."""
        from robothor.engine import feature_flags as ff

        monkeypatch.setattr(ff, "_resolve_raw", lambda name, default="": "ENFORCE!!")
        assert ff.step_efficiency_mode() == "observe"

    def test_there_is_no_alert_rung(self) -> None:
        """Nothing here pages anybody: it is a pacing aid, not a guardrail."""
        from robothor.flags.store import valid_values_for

        assert valid_values_for("ROBOTHOR_STEP_EFFICIENCY_MODE") == ("off", "observe", "enforce")

    def test_the_panic_switch_forces_it_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from robothor.engine import feature_flags as ff

        monkeypatch.setattr(ff, "_disabled_all", lambda: True)
        assert ff.step_efficiency_mode() == "off"

    def test_the_flag_is_read_through_the_store_not_os_environ(self) -> None:
        """Governed means an operator can flip it from Controls.

        Reading ``os.environ`` here would make the dashboard's switch inert and
        would also grow the env-read ratchet in tests/test_settings_registry.py.
        """
        import inspect

        from robothor.engine import feature_flags as ff

        body = inspect.getsource(ff.step_efficiency_mode)
        assert "_resolve_raw(" in body
        assert "os.environ" not in body


class TestItIsReachableFromTheDashboard:
    def test_it_is_governed(self) -> None:
        from robothor.flags.store import GOVERNED_FLAGS

        assert "ROBOTHOR_STEP_EFFICIENCY_MODE" in GOVERNED_FLAGS

    def test_it_has_an_evidence_source(self) -> None:
        """Without one, nothing can say whether the control ever fired."""
        from robothor.flags.evidence import EVIDENCE_SOURCES

        assert "ROBOTHOR_STEP_EFFICIENCY_MODE" in EVIDENCE_SOURCES

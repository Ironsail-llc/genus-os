"""Tests for continuous execution mode (v2.continuous)."""

from __future__ import annotations

from robothor.engine.config import manifest_to_agent_config


class TestContinuousOverridesApplied:
    def test_continuous_overrides_applied(self):
        """Continuous mode should raise safety_cap, timeout, max_iterations, and enable checkpoints."""
        config = manifest_to_agent_config({"id": "test", "v2": {"continuous": True}})
        assert config.continuous is True
        assert config.safety_cap >= 2000
        assert config.timeout_seconds >= 86400
        assert config.max_iterations >= 100
        assert config.checkpoint_enabled is True


class TestContinuousDefaultsNotOverridden:
    def test_non_continuous_keeps_defaults(self):
        """A non-continuous agent should keep the standard default values."""
        config = manifest_to_agent_config({"id": "test"})
        assert config.continuous is False
        assert config.safety_cap == 200
        assert config.timeout_seconds == 600
        assert config.max_iterations == 20
        assert config.checkpoint_enabled is True  # default changed to True for reliability

    def test_explicit_false(self):
        """Explicitly setting continuous=False should behave like default."""
        config = manifest_to_agent_config({"id": "test", "v2": {"continuous": False}})
        assert config.continuous is False
        assert config.safety_cap == 200


class TestContinuousPreservesHigherValues:
    def test_higher_safety_cap_preserved(self):
        """If manifest already sets safety_cap=5000, continuous mode should not lower it."""
        config = manifest_to_agent_config(
            {
                "id": "test",
                "v2": {"continuous": True, "safety_cap": 5000},
            }
        )
        assert config.safety_cap == 5000

    def test_higher_timeout_preserved(self):
        """If manifest already sets timeout > 86400, continuous should not lower it."""
        config = manifest_to_agent_config(
            {
                "id": "test",
                "schedule": {"timeout_seconds": 172800},
                "v2": {"continuous": True},
            }
        )
        assert config.timeout_seconds == 172800

    def test_higher_max_iterations_preserved(self):
        """If manifest already sets max_iterations=500, continuous should not lower it."""
        config = manifest_to_agent_config(
            {
                "id": "test",
                "schedule": {"max_iterations": 500},
                "v2": {"continuous": True},
            }
        )
        assert config.max_iterations == 500


class TestProgressReportInterval:
    def test_default_interval(self):
        """Default progress_report_interval should be 50."""
        config = manifest_to_agent_config({"id": "test", "v2": {"continuous": True}})
        assert config.progress_report_interval == 50

    def test_custom_interval(self):
        """Custom progress_report_interval should be honoured."""
        config = manifest_to_agent_config(
            {
                "id": "test",
                "v2": {"continuous": True, "progress_report_interval": 25},
            }
        )
        assert config.progress_report_interval == 25


class TestContinuousConfigParsing:
    def test_full_v2_block(self):
        """Test that a full v2 block with continuous fields is parsed correctly."""
        config = manifest_to_agent_config(
            {
                "id": "test",
                "v2": {
                    "continuous": True,
                    "max_cost_usd": 5.0,
                    "hard_budget": True,
                    "progress_report_interval": 25,
                },
            }
        )
        assert config.continuous is True
        assert config.max_cost_usd == 5.0
        assert config.hard_budget is True
        assert config.progress_report_interval == 25
        # Continuous overrides should still apply
        assert config.safety_cap >= 2000
        assert config.checkpoint_enabled is True

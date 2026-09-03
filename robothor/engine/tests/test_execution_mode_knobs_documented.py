"""A knob an operator cannot discover is a knob that does not exist for them.

2026-08-27. The thermal thresholds lived only as shell defaults inside
thermal-guard.sh; ROBOTHOR_LAST_RESORT_MODEL, the cost cap and every knob this
plan introduced appeared in no example file at all. On a new box the only way
to learn them was to read the source of the thing you were trying to configure.

Scoped deliberately to the modules this work introduced rather than the whole
codebase: a guard that fails on a pre-existing backlog gets muted, and a muted
guard protects nothing.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).parents[3]
EXAMPLE = ROOT / "infra" / "systemd" / "robothor.env.example"

#: The modules whose knobs this plan introduced.
MODULES = (
    "robothor/engine/execution_mode.py",
    "robothor/engine/host_profile.py",
    "robothor/engine/mode_policy.py",
    "robothor/engine/thermal_pressure.py",
    "robothor/engine/admission.py",
)

#: Read but deliberately not operator-facing: the global guardrail kill switch
#: is documented with the guardrail ladder, and OLLAMA_* belong to the model
#: server's own unit (mirrored under infra/systemd/ollama.service.d/).
NOT_OPERATOR_FACING = {"ROBOTHOR_DISABLE_ALL_GUARDRAILS"}


def _env_vars_read_by(rel_path: str) -> set[str]:
    source = (ROOT / rel_path).read_text()
    return set(re.findall(r"ROBOTHOR_[A-Z0-9_]+", source))


class TestEveryKnobIsDiscoverable:
    def test_the_example_file_exists(self):
        assert EXAMPLE.exists()

    def test_every_knob_these_modules_read_is_documented(self):
        documented = EXAMPLE.read_text()
        missing: dict[str, set[str]] = {}
        for module in MODULES:
            undocumented = {
                var
                for var in _env_vars_read_by(module)
                if var not in NOT_OPERATOR_FACING and var not in documented
            }
            if undocumented:
                missing[module] = undocumented
        assert not missing, (
            f"knobs read but undocumented in robothor.env.example: {missing}. "
            "An operator on a new box cannot discover these."
        )

    def test_the_thermal_thresholds_are_documented_with_their_defaults(self):
        """They were shell-only defaults; a number with no default stated is
        not documentation."""
        text = EXAMPLE.read_text()
        for var, default in (
            ("ROBOTHOR_THERMAL_THROTTLE_C", "85"),
            ("ROBOTHOR_THERMAL_WARN_C", "90"),
            ("ROBOTHOR_THERMAL_CRIT_C", "94"),
            ("ROBOTHOR_THERMAL_RESTORE_C", "75"),
        ):
            assert f"{var}={default}" in text, f"{var} documented without its default"

    def test_the_documented_thermal_defaults_match_the_code(self):
        """Documentation that drifts from the code is worse than none."""
        from robothor.engine import thermal_pressure as tp

        text = EXAMPLE.read_text()
        for var, value in (
            ("ROBOTHOR_THERMAL_THROTTLE_C", tp.THROTTLE_C),
            ("ROBOTHOR_THERMAL_WARN_C", tp.WARN_C),
            ("ROBOTHOR_THERMAL_CRIT_C", tp.CRIT_C),
            ("ROBOTHOR_THERMAL_RESTORE_C", tp.RESTORE_C),
        ):
            assert f"{var}={value}" in text, f"{var} documented default != code default"

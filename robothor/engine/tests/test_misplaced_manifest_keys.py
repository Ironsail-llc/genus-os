"""A manifest key in the wrong block must not be silent.

`bench/wildclaw/agent.yaml` carried `rate_limit_per_minute: 300` under
`schedule:` with a comment explaining exactly why the throttle needed
raising. `config.py` reads that key from the `v2:` block. The loaded value
was 0, so the agent fell back to the 30/min default and was throttled for
every measurement taken after the knob "shipped" — five guardrail blocks in
one run, on a task that had previously scored full marks.

Nothing warned. The manifest was valid YAML, the key was spelled correctly,
and it sat one block away from where it is read.

This is the same failure shape the manifest guard, the fleet guard and the
exec timeout each had: a correct mechanism whose caller never reaches it.
The generic defense is to know which keys belong to which block and say so
when one is misplaced — a typo'd key is a nuisance, but a REAL key in the
WRONG block silently does nothing while looking deliberate.
"""

from __future__ import annotations

from robothor.engine.config_schema import validate_manifest


class TestMisplacedKeysAreReported:
    def test_a_v2_key_under_schedule_warns(self):
        warnings = validate_manifest({"id": "probe", "schedule": {"rate_limit_per_minute": 300}})
        joined = " ".join(warnings)
        assert "rate_limit_per_minute" in joined
        assert "v2" in joined, "the warning must name the block it belongs in"

    def test_the_warning_names_the_block_it_was_found_in(self):
        warnings = validate_manifest({"id": "probe", "schedule": {"rate_limit_per_minute": 300}})
        assert "schedule" in " ".join(warnings)

    def test_the_same_key_in_its_real_block_is_fine(self):
        warnings = validate_manifest({"id": "probe", "v2": {"rate_limit_per_minute": 300}})
        assert not [w for w in warnings if "rate_limit_per_minute" in w]

    def test_a_schedule_key_under_v2_warns_too(self):
        """The mistake runs both ways."""
        warnings = validate_manifest({"id": "probe", "v2": {"max_iterations": 80}})
        joined = " ".join(warnings)
        assert "max_iterations" in joined and "schedule" in joined

    def test_unrelated_keys_are_left_alone(self):
        """Only keys KNOWN to belong elsewhere are reported. An unrecognised
        key may be a future field or an instance extension; guessing at it
        would make this warning noise, and noisy warnings get muted."""
        warnings = validate_manifest({"id": "probe", "schedule": {"some_future_option": 1}})
        assert not [w for w in warnings if "some_future_option" in w]

    def test_a_clean_manifest_produces_no_misplacement_warnings(self):
        warnings = validate_manifest(
            {
                "id": "probe",
                "schedule": {"timeout_seconds": 600, "max_iterations": 20},
                "v2": {"rate_limit_per_minute": 120},
            }
        )
        assert not [w for w in warnings if "belongs under" in w]

    def test_non_dict_blocks_do_not_raise(self):
        validate_manifest({"id": "probe", "schedule": "not a dict", "v2": None})


class TestTheBenchManifestIsCorrect:
    """The instance that exposed this. A regression test on the real file."""

    def test_the_bench_agent_rate_limit_is_actually_loaded(self, tmp_path):
        from pathlib import Path

        from robothor.engine.config import load_agent_config

        manifest_dir = Path(__file__).resolve().parents[3] / "bench" / "wildclaw"
        config = load_agent_config("wildclaw", manifest_dir)
        assert config is not None
        assert config.rate_limit_per_minute >= 120, (
            f"the bench agent loaded rate_limit_per_minute="
            f"{config.rate_limit_per_minute} — it is throttled at the default"
        )


class TestTheV2KeyListCannotDriftAgain:
    """It was hand-maintained and had drifted three keys."""

    def test_every_key_config_reads_is_known(self):
        import re
        from pathlib import Path

        import robothor.engine.config as cfg
        from robothor.engine.config_schema import _KNOWN_V2_KEYS

        src = Path(cfg.__file__).read_text(encoding="utf-8")
        read = set(re.findall(r"""v2\.get\(\s*['"]([a-z0-9_]+)""", src))
        assert read, "the derivation found no v2 keys — the pattern went stale"
        assert not (read - set(_KNOWN_V2_KEYS)), (
            f"config.py reads v2 keys the validator calls typos: "
            f"{sorted(read - set(_KNOWN_V2_KEYS))}"
        )

    def test_a_correctly_placed_v2_key_is_never_called_a_typo(self):
        from robothor.engine.config_schema import validate_manifest

        for key in ("rate_limit_per_minute", "tool_timeout_seconds", "sandbox"):
            warnings = validate_manifest({"id": "p", "v2": {key: 1}})
            assert not [w for w in warnings if "typo" in w], (key, warnings)

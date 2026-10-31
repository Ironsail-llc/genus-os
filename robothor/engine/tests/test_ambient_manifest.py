"""The ``ambient:`` manifest block: what the validator accepts and rejects.

An agent opts in to ambient follow-ups with one block. The validator has to
reject the mistakes that would otherwise be silent: a tier that is not one of
the three, a duration nobody can parse, a digest time that never fires, and a
watch source the agent cannot read — an ambient agent must only ever look at
what it already has access to.
"""

from __future__ import annotations

import pytest

from robothor.engine.ambient import AmbientConfig, parse_ambient, parse_duration
from robothor.engine.manifest_schema import ManifestIssue, validate


def _manifest(ambient: object, **extra: object) -> dict:
    base: dict = {
        "id": "example-agent",
        "name": "Example Agent",
        "description": "Answers the shared inbox",
        "version": "2026-10-31",
        "department": "operations",
        "model": {"primary": "openrouter/x/y", "fallbacks": ["openrouter/x/z"]},
        "delivery": {"mode": "none"},
        "streams": {"read": ["support"]},
        "ambient": ambient,
    }
    base.update(extra)
    return base


def _valid_block() -> dict:
    return {
        "watch": ["inbox", "stream:support"],
        "stalled_after": "4h",
        "digest_at": "0 9,15 * * *",
        "tiers": {
            "nudge_thread": "approve",
            "send_digest": "auto",
            "reply_on_behalf": "never",
        },
    }


def _ambient_errors(issues: list[ManifestIssue]) -> list[ManifestIssue]:
    return [i for i in issues if i.severity == "error" and i.path.startswith("ambient")]


class TestTheValidatorAcceptsAWellFormedBlock:
    def test_a_full_block_has_no_errors(self):
        assert _ambient_errors(validate(_manifest(_valid_block()), strict=True)) == []

    def test_digest_at_may_be_a_list_of_times(self):
        block = _valid_block() | {"digest_at": "09:00, 15:30"}
        assert _ambient_errors(validate(_manifest(block), strict=True)) == []

    def test_tiers_may_be_left_to_their_defaults(self):
        block = {"watch": ["inbox"], "stalled_after": "30m", "digest_at": "0 9 * * *"}
        assert _ambient_errors(validate(_manifest(block), strict=True)) == []


class TestTheValidatorRejectsEachMistakeOnce:
    @pytest.mark.parametrize(
        ("change", "path"),
        [
            ({"tiers": {"nudge_thread": "sometimes"}}, "ambient.tiers.nudge_thread"),
            ({"stalled_after": "soon"}, "ambient.stalled_after"),
            ({"stalled_after": "0m"}, "ambient.stalled_after"),
            ({"digest_at": "every morning"}, "ambient.digest_at"),
            ({"digest_at": "25:00"}, "ambient.digest_at"),
            ({"watch": ["stream:email"]}, "ambient.watch[0]"),
            ({"watch": ["telepathy"]}, "ambient.watch[0]"),
        ],
    )
    def test_one_error_at_the_offending_path(self, change, path):
        block = _valid_block() | change
        errors = _ambient_errors(validate(_manifest(block), strict=True))
        assert [e.path for e in errors] == [path], errors

    def test_an_unknown_key_under_ambient_is_reported(self):
        block = _valid_block() | {"chase_everyone": True}
        errors = _ambient_errors(validate(_manifest(block), strict=True))
        assert [(e.path, e.code) for e in errors] == [("ambient.chase_everyone", "unknown_key")]

    def test_an_unknown_action_kind_under_tiers_is_reported(self):
        block = _valid_block() | {"tiers": {"delete_thread": "auto"}}
        errors = _ambient_errors(validate(_manifest(block), strict=True))
        assert [e.path for e in errors] == ["ambient.tiers.delete_thread"]

    def test_stalled_after_and_digest_at_are_required_when_the_block_is_present(self):
        errors = _ambient_errors(validate(_manifest({"watch": ["inbox"]}), strict=True))
        assert sorted(e.path for e in errors) == ["ambient.digest_at", "ambient.stalled_after"]

    def test_a_stream_the_agent_reads_is_accepted_and_one_it_does_not_is_not(self):
        ok = _valid_block() | {"watch": ["stream:support"]}
        assert _ambient_errors(validate(_manifest(ok), strict=True)) == []
        no_streams = _manifest(_valid_block() | {"watch": ["stream:support"]}, streams={})
        assert [e.path for e in _ambient_errors(validate(no_streams, strict=True))] == [
            "ambient.watch[0]"
        ]


class TestParsing:
    def test_durations(self):
        assert parse_duration("90s") == 90
        assert parse_duration("30m") == 1800
        assert parse_duration("4h") == 14400
        assert parse_duration("1d") == 86400
        assert parse_duration("1h30m") == 5400
        for bad in ("", "soon", "4", "-1h", "0m", 4):
            assert parse_duration(bad) is None

    def test_no_block_means_not_opted_in(self):
        assert parse_ambient(None) is None
        assert parse_ambient({}) is None

    def test_a_block_parses_with_default_tiers(self):
        cfg = parse_ambient({"watch": ["inbox"], "stalled_after": "4h", "digest_at": "0 9 * * *"})
        assert isinstance(cfg, AmbientConfig)
        assert cfg.watch == ["inbox"]
        assert cfg.stalled_after_seconds == 14400
        assert cfg.digest_at == "0 9 * * *"
        # Nothing is sent alone unless the operator says so, and nothing is
        # ever said on the operator's behalf unless they say so.
        assert cfg.tiers == {
            "nudge_thread": "approve",
            "send_digest": "approve",
            "reply_on_behalf": "never",
        }

    def test_manifest_to_agent_config_carries_the_block(self):
        from robothor.engine.config import manifest_to_agent_config

        agent = manifest_to_agent_config(_manifest(_valid_block()))
        assert agent.ambient is not None
        assert agent.ambient.tiers["send_digest"] == "auto"
        plain = _manifest(None)
        plain.pop("ambient")
        assert manifest_to_agent_config(plain).ambient is None

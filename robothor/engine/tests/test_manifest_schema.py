"""The manifest schema validator — structural rules read from the schema file.

`docs/agents/schema.yaml` was, until this module existed, a document that only a
repo script read. `config_schema.validate_manifest` was a hand-written list of
checks that had never seen it, and README claimed the schema was enforced at
startup. A manifest key that was a typo of a real one was accepted in silence —
the 2026-08-24 outage class.

These tests pin the two halves that make the claim true: structural rules are
DERIVED from the schema file (so the file is the source of truth, not a second
hand-written list), and the semantic rules that already existed keep their exact
messages when they are reached through either entry point.
"""

from __future__ import annotations

import yaml

from robothor.engine import manifest_schema
from robothor.engine.config_schema import validate_manifest
from robothor.engine.manifest_schema import ManifestIssue, validate


def _valid() -> dict:
    """A manifest that is clean under `strict=True`."""
    return {
        "id": "ticket-router",
        "name": "Ticket Router",
        "description": "Routes inbound tickets to the right queue",
        "version": "2026-09-11",
        "department": "operations",
        "model": {"primary": "openrouter/x/y", "fallbacks": ["openrouter/x/z"]},
        "schedule": {"cron": "0 * * * *", "timezone": "UTC", "max_iterations": 10},
        "delivery": {"mode": "none"},
    }


def _errors(issues: list[ManifestIssue]) -> list[ManifestIssue]:
    return [i for i in issues if i.severity == "error"]


def _codes(issues: list[ManifestIssue]) -> set[str]:
    return {i.code for i in issues}


# ── The schema file is the source ───────────────────────────────────


class TestSchemaIsLoadedFromTheFile:
    def test_the_packaged_schema_file_exists_and_parses(self):
        text = manifest_schema.SCHEMA_PATH.read_text(encoding="utf-8")
        assert yaml.safe_load(text), "the packaged schema must be non-empty YAML"

    def test_required_keys_come_from_the_schema_required_block(self):
        raw = yaml.safe_load(manifest_schema.SCHEMA_PATH.read_text(encoding="utf-8"))
        assert manifest_schema.required_keys() == frozenset(raw["required"])

    def test_known_top_level_keys_cover_all_three_presence_classes(self):
        known = manifest_schema.known_top_level_keys()
        for name in ("id", "model", "v2", "heartbeat", "hooks"):
            assert name in known


# ── Structural rules ────────────────────────────────────────────────


class TestStructuralRules:
    def test_a_valid_manifest_has_no_errors(self):
        assert _errors(validate(_valid())) == []

    def test_a_valid_manifest_has_no_errors_under_strict(self):
        assert _errors(validate(_valid(), strict=True)) == []

    def test_missing_required_field_is_an_error(self):
        data = _valid()
        del data["name"]
        issues = _errors(validate(data))
        assert [i.path for i in issues] == ["name"]
        assert issues[0].code == "missing_required"

    def test_missing_id_keeps_its_original_message(self):
        data = _valid()
        del data["id"]
        assert "Missing required field: id" in [i.message for i in validate(data)]

    def test_wrong_type_names_the_nested_path(self):
        data = _valid()
        data["schedule"]["cron"] = 5
        issues = [i for i in _errors(validate(data)) if i.path == "schedule.cron"]
        assert issues, "a non-string cron must be reported at schedule.cron"
        assert issues[0].code == "wrong_type"

    def test_wrong_type_on_a_whole_block_is_reported(self):
        data = _valid()
        data["schedule"] = "not a dict"
        assert "schedule" in [i.path for i in _errors(validate(data))]

    def test_an_empty_block_is_not_an_error(self):
        """`v2:` with nothing under it parses as None. That is empty, not wrong."""
        data = _valid()
        data["v2"] = None
        assert _errors(validate(data)) == []

    def test_enum_violation_is_an_error_at_the_enum_path(self):
        data = _valid()
        data["v2"] = {"sandbox": "nonsense"}
        issues = [i for i in _errors(validate(data)) if i.path == "v2.sandbox"]
        assert issues and issues[0].code == "invalid_enum"

    def test_a_list_of_the_wrong_element_type_is_an_error(self):
        data = _valid()
        data["tools_denied"] = [1, 2]
        assert "tools_denied[0]" in [i.path for i in _errors(validate(data))]

    def test_nested_list_items_are_checked(self):
        data = _valid()
        data["hooks"] = [{"stream": 7, "event_type": "email.new"}]
        assert "hooks[0].stream" in [i.path for i in _errors(validate(data))]

    def test_object_properties_are_checked(self):
        data = _valid()
        data["messaging"] = {"can_send": "yes"}
        assert "messaging.can_send" in [i.path for i in _errors(validate(data))]

    def test_a_boolean_is_not_accepted_where_an_integer_is_declared(self):
        data = _valid()
        data["schedule"]["max_iterations"] = True
        assert "schedule.max_iterations" in [i.path for i in _errors(validate(data))]


class TestUnknownKeys:
    def test_unknown_top_level_key_is_a_warning_by_default(self):
        data = _valid()
        data["descrption"] = "typo of description"
        issues = [i for i in validate(data) if i.path == "descrption"]
        assert issues and issues[0].code == "unknown_key"
        assert issues[0].severity == "warning"

    def test_unknown_top_level_key_is_an_error_under_strict(self):
        data = _valid()
        data["descrption"] = "typo of description"
        issues = [i for i in validate(data, strict=True) if i.path == "descrption"]
        assert issues and issues[0].severity == "error"

    def test_unknown_nested_key_is_reported_at_its_path(self):
        data = _valid()
        data["schedule"]["timezon"] = "UTC"
        paths = [i.path for i in validate(data, strict=True) if i.code == "unknown_key"]
        assert "schedule.timezon" in paths

    def test_unknown_keys_do_not_reach_the_legacy_warning_list(self):
        """`validate_manifest` feeds a boot-time log line, and an unrecognised
        key may be a future field or an instance extension. Reporting those
        there would make the log noisy, and a noisy log gets muted — which is
        the failure this validator exists to prevent."""
        data = _valid()
        data["some_future_option"] = 1
        assert not [w for w in validate_manifest(data) if "some_future_option" in w]


# ── Semantic rules reached through both entry points ────────────────


class TestSemanticRuleParity:
    """Three rules, asserted identical through `validate` and the old shim.

    The messages are load-bearing: other tests in this suite grep them.
    """

    def test_unknown_sandbox_mode(self):
        data = {"id": "a", "v2": {"sandbox": "nonsense"}}
        assert "Unknown sandbox mode: 'nonsense'" in [i.message for i in validate(data)]
        assert "Unknown sandbox mode: 'nonsense'" in validate_manifest(data)

    def test_unknown_guardrail(self):
        data = {"id": "a", "v2": {"guardrails": ["not_a_policy"]}}
        assert "Unknown guardrail: 'not_a_policy'" in [i.message for i in validate(data)]
        assert "Unknown guardrail: 'not_a_policy'" in validate_manifest(data)

    def test_misplaced_key(self):
        data = {"id": "a", "schedule": {"rate_limit_per_minute": 300}}
        expected = (
            "schedule.rate_limit_per_minute is ignored — 'rate_limit_per_minute' "
            "is read from the 'v2' block. Move it under v2: or it silently does nothing."
        )
        assert expected in [i.message for i in validate(data)]
        assert expected in validate_manifest(data)

    def test_out_of_range_number(self):
        data = {"id": "a", "v2": {"max_nesting_depth": 9}}
        expected = "max_nesting_depth=9 is outside expected range [0, 3]"
        assert expected in [i.message for i in validate(data)]
        assert expected in validate_manifest(data)

    def test_lifecycle_hook_missing_event(self):
        data = {"id": "a", "v2": {"lifecycle_hooks": [{"handler_type": "command"}]}}
        messages = [i.message for i in validate(data)]
        assert "lifecycle_hooks[0] missing 'event'" in messages
        assert "lifecycle_hooks[0] missing 'event'" in validate_manifest(data)


class TestTheShimKeepsItsSignature:
    def test_it_returns_a_list_of_strings(self):
        out = validate_manifest(_valid())
        assert isinstance(out, list)
        assert all(isinstance(w, str) for w in out)

    def test_structural_errors_reach_the_legacy_list(self):
        data = _valid()
        data["schedule"]["cron"] = 5
        assert any("schedule.cron" in w for w in validate_manifest(data))

    def test_non_dict_blocks_do_not_raise(self):
        validate_manifest({"id": "probe", "schedule": "not a dict", "v2": None})


# ── Codes are stable ────────────────────────────────────────────────


class TestIssueShape:
    def test_every_issue_carries_path_code_message_severity(self):
        data = _valid()
        data["schedule"]["cron"] = 5
        data["nope"] = 1
        for issue in validate(data, strict=True):
            assert isinstance(issue.path, str)
            assert issue.code
            assert issue.message
            assert issue.severity in ("error", "warning")

    def test_codes_used_are_declared(self):
        data = _valid()
        data["schedule"]["cron"] = 5
        data["nope"] = 1
        del data["name"]
        assert _codes(validate(data, strict=True)) <= manifest_schema.ISSUE_CODES

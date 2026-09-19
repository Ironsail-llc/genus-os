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
    def test_spawn_allowlist_is_validated_and_loaded(self):
        from robothor.engine.config import manifest_to_agent_config

        data = _valid()
        data["v2"] = {
            "can_spawn_agents": True,
            "spawn_allowed_agents": ["research-worker"],
            "max_spawn_total": 3,
        }
        assert _errors(validate(data, strict=True)) == []
        assert manifest_to_agent_config(data).spawn_allowed_agents == ["research-worker"]
        assert manifest_to_agent_config(data).max_spawn_total == 3
        data["v2"]["spawn_allowed_agents"] = "research-worker"
        assert any(i.path == "v2.spawn_allowed_agents" for i in _errors(validate(data)))

    def test_json_response_mode_is_explicit_and_validated(self):
        data = _valid()
        data["model"]["response_format"] = "json_object"
        assert _errors(validate(data, strict=True)) == []
        data["model"]["response_format"] = "json"
        issues = _errors(validate(data, strict=True))
        assert any(
            issue.path == "model.response_format" and issue.code == "invalid_enum"
            for issue in issues
        )

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


class TestDeliveryModeHasOneSource:
    """The schema enum is it.

    `config_schema` carried a parallel `_KNOWN_DELIVERY_MODES` that still
    accepted `summary` and `full` — modes `models.DeliveryMode` has not had for
    a long time — while rejecting nothing the schema rejects. A second opinion
    about an enum is the drift this whole change exists to end.
    """

    def test_log_is_a_real_mode_and_produces_nothing(self):
        data = _valid()
        data["delivery"] = {"mode": "log"}
        assert not [i for i in validate(data, strict=True) if i.path == "delivery.mode"]
        assert not [w for w in validate_manifest(data) if "delivery" in w]

    def test_a_retired_mode_is_reported_exactly_once(self):
        data = _valid()
        data["delivery"] = {"mode": "summary"}
        issues = [i for i in validate(data) if i.path == "delivery.mode"]
        assert len(issues) == 1, issues
        assert issues[0].code == "invalid_enum"
        assert issues[0].severity == "error"


class TestKnownV2KeysComeFromTheSchemaToo:
    def test_schema_documented_v2_keys_are_never_called_typos(self):
        """`token_budget` and `cost_budget_usd` are documented in the schema's
        `v2:` block and deprecated rather than removed, so manifests still set
        them — and every load said "possible typo?" because the known set was
        derived from `config.py` alone."""
        data = _valid()
        data["v2"] = {"token_budget": 0, "cost_budget_usd": 0.0}
        assert not [i for i in validate(data) if "typo" in i.message], validate(data)
        assert not [w for w in validate_manifest(data) if "typo" in w]

    def test_a_deprecated_key_is_named_as_deprecated(self):
        """Silence would say "live field". It is not one.

        `token_budget` is DEPRECATED — auto-derived at runtime — so a manifest
        setting it is expressing an intent nothing acts on. Accepting it
        without comment is the same silence as accepting a typo: the operator
        believes a knob is turned.
        """
        data = _valid()
        data["v2"] = {"token_budget": 40000}
        issues = [i for i in validate(data) if i.code == "deprecated_key"]
        assert issues, validate(data)
        assert issues[0].path == "v2.token_budget"
        assert issues[0].severity == "warning"
        assert "token_budget" in issues[0].message

    def test_a_deprecated_key_never_blocks_enforce(self):
        """It is advice about a live, loadable manifest — not an error."""
        data = _valid()
        data["v2"] = {"token_budget": 0, "cost_budget_usd": 0.0}
        assert _errors(validate(data, strict=True)) == []

    def test_a_live_key_is_not_called_deprecated(self):
        data = _valid()
        data["v2"] = {"max_cost_usd": 1.0}
        assert not [i for i in validate(data) if i.code == "deprecated_key"]

    def test_a_real_typo_is_still_a_typo(self):
        data = _valid()
        data["v2"] = {"planing_enabled": True}
        assert any("possible typo" in i.message for i in validate(data))

    def test_the_known_set_still_covers_every_key_config_reads(self):
        """Union, not replacement — the config.py derivation is what stops a
        key the engine honours from being called a typo."""
        for key in ("rate_limit_per_minute", "tool_timeout_seconds", "human_approval_fail_open"):
            assert key in manifest_schema._KNOWN_V2_KEYS


class TestOneFindingPerDefect:
    def test_a_numeric_typo_is_reported_once(self):
        """A non-numeric where the schema says integer is both a structural
        `wrong_type` error and, historically, a semantic `wrong_type` warning
        with a different wording. Two lines about one character helps nobody."""
        data = _valid()
        data["schedule"]["max_iterations"] = "twenty"
        issues = [i for i in validate(data) if i.path == "schedule.max_iterations"]
        assert len(issues) == 1, issues
        assert issues[0].severity == "error"
        assert len([w for w in validate_manifest(data) if "max_iterations" in w]) == 1

    def test_an_out_of_range_number_still_warns(self):
        """Suppression is scoped to the type disagreement. A number that IS a
        number but is out of range has no structural error to hide behind."""
        data = _valid()
        data["schedule"]["max_iterations"] = 99999
        messages = [i.message for i in validate(data)]
        assert "max_iterations=99999 is outside expected range [0, 10000]" in messages


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


# ── "Never raises" is a promise, not a hope ─────────────────────────


class TestValidateNeverRaises:
    """The docstring says it, so it has to be true of ANY document.

    It was not. Until the agent-manifest API shipped, every caller was the
    manifest loader, handing this function a document PyYAML had just parsed
    off disk — and a manifest that was wrong was usually wrong by having the
    wrong VALUE, not the wrong SHAPE. `POST /api/agent-manifests/validate` is
    the first caller to hand it an arbitrary HTTP body, and the promise became
    load-bearing: a raise here does not lose a verdict, it hides the file from
    the operator who needs to repair it.

    The four comparisons below are membership tests against a frozenset, so an
    unhashable value raises `TypeError: unhashable type` rather than being
    reported as the wrong type it plainly is.
    """

    WRONG_SHAPES = {
        "sandbox_is_a_mapping": ("v2", {"sandbox": {"enabled": True}}),
        "sandbox_is_a_list": ("v2", {"sandbox": ["local"]}),
        "guardrail_is_a_dict": ("v2", {"guardrails": [{"name": "x"}]}),
        "guardrail_is_a_list": ("v2", {"guardrails": [["x"]]}),
        "difficulty_is_a_mapping": ("v2", {"difficulty_class": {"tier": 1}}),
        "difficulty_is_a_list": ("v2", {"difficulty_class": ["simple"]}),
        "session_target_is_a_mapping": ("schedule", {"session_target": {"mode": "isolated"}}),
        "session_target_is_a_list": ("schedule", {"session_target": ["isolated"]}),
    }

    def _with(self, section: str, overrides: dict) -> dict:
        data = _valid()
        data[section] = {**data.get(section, {}), **overrides}
        return data

    def test_an_unhashable_value_is_reported_not_raised(self):
        for name, (section, overrides) in self.WRONG_SHAPES.items():
            issues = validate(self._with(section, overrides))
            assert issues, f"{name}: validated clean, which is worse than raising"

    def test_the_finding_names_the_path_and_the_wrong_type(self):
        issues = validate(self._with("v2", {"sandbox": {"enabled": True}}))

        sandbox = [i for i in issues if i.path == "v2.sandbox"]
        assert sandbox, [i.path for i in issues]
        assert sandbox[0].code == "wrong_type"
        assert "dict" in sandbox[0].message

    def test_codes_stay_declared_for_the_wrong_shapes_too(self):
        for section, overrides in self.WRONG_SHAPES.values():
            assert _codes(validate(self._with(section, overrides))) <= manifest_schema.ISSUE_CODES

    def test_a_document_that_is_not_a_mapping_at_all_is_reported(self):
        for data in ([], "id: main", 7, None):
            issues = validate(data)  # type: ignore[arg-type]
            assert issues and issues[0].code == "wrong_type"

    def test_a_check_that_raises_anyway_becomes_a_finding(self, monkeypatch):
        """The backstop, probed rather than assumed.

        The four comparisons above are fixed at the source, but the next check
        someone adds will not be, and a promise this file makes to an HTTP
        handler cannot depend on every future author remembering.
        """

        def _explode(*args, **kwargs):
            raise RuntimeError("a future check did something unguarded")

        monkeypatch.setattr(manifest_schema, "_check_semantics", _explode)

        issues = validate(_valid())

        assert any(i.code == "validator_error" for i in issues), issues
        assert not any("unguarded" in i.message for i in issues), "the raise text leaked"

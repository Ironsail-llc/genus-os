"""One list of guardrails, everywhere.

Three sets had drifted independently:

  * config_schema._KNOWN_GUARDRAILS (the manifest validator) was missing
    inbound_only and no_recent_changelog_reversal — both real, enforced
    policies — so every engine boot logged a spurious "Unknown guardrail:
    'inbound_only'" for email-responder, training the operator to ignore
    config warnings.
  * It also carried requires_human_task_closure, which guardrails.py's OWN
    _KNOWN_POLICIES did not — despite guardrails.py enforcing it in
    check_task_closure_post_run. An agent declaring it passed validation,
    got real enforcement, and GuardrailEngine's unknown-policy log at :243
    flagged it as unknown. Three files, three opinions.

Hardcoded parallel lists are how this project keeps getting hurt (the alert
name list, the model-switch list, this). The registry of descriptions in
guardrails.POLICY_DESCRIPTIONS and the enforcement sets are now the single
source; the validator derives from them.
"""

from __future__ import annotations

from robothor.engine import config_schema, guardrails


def test_validator_accepts_exactly_the_enforced_policies():
    assert config_schema._KNOWN_GUARDRAILS == guardrails._KNOWN_POLICIES


def test_every_enforced_policy_is_known_to_the_engine_itself():
    """guardrails.py enforced requires_human_task_closure while its own
    known-set said 'unknown policy'."""
    assert "requires_human_task_closure" in guardrails._KNOWN_POLICIES


def test_every_known_policy_has_a_description():
    """The description dict feeds agent system prompts; a policy without one
    is enforced invisibly."""
    missing = guardrails._KNOWN_POLICIES - set(guardrails.POLICY_DESCRIPTIONS)
    assert not missing, f"policies with no description: {sorted(missing)}"


def test_no_description_for_a_policy_nothing_enforces():
    phantom = set(guardrails.POLICY_DESCRIPTIONS) - guardrails._KNOWN_POLICIES
    assert not phantom, f"described but unenforced: {sorted(phantom)}"


def test_the_live_fleet_warning_is_gone():
    """The concrete symptom: email-responder's manifest declares inbound_only
    and was warned about on every single engine boot."""
    warnings = config_schema.validate_manifest(
        {"id": "email-responder", "v2": {"guardrails": ["inbound_only"]}}
    )
    assert not any("inbound_only" in w for w in warnings), warnings

"""The shared names and two pure helpers every remote-tools surface imports."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from robothor.tenancy import remote_contract as rc


def test_the_four_modes_and_their_default():
    assert rc.REMOTE_MODES == ("off", "read", "dry_run", "full")
    assert (rc.MODE_OFF, rc.MODE_READ, rc.MODE_DRY_RUN, rc.MODE_FULL) == rc.REMOTE_MODES
    assert rc.DEFAULT_MODE == "full"
    assert rc.UNKNOWN_MODE_FALLBACK == "off"


def test_every_refusal_code_in_the_contract():
    assert (
        frozenset(
            {
                "remote_tools_off",
                "remote_write_forbidden",
                "remote_tool_unknown",
                "remote_access_mismatch",
                "remote_caller_forbidden",
                "remote_not_permitted",
                "remote_attestation_missing",
                "remote_bad_arguments",
                "remote_outcome_unknown",
                "remote_too_large",
                "remote_timeout",
            }
        )
        == rc.ERROR_CODES
    )
    assert rc.ERR_TOOLS_OFF == "remote_tools_off"
    assert rc.ERR_TIMEOUT == "remote_timeout"


def test_outcomes_access_and_names():
    assert frozenset({"ok", "error", "dry_run", "replayed"}) == rc.OUTCOMES
    assert frozenset({"read", "write"}) == rc.ACCESS_VALUES
    assert rc.EVENT_REMOTE_TOOL_CALL == "agent.remote_tool_call"
    assert rc.ROLE_PARENT_AGENT == "parent_agent"
    assert rc.SCOPE_ORG_REMOTE == "org:remote"
    assert rc.BRIDGE_SERVICE_ID == "genus-bridge"


def test_limits():
    assert rc.REMOTE_RESULT_MAX_BYTES == 262144
    assert rc.TARGET_TIMEOUT_S == 150
    assert rc.BRIDGE_TIMEOUT_S == 165
    assert rc.HOME_TIMEOUT_S == 180
    assert rc.CHECK_CACHE_TTL_S == 60
    assert rc.DESCRIPTION_MAX_CHARS == 200
    assert rc.NOTES_MAX_CHARS == 600
    # Each hop waits longer than the one it calls, so the inner timeout fires first.
    assert rc.TARGET_TIMEOUT_S < rc.BRIDGE_TIMEOUT_S < rc.HOME_TIMEOUT_S


@pytest.mark.parametrize("token", ["baa", "a", "hipaa_2", "a" + "b" * 31])
def test_attestation_token_accepts(token):
    assert rc.ATTESTATION_TOKEN_RE.fullmatch(token)


@pytest.mark.parametrize("token", ["", "BAA", "2baa", "_baa", "baa-1", "a" + "b" * 32, "baa "])
def test_attestation_token_refuses(token):
    assert not rc.ATTESTATION_TOKEN_RE.fullmatch(token)


def test_attestation_pattern_is_the_contract_one():
    assert isinstance(rc.ATTESTATION_TOKEN_RE, re.Pattern)
    assert rc.ATTESTATION_TOKEN_RE.pattern == r"^[a-z][a-z0-9_]{0,31}$"


def test_args_digest_is_sha256_of_canonical_json():
    args = {"b": [1, 2], "a": {"y": "é", "x": None}}
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert rc.args_digest(args) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_args_digest_ignores_key_order_and_is_stable():
    assert rc.args_digest({"a": 1, "b": 2}) == rc.args_digest({"b": 2, "a": 1})
    assert rc.args_digest({"a": 1}) != rc.args_digest({"a": 2})
    # Pinned: a digest that changed between releases would break audit joins.
    assert rc.args_digest({}) == hashlib.sha256(b"{}").hexdigest()
    assert len(rc.args_digest({"a": 1})) == 64


def test_args_digest_of_none_is_the_empty_object():
    assert rc.args_digest(None) == rc.args_digest({})


@pytest.mark.parametrize("raw", ["off", "read", "dry_run", "full"])
def test_normalise_mode_keeps_each_rung(raw):
    assert rc.normalise_mode(raw) == raw
    assert rc.normalise_mode(f"  {raw} ") == raw


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_normalise_mode_unset_is_full(raw):
    assert rc.normalise_mode(raw) == "full"


@pytest.mark.parametrize("raw", ["mostly", "on", "true", "enforce", "Full", "dry-run"])
def test_normalise_mode_unknown_is_off(raw):
    assert rc.normalise_mode(raw) == "off"

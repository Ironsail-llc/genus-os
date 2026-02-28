"""Tests for the guardrails framework."""

from __future__ import annotations

import time
from unittest.mock import patch

from robothor.engine.guardrails import DEFAULT_RATE_LIMIT, GuardrailEngine


class TestNoDestructiveWrites:
    def test_blocks_rm_rf(self):
        engine = GuardrailEngine(enabled_policies=["no_destructive_writes"])
        result = engine.check_pre_execution("exec", {"command": "rm -rf /tmp/data"})
        assert not result.allowed
        assert result.action == "blocked"
        assert "no_destructive_writes" in result.guardrail_name

    def test_blocks_drop_table(self):
        engine = GuardrailEngine(enabled_policies=["no_destructive_writes"])
        result = engine.check_pre_execution("exec", {"command": "psql -c 'DROP TABLE users'"})
        assert not result.allowed

    def test_blocks_delete_from(self):
        engine = GuardrailEngine(enabled_policies=["no_destructive_writes"])
        result = engine.check_pre_execution("exec", {"command": "psql -c 'DELETE FROM users'"})
        assert not result.allowed

    def test_allows_safe_commands(self):
        engine = GuardrailEngine(enabled_policies=["no_destructive_writes"])
        result = engine.check_pre_execution("exec", {"command": "ls -la /tmp"})
        assert result.allowed

    def test_ignores_non_exec_tools(self):
        engine = GuardrailEngine(enabled_policies=["no_destructive_writes"])
        result = engine.check_pre_execution("read_file", {"path": "/etc/hosts"})
        assert result.allowed


class TestNoExternalHttp:
    def test_blocks_web_fetch(self):
        engine = GuardrailEngine(enabled_policies=["no_external_http"])
        result = engine.check_pre_execution("web_fetch", {"url": "https://example.com"})
        assert not result.allowed
        assert "no_external_http" in result.guardrail_name

    def test_blocks_web_search(self):
        engine = GuardrailEngine(enabled_policies=["no_external_http"])
        result = engine.check_pre_execution("web_search", {"query": "test"})
        assert not result.allowed

    def test_allows_other_tools(self):
        engine = GuardrailEngine(enabled_policies=["no_external_http"])
        result = engine.check_pre_execution("read_file", {"path": "/tmp/x"})
        assert result.allowed


class TestNoSensitiveData:
    def test_warns_on_aws_key(self):
        engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
        result = engine.check_post_execution("exec", {"output": "AKIAIOSFODNN7EXAMPLE"})
        assert result.action == "warned"
        assert "no_sensitive_data" in result.guardrail_name

    def test_warns_on_github_token(self):
        engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
        result = engine.check_post_execution("exec", {"output": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh"})
        assert result.action == "warned"

    def test_ok_on_clean_output(self):
        engine = GuardrailEngine(enabled_policies=["no_sensitive_data"])
        result = engine.check_post_execution("exec", {"output": "Hello world"})
        assert result.allowed
        assert result.action == "allowed"


class TestRateLimit:
    def test_allows_under_limit(self):
        engine = GuardrailEngine(enabled_policies=["rate_limit"])
        for _ in range(DEFAULT_RATE_LIMIT - 1):
            result = engine.check_pre_execution("tool", {}, agent_id="a")
            assert result.allowed

    def test_blocks_at_limit(self):
        engine = GuardrailEngine(enabled_policies=["rate_limit"])
        for _ in range(DEFAULT_RATE_LIMIT):
            engine.check_pre_execution("tool", {}, agent_id="a")
        result = engine.check_pre_execution("tool", {}, agent_id="a")
        assert not result.allowed
        assert "rate_limit" in result.guardrail_name


class TestMultiplePolicies:
    def test_first_blocking_wins(self):
        engine = GuardrailEngine(
            enabled_policies=["no_destructive_writes", "no_external_http"]
        )
        result = engine.check_pre_execution("exec", {"command": "rm -rf /"})
        assert not result.allowed
        assert result.guardrail_name == "no_destructive_writes"

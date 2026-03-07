"""Tests for error classification and recovery actions."""

from __future__ import annotations

from robothor.engine.error_recovery import (
    MAX_HELPER_SPAWNS,
    classify_error,
    get_recovery_action,
)
from robothor.engine.models import AgentConfig, ErrorType


class TestClassifyError:
    def test_auth_401(self):
        assert classify_error("web_fetch", "401 Unauthorized") == ErrorType.AUTH

    def test_auth_403(self):
        assert classify_error("web_fetch", "403 Forbidden: invalid token") == ErrorType.AUTH

    def test_auth_access_denied(self):
        assert classify_error("exec", "Access denied for resource") == ErrorType.AUTH

    def test_auth_invalid_token(self):
        assert classify_error("web_fetch", "Error: invalid API token") == ErrorType.AUTH

    def test_rate_limit_429(self):
        assert classify_error("web_fetch", "429 Too Many Requests") == ErrorType.RATE_LIMIT

    def test_rate_limit_text(self):
        assert classify_error("exec", "Rate limit exceeded") == ErrorType.RATE_LIMIT

    def test_rate_limit_throttle(self):
        assert classify_error("web_fetch", "Request throttled") == ErrorType.RATE_LIMIT

    def test_rate_limit_quota(self):
        assert classify_error("web_fetch", "API quota exceeded for today") == ErrorType.RATE_LIMIT

    def test_not_found_404(self):
        assert classify_error("web_fetch", "404 Not Found") == ErrorType.NOT_FOUND

    def test_not_found_file(self):
        assert (
            classify_error("read_file", "FileNotFoundError: /tmp/missing.txt")
            == ErrorType.NOT_FOUND
        )

    def test_not_found_no_such_file(self):
        assert classify_error("read_file", "No such file or directory") == ErrorType.NOT_FOUND

    def test_not_found_does_not_exist(self):
        assert classify_error("exec", "Resource does not exist") == ErrorType.NOT_FOUND

    def test_timeout_error(self):
        assert classify_error("web_fetch", "TimeoutError: request timed out") == ErrorType.TIMEOUT

    def test_timeout_deadline(self):
        assert classify_error("exec", "deadline exceeded") == ErrorType.TIMEOUT

    def test_timeout_text(self):
        assert classify_error("web_fetch", "Connection timed out after 30s") == ErrorType.TIMEOUT

    def test_dependency_module(self):
        assert (
            classify_error("exec", "ModuleNotFoundError: No module named 'pandas'")
            == ErrorType.DEPENDENCY
        )

    def test_dependency_import(self):
        assert (
            classify_error("exec", "ImportError: cannot import name 'foo'") == ErrorType.DEPENDENCY
        )

    def test_dependency_command(self):
        assert classify_error("exec", "bash: jq: command not found") == ErrorType.DEPENDENCY

    def test_permission_error(self):
        assert classify_error("write_file", "PermissionError: [Errno 13]") == ErrorType.PERMISSION

    def test_permission_denied(self):
        assert classify_error("exec", "permission denied: /root/secret") == ErrorType.PERMISSION

    def test_api_deprecated(self):
        assert (
            classify_error("web_fetch", "API endpoint deprecated since v3")
            == ErrorType.API_DEPRECATED
        )

    def test_api_deprecated_410(self):
        assert classify_error("web_fetch", "410 Gone") == ErrorType.API_DEPRECATED

    def test_unknown_gibberish(self):
        assert classify_error("exec", "something weird happened") == ErrorType.UNKNOWN

    def test_unknown_empty(self):
        assert classify_error("exec", "") == ErrorType.UNKNOWN

    def test_dependency_before_not_found(self):
        """ModuleNotFoundError should classify as DEPENDENCY, not NOT_FOUND."""
        result = classify_error("exec", "ModuleNotFoundError: No module named 'requests'")
        assert result == ErrorType.DEPENDENCY


class TestGetRecoveryAction:
    def _config(self, can_spawn: bool = True) -> AgentConfig:
        return AgentConfig(
            id="test-agent",
            name="Test Agent",
            can_spawn_agents=can_spawn,
        )

    def test_rate_limit_backoff(self):
        action = get_recovery_action(ErrorType.RATE_LIMIT, 1, self._config(), "web_fetch", "429")
        assert action is not None
        assert action.action == "backoff"
        assert action.delay_seconds == 2  # 2^1

    def test_rate_limit_backoff_exponential(self):
        action = get_recovery_action(ErrorType.RATE_LIMIT, 3, self._config(), "web_fetch", "429")
        assert action is not None
        assert action.delay_seconds == 8  # 2^3

    def test_rate_limit_backoff_capped(self):
        action = get_recovery_action(ErrorType.RATE_LIMIT, 10, self._config(), "web_fetch", "429")
        assert action is not None
        assert action.delay_seconds == 30  # capped at 30

    def test_rate_limit_never_spawns(self):
        action = get_recovery_action(ErrorType.RATE_LIMIT, 5, self._config(), "web_fetch", "429")
        assert action is not None
        assert action.action == "backoff"  # never spawn for rate limits

    def test_auth_no_action_at_count_1(self):
        action = get_recovery_action(ErrorType.AUTH, 1, self._config(), "web_fetch", "401")
        assert action is None

    def test_auth_spawns_at_count_2(self):
        action = get_recovery_action(
            ErrorType.AUTH, 2, self._config(), "web_fetch", "401 Forbidden"
        )
        assert action is not None
        assert action.action == "spawn"
        assert action.agent_id == "main"
        assert "web_fetch" in action.message

    def test_auth_no_spawn_when_disabled(self):
        action = get_recovery_action(
            ErrorType.AUTH, 2, self._config(can_spawn=False), "web_fetch", "401"
        )
        assert action is None

    def test_not_found_spawns_at_count_2(self):
        action = get_recovery_action(
            ErrorType.NOT_FOUND, 2, self._config(), "read_file", "not found"
        )
        assert action is not None
        assert action.action == "spawn"

    def test_timeout_retry_at_count_1(self):
        action = get_recovery_action(ErrorType.TIMEOUT, 1, self._config(), "web_fetch", "timeout")
        assert action is not None
        assert action.action == "retry"

    def test_timeout_spawn_at_count_2(self):
        action = get_recovery_action(ErrorType.TIMEOUT, 2, self._config(), "web_fetch", "timeout")
        assert action is not None
        assert action.action == "spawn"

    def test_dependency_spawns_at_count_1(self):
        action = get_recovery_action(
            ErrorType.DEPENDENCY, 1, self._config(), "exec", "ModuleNotFoundError"
        )
        assert action is not None
        assert action.action == "spawn"

    def test_permission_injects(self):
        action = get_recovery_action(
            ErrorType.PERMISSION, 1, self._config(), "write_file", "PermissionError"
        )
        assert action is not None
        assert action.action == "inject"

    def test_logic_returns_none(self):
        action = get_recovery_action(ErrorType.LOGIC, 3, self._config(), "exec", "assertion failed")
        assert action is None

    def test_unknown_returns_none(self):
        action = get_recovery_action(ErrorType.UNKNOWN, 3, self._config(), "exec", "mysterious")
        assert action is None

    def test_max_spawns_blocks_spawn(self):
        """After MAX_HELPER_SPAWNS, spawn actions fall back to None."""
        action = get_recovery_action(
            ErrorType.AUTH,
            2,
            self._config(),
            "web_fetch",
            "401",
            helper_spawns_used=MAX_HELPER_SPAWNS,
        )
        assert action is None

    def test_max_spawns_rate_limit_still_works(self):
        """Rate limit backoff still works even when spawn limit hit."""
        action = get_recovery_action(
            ErrorType.RATE_LIMIT,
            1,
            self._config(),
            "web_fetch",
            "429",
            helper_spawns_used=MAX_HELPER_SPAWNS,
        )
        assert action is not None
        assert action.action == "backoff"

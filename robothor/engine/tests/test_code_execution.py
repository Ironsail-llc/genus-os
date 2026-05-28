"""Tests for Rip 6 execute_code sandbox foundation."""

from __future__ import annotations

from robothor.engine.code_execution import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    SANDBOX_ALLOWED_TOOLS,
    SandboxResult,
    truncate_with_marker,
)


class TestResourceCaps:
    """Pinned values match Hermes battle-tested defaults."""

    def test_timeout(self) -> None:
        assert DEFAULT_TIMEOUT_SECONDS == 300

    def test_max_tool_calls(self) -> None:
        assert DEFAULT_MAX_TOOL_CALLS == 50

    def test_stdout_cap(self) -> None:
        assert MAX_STDOUT_BYTES == 50_000

    def test_stderr_cap(self) -> None:
        assert MAX_STDERR_BYTES == 10_000


class TestAllowedTools:
    def test_safe_tools_included(self) -> None:
        for t in ("web_search", "read_file", "write_file", "search_files", "patch"):
            assert t in SANDBOX_ALLOWED_TOOLS

    def test_terminal_allowed(self) -> None:
        # Matches Hermes — terminal is the fastest iteration path.
        assert "terminal" in SANDBOX_ALLOWED_TOOLS

    def test_dangerous_tools_excluded(self) -> None:
        for t in (
            "spawn_agent",
            "spawn_agents",
            "send_telegram",
            "send_email",
            "create_pull_request",
            "git_commit",
            "git_push",
            "memory_write",  # background_review owns memory writes
            "create_skill",  # background_review owns skill writes
            "update_skill",
            "execute_code",  # no recursion
        ):
            assert t not in SANDBOX_ALLOWED_TOOLS


class TestSandboxResult:
    def test_as_dict_roundtrip(self) -> None:
        r = SandboxResult(
            stdout="ok",
            stderr="",
            returncode=0,
            tool_call_count=3,
            timed_out=False,
        )
        d = r.as_dict()
        assert d["stdout"] == "ok"
        assert d["returncode"] == 0
        assert d["tool_call_count"] == 3
        assert d["timed_out"] is False

    def test_truncation_flags_default_false(self) -> None:
        r = SandboxResult(stdout="", stderr="", returncode=0, tool_call_count=0)
        assert r.stdout_truncated is False
        assert r.stderr_truncated is False


class TestTruncate:
    def test_short_input_returns_unchanged(self) -> None:
        text, truncated = truncate_with_marker("hello", 100)
        assert text == "hello"
        assert truncated is False

    def test_long_input_capped_with_marker(self) -> None:
        payload = "x" * 1000
        text, truncated = truncate_with_marker(payload, 100)
        assert truncated is True
        assert "[truncated" in text
        # Capped portion is <= 100 bytes plus the marker.
        assert text.startswith("x" * 100)

    def test_bytes_input_decoded(self) -> None:
        text, truncated = truncate_with_marker(b"hello", 100)
        assert text == "hello"
        assert truncated is False

    def test_unicode_safe_truncation(self) -> None:
        # Multi-byte chars at the boundary must not produce mojibake.
        payload = "abc" + "🦀" * 200  # crab = 4 bytes each
        text, truncated = truncate_with_marker(payload, 50)
        assert truncated is True
        # No raw bytes / replacement chars in the capped portion.
        assert "�" not in text.split("[truncated")[0]

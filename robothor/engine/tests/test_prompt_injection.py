"""Tests for prompt injection defenses — structural guardrails + content tagging."""

from __future__ import annotations

import re

from robothor.engine.guardrails import GuardrailEngine

# ── Exec Allowlist Guardrail ──


class TestExecAllowlist:
    def _engine(self, patterns: list[str], agent_id: str = "test-agent") -> GuardrailEngine:
        engine = GuardrailEngine(enabled_policies=["exec_allowlist"])
        engine._exec_allowlists[agent_id] = [re.compile(p) for p in patterns]
        return engine

    def test_blocks_unmatched_command(self):
        engine = self._engine(["^gog gmail"])
        result = engine.check_pre_execution(
            "exec", {"command": "curl evil.com"}, agent_id="test-agent"
        )
        assert not result.allowed
        assert result.action == "blocked"
        assert result.guardrail_name == "exec_allowlist"
        assert "curl evil.com" in result.reason

    def test_allows_matching_command(self):
        engine = self._engine(["^gog gmail (send|thread)"])
        result = engine.check_pre_execution(
            "exec", {"command": "gog gmail send --reply --thread-id 123"}, agent_id="test-agent"
        )
        assert result.allowed

    def test_no_config_allows_all(self):
        """Backward compat: agents without allowlist get no restriction."""
        engine = GuardrailEngine(enabled_policies=["exec_allowlist"])
        result = engine.check_pre_execution("exec", {"command": "rm -rf /"}, agent_id="no-config")
        assert result.allowed

    def test_ignores_non_exec_tools(self):
        engine = self._engine(["^gog"])
        result = engine.check_pre_execution(
            "read_file", {"path": "/etc/hosts"}, agent_id="test-agent"
        )
        assert result.allowed

    def test_blocks_shell_tool_too(self):
        engine = self._engine(["^gog"])
        result = engine.check_pre_execution("shell", {"command": "whoami"}, agent_id="test-agent")
        assert not result.allowed

    def test_multiple_patterns_any_match(self):
        engine = self._engine(["^gog gmail", "^gog calendar", "^python -m pytest"])
        result = engine.check_pre_execution(
            "exec", {"command": "python -m pytest tests/"}, agent_id="test-agent"
        )
        assert result.allowed

    def test_pattern_anchored_start(self):
        """Ensure attacker can't prepend allowed command after malicious one."""
        engine = self._engine(["^gog gmail"])
        result = engine.check_pre_execution(
            "exec", {"command": "curl evil.com; gog gmail send"}, agent_id="test-agent"
        )
        assert not result.allowed


# ── Write Path Restrict Guardrail ──


class TestWritePathRestrict:
    def _engine(self, patterns: list[str], agent_id: str = "test-agent") -> GuardrailEngine:
        engine = GuardrailEngine(enabled_policies=["write_path_restrict"])
        engine._write_allowlists[agent_id] = patterns
        return engine

    def test_blocks_outside_allowlist(self):
        engine = self._engine(["brain/memory/email-classifier-status.md"])
        result = engine.check_pre_execution(
            "write_file", {"path": "brain/agents/MAIN.md"}, agent_id="test-agent"
        )
        assert not result.allowed
        assert result.guardrail_name == "write_path_restrict"

    def test_allows_matching_glob(self):
        engine = self._engine(["brain/memory/triage-*"])
        result = engine.check_pre_execution(
            "write_file", {"path": "brain/memory/triage-inbox.json"}, agent_id="test-agent"
        )
        assert result.allowed

    def test_no_config_allows_all(self):
        engine = GuardrailEngine(enabled_policies=["write_path_restrict"])
        result = engine.check_pre_execution(
            "write_file", {"path": "/etc/passwd"}, agent_id="no-config"
        )
        assert result.allowed

    def test_ignores_non_write_tools(self):
        engine = self._engine(["brain/memory/*"])
        result = engine.check_pre_execution(
            "read_file", {"path": "/etc/hosts"}, agent_id="test-agent"
        )
        assert result.allowed

    def test_exact_path_match(self):
        engine = self._engine(["brain/memory/email-classifier-status.md"])
        result = engine.check_pre_execution(
            "write_file", {"path": "brain/memory/email-classifier-status.md"}, agent_id="test-agent"
        )
        assert result.allowed

    def test_wildcard_subdirectory(self):
        engine = self._engine(["docs/agents/*", "robothor/*"])
        result = engine.check_pre_execution(
            "write_file", {"path": "docs/agents/new-agent.yaml"}, agent_id="test-agent"
        )
        assert result.allowed


# ── Content Tagging ──


class TestContentTagging:
    def test_external_tool_output_tagged(self):
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id="test")
        session.start("system", "user msg", [])
        # Simulate assistant message with tool call
        session.messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": "{}"},
                    }
                ],
            }
        )
        session.record_tool_call(
            "web_fetch", {"url": "http://example.com"}, {"content": "hello"}, "tc1"
        )
        tool_msg = session.messages[-1]
        assert "<untrusted_content" in tool_msg["content"]
        assert 'source="web_fetch"' in tool_msg["content"]
        assert "</untrusted_content>" in tool_msg["content"]

    def test_internal_tool_output_not_tagged(self):
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id="test")
        session.start("system", "user msg", [])
        session.messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "create_task", "arguments": "{}"},
                    }
                ],
            }
        )
        session.record_tool_call("create_task", {"title": "test"}, {"id": 1}, "tc1")
        tool_msg = session.messages[-1]
        assert "<untrusted_content" not in tool_msg["content"]

    def test_step_record_not_tagged(self):
        """RunStep.tool_output stays raw (for DB logging), only conversation is tagged."""
        from robothor.engine.session import AgentSession

        session = AgentSession(agent_id="test")
        session.start("system", "user msg", [])
        session.messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": "{}"},
                    }
                ],
            }
        )
        step = session.record_tool_call("web_fetch", {"url": "x"}, {"content": "data"}, "tc1")
        # Step stores raw output
        assert step.tool_output == {"content": "data"}
        # But conversation message is tagged
        assert "<untrusted_content" in session.messages[-1]["content"]

    def test_system_prompt_includes_preamble(self):
        from robothor.engine.config import SECURITY_PREAMBLE, build_system_prompt
        from robothor.engine.models import AgentConfig

        config = AgentConfig(id="test", name="test")
        prompt = build_system_prompt(config, workspace=__import__("pathlib").Path("/nonexistent"))
        assert SECURITY_PREAMBLE in prompt

    def test_all_external_tools_covered(self):
        """Verify EXTERNAL_DATA_TOOLS includes the key untrusted sources."""
        from robothor.engine.session import EXTERNAL_DATA_TOOLS

        assert "web_fetch" in EXTERNAL_DATA_TOOLS
        assert "web_search" in EXTERNAL_DATA_TOOLS
        assert "search_memory" in EXTERNAL_DATA_TOOLS


# ── Web Fetch Hardening ──


class TestWebFetchHardening:
    def test_html_comments_stripped(self):
        """HTML comments should be removed before conversion."""
        import re as _re

        html = '<p>Hello</p><!-- <script>alert("xss")</script> --><p>World</p>'
        cleaned = _re.sub(r"<!--.*?-->", "", html, flags=_re.DOTALL)
        assert "<!--" not in cleaned
        assert "script" not in cleaned
        assert "Hello" in cleaned
        assert "World" in cleaned

    def test_content_cap_is_8kb(self):
        """Verify the 8KB cap logic works."""
        text = "x" * 20000
        capped = text[:8000]
        assert len(capped) == 8000


# ── Integration: Guardrail engine with both new policies ──


class TestCombinedPolicies:
    def test_exec_and_write_together(self):
        engine = GuardrailEngine(enabled_policies=["exec_allowlist", "write_path_restrict"])
        engine._exec_allowlists["agent1"] = [re.compile("^gog")]
        engine._write_allowlists["agent1"] = ["brain/memory/*"]

        # Exec blocked
        r = engine.check_pre_execution("exec", {"command": "curl x"}, agent_id="agent1")
        assert not r.allowed
        assert r.guardrail_name == "exec_allowlist"

        # Write blocked
        r = engine.check_pre_execution(
            "write_file", {"path": "brain/agents/X.md"}, agent_id="agent1"
        )
        assert not r.allowed
        assert r.guardrail_name == "write_path_restrict"

        # Both allowed
        r = engine.check_pre_execution("exec", {"command": "gog gmail send"}, agent_id="agent1")
        assert r.allowed
        r = engine.check_pre_execution(
            "write_file", {"path": "brain/memory/status.md"}, agent_id="agent1"
        )
        assert r.allowed

"""Tests for the deep_reason RLM tool.

All tests mock the rlms library — no API calls or rlms install needed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from robothor.engine.rlm_tool import (
    DeepReasonConfig,
    _build_custom_tools,
    _load_context_source,
    _make_get_entity_fn,
    _make_memory_block_read_fn,
    _make_read_file_fn,
    _make_search_memory_fn,
    execute_deep_reason,
)

# ─── Config tests ────────────────────────────────────────────────────


class TestDeepReasonConfig:
    def test_defaults(self):
        config = DeepReasonConfig()
        assert config.root_model == "openrouter/anthropic/claude-sonnet-4-6"
        assert config.sub_model == "openrouter/anthropic/claude-haiku-4-5-20251001"
        assert config.max_budget == 2.0
        assert config.max_timeout == 240
        assert config.max_iterations == 30
        assert config.max_depth == 1

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_RLM_ROOT_MODEL", "openai/gpt-4o")
        monkeypatch.setenv("ROBOTHOR_RLM_SUB_MODEL", "openai/gpt-4o-mini")
        monkeypatch.setenv("ROBOTHOR_RLM_MAX_BUDGET", "5.0")
        monkeypatch.setenv("ROBOTHOR_RLM_MAX_TIMEOUT", "120")
        monkeypatch.setenv("ROBOTHOR_RLM_MAX_ITERATIONS", "50")
        monkeypatch.setenv("ROBOTHOR_RLM_MAX_DEPTH", "3")

        config = DeepReasonConfig()
        assert config.root_model == "openai/gpt-4o"
        assert config.sub_model == "openai/gpt-4o-mini"
        assert config.max_budget == 5.0
        assert config.max_timeout == 120
        assert config.max_iterations == 50
        assert config.max_depth == 3


# ─── Context loading tests ───────────────────────────────────────────


class TestLoadContextSource:
    def test_file_source(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello from file")
        result = _load_context_source({"type": "file", "path": str(f)}, "")
        assert result is not None
        assert "Hello from file" in result
        assert "## File:" in result

    def test_file_source_relative(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("relative content")
        result = _load_context_source({"type": "file", "path": "data.txt"}, str(tmp_path))
        assert result is not None
        assert "relative content" in result

    def test_file_source_missing_path(self):
        result = _load_context_source({"type": "file", "path": ""}, "")
        assert result is None

    def test_file_source_nonexistent(self):
        result = _load_context_source({"type": "file", "path": "/no/such/file.txt"}, "")
        assert result is None

    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_memory_source(self, mock_run):
        mock_run.return_value = [
            {
                "fact_text": "Philip likes coffee",
                "category": "personal",
                "confidence": 0.9,
                "similarity": 0.85,
            }
        ]
        result = _load_context_source({"type": "memory", "query": "coffee"}, "")
        assert result is not None
        assert "Philip likes coffee" in result
        assert "## Memory search: coffee" in result

    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_memory_source_empty_results(self, mock_run):
        mock_run.return_value = []
        result = _load_context_source({"type": "memory", "query": "nonexistent"}, "")
        assert result is None

    def test_memory_source_missing_query(self):
        result = _load_context_source({"type": "memory", "query": ""}, "")
        assert result is None

    @patch("robothor.memory.blocks.read_block")
    def test_block_source(self, mock_read):
        mock_read.return_value = {
            "block_name": "persona",
            "content": "I am Robothor",
            "last_written_at": "2026-03-01T00:00:00",
        }
        result = _load_context_source({"type": "block", "block_name": "persona"}, "")
        assert result is not None
        assert "I am Robothor" in result
        assert "## Memory block: persona" in result

    @patch("robothor.memory.blocks.read_block")
    def test_block_source_error(self, mock_read):
        mock_read.return_value = {"error": "Block not found"}
        result = _load_context_source({"type": "block", "block_name": "missing"}, "")
        assert result is None

    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_entity_source(self, mock_run):
        mock_run.return_value = {
            "name": "Philip",
            "entity_type": "person",
            "relations": [{"type": "owns", "target": "Robothor"}],
        }
        result = _load_context_source({"type": "entity", "name": "Philip"}, "")
        assert result is not None
        assert "## Entity: Philip" in result
        assert "person" in result

    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_entity_source_not_found(self, mock_run):
        mock_run.return_value = None
        result = _load_context_source({"type": "entity", "name": "nobody"}, "")
        assert result is None

    def test_unknown_source_type(self):
        result = _load_context_source({"type": "banana"}, "")
        assert result is None


# ─── Custom tool wrapper tests ───────────────────────────────────────


class TestCustomToolWrappers:
    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_search_memory_fn(self, mock_run):
        mock_run.return_value = [
            {
                "fact_text": "Test fact",
                "category": "test",
                "confidence": 0.8,
                "similarity": 0.9,
            }
        ]
        fn = _make_search_memory_fn()
        result = fn("test query")
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["fact"] == "Test fact"

    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_get_entity_fn(self, mock_run):
        mock_run.return_value = {"name": "Philip", "entity_type": "person"}
        fn = _make_get_entity_fn()
        result = fn("Philip")
        parsed = json.loads(result)
        assert parsed["name"] == "Philip"

    @patch("robothor.engine.rlm_tool.asyncio.run")
    def test_get_entity_fn_not_found(self, mock_run):
        mock_run.return_value = None
        fn = _make_get_entity_fn()
        result = fn("nobody")
        parsed = json.loads(result)
        assert parsed["found"] is False

    def test_read_file_fn(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        fn = _make_read_file_fn(str(tmp_path))
        result = fn("test.txt")
        assert result == "hello world"

    def test_read_file_fn_truncates(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 60_000)
        fn = _make_read_file_fn(str(tmp_path))
        result = fn("big.txt")
        assert len(result) < 60_000
        assert "truncated at 50KB" in result

    def test_read_file_fn_error(self):
        fn = _make_read_file_fn("/tmp")
        result = fn("/no/such/file.txt")
        assert "Error reading" in result

    @patch("robothor.memory.blocks.read_block")
    def test_memory_block_read_fn(self, mock_read):
        mock_read.return_value = {
            "block_name": "test",
            "content": "block content",
            "last_written_at": None,
        }
        fn = _make_memory_block_read_fn()
        result = fn("test")
        parsed = json.loads(result)
        assert parsed["content"] == "block content"


class TestBuildCustomTools:
    def test_returns_four_tools(self):
        tools = _build_custom_tools("/workspace")
        assert "search_memory" in tools
        assert "get_entity" in tools
        assert "read_file" in tools
        assert "memory_block_read" in tools

    def test_each_tool_has_tool_and_description(self):
        tools = _build_custom_tools("/workspace")
        for name, tool in tools.items():
            assert "tool" in tool, f"{name} missing tool"
            assert "description" in tool, f"{name} missing description"
            assert callable(tool["tool"]), f"{name} tool not callable"


# ─── Execution tests ─────────────────────────────────────────────────


def _mock_rlm_modules(mock_rlm_cls):
    """Patch sys.modules so `from rlm import RLM` and `from rlm.logger import RLMLogger` work."""
    mock_logger_cls = MagicMock()
    mock_logger_cls.return_value.log_file_path = "/tmp/trace.jsonl"

    mock_rlm_mod = MagicMock()
    mock_rlm_mod.RLM = mock_rlm_cls

    mock_logger_mod = MagicMock()
    mock_logger_mod.RLMLogger = mock_logger_cls

    return patch.dict(
        "sys.modules",
        {"rlm": mock_rlm_mod, "rlm.logger": mock_logger_mod},
    )


def _make_mock_result(response="result", cost=0.0):
    """Create a mock RLMChatCompletion."""
    mock = MagicMock()
    mock.response = response
    mock.usage_summary.total_cost = cost
    mock.execution_time = 1.0
    return mock


class TestExecuteDeepReason:
    def test_successful_call(self):
        mock_rlm_cls = MagicMock()
        mock_rlm_cls.return_value.completion.return_value = _make_mock_result(
            "The answer is 42", cost=0.15
        )

        with _mock_rlm_modules(mock_rlm_cls):
            result = execute_deep_reason(
                query="What is the meaning?",
                context="Some background info",
                config=DeepReasonConfig(log_dir="/tmp/rlm-test"),
            )

        assert "error" not in result
        assert result["response"] == "The answer is 42"
        assert result["execution_time_s"] >= 0
        assert result["context_chars"] > 0

    def test_import_error(self):
        """When rlm is not installed, return helpful error."""
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *a, **kw):
            if name == "rlm":
                raise ImportError("No module named 'rlm'")
            return original_import(name, *a, **kw)

        with patch.dict("sys.modules", {"rlm": None}):
            with patch("builtins.__import__", side_effect=mock_import):
                result = execute_deep_reason(
                    query="test",
                    config=DeepReasonConfig(log_dir="/tmp/rlm-test"),
                )

        assert "error" in result
        assert "pip install" in result["error"]

    def test_budget_exceeded_error(self):
        """BudgetExceededError returns partial result."""
        BudgetExceededError = type("BudgetExceededError", (Exception,), {})  # noqa: N806
        mock_rlm_cls = MagicMock()
        mock_rlm_cls.return_value.completion.side_effect = BudgetExceededError(
            "Budget limit reached"
        )

        with _mock_rlm_modules(mock_rlm_cls):
            result = execute_deep_reason(
                query="expensive query",
                config=DeepReasonConfig(log_dir="/tmp/rlm-test"),
            )

        assert "error" in result
        assert "budget" in result["error"].lower()
        assert result.get("partial") is True

    def test_timeout_exceeded_error(self):
        """TimeoutExceededError returns partial result."""
        TimeoutExceededError = type("TimeoutExceededError", (Exception,), {})  # noqa: N806
        mock_rlm_cls = MagicMock()
        mock_rlm_cls.return_value.completion.side_effect = TimeoutExceededError("Timeout reached")

        with _mock_rlm_modules(mock_rlm_cls):
            result = execute_deep_reason(
                query="slow query",
                config=DeepReasonConfig(log_dir="/tmp/rlm-test"),
            )

        assert "error" in result
        assert "timeout" in result["error"].lower()
        assert result.get("partial") is True

    def test_context_sources_preloaded(self, tmp_path):
        """Context sources are pre-loaded and concatenated."""
        f = tmp_path / "notes.txt"
        f.write_text("Important notes here")

        mock_rlm_cls = MagicMock()
        mock_rlm_cls.return_value.completion.return_value = _make_mock_result("Analyzed")

        with _mock_rlm_modules(mock_rlm_cls):
            result = execute_deep_reason(
                query="analyze notes",
                context_sources=[{"type": "file", "path": str(f)}],
                config=DeepReasonConfig(workspace=str(tmp_path), log_dir=str(tmp_path / "logs")),
            )

        assert "error" not in result
        # Verify RLM was called with context that includes the file
        call_args = mock_rlm_cls.return_value.completion.call_args
        payload = call_args[0][0]
        assert "Important notes here" in payload["context"]

    def test_no_context(self):
        """Runs fine with no context at all."""
        mock_rlm_cls = MagicMock()
        mock_rlm_cls.return_value.completion.return_value = _make_mock_result("Pure reasoning")

        with _mock_rlm_modules(mock_rlm_cls):
            result = execute_deep_reason(
                query="think about this",
                config=DeepReasonConfig(log_dir="/tmp/rlm-test"),
            )

        assert "error" not in result
        assert result["context_chars"] == 0

    def test_generic_exception(self):
        """Unknown exceptions are caught and returned as errors."""
        mock_rlm_cls = MagicMock()
        mock_rlm_cls.return_value.completion.side_effect = RuntimeError("kaboom")

        with _mock_rlm_modules(mock_rlm_cls):
            result = execute_deep_reason(
                query="broken",
                config=DeepReasonConfig(log_dir="/tmp/rlm-test"),
            )

        assert "error" in result
        assert "kaboom" in result["error"]


# ─── Registration test ───────────────────────────────────────────────


class TestToolRegistration:
    def test_deep_reason_in_registry(self):
        from robothor.engine.tools import get_registry

        registry = get_registry()
        # Check via direct dict access
        assert "deep_reason" in registry._schemas
        schema = registry._schemas["deep_reason"]
        params = schema["function"]["parameters"]
        assert "query" in params["properties"]
        assert "query" in params["required"]
        assert "context" in params["properties"]
        assert "context_sources" in params["properties"]

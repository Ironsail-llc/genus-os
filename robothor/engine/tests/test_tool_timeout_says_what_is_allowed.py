"""A timeout message that says "skip this step" gets the step skipped.

MEASURED 2026-09-16. An image-classification task timed out three times on
`exec` and received, each time:

    Tool 'exec' timed out after 120s. Try a different approach or skip this step.

It had 780 seconds of budget left and a 900-second ceiling available on the very
parameter it was never told about — `exec`'s own handler says so, in a message
the generic `TimeoutError` branch in the registry intercepted first. The model
followed the instruction it was given: took a different approach, guessed the
answers from filenames, and scored 0.28 against a competitor's 0.99 on a task
whose structural criteria it had already got perfect.

So: a handler that knows its own escalation gets to say so, and the generic text
never says "skip".
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.registry import timeout_guidance


class TestItNeverTellsTheModelToGiveUp:
    @pytest.mark.parametrize("tool", ["exec", "web_fetch", "some_plugin_tool"])
    def test_no_message_says_skip(self, tool):
        assert "skip" not in timeout_guidance(tool).lower(), tool

    @pytest.mark.parametrize("tool", ["exec", "web_fetch", "some_plugin_tool"])
    def test_every_message_names_something_to_do(self, tool):
        """"Try a different approach" is the only instruction the model was
        given, and it followed it. The remedy has to be in the message."""
        text = timeout_guidance(tool).lower()
        assert "narrow" in text or "smaller" in text or "more time" in text, tool


class TestExecSaysWhatItActuallyAllows:
    def test_it_names_the_timeout_parameter(self):
        assert "`timeout`" in timeout_guidance("exec")

    def test_it_names_the_real_ceiling(self):
        from robothor.engine.tools.handlers.filesystem import MAX_EXEC_TIMEOUT

        assert str(MAX_EXEC_TIMEOUT) in timeout_guidance("exec")

    def test_it_does_not_contradict_the_handler_s_own_message(self):
        """The handler's message and this one reach the model through different
        branches of the same failure. Two different remedies for one fault is
        how an agent learns to trust neither."""
        assert "more time" in timeout_guidance("exec").lower()

    def test_an_unknown_tool_gets_the_generic_text(self):
        assert timeout_guidance("exec") != timeout_guidance("not_a_real_tool")


class TestTheErrorCarriesIt:
    @pytest.mark.asyncio
    async def test_the_timeout_error_includes_the_guidance(self, monkeypatch):
        """The registry's own branch, which is the one that actually speaks —
        `asyncio.timeout` fires before the handler's `TimeoutExpired` can."""
        import robothor.engine.tools.registry as registry

        async def _hang(*_a, **_k):
            raise TimeoutError

        monkeypatch.setattr(registry, "_execute_tool", _hang)
        result = await registry.ToolRegistry().execute("exec", {}, timeout=1)
        assert "timed out after 1s" in result["error"]
        assert "`timeout`" in result["error"]
        assert "skip" not in result["error"].lower()

"""The exec timeout an agent can actually reach.

`filesystem.py` has honoured a caller-supplied timeout since it was written:

    timeout = int(args.get("timeout", 30))

The schema never exposed the parameter, and its description told the model
"Execute a shell command (30s timeout)". So the knob existed, worked, and
was unreachable — this instance's canonical failure shape: a correct
function with an inert caller.

The cost, traced through one benchmark run: a model API call needs more than
30s, so the agent backgrounds it with `&`/`nohup`; the sandbox reaps the
child when exec returns; the output file freezes mid-token on an unflushed
buffer; the agent misreads a mechanical failure as "the vision model is
unreliable here" and polls a dead file for 13 minutes — 65% of its budget.
Nothing in that chain happens if the agent can say `timeout: 120`.

A ceiling still applies, because an unbounded exec would outlive the run
that owns it.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.handlers.filesystem import (
    DEFAULT_EXEC_TIMEOUT,
    MAX_EXEC_TIMEOUT,
    resolve_exec_timeout,
)
from robothor.engine.tools.schemas import get_engine_schemas


class TestTheParameterIsReachable:
    def test_the_schema_exposes_timeout(self):
        params = get_engine_schemas()["exec"]["function"]["parameters"]
        assert "timeout" in params["properties"], "the knob is still unreachable"

    def test_only_command_stays_required(self):
        params = get_engine_schemas()["exec"]["function"]["parameters"]
        assert params["required"] == ["command"]

    def test_the_description_no_longer_hardcodes_30s(self):
        desc = get_engine_schemas()["exec"]["function"]["description"]
        assert "30s timeout" not in desc
        assert str(MAX_EXEC_TIMEOUT) in desc, "the model should know the ceiling"

    def test_the_description_names_the_background_trap(self):
        """The failure this fix exists to prevent, stated where the model reads
        it: backgrounding to dodge the limit loses the child."""
        desc = get_engine_schemas()["exec"]["function"]["description"].lower()
        assert "background" in desc


class TestResolvingTheTimeout:
    def test_absent_means_the_default(self):
        assert resolve_exec_timeout({}) == DEFAULT_EXEC_TIMEOUT

    def test_a_requested_value_is_honoured(self):
        assert resolve_exec_timeout({"timeout": 120}) == 120

    def test_a_string_from_the_model_is_accepted(self):
        """Models emit JSON numbers as strings often enough to matter."""
        assert resolve_exec_timeout({"timeout": "120"}) == 120

    def test_nonsense_falls_back_rather_than_raising(self):
        assert resolve_exec_timeout({"timeout": "soon"}) == DEFAULT_EXEC_TIMEOUT
        assert resolve_exec_timeout({"timeout": None}) == DEFAULT_EXEC_TIMEOUT

    def test_zero_and_negative_fall_back(self):
        assert resolve_exec_timeout({"timeout": 0}) == DEFAULT_EXEC_TIMEOUT
        assert resolve_exec_timeout({"timeout": -5}) == DEFAULT_EXEC_TIMEOUT

    def test_the_ceiling_is_enforced(self):
        assert resolve_exec_timeout({"timeout": 99999}) == MAX_EXEC_TIMEOUT

    def test_the_ceiling_is_generous_enough_to_matter(self):
        """30s was the whole problem; a ceiling that barely moves is no fix."""
        assert MAX_EXEC_TIMEOUT >= 600


class TestTheHandlerUsesIt:
    @pytest.mark.asyncio
    async def test_a_long_command_is_allowed_to_finish(self, monkeypatch, tmp_path):
        """End to end: a command that outlives the default completes when the
        agent asks for more time."""
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.filesystem import _exec

        result = await _exec(
            {"command": "sleep 2 && echo survived", "timeout": 60},
            ToolContext(workspace=str(tmp_path)),
        )
        assert "survived" in str(result.get("stdout", "") or result)

    @pytest.mark.asyncio
    async def test_the_timeout_error_reports_the_real_limit(self, monkeypatch, tmp_path):
        """A message that says 30 when the limit was 3 sends the agent hunting
        for the wrong problem."""
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.filesystem import _exec

        result = await _exec(
            {"command": "sleep 30", "timeout": 3}, ToolContext(workspace=str(tmp_path))
        )
        assert "3s" in str(result.get("error", "")), result

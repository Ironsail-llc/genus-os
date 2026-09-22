"""A feature nobody enabled must cost nobody anything.

Autonomy is opt-in: an owner enrols, turns execution on, and writes a standing
grant naming the agents it covers. Until all three have happened there is no
autonomy on this instance, and the engine has to look exactly as it did
before. Two regressions said otherwise, and both shipped to every instance:

* ``AgentSession.start`` appended a 136-token autonomy paragraph whenever the
  agent held the ``browser`` tool. Its wording — a standing grant is prior
  authorization, "do not ask for it again or impose a blanket stop before
  submission" — weakens the default posture of agents on instances that have
  no grants, no enrolment and no autonomy at all.
* The ``browser`` tool description went from 422 to several thousand
  characters, on every agent, on every instance, every turn. This instance
  measures schema tokens in a benchmark campaign, so that is a direct and
  visible regression in a number somebody is watching.

Both now key off one fact computed once per run: the feature is on for this
owner AND a live grant names this agent. The tests below pin both directions,
and the schema test asserts a character count so the description cannot
silently regrow.
"""

from __future__ import annotations

from robothor.engine.session import AgentSession

AUTONOMY_MARKER = "standing grant"


class TestTheSystemPromptParagraphIsConditional:
    def test_a_browser_agent_with_no_autonomy_gets_no_paragraph(self):
        session = AgentSession("test-agent")
        session.start("System prompt", "hello", ["browser", "exec"])
        system = session.messages[0]["content"]
        assert system.startswith("System prompt")
        assert AUTONOMY_MARKER not in system

    def test_a_browser_agent_under_a_live_grant_gets_the_paragraph(self):
        session = AgentSession("test-agent")
        session.start("System prompt", "hello", ["browser", "exec"], autonomy_active=True)
        system = session.messages[0]["content"]
        assert system.startswith("System prompt")
        assert AUTONOMY_MARKER in system
        assert "browser(action='autonomy'" in system

    def test_an_agent_without_the_browser_tool_never_gets_it(self):
        session = AgentSession("test-agent")
        session.start("System prompt", "hello", ["exec"], autonomy_active=True)
        assert session.messages[0]["content"].startswith("System prompt")
        assert AUTONOMY_MARKER not in session.messages[0]["content"]


class TestTheBrowserSchemaDoesNotGrowForEveryone:
    #: The description on ``main`` before autonomy was 422 characters; the
    #: ``act`` line then legitimately gained ``check`` and ``upload``, taking
    #: it to 436. Anything beyond this and a product feature has leaked back
    #: into the base schema of every agent on every instance.
    #:
    #: Raised from 460 to 490 once, deliberately. Trimming the description to
    #: 436 also removed the only occurrences of "form", "submit" and
    #: "website" — words that describe what the browser does with NO grant —
    #: and ``tool_search`` ranks on description words, so "fill in a payment
    #: form" went from rank 1 to rank 8 and "log in to a website" fell out of
    #: the top ten entirely. One 47-character sentence buys both back. The
    #: rest of the lost vocabulary (checkout, purchase, card, cart) is in
    #: ``TOOL_HINTS``, which the ranker reads and ``wire_schema`` strips, so
    #: it cost zero schema tokens; the cap is 483 actual + 7, not a budget to
    #: spend. ``test_browser_schema_paths_and_ranking.py`` holds the
    #: measurement.
    BASE_MAX = 490

    def _browser(self, schemas):
        found = [s for s in schemas if s.get("function", {}).get("name") == "browser"]
        assert found, "the fake registry did not advertise browser"
        return found[0]["function"]["description"]

    def test_a_run_with_autonomy_off_sees_the_short_description(self):
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        config = _browser_only_config()
        description = self._browser(registry.build_for_agent(config))
        assert len(description) <= self.BASE_MAX, (
            f"the base browser description is {len(description)} characters; "
            f"it must stay at or under {self.BASE_MAX}"
        )
        assert "autonomy" not in description.lower()
        assert "standing grant" not in description.lower()

    def test_a_run_with_autonomy_on_sees_the_autonomy_wording(self):
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        config = _browser_only_config()
        short = self._browser(registry.build_for_agent(config))
        long = self._browser(registry.build_for_agent(config, autonomy=True))
        assert long.startswith(short)
        assert len(long) > len(short) * 3
        assert "request.kind=status" in long

    def test_the_stored_schema_is_not_mutated_by_an_autonomy_run(self):
        """The registry holds ONE schema dict. Widening it in place would make
        the next agent's turn inherit the previous agent's grant wording."""
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        config = _browser_only_config()
        registry.build_for_agent(config, autonomy=True)
        after = self._browser(registry.build_for_agent(config))
        assert len(after) <= self.BASE_MAX


def _browser_only_config():
    from robothor.engine.models import AgentConfig

    return AgentConfig(
        id="test-agent",
        name="Test Agent",
        model_primary="test/model",
        tools_allowed=["browser"],
    )

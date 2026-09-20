"""Two things the browser-description split broke, measured.

Splitting ``browser``'s 8,410-character description into a 436-character base
plus a per-run autonomy half was right — every agent on every instance was
paying ~2,100 schema tokens for a feature nobody had enabled. It took two
things with it.

**I5 — discovery.** The trimmed-away half was mostly grant wording, but it
also carried the only occurrences of plain, grant-free words: *form*,
*submit*, *website*, *purchase*, *card*, *shopping*. ``tool_search`` ranks on
those words, so ``browser`` fell out of the results for plain Playwright work.
Measured over the whole registry (190 tools), browser's rank before the split
→ after it:

    submit a web form                3 → not in top 10
    enter credit card details        4 → gone
    fill in a payment form           1 → 8
    purchase something online        1 → gone
    pay for a subscription           1 → gone
    browser automation               1 → 1
    take a screenshot of a website   4 → 7
    log in to a website              2 → gone

Under deferred tool loading ``tool_search`` is the ONLY way an agent reaches a
non-CORE tool, so "submit a web form" returning ``web_fetch`` is the agent
never finding the browser at all. The fix puts one true sentence back in the
description and the rest of the operator's vocabulary in ``TOOL_HINTS``, which
the ranker reads and :func:`wire_schema` strips — so recall is restored at
zero schema-token cost, and none of the restored words describes an action an
agent without a grant cannot take.

**I4 — the widening reached one path of four.** ``build_for_agent(config,
autonomy=True)`` widens; three other ways a schema reaches a model did not:
``get_schema`` (what ``tool_describe`` returns on a deferred run), and both
Managed Agents bridge builders. A granted agent told by its system prompt to
call ``browser(action='autonomy', ...)`` could read the whole schema and find
no description of ``request`` at all.

Moving the wording into the ``request`` parameter's own description — one
place all four paths read — would have undone I5's whole point: the stored
parameter schema is what every path serves, so every agent would pay for the
autonomy wording again. The flag is threaded instead, and pinned here.
"""

from __future__ import annotations

import pytest

#: A sentence from the autonomy half. Its presence is the whole question.
AUTONOMY_SENTENCE = "request.kind=status"

#: Plain Playwright work, in the words an operator uses. None of these needs a
#: standing grant to be the right tool: a browser is how you reach a web form
#: whether or not anybody authorised a purchase at the end of it.
WEB_QUERIES = [
    "submit a web form",
    "enter credit card details",
    "fill in a payment form",
    "purchase something online",
    "pay for a subscription",
    "browser automation",
    "click a button on a page",
    "take a screenshot of a website",
    "log in to a website",
    "check out a shopping cart",
]

#: The defect ``keywords.py`` exists to fix, in reverse: ``browser`` used to
#: win "check my email" on the word "check". Restoring its web vocabulary must
#: not put it back in front of the families that own these queries.
NOT_BROWSER_QUERIES = [
    "check my email",
    "read my emails",
    "schedule a meeting",
    "cancel the meeting",
    "reply to that email",
    "send an email to a person",
    "look at my inbox",
    "create a task",
    "search my memory",
]


def _browser_config():
    from robothor.engine.models import AgentConfig

    return AgentConfig(
        id="test-agent",
        name="Test Agent",
        model_primary="test/model",
        tools_allowed=["browser"],
    )


def _description(schemas: list[dict]) -> str:
    found = [s for s in schemas if s.get("function", {}).get("name") == "browser"]
    assert found, "the registry did not advertise browser"
    return str(found[0]["function"]["description"])


@pytest.fixture
def registry():
    from robothor.engine.tools.registry import ToolRegistry

    return ToolRegistry()


class TestBrowserStaysFindableForPlainWebWork:
    """I5. Ranked over the FULL registry, the way the search handler does."""

    def _rank(self, registry, query: str) -> int | None:
        names = sorted(registry._schemas)
        hits = [h["name"] for h in registry.search_tools(names, query, limit=10)]
        return hits.index("browser") + 1 if "browser" in hits else None

    @pytest.mark.parametrize("query", WEB_QUERIES)
    def test_plain_playwright_work_reaches_the_browser(self, registry, query):
        rank = self._rank(registry, query)
        assert rank is not None and rank <= 3, (
            f"browser ranks {rank} for {query!r}; a deferred agent searching for "
            "the browser must find it in the first few hits"
        )

    @pytest.mark.parametrize("query", NOT_BROWSER_QUERIES)
    def test_the_restored_vocabulary_does_not_hijack_other_families(self, registry, query):
        rank = self._rank(registry, query)
        assert rank is None or rank > 3, (
            f"browser ranks {rank} for {query!r} — the web vocabulary has leaked "
            "back into the mail/calendar/CRM queries it used to steal"
        )

    def test_the_search_vocabulary_is_never_paid_for_on_the_wire(self, registry):
        """The words that restore the ranking must not reach a model.

        ``keywords``/``when_to_use``/``rank_bias`` are registry-only keys; a
        strict provider rejects an unknown key inside ``function`` outright.
        If the recall had been bought by growing the description instead, this
        is the test that would have to be weakened to ship it.
        """
        from robothor.engine.tools.registry import _HINT_KEYS

        stored = registry.get_schema("browser")
        assert stored is not None
        assert any(k in stored["function"] for k in _HINT_KEYS), (
            "browser carries no search hints at all, so the ranking is being "
            "paid for in description characters"
        )
        for schema in registry.build_for_agent(_browser_config()):
            assert not any(k in schema["function"] for k in _HINT_KEYS)

    def test_no_grant_wording_was_smuggled_back_into_the_base(self, registry):
        """The restored words describe what the browser does with no grant."""
        base = _description(registry.build_for_agent(_browser_config())).lower()
        for banned in ("autonomy", "grant", "purchase", "payment", "credential", "proposal"):
            assert banned not in base, f"{banned!r} is grant wording; it belongs in the half"


class TestEverySchemaPathAgreesAboutAutonomy:
    """I4. Four ways a browser schema reaches a model; all four must agree."""

    def test_build_for_agent_widens(self, registry):
        config = _browser_config()
        assert AUTONOMY_SENTENCE not in _description(registry.build_for_agent(config))
        assert AUTONOMY_SENTENCE in _description(registry.build_for_agent(config, autonomy=True))

    def test_get_schema_widens(self, registry):
        """What ``tool_describe`` returns on a deferred run.

        The probe that found this asked for ``browser``'s schema as a granted
        agent would and got the 436-character base: ``action=autonomy`` was in
        the enum with nothing anywhere saying what ``request`` should contain.
        """
        narrow = registry.get_schema("browser")
        assert narrow is not None
        assert AUTONOMY_SENTENCE not in str(narrow["function"]["description"])
        wide = registry.get_schema("browser", autonomy=True)
        assert wide is not None
        assert AUTONOMY_SENTENCE in str(wide["function"]["description"])

    def test_get_schema_does_not_mutate_the_stored_schema(self, registry):
        """One dict per registry, shared by every run in the process."""
        registry.get_schema("browser", autonomy=True)
        after = registry.get_schema("browser")
        assert after is not None
        assert AUTONOMY_SENTENCE not in str(after["function"]["description"])

    @pytest.mark.asyncio
    async def test_tool_describe_widens_for_a_granted_run(self, monkeypatch):
        out = await self._describe(monkeypatch, granted=True)
        assert AUTONOMY_SENTENCE in out["description"]

    @pytest.mark.asyncio
    async def test_tool_describe_stays_narrow_with_no_grant(self, monkeypatch):
        out = await self._describe(monkeypatch, granted=False)
        assert AUTONOMY_SENTENCE not in out["description"]

    @staticmethod
    async def _describe(monkeypatch, *, granted: bool) -> dict:
        import robothor.autonomy.availability as availability
        from robothor.engine.tools.constants import TOOLSEARCH_TOOLS
        from robothor.engine.tools.dispatch import (
            ToolContext,
            clear_deferred_allowed,
            set_deferred_allowed,
        )
        from robothor.engine.tools.handlers import toolsearch

        monkeypatch.setattr(availability, "autonomy_active", lambda *a, **k: granted)
        token = set_deferred_allowed(frozenset({"browser"}) | TOOLSEARCH_TOOLS)
        try:
            ctx = ToolContext(agent_id="test-agent", tenant_id="t", user_id="u", run_id="r")
            return await toolsearch._tool_describe({"name": "browser"}, ctx)
        finally:
            clear_deferred_allowed(token)

    def test_the_managed_agents_bridge_widens(self, registry):
        from robothor.engine.managed_agents.tool_bridge import build_ma_tools_for_agent

        config = _browser_config()
        narrow = build_ma_tools_for_agent(registry, config)
        wide = build_ma_tools_for_agent(registry, config, autonomy=True)
        assert AUTONOMY_SENTENCE not in narrow[0]["description"]
        assert AUTONOMY_SENTENCE in wide[0]["description"]

    def test_the_managed_agents_name_list_widens(self, registry):
        from robothor.engine.managed_agents.tool_bridge import build_ma_tools_from_names

        narrow = build_ma_tools_from_names(registry, ["browser"])
        wide = build_ma_tools_from_names(registry, ["browser"], autonomy=True)
        assert AUTONOMY_SENTENCE not in narrow[0]["description"]
        assert AUTONOMY_SENTENCE in wide[0]["description"]

    def test_the_managed_agents_runner_can_carry_the_answer(self):
        """The bridge's caller has to be able to pass it, or the parameter is
        decoration: ``_build_tools`` is the only way MA reaches the registry."""
        from robothor.engine.managed_agents.runner import _build_tools

        narrow = _build_tools("a", ["browser"], False)
        wide = _build_tools("a", ["browser"], False, autonomy=True)
        assert AUTONOMY_SENTENCE not in narrow[0]["description"]
        assert AUTONOMY_SENTENCE in wide[0]["description"]

    def test_the_wording_is_not_duplicated_into_the_request_parameter(self, registry):
        """The rejected alternative, pinned.

        Putting the autonomy text in ``parameters.properties.request`` would
        have reached all four paths with no flag — and made every agent on
        every instance pay for it again, on every path, which is the exact
        regression the split removed.
        """
        stored = registry.get_schema("browser")
        assert stored is not None
        request = stored["function"]["parameters"]["properties"]["request"]
        assert AUTONOMY_SENTENCE not in str(request.get("description", ""))
        assert len(str(request.get("description", ""))) < 500

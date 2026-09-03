"""Deferred tool loading is only safe if the agent can FIND the tool again.

`main` advertises 101 tools, 12,773 schema tokens, on every one of ~9,975 LLM
calls a week, and accounts for 36% of fleet spend. Deferral cuts that to 18
tools / 2,041 tokens — an 84% reduction — and the mechanism is built, tested
and armed. It is switched off, and the ranking is why:

    search "send an email to a person"
      -> browser, gws_gmail_reply, create_goal, tool_call, tool_search

`gws_gmail_send` is not in the top five. The scorer counted raw SUBSTRING
occurrences of every query term including stopwords, so `haystack.count("a")`
counted every letter *a* in the description. `browser` scored 34, of which 29
came from the letter "a" in a 422-character description; `gws_gmail_send`
scored 28 with its actual signal — "send", "email", and a name match — buried
underneath.

An agent that searches for the tool it needs and is handed `browser` is worse
off than one that was simply given all 101 schemas. So this has to rank
properly before the flag can be promoted, which is the whole reason it is
still off.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.registry import ToolRegistry


@pytest.fixture(scope="module")
def registry():
    return ToolRegistry()


@pytest.fixture(scope="module")
def names(registry):
    return list(registry._schemas)


def _top(registry, names, query, n=5):
    return [h["name"] for h in registry.search_tools(names, query, limit=n)]


# ── The queries that failed ──────────────────────────────────────────


def test_sending_an_email_finds_the_email_sender(registry, names):
    top = _top(registry, names, "send an email to a person")
    assert any("gmail_send" in t or t == "send_email" for t in top), top


def test_updating_a_crm_record_finds_a_crm_writer(registry, names):
    top = _top(registry, names, "update a CRM record for a person")
    assert any(t.startswith("update_") and "goal" not in t for t in top), top


def test_reading_a_file_finds_read_file(registry, names):
    assert "read_file" in _top(registry, names, "read a file from disk", n=3)


def test_searching_memory_finds_search_memory(registry, names):
    assert "search_memory" in _top(registry, names, "search my memory for a fact", n=3)


# ── Stopwords must not decide the ranking ────────────────────────────


def test_a_long_description_does_not_outrank_an_exact_name_match(registry, names):
    """The exact defect: `browser` beat `gws_gmail_send` on letter frequency."""
    top = _top(registry, names, "send an email", n=3)
    assert "browser" not in top, top


def test_stopwords_alone_do_not_rank_anything_highly(registry, names):
    """A query that is only stopwords carries no signal, and the scorer must
    not invent some by counting letters."""
    results = registry.search_tools(names, "a an the to of", limit=5)
    assert all(r["name"] for r in results)  # no crash, well-formed


def test_a_single_letter_term_is_ignored(registry, names):
    with_letter = _top(registry, names, "read a file", n=3)
    without = _top(registry, names, "read file", n=3)
    assert with_letter == without, "the stray 'a' changed the ranking"


# ── Robustness ───────────────────────────────────────────────────────


def test_an_empty_query_returns_something_rather_than_exploding(registry, names):
    assert registry.search_tools(names, "", limit=3)


def test_the_limit_is_honoured(registry, names):
    assert len(registry.search_tools(names, "email", limit=2)) <= 2


def test_search_is_scoped_to_the_names_it_is_given(registry):
    """tool_search receives the agent's allow-set. Returning a tool outside it
    would advertise something the agent is then refused when it calls."""
    allowed = {"read_file", "list_directory"}
    results = registry.search_tools(allowed, "send an email", limit=5)

    assert {r["name"] for r in results} <= allowed


def test_every_result_carries_a_description(registry, names):
    """The description is what the agent decides from; a bare name is useless."""
    for hit in registry.search_tools(names, "calendar event", limit=5):
        assert hit["description"].strip(), hit["name"]


# ── A term common to many tool names carries less information ──────────


def test_a_shell_command_query_finds_exec(registry, names):
    """ "run" appears in classify_run_failure, get_agent_run, list_agent_runs...
    so a single match on it outranked `exec`, which matches "shell" and
    "command" in its description. A term shared by many tools discriminates
    between them less, which is what IDF is for."""
    assert "exec" in _top(registry, names, "run a shell command", n=2)


def test_looking_up_a_company_finds_the_getter(registry, names):
    assert "get_company" in _top(registry, names, "look up a company by name", n=3)


def test_a_distinctive_term_still_dominates(registry, names):
    """IDF must not flatten everything: a rare, exact term is the strongest
    signal there is."""
    assert _top(registry, names, "gmail", n=1)[0].startswith("gws_gmail")

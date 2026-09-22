"""Tool Registry — schema filtering + execution for the Agent Engine."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.spawn_cancel import tool_deadline
from robothor.engine.tools.constants import (
    CORE_TOOLS,
    GOAL_TOOLS,
    OPT_IN_TOOLS,
    SPAWN_TOOLS,
    TODO_TOOLS,
    TOOLSEARCH_TOOLS,
)
from robothor.engine.tools.dispatch import _execute_tool
from robothor.engine.tools.schemas import get_engine_schemas
from robothor.engine.workflow_budget import WorkflowDeadlineError

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.models import AgentConfig
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)


def builtin_schemas() -> dict[str, Any]:
    """Every tool schema CORE ships: the MCP definitions plus the engine's.

    One expression, read by ``ToolRegistry._register_all`` (which seeds
    ``_schemas`` with it, and then reserves those names against plugins) and by
    ``loader.builtin_names("genus.schemas")``. Split out because the two had
    drifted: the loader's table pointed at ``get_engine_schemas()`` alone, so
    the 53 MCP tool names — ``create_person``, ``approve_task``, every CRM verb
    — were reserved in production and derivable by nobody, and a plugin
    shadowing one reported as loaded on every operator surface.
    """
    from robothor.api.mcp import get_tool_definitions

    schemas: dict[str, Any] = {}
    for defn in get_tool_definitions():
        name = defn["name"]
        schemas[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": defn["description"],
                "parameters": defn["inputSchema"],
            },
        }
    schemas.update(get_engine_schemas())
    _stamp_hints(schemas)
    return schemas


#: Registry-only keys stamped onto a schema's ``function`` block by
#: :func:`_stamp_hints`. They drive :meth:`ToolRegistry.search_tools`; they are
#: NOT part of the OpenAI function-schema contract, so :func:`wire_schema`
#: removes them again before anything is advertised to a model.
_HINT_KEYS = ("keywords", "when_to_use", "rank_bias")


def _stamp_hints(schemas: dict[str, Any]) -> None:
    """Attach each tool's search vocabulary to its schema, in place.

    Done here rather than in ``get_engine_schemas`` because the CRM/memory half
    of the registry comes from ``robothor.api.mcp`` and needs the same
    treatment: ``get_inbox`` (the agent's notification queue) and
    ``list_messages`` (ingested CRM correspondence) were ranking for mail
    queries beside the Gmail tools with nothing in either schema to tell them
    apart.
    """
    from robothor.engine.tools.keywords import TOOL_HINTS

    for name, hint in TOOL_HINTS.items():
        fn = schemas.get(name, {}).get("function")
        if not isinstance(fn, dict):
            continue
        if hint.keywords:
            fn["keywords"] = list(hint.keywords)
        if hint.when_to_use:
            fn["when_to_use"] = hint.when_to_use
        if hint.rank_bias:
            fn["rank_bias"] = hint.rank_bias


def wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A schema with the registry-only hint keys removed, safe to send.

    Copy-on-write: a schema carrying no hints is returned as-is, so the common
    path allocates nothing. A provider that validates its request body rejects
    an unknown key inside ``function`` outright, which would take the whole
    turn down rather than degrade a ranking.
    """
    fn = schema.get("function")
    if not isinstance(fn, dict) or not any(k in fn for k in _HINT_KEYS):
        return schema
    stripped = dict(schema)
    stripped["function"] = {k: v for k, v in fn.items() if k not in _HINT_KEYS}
    return stripped


def with_autonomy(schema: dict[str, Any], autonomy: bool) -> dict[str, Any]:
    """The ``browser`` schema plus its autonomy half, or the schema as-is.

    Copies before it writes. ``wire_schema`` hands back a structure that
    shares the stored description string, and appending in place would leak
    one run's grant wording into every later run in the process.

    Module-level rather than a ``ToolRegistry`` method because a schema
    reaches a model by four routes — ``build_for_agent``, ``get_schema`` (what
    ``tool_describe`` serves) and both Managed Agents bridge builders — and
    only the first of them was widening. One function they all call is the
    only way they stay in agreement.
    """
    if not autonomy or schema.get("function", {}).get("name") != "browser":
        return schema
    from robothor.engine.tools.schemas import BROWSER_AUTONOMY_DESCRIPTION

    function = dict(schema["function"])
    function["description"] = str(function.get("description", "")) + BROWSER_AUTONOMY_DESCRIPTION
    return {**schema, "function": function}


def builtin_schema_names() -> set[str]:
    """The names :func:`builtin_schemas` provides — what a plugin may not claim."""
    return set(builtin_schemas())


# ── Tool search ranking ───────────────────────────────────────────────
#
# The first version scored `sum(haystack.count(term))` over the raw query
# split. `str.count` is SUBSTRING counting, so a one-letter term counted every
# occurrence of that letter in the description: searching "send an email to a
# person", `browser` scored 34 — 29 of them the letter "a" in a 422-character
# description — and beat `gws_gmail_send` at 28, whose points came from
# actually matching "send", "email" and its own name.
#
# An agent that searches for the tool it needs and is handed `browser` is
# worse off than one simply given all 101 schemas, which is why deferred tool
# loading could not be switched on.

_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "is",
        "are",
        "be",
        "as",
        "it",
        "its",
        "this",
        "that",
        "from",
        "into",
        "my",
        "me",
        "i",
        "you",
        "your",
        "please",
        "get",
        "some",
        "any",
        "do",
        "does",
        "how",
        "what",
        "when",
        "where",
        "who",
        "whom",
        "whose",
        "can",
        # Function words that carried real weight because they appear in
        # descriptions: "do I have any new email" ranked `gws_gmail_get` on
        # "have" ("when you already HAVE a message id"), and "email alice
        # about the invoice" spent a third of its signal on "about".
        "about",
        "have",
        "has",
        "had",
        "was",
        "were",
        "been",
        "being",
        "will",
        "would",
        "could",
        "should",
        "must",
        "might",
        "there",
        "here",
        "they",
        "them",
        "their",
        "she",
        "her",
        "hers",
        "his",
        "him",
        "our",
        "ours",
        "these",
        "those",
        "then",
        "than",
        "because",
        "while",
        "but",
        "not",
        "all",
        "other",
        "such",
        "own",
        "same",
        "very",
        "too",
        "also",
        "just",
        "still",
        "again",
        "out",
        "over",
        "off",
        "per",
        "via",
        "yet",
        "ever",
        "never",
        "done",
        "say",
        "said",
        "says",
        "tell",
        "told",
        "want",
        "need",
        "know",
        "think",
    }
)

#: Below this length a term carries no signal and a lot of noise.
_MIN_TERM_LENGTH = 3

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Words whose trailing "s" is part of the word, not a plural.
_NOT_PLURAL = ("ss", "us", "is", "as")


def _drop_silent_e(word: str) -> str:
    """Normalise a trailing "e" away, on EVERY word.

    "scheduled" loses its "ed" to give "schedul" while "schedule" keeps its
    "e" — so the conjugated form and the keyword would never meet. Dropping the
    "e" from both is what makes the two sides agree: "schedule"/"scheduled" ->
    "schedul", "archive"/"archived" -> "archiv". Short words keep it, so
    "note"/"notes" stay "note" together.
    """
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


#: A final consonant doubled before a suffix. English doubles it on a stressed
#: final syllable — cancel/cancelled, ship/shipped, refer/referred — and
#: stripping the suffix leaves the double behind, so `cancelled` became
#: `cancell` and never met `cancel`, the intent verb that picks
#: `gws_calendar_delete` out of a family that all shares the noun.
#:
#: Only the consonants English actually doubles that way. `s` is the important
#: exclusion: `address`, `process` and `pass` end in a double that is part of
#: the word, and `addressed` -> `address` is already correct.
_DOUBLED_RE = re.compile(r"(?<=[a-z])([bdfglmnprt])\1$")


def _collapse_doubled(word: str) -> str:
    """Normalise a doubled final consonant away, on EVERY word.

    Applied to every word rather than only to a word a suffix was stripped
    from, for the same reason :func:`_drop_silent_e` is: the two sides have to
    receive the SAME transformation or they cannot meet. `cancel` keeps its
    single `l` and `cancelled` loses one, so both arrive at `cancel`; `call`
    and `called` both arrive at `cal`, which is a word neither of them is and
    exactly what a crude stemmer is for.
    """
    return _DOUBLED_RE.sub(r"\1", word) if len(word) > 3 else word


#: Verb endings this strips. "who emailed me" and "who sent me that" are among
#: the most natural phrasings an operator uses, and a plural-only stemmer left
#: `emailed` as `emailed` — matching nothing, so the query reached no mail tool
#: at all.
_VERB_SUFFIXES = ("ing", "ed")


def _stem(word: str) -> str:
    """A crude singulariser and de-conjugator, applied to BOTH sides.

    "read my emails" reached no Gmail tool partly because the schemas say
    "email" and the operator said "emails"; "meetings", "attendees" and
    "calendars" had the same problem, and so did "emailed" and "forwarding".
    A real stemmer would be a dependency and a source of surprises ("business"
    -> "busines"); this handles the two cases that show up in tool vocabulary —
    a plural noun and a conjugated verb — and leaves everything else alone. It
    is applied to the query, to keywords, to name words and to description
    words, so the two sides can never disagree about a word.

    It is NOT idempotent, and does not need to be; see the comment below.
    """
    # Plural first, then the verb ending, then the endings that are normalised
    # on every word — in that order and in ONE pass. Stripping the plural and
    # returning left "meetings" -> "meeting" while "meeting" -> "meet", and the
    # two never met.
    #
    # One pass is not the same as idempotent, and the docstring above used to
    # claim it was: the `break` below leaves a second verb suffix in place, so
    # "proceedings" -> "proceed" while "proceed" -> "proc". No caller stems
    # already-stemmed text — every one starts from raw words — so the property
    # that matters is that both sides get the same transformation, which one
    # pass does give. `test_the_stemmer_does_not_promise_idempotence` pins it.
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("es") and word[-4:-2] in ("ch", "sh", "ss", "zz"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(_NOT_PLURAL):
        word = word[:-1]

    for suffix in _VERB_SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break

    return _collapse_doubled(_drop_silent_e(word))


def _stems(text: str) -> set[str]:
    """The stemmed word set of a piece of text."""
    return {_stem(w) for w in _WORD_RE.findall(text.lower())}


def _query_terms(query: str) -> list[str]:
    """Content words from a free-text query, stemmed, deduplicated, in order."""
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(query.lower()):
        if len(word) >= _MIN_TERM_LENGTH and word not in _SEARCH_STOPWORDS:
            seen.setdefault(_stem(word), None)
    return list(seen)


#: What an agent SAYS it wants, mapped to the verb a tool is named with.
#: Without this, "look up a company" ranks the vision tool `look` first on an
#: exact name match, and the four *_company tools tie so the answer falls out
#: alphabetically — `create_company` for a read-only request. Intent is the
#: difference between keyword search and a router.
_INTENT_VERBS: dict[str, frozenset[str]] = {
    "look": frozenset({"get", "list", "search", "read", "find"}),
    "find": frozenset({"get", "list", "search", "read"}),
    "fetch": frozenset({"get", "read", "list"}),
    "retrieve": frozenset({"get", "read", "list"}),
    "show": frozenset({"get", "list", "read"}),
    "view": frozenset({"get", "list", "read"}),
    "check": frozenset({"get", "list", "search"}),
    "make": frozenset({"create", "add", "new"}),
    "add": frozenset({"create", "add"}),
    "new": frozenset({"create", "add"}),
    "change": frozenset({"update", "modify", "set", "edit"}),
    "edit": frozenset({"update", "modify", "set"}),
    "modify": frozenset({"update", "modify", "set"}),
    "remove": frozenset({"delete", "remove", "drop"}),
    "drop": frozenset({"delete", "remove"}),
    "execute": frozenset({"exec", "run"}),
    # Mail and calendar verbs. "schedule a meeting", "book a call" and "cancel
    # the meeting" returned nothing or the wrong half of the calendar family:
    # every calendar tool shares the noun, and only the verb says which.
    "schedule": frozenset({"create", "add", "book"}),
    "book": frozenset({"create", "add"}),
    "arrange": frozenset({"create", "add"}),
    "invite": frozenset({"create", "add"}),
    "cancel": frozenset({"delete", "remove", "cancel"}),
    "reply": frozenset({"reply"}),
    "respond": frozenset({"reply"}),
    "answer": frozenset({"reply"}),
    "send": frozenset({"send"}),
    "compose": frozenset({"send"}),
    "draft": frozenset({"send"}),
    "forward": frozenset({"send"}),
    "mark": frozenset({"modify", "update", "set"}),
    "archive": frozenset({"modify"}),
    "label": frozenset({"modify"}),
}

#: The same table, stemmed on both sides.
#:
#: Query terms are stemmed before they reach the ranker, so "schedule" arrives
#: as "schedul" and a raw lookup in the table above missed it entirely —
#: "schedule a meeting" lost its intent bonus and `gws_calendar_list` took the
#: tie on rank_bias, which is the read tool for a plainly-a-write request. The
#: VALUES are stemmed too, because they are compared against a tool's stemmed
#: vocabulary ("create_task" -> {creat, task}).
_INTENT_VERBS_STEMMED: dict[str, frozenset[str]] = {
    _stem(verb): frozenset(_stem(target) for target in targets)
    for verb, targets in _INTENT_VERBS.items()
}

#: Worth less than an exact name match, more than a description hit: it says
#: which of several same-noun tools the agent meant.
_INTENT_BONUS = 4.0


def _object_terms(terms: list[str]) -> list[str]:
    """The query's terms that name a THING rather than an action.

    "read my emails" is a verb the whole registry answers to and a noun only
    three tools answer to. The verb half was the only half the intent bonus
    looked at, so `read_file` and `memory_block_read` collected four points for
    the word "read" and finished above every Gmail tool, which is the second
    of the two ways the agent was handed the wrong tool.
    """
    return [t for t in terms if t not in _INTENT_VERBS_STEMMED]


def _intent_bonus(vocabulary: set[str], terms: list[str], objects: list[str]) -> float:
    """Reward a tool whose verb matches what the query asked to DO.

    Only when the tool's own vocabulary — its name words, its keywords, the
    words of its description — mentions at least one of the things the query
    named. A tool that has nothing to do with email does not get four points
    for the word "read"; a tool that has nothing to do with a meeting does not
    get four points for "cancel".
    """
    if not objects:
        # A query that is only a verb names nothing to disambiguate between.
        # "what is on my schedule" is not a request to create something, and
        # crediting every `create_*` tool for the verb is how it became one.
        return 0.0
    if not vocabulary & set(objects):
        return 0.0
    for term in terms:
        wanted = _INTENT_VERBS_STEMMED.get(term)
        if wanted and (vocabulary & wanted):
            return _INTENT_BONUS
    return 0.0


class SearchHit(TypedDict, total=False):
    """One ``tool_search`` result.

    A TypedDict rather than ``dict[str, str]`` because the handler adds
    ``in_toolset``, a bool — which made `mypy robothor/` red on this branch
    while green on main, and the commit that introduced the line rewrote it
    without noticing. Declaring the shape is what stops the next field from
    doing the same silently.
    """

    name: str
    description: str
    #: Set by the ``tool_search`` handler, per hit: is this tool already
    #: advertised to the model this turn, or does it need ``tool_call``?
    in_toolset: bool


class _Candidate(NamedTuple):
    """One tool as the ranker sees it."""

    name: str
    description: str
    keywords: tuple[str, ...]
    bias: float = 0.0


def _term_weights(terms: list[str], corpus: list[_Candidate]) -> dict[str, float]:
    """Inverse document frequency over the candidate tools.

    "run" appears in classify_run_failure, get_agent_run, list_agent_runs and
    more, so a single match on it is weak evidence — yet it outranked `exec`,
    which matched "shell" AND "command" in its description. A term shared by
    many candidates discriminates between them less. A rare exact term keeps
    its full weight, which is what keeps "gmail" decisive.
    """
    total = max(1, len(corpus))
    weights: dict[str, float] = {}
    for term in terms:
        df = 0
        for candidate in corpus:
            haystack = (
                _stems(candidate.name)
                | _stems(candidate.description)
                | {_stem(k) for k in candidate.keywords}
            )
            if term in haystack or term in candidate.name.lower():
                df += 1
        weights[term] = math.log(1 + total / (1 + df))
    return weights


def _score_tool(
    name: str,
    description: str,
    terms: list[str],
    weights: dict[str, float] | None = None,
    keywords: tuple[str, ...] = (),
    bias: float = 0.0,
) -> tuple[float, int]:
    """Rank one tool against the query's content words.

    Returns ``(score, keyword coverage)``. The name is worth far more than the
    description: a tool is named for what it does, while descriptions vary in
    length by a factor of five and a long one should not win on volume. Each
    term is counted ONCE per field for the same reason — repetition is a
    property of the prose, not of relevance.

    A keyword hit counts as much as a hit on the tool's own name. That is the
    whole point of the vocabulary table: ``gws_gmail_search`` is not named
    "email" and never will be, and "email" is the word the operator uses. The
    coverage count rides along because it decides ties — three tools scored
    7.28 for "check my email" and the answer fell out alphabetically, which is
    how ``browser`` came first.
    """
    name_words = {_stem(w) for w in _WORD_RE.findall(name.lower())}
    desc_words = _stems(description)
    keyword_set = {_stem(k) for k in keywords}
    score = 0.0
    for term in terms:
        w = (weights or {}).get(term, 1.0)
        if term in name_words or term in keyword_set:
            score += 6.0 * w  # exact word in the tool's own name or vocabulary
        elif term in name.lower():
            score += 3.0 * w  # substring, e.g. "mail" in gws_gmail_send
        if term in desc_words:
            score += 2.0 * w
        elif term in description.lower():
            score += 0.5 * w
    # A tie between a tool matching two terms and one matching one is decided
    # by coverage, not by which happened to sort first.
    matched = sum(
        1
        for t in terms
        if t in name_words or t in desc_words or t in keyword_set or t in name.lower()
    )
    covered = sum(1 for t in terms if t in keyword_set)
    vocabulary = name_words | desc_words | keyword_set
    total = score + matched * 0.25 + _intent_bonus(vocabulary, terms, _object_terms(terms))
    # The family entry-point nudge, and only for a tool that matched at all:
    # a bias must never float an unrelated tool into the results.
    if score > 0:
        total += bias
    return total, covered


class ToolRegistry:
    """Registry of available tools with schema filtering per agent."""

    # Warn-once dedup for unresolved-tool warnings, keyed by agent id. Class-level
    # (shared across instances) so re-instantiated registries — the sub-agent
    # runner and template validator each build a fresh ToolRegistry() — don't
    # degrade the "warn once" guarantee into "warn every build".
    _warned_unresolved: set[str] = set()

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        #: Names contributed by plugins, so a reload can withdraw them again.
        self._plugin_schema_names: set[str] = set()
        #: Plugin generation `_schemas` reflects; -1 until first registration.
        self._schema_generation: int = -1
        self._adapter_routes: dict[str, str] = {}  # tool_name → adapter server name
        self._register_all()

    @classmethod
    def reset_unresolved_warnings(cls) -> None:
        """Clear the process-wide warn-once dedup (test isolation hook)."""
        cls._warned_unresolved.clear()

    def _register_all(self) -> None:
        """Register all tool schemas."""
        self._schemas.update(builtin_schemas())

        # Plugin-contributed schemas. Without this the seam is half-wired:
        # dispatch.py registers a plugin's HANDLER, so the tool can be
        # executed, but `_get_filtered_names` filters tools_allowed by
        # membership in `_schemas`, so a plugin tool named in a manifest was
        # routed to the "silently unavailable" branch and never advertised to
        # the model. docs/PLUGINS.md promised the opposite in plain words, and
        # the seam's own tests asserted against the loader's dataclass rather
        # than the registry, so they passed while nothing worked.
        #
        # Built-ins are passed as reserved so a plugin cannot shadow `exec`
        # or `write_file` — a takeover, not an extension. The reserved set is
        # taken AFTER the built-ins are registered, so it is the real one.
        self._register_plugin_schemas()

    def _refresh_plugin_schemas_if_stale(self) -> None:
        """Re-read plugin schemas when the plugin generation has moved.

        Installing a plugin used to require a restart before the model was
        told the tool existed. The loader's generation counter marks this
        cache stale; the names a plugin contributed are tracked so they can
        be withdrawn again when the plugin goes away, which a plain re-add
        would never do.
        """
        from robothor.plugins import generation

        current = generation()
        if current == self._schema_generation:
            return
        for name in self._plugin_schema_names:
            self._schemas.pop(name, None)
        self._plugin_schema_names = set()
        self._register_plugin_schemas()

    def _register_plugin_schemas(self) -> None:
        """Advertise plugin tools to the model. Never raises: one broken
        package must not stop the engine from starting."""
        try:
            from robothor.plugins import load_plugins

            plugins = load_plugins(reserved_names=set(self._schemas))
        except Exception as e:  # noqa: BLE001 - a plugin must not break boot
            logger.warning("Plugin schema registration skipped: %s", e)
            return

        from robothor.plugins import generation

        self._schema_generation = generation()
        registered: list[str] = []
        for name, schema in (plugins.schemas or {}).items():
            if name in self._schemas:
                logger.warning(
                    "Plugin schema %r refused — it would shadow a built-in tool",
                    name,
                )
                continue
            self._schemas[name] = schema
            self._plugin_schema_names.add(name)
            registered.append(name)

        # A handler with no declared schema still has to be advertised, or the
        # documented quickstart is false: docs/PLUGINS.md's example contributes
        # only `handlers` and then promises the tool "is available to any agent
        # whose manifest lists it in tools_allowed". Synthesize a permissive
        # schema from the handler's docstring so that promise holds. An
        # explicit schema always wins.
        for name, handler in (plugins.tools or {}).items():
            if name in self._schemas:
                continue
            doc = (getattr(handler, "__doc__", "") or "").strip().split("\n")[0]
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc or f"Plugin tool {name}.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            registered.append(name)

        if registered:
            logger.info(
                "Registered %d plugin tool schema(s): %s",
                len(registered),
                ", ".join(sorted(registered)),
            )

    # Adapter connection failure cache: {adapter_name: (fail_time, backoff_seconds)}
    _adapter_failures: dict[str, tuple[float, float]] = {}

    async def register_adapter_tools(self, adapters: list[Any]) -> None:
        """Connect to adapter MCP servers, discover tools, register as first-class schemas.

        Resilience features:
        - 5s timeout per adapter connection (don't block agent startup)
        - Failed adapters cached with exponential backoff (5min initial, 30min max)
        - Failures logged once at WARNING, then suppressed until retry window
        """
        import asyncio
        import time

        from robothor.engine.adapters import verify_adapter_integrity
        from robothor.engine.mcp_client import get_mcp_client_pool

        pool = get_mcp_client_pool()
        for adapter in adapters:
            # Integrity BEFORE connection: an unverified stdio binary must not
            # even be spawned, let alone asked for its tool list.
            ok, reason = verify_adapter_integrity(adapter)
            if not ok:
                logger.error(
                    "Adapter '%s' REFUSED — integrity check failed: %s",
                    adapter.name,
                    reason,
                )
                continue
            # Check failure cache — skip if in backoff window
            now = time.monotonic()
            if adapter.name in self._adapter_failures:
                fail_time, backoff = self._adapter_failures[adapter.name]
                if now - fail_time < backoff:
                    logger.debug(
                        "Adapter '%s': skipping (backoff %.0fs remaining)",
                        adapter.name,
                        backoff - (now - fail_time),
                    )
                    continue

            try:
                session = await asyncio.wait_for(pool.get_session(adapter.name), timeout=5.0)
                mcp_tools = await asyncio.wait_for(session.list_tools(), timeout=5.0)
                allow = set(adapter.tools_allowed)
                if not allow:
                    logger.warning(
                        "Adapter '%s' has no tools_allowed list — registering "
                        "every tool its server offers (legacy allow-all). Pin "
                        "the expected tools in the adapter YAML so a changed "
                        "server cannot sprout new capabilities silently.",
                        adapter.name,
                    )
                drifted: list[str] = []
                for tool in mcp_tools:
                    name = tool.get("name", "")
                    if not name:
                        continue
                    if allow and name not in allow:
                        drifted.append(name)
                        continue
                    self._schemas[name] = {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": tool.get("description", ""),
                            "parameters": tool.get(
                                "inputSchema", {"type": "object", "properties": {}}
                            ),
                        },
                    }
                    self._adapter_routes[name] = adapter.name
                if drifted:
                    # Loud on purpose: the server offered tools the bundle
                    # never declared. That is the exact supply-chain move —
                    # a compromised or auto-updated server growing new
                    # capabilities — and it must read as an event, not debug.
                    logger.warning(
                        "Adapter '%s' DRIFT: server offered %d tool(s) not in "
                        "tools_allowed, refused: %s",
                        adapter.name,
                        len(drifted),
                        ", ".join(sorted(drifted)),
                    )
                # Clear failure cache on success
                self._adapter_failures.pop(adapter.name, None)
                logger.info("Adapter '%s': discovered %d tools", adapter.name, len(mcp_tools))
            except Exception as e:
                # Exponential backoff: 300s (5min) -> 600s -> 1200s -> max 1800s (30min)
                _, prev_backoff = self._adapter_failures.get(adapter.name, (0, 150.0))
                new_backoff = min(prev_backoff * 2, 1800.0)
                self._adapter_failures[adapter.name] = (now, new_backoff)
                if prev_backoff <= 150.0:
                    # First failure or first retry — log at WARNING
                    logger.warning(
                        "Adapter '%s' unavailable (backoff %.0fs): %s",
                        adapter.name,
                        new_backoff,
                        e,
                    )
                else:
                    logger.debug(
                        "Adapter '%s' still unavailable, backoff %.0fs", adapter.name, new_backoff
                    )

    def get_adapter_route(self, tool_name: str) -> str | None:
        """Return the adapter server name for a tool, or None if not adapter-provided."""
        return self._adapter_routes.get(tool_name)

    def registered_tool_names(self) -> set[str]:
        """Every tool name this instance can dispatch, right now.

        Including the ones registered at runtime from an adapter's
        ``tools/list``, which no static schema module knows about. A caller
        building a DENY-list needs exactly this set: the benchmark harness
        computed its deny-list from ``robothor.api.mcp.get_tool_definitions()``
        and ``get_engine_schemas()``, and an adapter tool is in neither — so it
        was advertised to a graded sub-agent and dispatched at
        ``dispatch._execute_tool`` before ``ToolContext`` (and therefore before
        every ``ctx.is_benchmark`` gate) existed.
        """
        return set(self._schemas) | set(self._adapter_routes)

    def adapter_tool_names(self) -> set[str]:
        """Just the adapter-provided names — the ones that leave this process."""
        return set(self._adapter_routes)

    def build_for_agent(
        self, config: AgentConfig, *, autonomy: bool = False
    ) -> list[dict[str, Any]]:
        """Return filtered tool schemas for an agent based on allow/deny lists.

        When deferral (Rip 16 / G4) is active for this agent, advertise only the
        CORE_TOOLS subset plus the tool_search/tool_describe/tool_call meta-tools;
        the agent's other allowed tools load on demand. Enforcement of the real
        allow-list lives in the tool_call/tool_describe handlers, which check the
        ``_deferred_allowed`` set the runner publishes via ``set_deferred_allowed``
        (see deferred_whitelist) — so tool_call cannot reach a denied tool.

        ``autonomy`` widens the ``browser`` description with the delegated
        account/payment wording. It is off unless the feature is enabled for
        this run's owner AND a live grant names this agent — the runner works
        that out once, in toolset_prep. Left always-on it cost roughly 1,100
        schema tokens per turn to every agent on every instance, including the
        overwhelming majority that can never reach a grant.
        """
        self._refresh_plugin_schemas_if_stale()
        names = self._get_filtered_names(config)
        if self.should_defer(config):
            seen: set[str] = set()
            advertised: list[str] = []
            for n in [*names, *sorted(TOOLSEARCH_TOOLS)]:
                in_core = n in CORE_TOOLS or n in TOOLSEARCH_TOOLS
                if in_core and n in self._schemas and n not in seen:
                    seen.add(n)
                    advertised.append(n)
            names = advertised
        return [with_autonomy(wire_schema(self._schemas[n]), autonomy) for n in names]

    def should_defer(self, config: AgentConfig) -> bool:
        """True iff this agent's toolset should be deferred (Rip 16 / G4).

        Deferral kicks in only for broad-access agents — those whose advertised
        tool count exceeds the threshold — so curated small-toolset workers keep
        their full set with no extra tool_search round-trip.
        """
        from robothor.engine.feature_flags import (
            deferred_tools_enabled,
            deferred_tools_threshold,
        )

        if not deferred_tools_enabled():
            return False
        return len(self._get_filtered_names(config)) > deferred_tools_threshold()

    def deferred_whitelist(self, config: AgentConfig) -> frozenset[str]:
        """The deferred-run allow-set the runner publishes via set_deferred_allowed.

        The agent's full allowed set plus the meta-tools. tool_describe/tool_call
        check membership in this set (the _deferred_allowed ContextVar), so a
        direct CORE call or a tool_call to any allowed tool passes, while a
        tool_call to a denied tool is refused before registry.execute.
        """
        return frozenset(self._get_filtered_names(config)) | TOOLSEARCH_TOOLS

    def search_tools(self, names: Any, query: str, limit: int = 10) -> list[SearchHit]:
        """Rank a set of tool names against a free-text query.

        Returns ``[{"name", "description"}]`` for the top matches. Used by the
        tool_search meta-tool, which passes the agent's allow-set (the runner's
        _deferred_allowed set) so search is scoped to what the agent may run.

        The description comes back WHOLE unless it is very long. It used to be
        cut at 200 characters, which is 48 characters short of the end of
        ``gws_gmail_reply``'s — so the sentence the cut removed was "Use this
        instead of gws_gmail_send for all replies", the one fact that decides
        between the two tools the search returns together.
        """
        terms = _query_terms(query)
        corpus: list[_Candidate] = []
        for n in names:
            fn = self._schemas.get(n, {}).get("function", {})
            corpus.append(
                _Candidate(
                    name=n,
                    description=str(fn.get("description", "")),
                    keywords=tuple(str(k) for k in fn.get("keywords", ())),
                    bias=float(fn.get("rank_bias", 0.0) or 0.0),
                )
            )
        weights = _term_weights(terms, corpus) if terms else {}
        scored: list[tuple[float, int, str, str]] = []
        for candidate in corpus:
            score, covered = (
                _score_tool(
                    candidate.name,
                    candidate.description,
                    terms,
                    weights,
                    candidate.keywords,
                    candidate.bias,
                )
                if terms
                else (0.0, 0)
            )
            if score > 0 or not terms:
                scored.append(
                    (
                        score,
                        covered,
                        candidate.name,
                        self._search_description(candidate.name, candidate.description),
                    )
                )
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        return [SearchHit(name=n, description=d) for _s, _c, n, d in scored[: max(1, limit)]]

    #: Longer than this and a search result stops being scannable. Meant to sit
    #: above the longest DISAMBIGUATING description — one carrying a
    #: ``when_to_use`` — so search never cuts the text that decides between two
    #: tools. It did not: ``gws_calendar_create`` stood at 411 against this 400,
    #: making it the one tool search truncated and the only reason the fallback
    #: below was ever reached in production. That description was trimmed, and
    #: ``test_every_deciding_description_is_shown_whole`` now enforces the claim
    #: this comment makes instead of restating it.
    _SEARCH_DESC_MAX = 400

    def _search_description(self, name: str, description: str) -> str:
        """What one search hit shows: the whole description, or as much of it as
        fits after the sentence that decides between this tool and its sibling.

        Returning ``when_to_use`` ALONE lost everything after the first
        sentence — for ``gws_calendar_create`` at 596 characters that was the
        whole of "whose calendar", the single most important new fact in this
        change, absent from the one surface a deferred agent reads first. The
        sentence still comes first, because it is the one that decides; what
        follows is as much of the rest as the budget allows.
        """
        if len(description) <= self._SEARCH_DESC_MAX:
            return description
        when = str(self._schemas.get(name, {}).get("function", {}).get("when_to_use", ""))
        if not when:
            return description[: self._SEARCH_DESC_MAX] + "…"
        rest = description[len(when) :].strip() if description.startswith(when) else description
        room = self._SEARCH_DESC_MAX - len(when) - 1
        if room <= 0 or not rest:
            return when
        return f"{when} {rest[:room]}…" if len(rest) > room else f"{when} {rest}"

    @staticmethod
    def absent_capability_note(query: str) -> str:
        """A sentence naming a capability this platform does not have, or ``""``.

        A search for Drive, Docs, Sheets, Google Tasks or Contacts has no right
        answer here, and the ranker will always produce SOMETHING — the agent
        then calls it. Saying "there is no such tool" is the answer.
        """
        from robothor.engine.tools.keywords import absent_capability_note

        return absent_capability_note(query)

    def get_schema(self, name: str, *, autonomy: bool = False) -> dict[str, Any] | None:
        """Return the full OpenAI-function schema for one tool, or None.

        ``autonomy`` widens ``browser`` exactly as :meth:`build_for_agent`
        does, and for the same reason: this is the OTHER way a schema reaches
        a model. ``tool_describe`` serves what this returns, so on a deferred
        run a granted agent told to call ``browser(action='autonomy', ...)``
        read the 436-character base, found ``autonomy`` in the action enum and
        nothing at all about what ``request`` should contain. Off by default,
        so an agent with no grant still pays nothing on this path either.
        """
        self._refresh_plugin_schemas_if_stale()
        schema = self._schemas.get(name)
        if schema is None:
            return None
        return with_autonomy(schema, autonomy)

    def _readonly_names(self) -> set[str]:
        """Core's table plus whatever installed plugins declared about their own tools.

        A plugin ships handlers and schemas; before this, safety
        classification stayed hardcoded here, so extracting an integration to
        a plugin left a fact about that instance behind in core. A plugin that
        declares nothing contributes nothing — absent means WRITE, which is
        the safe default.
        """
        from robothor.engine.tools.constants import READONLY_TOOLS

        names = set(READONLY_TOOLS)
        try:
            from robothor.plugins import load_plugins

            names |= load_plugins(reserved_names=set()).read_only
        except Exception as e:  # noqa: BLE001 - a plugin must not break plan mode
            logger.warning("Plugin read-only declarations skipped: %s", e)
        return names

    def build_readonly_for_agent(self, config: AgentConfig) -> list[dict[str, Any]]:
        """Return only read-only tool schemas for plan mode."""
        self._refresh_plugin_schemas_if_stale()
        full_names = set(self.get_tool_names(config))
        readonly_names = sorted(full_names & self._readonly_names())
        return [wire_schema(self._schemas[n]) for n in readonly_names if n in self._schemas]

    def get_readonly_tool_names(self, config: AgentConfig) -> list[str]:
        """Return read-only tool names for plan mode."""
        full_names = set(self.get_tool_names(config))
        return sorted(full_names & self._readonly_names())

    def get_tool_names(self, config: AgentConfig) -> list[str]:
        """Return filtered tool names for an agent."""
        return self._get_filtered_names(config)

    def _get_filtered_names(self, config: AgentConfig) -> list[str]:
        if config.tools_allowed:
            names = [n for n in config.tools_allowed if n in self._schemas]
            unresolved = [n for n in config.tools_allowed if n not in self._schemas]
            if unresolved and config.id not in self._warned_unresolved:
                self._warned_unresolved.add(config.id)
                logger.warning(
                    "Agent %s declares %d tool(s) with no registered schema/adapter "
                    "route; they are silently unavailable: %s",
                    config.id,
                    len(unresolved),
                    ", ".join(sorted(unresolved)),
                )
                self._escalate_unresolved(config.id, sorted(unresolved))
            names.extend(n for n in GOAL_TOOLS if n in self._schemas and n not in names)
        else:
            # "Everything" excludes the opt-in families. A sales subsystem the
            # instance never deployed, and a headless browser, are not what an
            # agent that declared no tool list was asking for. See OPT_IN_TOOLS.
            names = [n for n in self._schemas if n not in OPT_IN_TOOLS]

        if config.id == "main":
            from robothor.goals.tools import TOOL_NAMES

            names.extend(n for n in sorted(TOOL_NAMES) if n in self._schemas and n not in names)

        if config.id == "main":
            from robothor.goals.tools import TOOL_NAMES

            names.extend(n for n in sorted(TOOL_NAMES) if n in self._schemas and n not in names)

        if config.tools_denied:
            # Support glob patterns (e.g. "mcp_*", "gws_*") in tools_denied
            has_globs = any(c in p for p in config.tools_denied for c in "*?[")
            if has_globs:
                from fnmatch import fnmatch

                names = [n for n in names if not any(fnmatch(n, p) for p in config.tools_denied)]
            else:
                denied = set(config.tools_denied)
                names = [n for n in names if n not in denied]

        # Exclude spawn tools unless agent has can_spawn_agents enabled
        if not config.can_spawn_agents:
            names = [n for n in names if n not in SPAWN_TOOLS]

        # Exclude todo list tools unless agent has todo_list_enabled
        if not config.todo_list_enabled:
            names = [n for n in names if n not in TODO_TOOLS]

        # Meta-tools (tool_search/tool_describe/tool_call) are never part of the
        # normal advertised set; build_for_agent injects them only when deferring.
        if TOOLSEARCH_TOOLS:
            names = [n for n in names if n not in TOOLSEARCH_TOOLS]

        return names

    @staticmethod
    def _escalate_unresolved(agent_id: str, unresolved: list[str]) -> None:
        """Escalate declared-but-unresolvable manifest tools to the operator.

        This is operator-actionable instance drift — the manifest names a tool
        the platform does not register — and the journald warning alone has
        proven invisible (months of unactioned warnings). Fire-and-forget at
        warning level, deduped per agent per process by the caller's warn-once
        set. Degrades to log-only when no event loop is running (CLI, template
        validation, tests).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        from robothor.engine import alerts
        from robothor.engine.task_registry import get_task_registry

        get_task_registry().spawn(
            alerts.alert(
                "warning",
                f"Agent '{agent_id}' declares unavailable tools",
                "Declared in the manifest but not registered — silently "
                "unavailable to the agent: " + ", ".join(unresolved) + ". "
                "Fix the manifest, or register the missing tool schema.",
            ),
            name=f"tool-drift-alert:{agent_id}",
        )

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str = "",
        run_id: str = "",
        tenant_id: str = "",
        workspace: str = "",
        user_id: str = "",
        user_role: str = "",
        accessible_tenant_ids: tuple[str, ...] = (),
        timeout: int = 120,
        task_author_override: str = "",
        is_benchmark: bool = False,
        identity: IdentityContext | None = None,
    ) -> dict[str, Any]:
        """Execute a tool and return the result dict.

        Args:
            timeout: Per-tool timeout in seconds. 0 = unlimited.
            accessible_tenant_ids: Tenant IDs this run may access
                (resolved from user role + tenant hierarchy).
            is_benchmark: When True, side-effect tool wrappers refuse
                mutations (see ToolContext.is_benchmark).
            identity: The run's resolved IdentityContext (Task 2), or None
                for system/cron/heartbeat runs. Threaded onto ToolContext for
                data-scoping (Task 5); every existing caller omitting this
                gets the unaffected default.
        """
        # Match supplied argument names to this tool's declared parameters,
        # ignoring case and underscores. 82 tools are snake_case and 26 are
        # camelCase, so a model that has just called get_task({"id": ...})
        # sends `to_agent` to a tool declaring `toAgent` — and gets a generic
        # failure with nothing naming the wrong key. Exact matches win;
        # ambiguity is left alone. See normalise_arguments.
        from robothor.engine.tools.dispatch import normalise_arguments

        # getattr, not self._schemas: this method is exercised with stub
        # registries that never build a schema map, and argument normalisation
        # must never be the reason a tool call fails.
        _schemas = getattr(self, "_schemas", None) or {}
        _props = (
            ((_schemas.get(tool_name) or {}).get("function") or {})
            .get("parameters", {})
            .get("properties")
        )
        arguments = normalise_arguments(arguments, _props)

        try:
            if timeout > 0:
                # tool_deadline names this deadline for the frames below it.
                # A sub-agent runs inline under it, and on cancellation the
                # spawn path has to say whether the deadline fired (timeout)
                # or something else cancelled the parent (cancelled).
                with tool_deadline(timeout):
                    async with asyncio.timeout(timeout):
                        return await _execute_tool(
                            tool_name,
                            arguments,
                            agent_id=agent_id,
                            run_id=run_id,
                            tenant_id=tenant_id,
                            workspace=workspace,
                            user_id=user_id,
                            user_role=user_role,
                            accessible_tenant_ids=accessible_tenant_ids,
                            task_author_override=task_author_override,
                            is_benchmark=is_benchmark,
                            identity=identity,
                        )
            else:
                return await _execute_tool(
                    tool_name,
                    arguments,
                    agent_id=agent_id,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    workspace=workspace,
                    user_id=user_id,
                    user_role=user_role,
                    accessible_tenant_ids=accessible_tenant_ids,
                    task_author_override=task_author_override,
                    is_benchmark=is_benchmark,
                    identity=identity,
                )
        except (WorkflowDeadlineError, RuntimeDeadlineError):
            # NOT this tool's timeout. `spawn_agent` runs a child
            # `runner.execute` INLINE in the parent's task, so it inherits the
            # workflow's deadline scope and its chain walk can raise here.
            # `WorkflowDeadlineError` subclasses `TimeoutError`, so the handler
            # below used to turn it into "Tool 'spawn_agent' timed out after
            # 120s" — a fabricated cause at a duration that never elapsed, and
            # the same destruction of the deadline's identity that
            # `propagates_to_caller` exists to prevent one frame up. Only the
            # workflow engine can name the step, so it has to keep travelling.
            raise
        except TimeoutError:
            logger.warning("Tool %s timed out after %ds", tool_name, timeout)
            return {
                "error": f"Tool '{tool_name}' timed out after {timeout}s. "
                + timeout_guidance(tool_name)
            }
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return {"error": f"Tool execution failed: {e}"}


#: What to tell a model whose tool ran out of time, when the tool itself has a
#: documented escalation. Named here, and lazily, because this layer is the one
#: that SHADOWS the handler's own message: `asyncio.timeout` above fires before
#: the handler's own `TimeoutExpired` branch can speak, so whatever this branch
#: says is what the model actually reads.
#:
#: MEASURED 2026-09-16: an image task timed out three times on `exec` and was
#: told each time to "try a different approach or skip this step", with 780s of
#: budget left and a 900s ceiling available on a parameter it was never told
#: about. It did exactly as instructed, guessed, and scored 0.28 against a
#: competitor's 0.99 on a task whose structure it had already got perfect.
_TIMEOUT_GUIDANCE: dict[str, Callable[[], str]] = {}


def _exec_timeout_guidance() -> str:
    from robothor.engine.tools.handlers.filesystem import MAX_EXEC_TIMEOUT

    return (
        f"Ask for more time with the `timeout` parameter (up to {MAX_EXEC_TIMEOUT}s), "
        "or narrow the command so it finishes sooner."
    )


_TIMEOUT_GUIDANCE["exec"] = _exec_timeout_guidance


def timeout_guidance(tool_name: str) -> str:
    """What this tool allows when it runs out of time. Never "skip".

    A timeout is a statement about this call, not about the approach. The
    generic text therefore names the two things every tool permits — a smaller
    scope, or a longer limit where the tool takes one — and leaves the decision
    to abandon the method to the model, on evidence rather than on instruction.
    """
    builder = _TIMEOUT_GUIDANCE.get(tool_name)
    if builder is not None:
        try:
            return builder()
        except Exception:  # noqa: BLE001 — a guidance string must never raise
            logger.debug("timeout guidance for %s failed", tool_name, exc_info=True)
    return (
        "Retry with a narrower scope, or with a longer timeout if this tool takes one. "
        "A timeout is a statement about this call, not about the approach."
    )


# Singleton
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Get or create the singleton tool registry."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

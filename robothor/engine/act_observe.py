"""Acting invalidates what you observed before you acted.

MEASURED 2026-09-16, the second Social task we lost. The task's whole design is
that messaging an internal contact makes the mock service append a follow-up to
the inbox *and* hand it back inline in that very response. All three follow-ups
fired. Our agent made nineteen state-changing calls inside one shell script that
printed ``result.get("status")`` and nothing else, so its entire view of twelve
of them was ``[1/12] To: … → sent``, twelve times — and it never read that inbox
again before writing its report. The grader scored ``output_quality`` **1.0**
and ``severity_accuracy`` **0.0** in the same pass: a perfect report about the
wrong world.

Nothing in the engine said either of the two things that would have caught it:

* a state-changing call's RESPONSE is evidence, not a receipt; and
* once you have changed a source, what you knew about it is out of date.

This module is the classification both rules need, kept free of any session or
run so it can be tested against a table of names and command strings:

* :func:`classify` — is this call a read of some source, a change to one, or
  neither. Tool names come from the platform's own read-only classification
  (the table the parallel planner already trusts); ``exec`` and
  ``execute_code`` are shell, and get a deliberately narrow verb heuristic.
* :func:`source_tokens` — which sources a call touched, so "you have not read
  THAT inbox" is answerable rather than "you have not read anything".
* :func:`unread_proxy_responses` — how many ``genus_tools`` calls a snippet
  made whose response it never printed. This is the task-5 failure expressed
  as a number the model can see.

Conservative in one direction on purpose: an unrecognised call is ``neither``,
never ``change``. A false "you changed something" teaches an agent to ignore
the note, and a note that is ignored is worse than no note.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CHANGE",
    "CHANGE_TOOLS",
    "NEITHER",
    "READ",
    "WORKSPACE_TOOLS",
    "act_observe_note",
    "classify",
    "remote_tokens",
    "response_evidence",
    "source_tokens",
    "unread_proxy_responses",
]

READ = "read"
CHANGE = "change"
NEITHER = "neither"

#: The two tools whose argument is a program rather than a request. Everything
#: else is classified by name.
_SHELL_TOOLS = frozenset({"exec", "execute_code"})

#: An HTTP write VERB, spelled out. This is unambiguous on its own: nobody
#: writes `-X POST` or `requests.post(` about a local file, so a command
#: carrying one is a remote change whether or not the URL is visible in the
#: command text. That last clause is the point — the target is very often a
#: variable (`requests.post(url, json=m)`, `curl -X POST "$ENDPOINT"`), and
#: requiring a literal host in the string would make the control blind to
#: exactly the loop the measured run wrote.
_MUTATING_VERB = re.compile(
    r"(?:-X|--request)\s+[\"']?(?:POST|PUT|PATCH|DELETE)\b"
    r"|\brequests\.(?:post|put|patch|delete)\s*\("
    r"|\bsession\.(?:post|put|patch|delete)\s*\("
    r"|\bhttpx\.(?:post|put|patch|delete)\s*\("
    r"|\bmethod\s*=\s*[\"'](?:POST|PUT|PATCH|DELETE)[\"']",
    re.IGNORECASE,
)

#: And the weak signal: curl with a body is a POST whether or not `-X` says so,
#: which is how the measured run sent nineteen messages without the string
#: "POST" appearing near some of them. Weak because `--data` is a flag other
#: programs also take, so this one needs a source in the command before it
#: counts.
_MUTATING_BODY_FLAG = re.compile(r"--data(?:-raw|-binary|-urlencode)?\b", re.IGNORECASE)

#: A shell command that READS something at the other end. A command with no
#: source token at all reads nothing this module has an opinion about.
_FETCHING_SHELL = re.compile(
    r"\bcurl\b|\bwget\b|\bhttpx\b|\brequests\.get\s*\(|\burlopen\s*\(|\bfetch\s*\(",
    re.IGNORECASE,
)

#: What a "source" looks like in a command: a URL, an endpoint, a path. The
#: character class is a single bounded repetition, so this stays linear on
#: hostile input (the rule `deliverable_extract` records five findings for).
_TOKEN = re.compile(r"[A-Za-z0-9._~:/?#@!$&'*+,;=%-]{4,200}")

#: What makes a token a source somewhere OTHER than this workspace: a scheme,
#: or a dotted host with a path. A filesystem path is deliberately excluded —
#: see :func:`remote_tokens`.
_REMOTE = re.compile(
    r"^[a-z][a-z0-9+.\-]{1,15}://"
    r"|^(?:[a-z0-9-]{1,63}\.){1,4}[a-z]{2,24}(?::\d{1,5})?/"
    # A bare `host:port/path` with no dot and no scheme. This alternative is
    # not decoration: the mock services every graded task talks to are reached
    # as `localhost:9110/...`, and without it the one workload this control was
    # built for would classify as touching nothing.
    r"|^[a-z0-9-]{1,63}:\d{1,5}/",
    re.IGNORECASE,
)

#: Tools whose only target is this run's own workspace or its own bookkeeping.
#: They are absent from the read-only table because they WRITE, and they are
#: here because what they write is the run's own output — changing it
#: invalidates no earlier observation. Closed and short on purpose; anything
#: not listed falls through to the target test below, which answers NEITHER
#: for a call that names nothing remote.
WORKSPACE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "search_files",
        "todo_write",
        "team_scratchpad_read",
    }
)

#: Name shapes that reach something outside this run whether or not the
#: arguments name a host: a person, another agent, a CRM row, a repository, a
#: real screen. Read as a family rather than a list of tools, so a new
#: `send_*` or `*_reply` is classified the day it is registered — the drift
#: `hardcoded-names-drift` records is a hand-maintained list of NAMES, and this
#: is a rule about shape.
_CHANGE_PREFIXES: tuple[str, ...] = (
    "send_",
    "spawn_",
    "create_",
    "update_",
    "delete_",
    "merge_",
    "link_",
    "store_",
    "enroll_",
    "unenroll_",
    "approve_",
    "reject_",
    "resolve_",
    "ack_",
    "register_",
    "desktop_",
)
_CHANGE_SUFFIXES: tuple[str, ...] = (
    "_send",
    "_write",
    "_reply",
    "_modify",
    "_push",
    "_commit",
)

#: And the ones whose name says nothing either way.
CHANGE_TOOLS: frozenset[str] = frozenset(
    {
        "browser",
        "make_call",
        "mcp_call_tool",
        "vault_set",
        "vault_delete",
        "federation_trigger",
    }
)

#: A response field short enough that seeing it in stdout proves nothing. The
#: measured snippet printed `sent`; the payload it threw away was a paragraph.
MIN_EVIDENCE_CHARS = 12

#: How many leaf values are kept per proxied response, and how much of each.
#: Bounded because this is held for the length of a turn and a snippet may make
#: two hundred calls.
MAX_EVIDENCE_VALUES = 5
MAX_EVIDENCE_CHARS = 200


def source_tokens(value: Any) -> frozenset[str]:
    """The sources a call names: URLs, endpoints and paths, as bare strings.

    A token qualifies by containing a ``/`` — which is what separates
    ``/slack/messages`` and ``http://host/x`` from ``--silent`` and ``json``.
    Returns an empty set rather than guessing when a call names no source.
    """
    if isinstance(value, dict):
        text = " ".join(str(v) for v in value.values() if isinstance(v, (str, int, float)))
    else:
        text = str(value or "")
    return frozenset(
        token.strip("\"'`,;")
        for token in _TOKEN.findall(text)
        if "/" in token and not token.startswith("//")
    )


def remote_tokens(value: Any) -> frozenset[str]:
    """The sources in *value* that live somewhere other than this workspace.

    A path is not a source this control has an opinion about.
    ``/tmp_workspace/results/results.md`` is where the run is asked to put its
    ANSWER — telling an agent that it changed its own deliverable and should
    read it again before writing its answer is advice about nothing, and the
    first cut of this module gave exactly that advice on every WildClaw run,
    because every one of them writes to an absolute path (hostile review I1).

    Remote means a scheme (``https://``, ``postgres://``) or a dotted host with
    a path. Nothing else qualifies, and a call that names nothing remote is not
    a state change this control can say anything useful about.
    """
    return frozenset(token for token in source_tokens(value) if _REMOTE.search(token))


def classify(tool_name: str, tool_input: dict[str, Any], read_only: frozenset[str]) -> str:
    """``READ``, ``CHANGE`` or ``NEITHER`` for one admitted call.

    Args:
        tool_name: the registered name the call was admitted under.
        tool_input: its arguments.
        read_only: what this instance has CLASSIFIED read-only — the same
            answer ``parallel_tools`` is given, so the two controls can never
            disagree about whether a tool writes.

    By tool KIND and TARGET, in that order, because neither alone is enough.
    The platform's read-only table answers "may this run beside another call",
    which is a different question: ``write_file``, ``todo_write`` and
    ``tool_search`` are all absent from it and none of them changes anything an
    earlier observation was about.

    ``NEITHER`` is the honest answer for a call that reaches nothing outside
    this run, and it is the direction to be wrong in: a false "you changed
    something" teaches an agent to ignore the note, and a note that is ignored
    is worse than no note.
    """
    if tool_name in _SHELL_TOOLS:
        program = " ".join(
            str(v) for v in (tool_input or {}).values() if isinstance(v, (str, int, float))
        )
        # The verb first, and without requiring a visible host: a command that
        # says `-X POST` or `requests.post(` is a remote write even when the
        # URL it targets is a variable, which is how such a loop is normally
        # written. Asking for a literal host here made `requests.post(u, ...)`
        # classify as `neither` — the control blind to the exact shape it
        # exists for.
        if _MUTATING_VERB.search(program):
            return CHANGE
        if not remote_tokens(program):
            return NEITHER
        if _MUTATING_BODY_FLAG.search(program):
            return CHANGE
        return READ if _FETCHING_SHELL.search(program) else NEITHER
    if tool_name in read_only:
        return READ
    if not tool_name or tool_name in WORKSPACE_TOOLS:
        return NEITHER
    if tool_name.startswith(_CHANGE_PREFIXES) or tool_name.endswith(_CHANGE_SUFFIXES):
        return CHANGE
    if tool_name in CHANGE_TOOLS:
        return CHANGE
    return CHANGE if remote_tokens(tool_input) else NEITHER


def response_evidence(result: Any) -> tuple[str, ...]:
    """The strings that would PROVE a snippet looked at this response.

    Leaf string values long enough that their presence in stdout cannot be a
    coincidence. A response made entirely of short values yields nothing, and
    a call whose response yields nothing is never counted as unread — the
    absence of evidence must not become evidence of absence.
    """
    found: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if len(found) >= MAX_EVIDENCE_VALUES or depth > 6:
            return
        if isinstance(node, str):
            if len(node) >= MIN_EVIDENCE_CHARS:
                found.append(node[:MAX_EVIDENCE_CHARS])
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, depth + 1)

    walk(result, 0)
    return tuple(found)


def unread_proxy_responses(
    responses: list[tuple[str, tuple[str, ...]]], stdout: str
) -> dict[str, Any]:
    """How many of a snippet's proxied calls returned something it never showed.

    ``{}`` when every response left a trace in stdout, or when none carried
    anything substantial enough to look for. Otherwise a count and the tool
    names, so the model reads "3 of your 12 calls returned a body you did not
    print" rather than having to infer it.
    """
    unread: list[str] = []
    for name, evidence in responses:
        if not evidence:
            continue
        if any(value in stdout for value in evidence):
            continue
        unread.append(name)
    if not unread:
        return {}
    names = sorted(set(unread))
    return {
        "unread_responses": len(unread),
        "unread_response_tools": names,
        "unread_response_note": (
            f"{len(unread)} of the {len(responses)} tool calls this snippet made returned a "
            "response body the snippet never printed. A call through `genus_tools` keeps its "
            "full response on this run's step ledger whatever you print; a call through "
            "`urllib` or `curl` keeps only what you print. If a reply, a new record or a "
            f"follow-up could have come back from {', '.join(names)}, print it and look."
        ),
    }


def act_observe_note(changes: list[tuple[int, str, frozenset[str]]]) -> str:
    """ "You changed this and have not looked since" — or "" when nothing did.

    One sentence per source, not per call: twelve sends to one inbox is one
    thing the agent has not re-read, and a note that lists twelve of anything
    is a note that gets skimmed.
    """
    if not changes:
        return ""
    tools = sorted({tool for _step, tool, _sources in changes})
    sources = sorted({source for _s, _t, srcs in changes for source in srcs})[:3]
    where = f" against {', '.join(sources)}" if sources else ""
    plural = "call" if len(changes) == 1 else "calls"
    return (
        f"[SYSTEM] You have made {len(changes)} state-changing {plural} "
        f"({', '.join(tools)}){where} and have not read that source since. A response you "
        "did not inspect may have carried new information, and anything you knew about "
        "that source before you changed it is now out of date. Read it again before you "
        "write your answer."
    )

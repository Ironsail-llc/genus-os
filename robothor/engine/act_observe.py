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
* :func:`raw_http_responses` — the same pairs for the HTTP a snippet made on
  its OWN, through ``urllib`` or ``requests``, as the sandbox recorder saw it
  (`sandbox_runtime/http_recorder.py`), or through a spawned ``curl``, as the
  spawn recorder saw it (`sandbox_runtime/spawn_recorder.py`, merged in with
  ``via``). Measured 2026-09-17: the rerun of the same task sent everything
  with ``urllib`` and printed ``OK`` per call, and the proxied count was an
  honest, useless zero. Measured 2026-09-18: the next rerun sent everything
  with ``subprocess.run(["curl", …])`` and both counts were zero.
* :func:`lost_responses` — the bodies themselves, for a snippet that ended
  non-zero or timed out AFTER its writes took effect and never printed what
  came back. The 2026-09-18 run consumed three one-shot replies and crashed
  on a ``TypeError`` one line later; nothing outside the dead process had a
  copy, and the agent re-sent. A crash is not a rollback, and a re-send is a
  duplicate.

Conservative in one direction on purpose: an unrecognised call is ``neither``,
never ``change``. A false "you changed something" teaches an agent to ignore
the note, and a note that is ignored is worse than no note.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "CHANGE",
    "CHANGE_TOOLS",
    "NEITHER",
    "READ",
    "WORKSPACE_TOOLS",
    "act_observe_note",
    "SAFE_METHODS",
    "accepted_write",
    "classify",
    "http_origin",
    "lost_responses",
    "raw_http_responses",
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
#: exactly the loop the measured run wrote. The argv-LIST spelling —
#: `"-X", "POST"` inside a `subprocess.run([...])` — is the same verb with a
#: quote and a comma between the flag and its value; measured 2026-09-18,
#: eighteen writes in that spelling classified as nothing at all.
_MUTATING_VERB = re.compile(
    r"(?:-X|--request)(?:\s+|[\"']\s*,\s*)[\"']?(?:POST|PUT|PATCH|DELETE)\b"
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
_MUTATING_BODY_FLAG = re.compile(
    r"--data(?:-raw|-binary|-urlencode|-ascii)?\b|--json\b|--form(?:-string)?\b"
    r"|--post-(?:data|file)\b|--body-(?:data|file)\b",
    re.IGNORECASE,
)

#: The SHORT body flags, which count only beside `curl` or `wget` in the same
#: command text: a `-d` on its own is `date -d yesterday`, and a `-F` is
#: `awk -F,`. The measured 2026-09-18 snippet used `"-d", json.dumps(body)`
#: and nothing longer.
_SHORT_BODY_FLAG = re.compile(r"(?<![\w-])-(?:d|F)(?=[\s\"',=]|$)")
_HTTP_CLI_WORD = re.compile(r"\bcurl\b|\bwget\b", re.IGNORECASE)

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

#: HTTP methods that change nothing at the other end (RFC 9110 §9.2.1). Every
#: other method the recorder saw is a state change whose response is evidence.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


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
        if _HTTP_CLI_WORD.search(program) and _SHORT_BODY_FLAG.search(program):
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
            f"{len(unread)} of the {len(responses)} calls this snippet made returned a "
            "response body the snippet never printed. A call through `genus_tools` keeps its "
            "full response on this run's step ledger whatever you print; a call through "
            "`urllib`, `requests` or `curl` keeps only what you print. If a reply, a new "
            f"record or a follow-up could have come back from {', '.join(names)}, print it "
            "and look."
        ),
    }


#: What a host may look like once it is going to be quoted in a note: a DNS
#: name or IPv4 literal, or a bracketed IPv6 literal. The URL came from a
#: process the snippet controlled; a netloc is not allowed to carry a
#: `[SYSTEM]`, a backtick or a space into the model's context. Underscores
#: are allowed: `mock_slack:9110` is what a docker-compose network calls a
#: service, and refusing it would make every such write sourceless.
_HOSTNAME = re.compile(
    r"^(?:[a-z0-9_](?:[a-z0-9_-]{0,62}\.?)+|\[[0-9a-f:.]{2,45}\])$", re.IGNORECASE
)


def http_origin(url: str) -> str:
    """``scheme://host[:port]`` — the SOURCE a recorded call touched.

    The origin rather than the path, because that is what "read that source
    again" has to mean for a service: a POST to ``/slack/send`` is answered by
    a GET of ``/slack/messages`` on the same host, and never by another call
    to ``/slack/send``.

    Rebuilt from the parsed host and port, not copied from the netloc: no
    userinfo, lower-cased, and "" for anything that is not a hostname — a
    call against no origin is a call this control says nothing about.
    """
    try:
        parts = urlsplit(str(url or ""))
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if not _HOSTNAME.match(host):
        return ""
    return f"{parts.scheme.lower()}://{host}{f':{port}' if port else ''}"


#: A JSON string literal, for a body the recorder cut and `json.loads` can no
#: longer parse. Linear on hostile input: each character is either not a
#: quote/backslash or a backslash pair, so the two alternatives never overlap.
_JSON_LITERAL = re.compile(r'"((?:[^"\\]|\\.){' + str(MIN_EVIDENCE_CHARS) + r',})"')


def _cut_body_evidence(body: str) -> tuple[str, ...]:
    """What can still prove a CUT body was printed: the string literals that
    survived the cut, never the raw head. `print(resp)` shows a repr with
    single quotes and `json.dumps(resp, indent=2)` re-wraps, so the raw head
    is in neither — but the literals are in both. A cut body with no literal
    in it has nothing to look for and is never counted (D3)."""
    found = [m.group(1)[:MAX_EVIDENCE_CHARS] for m in _JSON_LITERAL.finditer(body)]
    return tuple(found[:MAX_EVIDENCE_VALUES])


def accepted_write(call: dict[str, Any]) -> bool:
    """A state-changing method that the service ACCEPTED. A 4xx or 5xx is a
    refusal — the measured snippet's 429s changed nothing and were retried —
    and counting one is the false positive that gets a note ignored.

    A call the spawn recorder saw (``via`` set) has no status line to read:
    its ``status`` is ``None`` unless the CLI's exit code proved a refusal,
    and the only acceptance signal is ``returncode == 0`` — withdrawn when
    the body it handed back was a JSON object with a top-level ``error``
    (``refused``, set at merge time). A 200 is never invented from an exit
    code, and neither is a change from a body that says it was refused.
    """
    method = str(call.get("method") or "").upper()
    if not method or method in SAFE_METHODS:
        return False
    if call.get("refused"):
        return False
    status = call.get("status")
    if status is None:
        return call.get("returncode") == 0
    return int(status or 0) < 400


def _clean_url(call: dict[str, Any]) -> str:
    """The URL as it may be quoted to the model: its whitespace and control
    characters gone. It came from a process the snippet controlled."""
    return re.sub(r"[\s\x00-\x1f\x7f]+", "", str(call.get("url") or ""))[:2048]


def _evidence_for(call: dict[str, Any]) -> tuple[str, ...]:
    """What would prove this recorded body was printed: the leaf strings of
    the body parsed as JSON — a snippet that printed the parsed dict shows
    Python's repr, in which they still appear — the surviving literals of a
    cut body, or the raw head of a body that is not JSON. A body the snippet
    never read, or one the recorder could not keep as text, yields nothing
    (D3: the rule is the same as for a proxied ``{"ok": true}``)."""
    body = str(call.get("body") or "")
    if call.get("truncated"):
        return _cut_body_evidence(body)
    try:
        # JSON: the leaf strings, and ONLY those — `{"ok": true}` is twelve
        # characters of nothing to look for, not a body worth flagging.
        return response_evidence(json.loads(body))
    except ValueError:
        return (body[:MAX_EVIDENCE_CHARS],) if len(body) >= MIN_EVIDENCE_CHARS else ()


def raw_http_responses(calls: list[dict[str, Any]]) -> list[tuple[str, tuple[str, ...]]]:
    """``(name, evidence)`` per recorded state-changing call, proxied-shaped.

    The name is ``METHOD url`` so the note can say which call; the evidence
    is :func:`_evidence_for`. The same gate the ledger applies: a call
    against nothing that parses as an origin is a call this control says
    nothing about.
    """
    out: list[tuple[str, tuple[str, ...]]] = []
    for call in calls:
        if not isinstance(call, dict) or not accepted_write(call):
            continue
        url = _clean_url(call)
        if not http_origin(url):
            continue
        out.append((f"{str(call.get('method')).upper()} {url}", _evidence_for(call)))
    return out


#: How many lost bodies a result carries, newest last, and how much of each.
#: The recorder's own body cap; a body cut there is cut here.
MAX_LOST_RESPONSES = 20
MAX_LOST_BODY_CHARS = 2000


def lost_responses(calls: list[dict[str, Any]], stdout: str) -> dict[str, Any]:
    """The response bodies a snippet that FAILED after its writes never printed.

    For the handler to call only when the snippet ended non-zero or timed
    out: on a clean exit :func:`unread_proxy_responses` is the message, and
    the two are never emitted for the same call. Each accepted non-safe call
    whose evidence (the same rule as the unread count) is not in stdout is
    attached with its recorded body, because that body no longer exists
    anywhere else — the process that read it is dead. A crash is not a
    rollback, and the note says what a re-send would be: a duplicate.
    """
    lost: list[dict[str, Any]] = []
    took_effect = 0
    for call in calls:
        if not isinstance(call, dict) or not accepted_write(call):
            continue
        url = _clean_url(call)
        if not http_origin(url):
            continue
        took_effect += 1
        evidence = _evidence_for(call)
        if not evidence or any(value in stdout for value in evidence):
            continue
        entry: dict[str, Any] = {
            "method": str(call.get("method")).upper(),
            "url": url,
            "status": call.get("status"),
        }
        if call.get("via"):
            entry["via"] = str(call["via"])[:32]
        entry["body"] = str(call.get("body") or "")[:MAX_LOST_BODY_CHARS]
        lost.append(entry)
    if not lost:
        return {}
    lost = lost[-MAX_LOST_RESPONSES:]
    counts: dict[str, int] = {}
    for entry in lost:
        name = f"{entry['method']} {entry['url']}"
        counts[name] = counts.get(name, 0) + 1
    listed = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in list(counts.items())[:6])
    if len(counts) > 6:
        listed += ", …"
    whose = "Their responses" if len(lost) == took_effect else f"{len(lost)} of their responses"
    return {
        "lost_responses": lost,
        "lost_responses_note": (
            f"This snippet failed AFTER {took_effect} write(s) took effect ({listed}). "
            f"{whose}, which the code consumed but never printed, are attached under "
            "lost_responses. Read them before re-sending: a re-send is a duplicate, and "
            "anything one-shot in those replies will not come back."
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

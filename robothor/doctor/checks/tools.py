"""Do an agent's instructions and its manifest agree about its tools?

Two ways they came apart on the first production instance, both of them
invisible until someone read a week of run steps:

**Instructions name tools the run does not grant.** Over two hundred refusals
in one week — "not available to this agent", "denied by per-task whitelist" —
for tools the agent's own instruction files told it to use. The
email-classifier's instructions mention ``exec`` three times; its manifest does
not grant it. The agent cannot see its manifest, so from inside the turn this
is a tool that simply stops working, and what it does next is find another way:
a CLI through a shell it may or may not have, or a plausible-looking tool name
it invents.

**The same manifest bans a tool and allows the CLI that does the same thing.**
The email-responder denied ``gws_gmail_send`` while its ``exec_allowlist``
admitted ``^gog gmail (thread|send|search)``. The denial is then decoration:
the agent sends the mail, through a path with no do-not-contact check, no
duplicate-reply guard, no threading and no benchmark gate.

Both are ``recommended``: the instance works, and an operator would want to
know. Neither result ever carries instruction TEXT — a manifest is prose
written for a model, and echoing it into a terminal, a log and a dashboard
would carry attacker-controlled text across three trust boundaries. Results
carry agent ids and tool names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, info, ok, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

#: How many agents a single result names before it summarises.
_MAX_LISTED = 8

#: A tool name as it appears in prose: backticked, called, or bare for the
#: families whose names are distinctive enough not to collide with English.
#: Checked against the REGISTERED set afterwards, so a false positive here
#: costs nothing — "read the file" cannot become `read_file`.
_MENTION_RE = re.compile(
    r"`(?P<ticked>[a-z][a-z0-9_]{2,})`"
    r"|(?P<called>[a-z][a-z0-9_]{2,})\s*\("
    r"|(?P<bare>gws_[a-z_]+)"
)

#: A fenced code block. Everything inside one is an EXAMPLE — a shell
#: transcript, another vendor's SDK, a JSON payload — not an instruction to
#: this agent, and reading `client.read_file('x')` out of a python fence as
#: "this agent needs read_file" is the fabrication this scanner promises not to
#: make.
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)

#: An HTML comment. Markdown renders none of it, so commented-out text is the
#: clearest possible signal that it is NOT a current instruction — and it was
#: the one form of "ignore this" the scanner read as an instruction.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: An unterminated one runs to the end of the file, like an unterminated fence.
_OPEN_HTML_COMMENT_RE = re.compile(r"<!--.*\Z", re.DOTALL)

#: An unterminated fence runs to the end of the file, which is what a truncated
#: instruction file looks like.
_OPEN_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~).*\Z", re.MULTILINE | re.DOTALL)

#: A URL or a filesystem path. `https://api.example.com/v1/gws_gmail_send` and
#: `/var/lib/gws_gmail_send/out.log` both contain a registered tool name and
#: neither is telling the agent to call it.
_URL_OR_PATH_RE = re.compile(r"\S*[/\\]\S*|\b[a-z0-9_.-]+\.[a-z]{2,}\b", re.IGNORECASE)

#: Adjectives that make a bare mention a prohibition — "`exec` is forbidden".
#: The trailing-negation rule covers "is not available"; this covers the case
#: with no "not" in it at all.
_NEGATIVE_PREDICATE_RE = re.compile(
    r"\b(?:is|are|was|were|stays?|remains?)\s+"
    r"(?:strictly\s+|absolutely\s+|completely\s+)?"
    r"(?:forbidden|prohibited|banned|disallowed|off[- ]limits|unavailable|denied)\b",
    re.IGNORECASE,
)

#: Where one clause ends and the next begins. A sentence is too coarse — "Use
#: `gws_gmail_reply`, not `gws_gmail_send`" would lose both — and a line is too
#: coarse for a bulleted instruction file.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;:\n]|(?<=[a-z0-9`\)])\s*,\s*")

#: Words that turn the rest of a clause into a prohibition.
_NEGATION_RE = re.compile(
    r"\b(?:never|not|no|none|non|without|avoid|avoids|forbidden|unavailable|"
    r"cannot|can't|don't|doesn't|won't|shouldn't|lacks|lack|denied|deny|"
    r"refuse|refuses|instead|rather)\b",
    re.IGNORECASE,
)

#: Predicates that negate a tool named EARLIER in the same clause —
#: "The `write_file` tool is NOT available."
_TRAILING_NEGATION_RE = re.compile(
    r"\b(?:is|are|was|were|will\s+be)\s+(?:not|no\s+longer)\b"
    r"|\bnot\s+(?:available|granted|allowed|permitted|enabled)\b",
    re.IGNORECASE,
)


def _manifest_dir(ctx: DoctorContext) -> Path:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().manifest_dir


def _workspace(ctx: DoctorContext) -> Path:
    from robothor.engine.config import EngineConfig

    return EngineConfig.from_env().workspace


def _adapter_names() -> set[str]:
    """Tool names the instance's MCP adapters serve, from their manifests.

    The registry learns these by CONNECTING to each adapter and listing its
    tools, which a doctor check must not do — it would spawn stdio binaries and
    dial remote servers to answer a config question. The bundle contract makes
    the offline answer authoritative anyway: ``tools_allowed`` in the adapter
    YAML is the ONLY set the registry will register, and anything else the
    server offers is refused as drift.

    An adapter declaring no ``tools_allowed`` is the legacy allow-all shape and
    contributes nothing here: its names are whatever its server says today, and
    inventing a vocabulary for it would be guessing.
    """
    try:
        from pathlib import Path as _Path

        from robothor.engine.adapters import load_adapters
        from robothor.settings import get_settings

        # Through settings rather than the module-level ``ADAPTER_DIR``, which
        # is resolved once at import: a doctor run after an operator points
        # ROBOTHOR_ADAPTER_DIR somewhere new would otherwise scan the old one.
        configured = str(get_settings().paths.adapter_dir or "").strip()
        directory = _Path(configured).expanduser() if configured else None
        return {str(name) for adapter in load_adapters(directory) for name in adapter.tools_allowed}
    except Exception:  # noqa: BLE001 - an unreadable adapter dir is not a doctor failure
        return set()


def _registered_names() -> set[str]:
    """Every tool name this instance can actually resolve.

    Built-ins AND adapter routes. Judging ``tools_allowed`` against the
    built-ins alone reported every MCP-adapter tool an instance grants as
    "resolves to nothing" — a permanent red on any instance with an adapter,
    which is how an operator learns to ignore a check.
    """
    from robothor.engine.tools.registry import builtin_schema_names

    return builtin_schema_names() | _adapter_names()


def mentioned_tools(text: str, registered: set[str]) -> set[str]:
    """Registered tool names an instruction file tells the agent to USE.

    A prohibition is not a requirement. ``Never use `exec`.`` and ``The
    `write_file` tool is NOT available.`` are instructions that the agent must
    NOT call those tools, and reading them as "this agent needs exec" is how
    this check came to fire on its own repository's rewritten template — which
    says, in a line this branch added, ``Do NOT reach for a shell: this agent
    has no `exec`.``

    So each mention is judged in its own clause: negated if a negation word
    precedes it there, or if the clause carries a trailing predicate that
    negates it ("is not available"). Clauses, not sentences, so that "Use
    `gws_gmail_reply`, not `gws_gmail_send`" keeps the first and drops the
    second.

    Fenced code blocks, HTML comments, URLs and filesystem paths are removed
    before scanning.
    Everything inside a fence is an EXAMPLE — a shell transcript, another
    vendor's SDK, a JSON payload — and
    ``https://api.example.com/v1/gws_gmail_send`` and
    ``/var/lib/gws_gmail_send/out.log`` both contain a registered tool name
    while neither tells the agent to call anything.

    This deliberately under-reports rather than over-reports. A missed mention
    costs an operator nothing; a fabricated one costs them their trust in the
    check, and a check nobody trusts is the one they learn to skip.
    """
    text = _FENCE_RE.sub(" ", text)
    text = _OPEN_FENCE_RE.sub(" ", text)
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _OPEN_HTML_COMMENT_RE.sub(" ", text)
    text = _URL_OR_PATH_RE.sub(" ", text)

    found: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(text):
        if not clause or not clause.strip():
            continue
        negated_after = (
            _TRAILING_NEGATION_RE.search(clause) is not None
            or _NEGATIVE_PREDICATE_RE.search(clause) is not None
        )
        for match in _MENTION_RE.finditer(clause):
            name = match.group("ticked") or match.group("called") or match.group("bare")
            if name not in registered:
                continue
            if negated_after:
                continue
            if _NEGATION_RE.search(clause[: match.start()]):
                continue
            found.add(name)
    return found


def _read_instruction_text(workspace: Path, manifest: dict[str, Any]) -> str:
    """Every instruction and bootstrap file this manifest loads, concatenated.

    Heartbeat and worker blocks included: a heartbeat run loads its own files
    and has its own ``heartbeat_tools_allowed``, so a tool named in a heartbeat
    instruction and granted only on the base run is the same defect one level
    down.
    """
    paths: list[str] = []
    for block in (manifest, manifest.get("heartbeat") or {}, manifest.get("worker") or {}):
        if not isinstance(block, dict):
            continue
        if block.get("instruction_file"):
            paths.append(str(block["instruction_file"]))
        paths.extend(str(p) for p in (block.get("bootstrap_files") or []))

    chunks: list[str] = []
    for relative in paths:
        try:
            candidate = (workspace / relative).resolve()
            # A manifest is instance data and its paths are not trusted input:
            # a file outside the workspace is not this agent's instructions.
            if not candidate.is_relative_to(workspace.resolve()):
                continue
            if candidate.is_file():
                chunks.append(candidate.read_text(errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _granted(manifest: dict[str, Any], registered: set[str]) -> set[str]:
    """Every tool this manifest actually grants at runtime.

    Modelled on ``ToolRegistry._get_filtered_names``, because a doctor whose
    verdict differs from the loader's is worse than no doctor:

    * an absent or empty ``tools_allowed`` means EVERY registered tool — that
      is the default manifest shape, and reading it as "grants nothing" made
      this check fail on a freshly initialised instance;
    * ``GOAL_TOOLS`` are appended unconditionally;
    * ``SPAWN_TOOLS`` are stripped without ``can_spawn_agents``, ``TODO_TOOLS``
      without ``todo_list_enabled``, and the meta-tools always — so a manifest
      listing ``spawn_agent`` without the flag does NOT have it, which the
      check used to get backwards.
    """
    from robothor.engine.tools.constants import (
        GOAL_TOOLS,
        SPAWN_TOOLS,
        TODO_TOOLS,
        TOOLSEARCH_TOOLS,
    )

    # The registry strips these regardless of what `tools_allowed` says
    # (`ToolRegistry._get_filtered_names`). Not modelling them meant a manifest
    # granting `spawn_agent` without `can_spawn_agents` was judged to HAVE it,
    # so the check's own headline defect — instructions naming a tool the run
    # does not grant — was invisible for the three families the registry
    # filters. `tool_search`/`tool_describe`/`tool_call` are never in the
    # normal advertised set at all.
    # Both flags live under `v2:`, which is where config.py reads them
    # (`v2.get("can_spawn_agents", False)`); reading the top level found
    # neither and stripped the tools from every agent that has them.
    v2 = manifest.get("v2") or {}
    if not isinstance(v2, dict):
        v2 = {}
    stripped: set[str] = set(TOOLSEARCH_TOOLS)
    if not v2.get("can_spawn_agents"):
        stripped |= set(SPAWN_TOOLS)
    if not v2.get("todo_list_enabled"):
        stripped |= set(TODO_TOOLS)

    allowed = manifest.get("tools_allowed")
    if not allowed:
        # Every registered tool, minus the same filters.
        return _with_meta_tools(registered - stripped)

    granted = {str(n) for n in allowed}
    for block_key, list_key in (
        ("heartbeat", "heartbeat_tools_allowed"),
        ("worker", "worker_tools_allowed"),
    ):
        block = manifest.get(block_key) or {}
        if isinstance(block, dict) and block.get(list_key):
            granted |= {str(n) for n in block[list_key]}
    # GOAL_TOOLS are appended unconditionally by the registry filter.
    granted |= set(GOAL_TOOLS) & registered
    return _with_meta_tools(granted - stripped)


def _with_meta_tools(filtered: set[str]) -> set[str]:
    """*filtered*, plus the meta-tools when this agent's set would be deferred.

    ``_get_filtered_names`` strips ``tool_search``/``tool_describe``/
    ``tool_call`` and ``build_for_agent`` INJECTS them again for a deferred
    agent — they are never in a ``tools_allowed`` list and never can be. The
    check modelled the strip and not the injection, so every broad agent whose
    instructions explain how to find a deferred tool was reported as naming
    three tools its manifest omits. On the live instance that was the check's
    largest single source of noise, and the manifests were right.

    The deferral test is the registry's own (``should_defer``): the flag, and
    the advertised-tool count against the threshold. An agent below the
    threshold does NOT get them, and an instruction telling it to call one is
    the genuine finding this check exists for.
    """
    from robothor.engine.feature_flags import deferred_tools_enabled, deferred_tools_threshold
    from robothor.engine.tools.constants import TOOLSEARCH_TOOLS

    if deferred_tools_enabled() and len(filtered) > deferred_tools_threshold():
        return filtered | set(TOOLSEARCH_TOOLS)
    return filtered


def _denied(manifest: dict[str, Any], name: str) -> bool:
    """Does this manifest's ``tools_denied`` cover ``name``? Globs included."""
    for pattern in manifest.get("tools_denied") or []:
        text = str(pattern)
        if text == name or fnmatch(name, text):
            return True
    return False


def _scan_manifests(directory: Path) -> list[dict[str, Any]]:
    """Every agent manifest, MERGED with ``_defaults.yaml`` as the engine merges it.

    Reading the raw per-file document reports tools the agent has. A fleet
    whose ``_defaults.yaml`` grants ``exec`` and ``gws_gmail_send`` to everyone
    had every inheriting agent reported as missing both — and the mirror case,
    a default that DENIES a tool the agent's instructions name, went unreported.
    The sibling check merges for exactly this reason and says why: "a doctor
    whose verdict differs from the loader's is worse than no doctor".
    """
    import yaml

    from robothor.engine import config as engine_config

    try:
        defaults = engine_config._load_defaults(directory)
    except Exception:  # noqa: BLE001 - a broken defaults file is manifests.broken's
        defaults = {}

    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:  # noqa: BLE001 - manifests.broken owns unreadable files
            continue
        if not isinstance(data, dict) or "id" not in data:
            continue
        agent_id = str(data.get("id") or path.stem)
        try:
            merged = engine_config._merged_manifest(
                data, agent_id=agent_id, defaults=defaults, workspace=None, trigger_type=None
            )
        except Exception:  # noqa: BLE001 - judge what parses; the rest is manifests.schema's
            merged = data
        found.append(merged if isinstance(merged, dict) else data)
    return found


# ── agents.tools_named_but_not_granted ────────────────────────────────


def _scan_named_but_not_granted(directory: Path, workspace: Path) -> list[str]:
    registered = _registered_names()
    lines: list[str] = []
    for manifest in _scan_manifests(directory):
        agent_id = str(manifest.get("id") or "")
        granted = _granted(manifest, registered)
        text = _read_instruction_text(workspace, manifest)
        named = mentioned_tools(text, registered)

        # A DENIED tool the instructions still name is the loud case, not a
        # quiet one: the agent is told to do something its manifest forbids,
        # and it will find another way. It was suppressed here, which put the
        # code and the module docstring in disagreement.
        denied = sorted(n for n in named if _denied(manifest, n))
        ungranted = sorted(n for n in named if n not in granted and n not in denied)
        declared = {str(n) for n in (manifest.get("tools_allowed") or [])}
        unresolved = sorted(n for n in declared if n not in registered)

        parts: list[str] = []
        if denied:
            parts.append("instructions name but manifest DENIES: " + ", ".join(denied))
        if ungranted:
            parts.append("instructions name but manifest omits: " + ", ".join(ungranted))
        if unresolved:
            parts.append("tools_allowed resolves to nothing: " + ", ".join(unresolved))
        if parts:
            lines.append(f"{agent_id}: " + "; ".join(parts))
    return lines


async def _tools_named_but_not_granted(ctx: DoctorContext) -> Result:
    """An agent is told to use a tool its manifest does not give it.

    The agent cannot see its own manifest. From inside the turn the tool simply
    fails — "not available to this agent" — and the model's next move is to
    find another way to the same end: a CLI through ``exec``, or a tool name it
    invents. Either grant the tool or stop naming it. The second half of each
    line is the reverse case: a ``tools_allowed`` entry that matches no
    registered schema, which the registry drops after ONE journald warning.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    workspace = _workspace(ctx)
    if not workspace.is_dir():
        return skip(f"no workspace at {workspace}")
    try:
        lines = await ctx.run_blocking(_scan_named_but_not_granted, directory, workspace)
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifests: {type(exc).__name__}")
    if not lines:
        return ok("every tool an agent's instructions name is one its manifest grants")
    shown = "; ".join(lines[:_MAX_LISTED])
    if len(lines) > _MAX_LISTED:
        shown += f"; and {len(lines) - _MAX_LISTED} more"
    return fail(f"{len(lines)} agent(s) with a tools/instructions mismatch: {shown}")


# ── tools.exec_allowlist_bypasses_denied_tool ─────────────────────────

#: ``(probe command, the native tools that do the same thing)``. An
#: ``exec_allowlist`` regex is matched against the probe: if it admits the CLI
#: and the manifest denies the native tool, the denial does nothing.
#: Not exhaustive, and cannot be: a shell has infinitely many spellings of the
#: same command. The catch-all probe above is what covers the general case; this
#: table is for an allowlist that is specific enough to look careful and still
#: admits a Google CLI. `^bash -lc`, `^env gog …`, an absolute path and a
#: `curl` straight at the REST API are all still missed by it, which is why a
#: finding here is a floor and not a bound.
_CLI_EQUIVALENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gog gmail send --to alice@example.com", ("gws_gmail_send", "gws_gmail_reply")),
    ("bash -lc 'gog gmail send'", ("gws_gmail_send", "gws_gmail_reply")),
    ("env gog gmail send", ("gws_gmail_send", "gws_gmail_reply")),
    ("/usr/local/bin/gog gmail send", ("gws_gmail_send", "gws_gmail_reply")),
    ("gog gmail drafts send", ("gws_gmail_send",)),
    ("gog gmail forward --to alice@example.com", ("gws_gmail_send", "gws_gmail_reply")),
    ("python -m gogcli gmail send", ("gws_gmail_send", "gws_gmail_reply")),
    (
        "curl -X POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        ("gws_gmail_send", "gws_gmail_reply"),
    ),
    ("gws gmail users messages send", ("gws_gmail_send", "gws_gmail_reply")),
    ("gog gmail reply --thread t", ("gws_gmail_reply",)),
    ("gog gmail search --query q", ("gws_gmail_search",)),
    ("gws gmail users messages list", ("gws_gmail_search",)),
    ("gog calendar events insert", ("gws_calendar_create",)),
    ("gws calendar events insert", ("gws_calendar_create",)),
    ("gog calendar events delete", ("gws_calendar_delete",)),
    ("gws calendar events delete", ("gws_calendar_delete",)),
    ("gog chat send --space s", ("gws_chat_send",)),
    ("gws chat spaces messages create", ("gws_chat_send",)),
)


def _exec_patterns(manifest: dict[str, Any]) -> list[str]:
    v2 = manifest.get("v2") or {}
    if not isinstance(v2, dict):
        return []
    return [str(p) for p in (v2.get("exec_allowlist") or [])]


#: A command that is not a Google CLI at all. A pattern admitting THIS admits
#: anything, so the agent can reach any denied tool through any route — and a
#: catch-all used to be reported only for the six natives the probe table
#: happens to name. `exec_allowlist: ["^.*$"]` with three denied tools produced
#: no finding at all.
_CATCH_ALL_PROBE = "echo hello"


def _admits(patterns: list[str], probe: str) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, probe):
                return True
        except re.error:
            continue  # a broken regex is manifests.schema's problem
    return False


def _has_a_shell(manifest: dict[str, Any], registered: set[str]) -> bool:
    """Is this agent actually granted ``exec``?

    An ``exec_allowlist`` left behind on an agent with no shell is a tidy-up,
    not a bypass: ``guardrails._check_exec_allowlist`` returns early for any
    other tool, and the registry never advertises ``exec`` when a non-empty
    ``tools_allowed`` omits it. Reporting it as one — with wording about mail
    going out past the do-not-contact check — was untrue there.
    """
    if _denied(manifest, "exec"):
        return False
    return "exec" in _granted(manifest, registered)


def _scan_exec_bypasses(directory: Path) -> list[str]:
    registered = _registered_names()
    lines: list[str] = []
    for manifest in _scan_manifests(directory):
        agent_id = str(manifest.get("id") or "")
        patterns = _exec_patterns(manifest)
        if not patterns or not _has_a_shell(manifest, registered):
            continue

        findings: set[str] = set()
        if _admits(patterns, _CATCH_ALL_PROBE):
            # It admits anything, so every denied tool is reachable — not just
            # the ones this module happens to have a probe for.
            denied_tools = sorted(n for n in registered if _denied(manifest, n) and n != "exec")
            if denied_tools:
                shown = ", ".join(denied_tools[:5])
                if len(denied_tools) > 5:
                    shown += f" and {len(denied_tools) - 5} more"
                findings.add(f"an exec allowlist that admits any command, beside deny of {shown}")
        else:
            for probe, natives in _CLI_EQUIVALENTS:
                if not _admits(patterns, probe):
                    continue
                for native in natives:
                    if _denied(manifest, native):
                        findings.add(f"{native} via `{probe.split(' --')[0]}`")
        if findings:
            lines.append(f"{agent_id}: " + ", ".join(sorted(findings)))
    return lines


async def _exec_allowlist_bypasses_denied_tool(ctx: DoctorContext) -> Result:
    """A manifest denies a tool and then allows the CLI that does the same job.

    The denial reads as a policy and is not one: the agent reaches the same
    Google account through ``exec``, past the do-not-contact check, the
    duplicate-reply guard, the threading that ``gws_gmail_reply`` does for it
    and the benchmark gate that refuses every ``gws_*`` call on a graded run.
    Either allow the native tool or drop the CLI from ``exec_allowlist`` —
    whichever the denial actually meant.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    try:
        lines = await ctx.run_blocking(_scan_exec_bypasses, directory)
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifests: {type(exc).__name__}")
    if not lines:
        return ok("no exec allowlist re-opens a tool its manifest denies")
    shown = "; ".join(lines[:_MAX_LISTED])
    if len(lines) > _MAX_LISTED:
        shown += f"; and {len(lines) - _MAX_LISTED} more"
    return fail(f"{len(lines)} agent(s) whose exec allowlist bypasses a denied tool: {shown}")


# ── agents.approval_gate_available ────────────────────────────────────

#: Tools that destroy a record outright, where "it was wrong" is not something
#: the operator can undo from the UI.
#:
#: Deliberately NOT the list of tools that can do damage. `write_file`,
#: `gws_gmail_send` and `git_push` are all more dangerous and all are ordinary
#: grants — an early cut of this check named them and fired on 16 of the 16
#: stock templates, which is the "a check that fires on a clean install is a
#: check nobody reads" failure this branch already fixed once. A per-tool
#: policy question belongs to the manifest; what this check is for is the gap
#: between what a manifest CLAIMS to gate and what the running engine gates.
_DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {"delete_person", "delete_company", "delete_note", "delete_task"}
)


@dataclass(frozen=True)
class _Grant:
    """One agent's destructive grants, and what its manifest says about them.

    A record rather than a preformatted line so the caller can count TOOLS as
    well as agents: "3 destructive tools" and "1 agent" are different facts and
    the report states the first.
    """

    agent_id: str
    tools: tuple[str, ...]
    note: str = ""

    def __str__(self) -> str:
        tail = f" ({self.note})" if self.note else ""
        return f"{self.agent_id}: {', '.join(self.tools)}{tail}"


def _approval_gate_state() -> tuple[str, str]:
    """``(mode, why)`` for the fail-closed human-approval gate.

    Both variables are read rather than only ``approval_mode()`` so the report
    can say WHICH one is missing. ``_enforcement_mode`` returns ``off``
    whenever the enabled var is falsy regardless of the mode var, so
    ``ROBOTHOR_APPROVAL_MODE=enforce`` on its own is a no-op — and that is the
    variable an operator reading "set the mode to enforce" will set.

    Through ``approval_gate_inputs()``, not ``os.environ``: these are governed
    flags, so a value set on the Controls page lives in the flag store and not
    in this process's environment, and a doctor that disagreed with the engine
    about what is set would be worse than no doctor.
    """
    from robothor.engine.feature_flags import approval_gate_inputs, approval_mode

    mode = str(approval_mode())
    if mode == "enforce":
        return mode, ""
    enabled, declared = approval_gate_inputs()
    if not enabled:
        return mode, (
            "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED is unset, which forces the gate off "
            f"whatever ROBOTHOR_APPROVAL_MODE says (it is {declared or 'unset'!r})"
        )
    return mode, (
        f"ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED is set but ROBOTHOR_APPROVAL_MODE is "
        f"{declared or 'unset'!r}, so the gate is in {mode!r}: the verdict is computed "
        "and logged, and the tool call proceeds"
    )


def _destructive_grants(directory: Path) -> tuple[list[_Grant], list[_Grant]]:
    """``(ungated, gated)`` destructive grants across this instance's manifests.

    Scoped to grants the manifest has an OPINION about: a record-deleting tool
    (:data:`_DESTRUCTIVE_TOOLS`), or any tool the manifest itself names in
    ``v2.human_approval_tools``. An agent that grants neither is not this
    check's business — the question "should this tool need approval?" is a
    policy one, and a check that answers it for every agent fires on a clean
    install and gets ignored.

    **ungated** — the manifest itself does not gate them, so no environment
    setting can. Three ways: the policy is missing from ``v2.guardrails``;
    ``v2.human_approval_tools`` does not name the tool; or
    ``human_approval_fail_open: true`` defeats ``enforce`` outright. None of
    the three is a defect — ungated is what the platform does by default — but
    an operator reading a manifest that looks careful may believe otherwise,
    and that mismatch is worth recording.

    **gated** — the manifest declares both halves correctly. Whether anything
    actually escalates then depends entirely on the engine's two environment
    variables, which is the half nothing else brings together.
    """
    registered = _registered_names()
    ungated: list[_Grant] = []
    gated: list[_Grant] = []
    for manifest in _scan_manifests(directory):
        v2 = manifest.get("v2") or {}
        if not isinstance(v2, dict):
            v2 = {}
        policies = {str(p) for p in (v2.get("guardrails") or [])}
        named = {str(t) for t in (v2.get("human_approval_tools") or [])}
        holds = _granted(manifest, registered)
        # Only grants this manifest made ON PURPOSE. An agent with no
        # `tools_allowed` holds every registered tool, deletes included — that
        # is `main` and `morning-briefing` among the stock templates, and
        # counting them fired on a clean install with two findings the operator
        # can do nothing useful about. "This agent is unrestricted" is a real
        # observation and a different check's.
        declared = {str(n) for n in (manifest.get("tools_allowed") or [])}
        # `tools_denied` wins, exactly as the loader applies it AFTER
        # `tools_allowed` (`ToolRegistry._get_filtered_names`). A manifest that
        # lists `delete_person` in both never receives it, and reporting it as
        # an ungated destructive grant is a doctor disagreeing with the loader
        # — the rule this module's own docstrings state twice, and the same
        # defect as round-1 C3(a) reappearing in a new check. Globs included,
        # since `_denied` matches with `fnmatch`.
        declared = {n for n in declared if not _denied(manifest, n)}
        # What this manifest either destroys on purpose, or says out loud it
        # wants gated.
        interesting = (declared & _DESTRUCTIVE_TOOLS) | (holds & named)
        if not interesting:
            continue
        agent_id = str(manifest.get("id") or manifest.get("name") or "?")
        if "human_approval" not in policies:
            ungated.append(
                _Grant(
                    agent_id,
                    tuple(sorted(interesting)),
                    "v2.guardrails does not list human_approval, so nothing escalates",
                )
            )
            continue
        missing = tuple(sorted(interesting - named))
        if missing:
            ungated.append(_Grant(agent_id, missing, "not in human_approval_tools"))
        covered = tuple(sorted(interesting & named))
        if not covered:
            continue
        if v2.get("human_approval_fail_open"):
            ungated.append(
                _Grant(agent_id, covered, "human_approval_fail_open: true defeats enforce")
            )
        else:
            gated.append(_Grant(agent_id, covered))
    return ungated, gated


def _summarise(grants: list[_Grant]) -> str:
    shown = "; ".join(str(grant) for grant in grants[:_MAX_LISTED])
    if len(grants) > _MAX_LISTED:
        shown += f"; and {len(grants) - _MAX_LISTED} more"
    return shown


def _tool_total(grants: list[_Grant]) -> int:
    return sum(len(grant.tools) for grant in grants)


async def _approval_gate_available(ctx: DoctorContext) -> Result:
    """Which record-deleting grants declare the OPTIONAL approval gate.

    A fact for the record, never a recommendation. Genus OS runs agents
    autonomously by design; ``v2.human_approval_tools`` is an opt-in an
    instance may want for particular tools, and nothing in the platform may
    push anyone toward it.

    It shipped as ``agents.destructive_tool_not_gated`` at ``recommended``,
    which read as "you should gate this" — and on 2026-09-17 an instance did:
    a nightly CRM hygiene scan put ``delete_person`` behind ``human_approval``,
    so every duplicate-contact delete asked a person on an unattended run,
    waited out ``human_approval_timeout`` and was denied. The run deleted
    nothing and the operator got one prompt per duplicate.

    So the severity is ``info`` AND the result is a ``pass``. Severity alone
    only keeps the row out of the ``recommended`` count; a red cross beside a
    sentence explaining that this is the default is the same nudge wearing a
    different label.

    Not silence either. The gate is a good feature and turning it on must stay
    easy, so the line says the gate is AVAILABLE and names the two manifest
    keys that switch it on — an operator who wants a human in the loop for a
    refund or a payment should not have to go looking. What it does not do is
    imply that anything is missing.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    try:
        ungated, _gated = await ctx.run_blocking(_destructive_grants, directory)
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifests: {type(exc).__name__}")

    if not ungated:
        # Says what was EXAMINED, not more. The scoping deliberately skips
        # agents with no `tools_allowed` — `main` and `morning-briefing` among
        # the stock templates hold all four record-deleting tools that way — so
        # "every destructive grant is gated" would be a reassurance nobody
        # earned.
        return ok("every record-deleting grant an agent made on purpose declares the optional gate")
    return info(
        f"{_tool_total(ungated)} destructive tools are granted without an approval gate; "
        "the gate is optional (`human_approval_tools`) — autonomous is the default. "
        "It is available per agent and takes one manifest edit: add the tool to "
        "v2.human_approval_tools and human_approval to v2.guardrails. Worth it for an "
        f"irreversible external action — a refund, a payment; rarely for anything else: "
        f"{_summarise(ungated)}"
    )


async def _approval_gate_not_armed(ctx: DoctorContext) -> Result:
    """Manifests that ask for human approval, on an engine not enforcing it.

    The ENGINE half. ``human_approval`` needs both manifest halves AND both
    environment variables, and nothing else brings those two facts together: a
    manifest can read as carefully gated in review and run ungated in
    production, because ``ROBOTHOR_APPROVAL_MODE=enforce`` alone is a no-op.

    ``info``, and it says so in words as well: an unarmed gate is the DEFAULT
    posture, not a gap. ``observe`` is a deliberate rung on a documented ladder
    and the platform's own Helm chart ships it — a chart cannot guarantee an
    approver is wired, and ``enforce`` with none denies every escalated call.
    Reporting a correctly-configured instance mid-soak as ``degraded`` is the
    "a check that fires on a clean install is a check nobody reads" failure
    this module argues against twice.
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    try:
        _ungated, gated = await ctx.run_blocking(_destructive_grants, directory)
    except Exception as exc:  # noqa: BLE001 - a scan that raises is a failure
        return fail(f"cannot scan the manifests: {type(exc).__name__}")

    if not gated:
        return ok("no agent asks for human approval on a destructive tool")

    mode, why = _approval_gate_state()
    if mode == "enforce":
        return ok("the approval gate is enforcing, so every declared gate applies")
    return fail(
        f"{len(gated)} declared approval gate(s) this run will not apply: "
        f"{_summarise(gated)}. An unarmed gate is the default, not a gap — the gate is "
        f"opt-in and the platform runs agents autonomously. {why}. Escalations are "
        "recorded and the call proceeds; if this instance wants the gate, promote with "
        "the checklist in docs/runbooks/approval-enforce.md — wire an approver, prove "
        "one round-trip, soak 48h, then set ROBOTHOR_APPROVAL_MODE=enforce."
    )


# ── calendar.operator_calendar_writable ───────────────────────────────


def _calendar_tool_granted(directory: Path) -> bool:
    """Does any agent on this instance hold a calendar tool at all?

    The check is meaningless on an instance that does not do calendars, and a
    required check that fails for everyone who does not use the feature trains
    operators to ignore the report.
    """
    from robothor.engine.tools.constants import GWS_TOOLS

    registered = _registered_names()
    calendar_tools = {name for name in GWS_TOOLS if name.startswith("gws_calendar_")}
    for manifest in _scan_manifests(directory):
        if _granted(manifest, registered) & calendar_tools:
            return True
    return False


def _operator_calendar_access(owner_email: str) -> tuple[str, str]:
    """``(accessRole, error)`` for the operator's calendar on the bot's account."""
    import json as _json

    from robothor.engine.tools.handlers.gws import _run_gws

    listed = _run_gws(["calendar", "calendarList", "list", "--params", _json.dumps({})])
    if not isinstance(listed, dict):
        return "", "the gws CLI returned an unexpected shape"
    if "error" in listed:
        # The HINT, never the raw stderr. `_run_gws` always classifies, and a
        # doctor result is rendered to a terminal, a log and /api/doctor —
        # three surfaces that should not carry a CLI's verbatim output, which
        # has been observed holding an address and a token fragment.
        # Truncating it would not have made it safe.
        hint = str(listed.get("hint") or "").strip()
        return "", hint or "the gws CLI failed and did not say why"
    for entry in listed.get("items") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id", "")).lower() == owner_email:
            return str(entry.get("accessRole", "")), ""
    return "", ""


async def _operator_calendar_writable(ctx: DoctorContext) -> Result:
    """The assistant's Google account can write the OPERATOR's calendar.

    On 2026-09-16 an itinerary was planned, created and reported as done. It
    landed on the assistant's own calendar — ``primary`` is the bot's account,
    not the operator's — with the operator as an attendee and no invitation
    sent. The tools now default to the operator's calendar by id, which only
    works if the bot's account actually has writer access to it.

    Without that access, every calendar write fails or silently goes to the
    wrong place, and the failure looks like success in the agent's transcript.
    The fix is one action in the operator's Google Calendar: share it with the
    assistant's account with "Make changes to events".
    """
    directory = _manifest_dir(ctx)
    if not directory.is_dir():
        return skip(f"no manifest directory at {directory}")
    if ctx.offline:
        # `offline` is documented as "do not spend money, leave the box, or
        # start an external process", and this check forks the `gws` binary to
        # ask Google for a calendar list. The bridge's GET /api/doctor runs
        # offline, so without this every poll forked a subprocess. Emptying
        # PATH does not save us: `gws_available()` probes an absolute path.
        return skip("--offline: the calendar list was not fetched")
    if not await ctx.run_blocking(_calendar_tool_granted, directory):
        return skip("no agent on this instance is granted a calendar tool")

    from robothor.engine.tools.handlers.gws import gws_available

    if not gws_available():
        return skip("no gws CLI on this host, so no Google account to ask")

    from robothor.owner_config import load_owner_config

    try:
        owner = await ctx.run_blocking(load_owner_config)
    except Exception as exc:  # noqa: BLE001 - a bad owner.yaml is identity's problem
        return skip(f"operator identity unreadable: {type(exc).__name__}")
    owner_email = (getattr(owner, "email", "") or "").strip().lower()
    if not owner_email:
        return skip("no operator identity configured (~/.robothor/owner.yaml)")

    try:
        role, error = await ctx.run_blocking(_operator_calendar_access, owner_email)
    except Exception as exc:  # noqa: BLE001 - a CLI that raises is a result
        return fail(f"could not read the account's calendar list: {type(exc).__name__}")

    if error:
        return fail(
            "could not read this account's calendar list, so it is not known whether "
            f"the operator's calendar is writable: {error}"
        )
    if not role:
        return fail(
            "the operator's calendar is not on this account's calendar list at all. "
            "Every event an agent creates will land on the assistant's own calendar "
            "instead. Fix: in the operator's Google Calendar, share it with this "
            "instance's Google account and choose 'Make changes to events'."
        )
    if role not in ("owner", "writer"):
        return fail(
            f"this account has '{role}' access to the operator's calendar, which cannot "
            "create or delete events. Fix: in the operator's Google Calendar, change "
            "this account's permission to 'Make changes to events'."
        )
    return ok(f"the operator's calendar is writable by this account ({role})")


CHECKS: tuple[Check, ...] = (
    Check(
        id="calendar.operator_calendar_writable",
        title="The operator's calendar is writable by this instance",
        category="calendar",
        severity="required",
        run=_operator_calendar_writable,
    ),
    Check(
        id="agents.tools_named_but_not_granted",
        title="Instructions name only tools the manifest grants",
        category="agents",
        severity="recommended",
        run=_tools_named_but_not_granted,
    ),
    Check(
        id="agents.approval_gate_available",
        title="Record-deleting grants, and which declare the optional approval gate",
        category="agents",
        severity="info",
        run=_approval_gate_available,
    ),
    Check(
        id="agents.approval_gate_not_armed",
        title="The engine enforces the approval gates its manifests declare",
        category="agents",
        severity="info",
        run=_approval_gate_not_armed,
    ),
    Check(
        id="tools.exec_allowlist_bypasses_denied_tool",
        title="No exec allowlist re-opens a denied tool",
        category="tools",
        severity="recommended",
        run=_exec_allowlist_bypasses_denied_tool,
    ),
)

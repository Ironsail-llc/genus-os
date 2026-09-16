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
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

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


def _registered_names() -> set[str]:
    from robothor.engine.tools.registry import builtin_schema_names

    return builtin_schema_names()


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

    This deliberately under-reports rather than over-reports. A missed mention
    costs an operator nothing; a fabricated one costs them their trust in the
    check, and a check nobody trusts is the one they learn to skip.
    """
    found: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(text):
        if not clause or not clause.strip():
            continue
        trailing = _TRAILING_NEGATION_RE.search(clause) is not None
        for match in _MENTION_RE.finditer(clause):
            name = match.group("ticked") or match.group("called") or match.group("bare")
            if name not in registered:
                continue
            if trailing:
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


def _granted(manifest: dict[str, Any], registered: set[str]) -> set[str] | None:
    """Every tool this manifest grants, or ``None`` for "all of them".

    ``None`` is the engine's own semantics for an absent or empty
    ``tools_allowed``: ``ToolRegistry._get_filtered_names`` falls through to
    ``names = list(self._schemas.keys())``. That is the DEFAULT manifest shape,
    so treating it as "grants nothing" made this check fail on a freshly
    initialised instance — every tool its instructions named was reported
    missing from an agent that had all of them.
    """
    allowed = manifest.get("tools_allowed")
    if not allowed:
        return None
    granted = {str(n) for n in allowed}
    for block_key, list_key in (
        ("heartbeat", "heartbeat_tools_allowed"),
        ("worker", "worker_tools_allowed"),
    ):
        block = manifest.get(block_key) or {}
        if isinstance(block, dict) and block.get(list_key):
            granted |= {str(n) for n in block[list_key]}
    # GOAL_TOOLS are appended unconditionally by the registry filter.
    from robothor.engine.tools.constants import GOAL_TOOLS

    return granted | (set(GOAL_TOOLS) & registered)


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
        if granted is None:
            # "All tools" — only a deny can make a named tool unreachable.
            ungranted: list[str] = []
            unresolved: list[str] = []
        else:
            ungranted = sorted(n for n in named if n not in granted and n not in denied)
            unresolved = sorted(n for n in granted if n not in registered)

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
_CLI_EQUIVALENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gog gmail send --to alice@example.com", ("gws_gmail_send", "gws_gmail_reply")),
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


def _scan_exec_bypasses(directory: Path) -> list[str]:
    lines: list[str] = []
    for manifest in _scan_manifests(directory):
        agent_id = str(manifest.get("id") or "")
        patterns = _exec_patterns(manifest)
        if not patterns:
            continue
        findings: set[str] = set()
        for probe, natives in _CLI_EQUIVALENTS:
            admitted = False
            for pattern in patterns:
                try:
                    if re.search(pattern, probe):
                        admitted = True
                        break
                except re.error:
                    continue  # a broken regex is manifests.schema's problem
            if not admitted:
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
        granted = _granted(manifest, registered)
        # `None` is "every tool", which includes the calendar ones.
        if granted is None or (granted & calendar_tools):
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
        return "", str(listed.get("hint") or listed.get("error") or "")[:200]
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
        id="tools.exec_allowlist_bypasses_denied_tool",
        title="No exec allowlist re-opens a denied tool",
        category="tools",
        severity="recommended",
        run=_exec_allowlist_bypasses_denied_tool,
    ),
)

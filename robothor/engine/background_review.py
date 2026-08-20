"""Background-review fork — Rip 1 of the Tier 1 upgrade.

Adapted from the Hermes Agent pattern
(`/tmp/research/hermes-agent/agent/background_review.py:327-560`).
The Hermes version spawns a daemon thread with a forked AIAgent that
inherits the parent's cached system prompt so the review API call
hits the same prefix cache (~26% Sonnet 4.5 savings measured upstream).

Our engine is fully async (no `asyncio.run()` outside daemon.py /
cli.py per `robothor/engine/CLAUDE.md`), so the fork is an
`asyncio.create_task` instead of a Thread. The shape is otherwise
preserved:

* per-turn nudge counters (`_iters_since_skill`, `_turns_since_memory`)
  live on `AgentSession` (added in the Phase 0 refactor),
* when a counter trips, the runner's `_after_response_delivered` hook
  calls `spawn_background_review(...)`,
* the spawn re-enters the engine via `spawn_agent` with
  `mode="background_review"`, which installs a thread-local tool
  whitelist (`memory_*` + `skill_*` only) and forces
  `delivery=NONE` so the operator never sees the review chatter,
* the spawned fork's system prompt is the parent's cached value
  (`session._cached_system_prompt`) so the inference request hits
  the prefix cache instead of paying full cost.

This replaces the silent-dead `brain/memory_system/continuous_ingest.py`
(no successful fact extraction since 2026-04-16). Fact and skill
writes here go through the same DAL as foreground writes, so the
existing observability (`memory_facts_audit` from Rip 7, structured
logs) covers them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from robothor.engine.feature_flags import is_rip_enabled
from robothor.engine.sanitize import sanitize_log

if TYPE_CHECKING:
    from robothor.engine.session import AgentSession

logger = logging.getLogger(__name__)


# Which agent runs the review. The Hermes pattern forks the SAME
# agent so the cached system prompt matches — that's the prefix-cache
# trick. Until we plumb system_prompt_override through runner.execute
# (Rip 1 v2), we use a dedicated reviewer agent if configured, falling
# back to the parent agent.
DEFAULT_REVIEWER_AGENT_ID = "main"


# ── Tuning knobs ─────────────────────────────────────────────────────
# Trip the per-turn nudge after this many user turns / tool-loop
# iterations. Hermes defaults: 10 user turns / 15 iterations
# (`cli-config.yaml.example:478-485`). Genus matches.
MEMORY_NUDGE_INTERVAL = 10
SKILL_NUDGE_INTERVAL = 15


# ── Whitelist of tools the review fork may dispatch ──────────────────
# Anything outside this set is denied at dispatch time by the
# thread-local ContextVar (see robothor/engine/tools/dispatch.py).
# Memory writes and skill mutations are the only intended side
# effects; everything else (web fetch, terminal, git, send_message,
# etc.) is irrelevant to the review and risks an autonomous fork
# doing something the user didn't ask for.
REVIEW_TOOL_WHITELIST: frozenset[str] = frozenset(
    {
        # Memory — the registered Genus tool names. (The Hermes-upstream names
        # memory_search/memory_write/memory_update/memory_delete do not exist
        # here; carrying them silently denied every memory call the review
        # fork attempted. Guarded by test_tool_registry_parity.py.)
        "search_memory",
        "store_memory",
        # Skills
        "invoke_skill",
        "list_skills",
        "skill_view",
        "create_skill",
        "update_skill",
    }
)


# ── Prompts — ported verbatim from Hermes with Genus path subs ──────
# These are the load-bearing piece: the Hermes pattern's value comes
# largely from the explicit "do not capture" guardrail (lines
# 124-148 of the original) that prevents the Nightwatch failure
# mode of capturing transient environment errors as durable rules.

_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be "
    "ACTIVE — most sessions produce at least one skill update, even if "
    "small. A pass that does nothing is a missed learning opportunity, "
    "not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries. This "
    "shapes HOW you update, not WHETHER you update.\n\n"
    "Signals to look for (any one of these warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or "
    "verbosity. Frustration signals like 'stop doing X', 'this is too "
    "verbose', 'don't format like this', 'why are you explaining', "
    "'just give me the answer', 'you always do Y and I hate it', or an "
    "explicit 'remember this' are FIRST-CLASS skill signals, not just "
    "memory signals. Update the relevant skill(s) to embed the "
    "preference so the next session starts already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. "
    "Encode the correction as a pitfall or explicit step in the skill "
    "that governs that class of task.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged that a future session would benefit "
    "from. Capture it.\n"
    "  • A skill that got loaded or consulted this session turned out "
    "to be wrong, missing a step, or outdated. Patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits, but do "
    "pick one when a signal above fired:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the "
    "conversation for skills the user loaded via /skill-name or you "
    "read via skill_view. If any of them covers the territory of the "
    "new learning, PATCH that one first. It is the skill that was in "
    "play, so it's the right one to extend.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (via list_skills + skill_view). "
    "If no loaded skill fits but an existing class-level skill does, "
    "patch it. Add a subsection, a pitfall, or broaden a trigger.\n"
    "  3. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing "
    "skill covers the class. The name MUST be at the class level. "
    "The name MUST NOT be a specific PR number, error string, feature "
    "codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' "
    "session artifact. If the proposed name only makes sense for "
    "today's task, it's wrong — fall back to (1) or (2).\n\n"
    "User-preference embedding (important): when the user expressed a "
    "style/format/workflow preference, the update belongs in the "
    "SKILL.md body, not just in memory. Memory captures 'who the user "
    "is and what the current situation and state of your operations "
    "are'; skills capture 'how to do this class of task for this "
    "user'. When they complain about how you handled a task, the "
    "skill that governs that task needs to carry the lesson.\n\n"
    "If you notice two existing skills that overlap, note it in your "
    "reply — the background curator handles consolidation at scale.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Pinned skills (marked via meta.json `pinned: true`) CAN be "
    "improved — pin only blocks deletion/archive/consolidation by the "
    "curator, not content updates. Patch them when a pitfall or missing "
    "step turns up, same as any other agent-created skill.\n\n"
    "Do NOT capture (these become persistent self-imposed constraints "
    "that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y'). These harden into "
    "refusals the agent cites against itself for months after the actual "
    "problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "'Nothing to save.' is a real option but should NOT be the "
    "default. If the session ran smoothly with no corrections and "
    "produced no new technique, just say 'Nothing to save.' and stop. "
    "Otherwise, act."
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most "
    "sessions produce at least one skill update. A pass that does "
    "nothing is a missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md. Not a long flat list of narrow one-session-one-skill "
    "entries.\n\n"
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS skill "
    "signal, not just a memory signal. 'stop doing X', 'don't format "
    "like this', 'I hate when you Y' — embed the lesson in the skill "
    "that governs that task so the next session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patch it now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via /skill-name or skill_view in the conversation. If one "
    "of them covers the learning, PATCH it first. It was in play; "
    "it's the right place.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (list_skills + skill_view to "
    "find the right one). Patch it.\n"
    "  3. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. "
    "Name at the class level — NOT a PR number, error string, "
    "codename, library-alone name, or 'fix-X / debug-Y' session "
    "artifact. If the name only fits today's task, fall back to (1) "
    "or (2).\n\n"
    "User-preference embedding: when the user complains about how "
    "you handled a task, update the skill that governs that task — "
    "memory alone isn't enough. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; "
    "skills say 'how to do this class of task for this user'. Both "
    "should carry user-preference lessons when relevant.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken'). These harden into refusals the agent "
    "cites against itself for months after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended.\n"
    "  • One-off task narratives.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    "genuinely nothing stands out on either, say 'Nothing to save.' "
    "and stop — but don't reach for that conclusion as a default."
)


@dataclass(frozen=True)
class ReviewDecision:
    """Outcome of the per-turn nudge counter check.

    The runner asks ``maybe_spawn_review(session)`` after the foreground
    response is delivered; the answer says whether to spawn the
    background fork at all and which prompt to use.
    """

    should_review: bool
    review_memory: bool
    review_skills: bool

    @property
    def prompt(self) -> str:
        """Pick the right verbatim Hermes prompt for the active flags."""
        if self.review_memory and self.review_skills:
            return _COMBINED_REVIEW_PROMPT
        if self.review_skills:
            return _SKILL_REVIEW_PROMPT
        return _MEMORY_REVIEW_PROMPT


def maybe_spawn_review(
    session: AgentSession,
    *,
    memory_interval: int = MEMORY_NUDGE_INTERVAL,
    skill_interval: int = SKILL_NUDGE_INTERVAL,
) -> ReviewDecision:
    """Decide whether the background review fork should fire this turn.

    Reads the per-session counters promoted to ``AgentSession`` in the
    Phase 0 refactor. Resets the relevant counter(s) when the
    decision is to spawn, so the next turn starts a fresh interval.
    Does NOT spawn anything — pure logic, easy to unit test.
    """
    review_memory = session._turns_since_memory >= memory_interval
    review_skills = session._iters_since_skill >= skill_interval

    if review_memory:
        session._turns_since_memory = 0
    if review_skills:
        session._iters_since_skill = 0

    return ReviewDecision(
        should_review=review_memory or review_skills,
        review_memory=review_memory,
        review_skills=review_skills,
    )


def _build_review_message(decision: ReviewDecision, transcript_tail: str = "") -> str:
    """Wrap the review prompt with the conversation tail for the fork.

    The Hermes fork inherits parent ``messages`` so the model already
    sees the conversation; ours uses a fresh sub-agent run via
    ``spawn_agent``, so we splice a short transcript tail into the
    review message instead. ``transcript_tail`` is best-effort — the
    foreground agent renders the same memory tool, so even an empty
    tail will produce reasonable behaviour (the agent will reason from
    cold memory state).
    """
    prompt = decision.prompt
    if not transcript_tail:
        return prompt
    return f"{prompt}\n\n── Conversation tail for context ──\n{transcript_tail}\n── End of tail ──"


def _render_transcript_tail(messages: list[dict[str, object]] | None, last_n: int = 12) -> str:
    """Render the last N messages as a compact transcript for the review prompt."""
    if not messages:
        return ""
    tail = messages[-last_n:]
    parts: list[str] = []
    for m in tail:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, list):
            # tool result list — flatten textually
            content = " ".join(
                str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content
            )
        # Trim very long messages to keep the review prompt manageable
        if isinstance(content, str) and len(content) > 800:
            content = content[:800] + " …"
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def _fork_skip_reason(session: AgentSession) -> str | None:
    """Why the background-review fork must NOT fire for this run (or None).

    Three hard exclusions + one soak gate:
    - benchmark runs (sandboxed grading — must not mutate the skill library);
    - any nested/spawned run, CRUCIALLY the review fork itself (a SUB_AGENT at
      nesting_depth>0). Without this the fork spawns a fork spawns a fork…;
    - ROBOTHOR_RIP_1_AGENTS soak allowlist: when set (comma list), only those
      agents fork; unset/empty → fleet-wide.
    """
    run = session.run
    if getattr(run, "is_benchmark", False):
        return "benchmark"
    if getattr(run, "nesting_depth", 0) and run.nesting_depth > 0:
        return "nested"
    trigger = getattr(run, "trigger_type", None)
    if trigger is not None and getattr(trigger, "name", "") == "SUB_AGENT":
        return "sub_agent"
    allow = os.environ.get("ROBOTHOR_RIP_1_AGENTS", "").strip()
    if allow:
        allowed = {a.strip() for a in allow.split(",") if a.strip()}
        if run.agent_id not in allowed:
            return "not_in_allowlist"
    return None


async def spawn_background_review(
    session: AgentSession,
    *,
    reviewer_agent_id: str = DEFAULT_REVIEWER_AGENT_ID,
    decision: ReviewDecision | None = None,
) -> dict[str, object] | None:
    """Spawn the background-review fork (Rip 1 main entry point).

    Reads the per-session counters via :func:`maybe_spawn_review` (if no
    explicit ``decision`` is supplied), builds the review prompt with a
    short transcript tail, and dispatches a child agent run with
    ``mode='background_review'`` so the dispatch whitelist locks the
    child to memory + skill CRUD only.

    Returns the child run's structured result dict, or ``None`` when
    the rip is off / no counter tripped / spawn infrastructure
    unavailable. Never raises — failures log and return ``None`` so the
    runner's post-response hook stays free of background-review
    failure modes.
    """
    if not is_rip_enabled(1):
        return None

    skip = _fork_skip_reason(session)
    if skip is not None:
        logger.debug("background-review fork skipped (%s) for run %s", skip, session.run.id)
        return None

    if decision is None:
        decision = maybe_spawn_review(session)
    if not decision.should_review:
        return None

    try:
        from robothor.engine.tools.handlers.spawn import _handle_spawn_agent
    except Exception:
        logger.warning("spawn_background_review: spawn handler unavailable")
        return None

    transcript = _render_transcript_tail(session.messages)
    message = _build_review_message(decision, transcript_tail=transcript)

    # Rip 4: tag every skill / memory write originating inside the
    # forked review as 'background_review' so the curator (Rip 5) can
    # tell auto-created skills from user-authored ones and only prune
    # the former.
    from robothor.engine.skill_provenance import (
        BACKGROUND_REVIEW,
        reset_current_write_origin,
        set_current_write_origin,
    )

    provenance_token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        result = await _handle_spawn_agent(
            {
                "agent_id": reviewer_agent_id,
                "message": message,
                "mode": "background_review",
                # Cap iterations conservatively — review is a single
                # focused pass, not a wide search.
                "max_iterations": 16,
            },
            agent_id=session.run.agent_id,
        )
    except Exception as exc:  # noqa: BLE001 — background, never propagate
        logger.warning(
            "spawn_background_review failed for session %s: %s",
            session.run_id,
            exc,
        )
        return None
    finally:
        reset_current_write_origin(provenance_token)

    logger.info(
        "background_review completed: session=%s memory=%s skills=%s status=%s",
        sanitize_log(session.run_id),
        decision.review_memory,
        decision.review_skills,
        sanitize_log(result.get("status") if isinstance(result, dict) else "?"),
    )
    return result if isinstance(result, dict) else None


def fire_and_forget(session: AgentSession, **kwargs: object) -> asyncio.Task[object] | None:
    """Schedule :func:`spawn_background_review` as a non-blocking task.

    Used by the runner's ``_after_response_delivered`` hook so the
    background fork runs while the foreground turn returns. Returns
    the created task, or ``None`` when not on an asyncio event loop
    (e.g. when called from sync code with no loop available — falls
    silent rather than raising).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(spawn_background_review(session, **kwargs))  # type: ignore[arg-type]

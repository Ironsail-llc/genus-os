"""What tools a run gets, and how plan mode wraps its prompt.

Extracted from `execute`, which is 1,132 lines. Two things happen here that
are easy to get subtly wrong and were covered by no test:

* **The plan-mode sandwich.** Constraints go BEFORE the identity prompt and the
  reminder AFTER, so plan rules are not buried in the middle of SOUL.md's
  directives. Appending both at the end — the obvious simplification — puts the
  rules where a long identity prompt drowns them.
* **Adapter loading is non-fatal.** An external MCP server that is down must
  cost the agent that server's tools, not the whole run. `v2.mcp_servers` was
  dead code until it was wired in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)


@dataclass
class PreparedToolset:
    tool_schemas: list[dict[str, Any]]
    tool_names: list[str]
    system_prompt: str
    #: Allowed tools NOT in ``tool_schemas`` — reachable only via ``tool_call``.
    #: Empty when the toolset is not deferred.
    reachable_names: tuple[str, ...] = ()
    #: One sentence for the engine-context turn, or "" when there is nothing
    #: true to say. See :func:`deferred_toolset_note`.
    discovery_note: str = ""


def deferred_toolset_note(visible: int, reachable: int) -> str:
    """What the agent is told about its own toolset, or nothing.

    Deferral is invisible from inside the turn: the model is handed a short
    list of schemas and no statement that there is a longer one. Given
    documentation that names ``gws_gmail_reply`` and a toolset that contains
    ``exec``, the cheapest way to obey is ``exec("gog gmail send …")`` — which
    is what production shows it doing.

    Nothing is said on a run that is NOT deferred: a sentence about a
    mechanism that is not running costs tokens to be wrong.
    """
    if reachable <= 0:
        return ""
    return (
        f"{visible} tools are in your toolset; {reachable} more are reachable: "
        "call tool_search(query) then tool_call(name, arguments)"
    )


def principals_note() -> str:
    """One line naming the two principals this run acts between. Never "".

    The assistant is a SEPARATE Google account from the operator: its own
    address, its own calendar, its own Drive. Nothing in a run said so, and on
    2026-09-16 the model wrote the operator's itinerary to ``primary`` — its own
    calendar — added the operator as an attendee, and reported it as done. Every
    step was locally reasonable for an agent that believed "my calendar" and
    "the operator's calendar" were one thing.

    Both halves come from configuration, never a literal: the assistant's
    address from the declared ``channels.ai_email`` setting
    (``ROBOTHOR_AI_EMAIL``), the operator from ``~/.robothor/owner.yaml``. Read
    through ``get_settings`` rather than ``os.environ`` so the value has one
    definition and appears in ``genus config``.

    **Always returns a sentence, even with neither configured.** It used to
    return ``""`` there, which was wrong twice over:

    * the fact this line exists to carry — the two accounts are SEPARATE — is
      true whether or not either address is known, and a freshly installed
      instance is exactly where the model is most likely to assume "my
      calendar" and "the operator's calendar" name one thing. Saying nothing
      precisely when the instance is least configured is backwards.
    * and because ``AgentSession.start`` emits the engine-context turn only
      when there is something to put in it, an empty note SILENTLY CHANGED THE
      SHAPE OF THE WIRE: the turn vanished and every message index after the
      history shifted. That is a behaviour difference between one machine and
      another, and it is what made
      ``test_conversation_history_passed`` pass on a configured box and fail on
      CI with ``assert 'user' == 'developer'``.

    The addresses ENRICH the sentence; they were never a precondition for it.
    """
    assistant = ""
    try:
        from robothor.settings import get_settings

        assistant = (get_settings().channels.ai_email or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable setting is not a failed run
        logger.debug("assistant address unavailable for the principals note", exc_info=True)
    operator_name = ""
    operator_email = ""
    try:
        from robothor.owner_config import load_owner_config

        owner = load_owner_config()
        if owner is not None:
            operator_name = owner.full_name
            operator_email = owner.email or ""
    except Exception:  # noqa: BLE001 - a missing identity is not a failed run
        logger.debug("owner config unavailable for the principals note", exc_info=True)

    parts = []
    if assistant:
        parts.append(f"You are a separate principal with your own Google account ({assistant})")
    else:
        parts.append("You are a separate principal with your own Google account")
    who = " ".join(p for p in (operator_name, f"<{operator_email}>" if operator_email else "") if p)
    if who:
        parts.append(
            f"the operator is {who}. When the operator says 'my calendar', 'my email' or "
            "'my files' they mean theirs, not yours — work is only done for them when it "
            "landed in their account"
        )
    else:
        parts.append(
            "the operator's accounts are separate from yours — work is only done for them "
            "when it landed in their account"
        )
    return "; ".join(parts) + "."


def planner_tool_names(prepared: PreparedToolset) -> list[str]:
    """The tool list the PLANNER is shown.

    The planner used to receive every allowed name while the executing turn
    received the deferred subset, so it wrote plans naming tools that turn
    could not see. It gets the same set now, with the reachable ones marked:
    a plan step that needs ``gws_gmail_reply`` has to say how to reach it, or
    it is a plan for a different agent.
    """
    if not prepared.reachable_names:
        return list(prepared.tool_names)
    reachable = set(prepared.reachable_names)
    return [
        f"{name} (via tool_call)" if name in reachable else name for name in prepared.tool_names
    ]


def with_discovery_note(preamble: str, prepared: PreparedToolset) -> str:
    """The engine-context preamble, plus what the run has to be told.

    Two lines, either of which may be empty: who the two principals are, and —
    when the toolset is deferred — how to reach the rest of it.

    Its own turn (#547), never prepended to the operator's words: the run that
    did that had the main agent telling the operator, in every reply, that
    "your message carried a fake --- CURRENT USER --- header".
    """
    parts = [p for p in (preamble, principals_note(), prepared.discovery_note) if p]
    return "\n\n".join(parts)


def publish_toolset(registry: Any, agent_config: Any, tool_names: list[str]) -> tuple[Any, ...]:
    """Install this run's tool context vars; returns tokens for withdrawal.

    Two of them, and they are not the same thing:

    * the DEFERRED allow-set, set only when the toolset is deferred, is what
      ``tool_call`` checks — it is a security boundary, and setting it on a run
      that is not deferred would hand ``tool_call`` reach it is not meant to
      have;
    * the agent's TOOLSET, set on every run, gates nothing. It exists so
      ``tool_search`` can answer a non-deferred run with the agent's own tools
      instead of "only available on deferred runs", which it returned 18 times
      in one week to an agent that went on calling it.
    """
    from robothor.engine.tools.dispatch import set_agent_toolset, set_deferred_allowed

    defer_token = (
        set_deferred_allowed(registry.deferred_whitelist(agent_config))
        if registry.should_defer(agent_config)
        else None
    )
    return (defer_token, set_agent_toolset(frozenset(tool_names)))


def withdraw_toolset(tokens: tuple[Any, ...]) -> None:
    """Undo :func:`publish_toolset`. Safe to call with anything it returned."""
    from robothor.engine.tools.dispatch import clear_agent_toolset, clear_deferred_allowed

    defer_token, toolset_token = tokens
    if defer_token is not None:
        clear_deferred_allowed(defer_token)
    clear_agent_toolset(toolset_token)


async def prepare_toolset(
    registry: Any,
    agent_config: Any,
    *,
    agent_id: str,
    system_prompt: str,
    readonly_mode: bool,
    deep_plan: bool,
) -> PreparedToolset:
    """Load this agent's adapters, then pick and wrap its toolset."""
    await _load_adapters(registry, agent_config, agent_id)

    if not readonly_mode:
        schemas = registry.build_for_agent(agent_config)
        names = registry.get_tool_names(agent_config)
        advertised = {str(schema.get("function", {}).get("name", "")) for schema in schemas}
        reachable = tuple(n for n in names if n not in advertised)
        return PreparedToolset(
            tool_schemas=schemas,
            tool_names=names,
            system_prompt=system_prompt,
            reachable_names=reachable,
            discovery_note=deferred_toolset_note(len(schemas), len(reachable)),
        )

    from robothor.engine.prompts import (
        DEEP_PLAN_PREAMBLE,
        DEEP_PLAN_SUFFIX,
        PLAN_MODE_PREAMBLE,
        PLAN_MODE_SUFFIX,
    )

    tool_schemas = registry.build_readonly_for_agent(agent_config)
    tool_names = registry.get_readonly_tool_names(agent_config)

    if deep_plan:
        wrapped = DEEP_PLAN_PREAMBLE + system_prompt + DEEP_PLAN_SUFFIX
    else:
        # Name the tools the agent actually has: a plan written against tools
        # it cannot call is a plan that fails at execution time.
        tool_list = ", ".join(f"`{t}`" for t in sorted(tool_names)) if tool_names else "(none)"
        preamble = PLAN_MODE_PREAMBLE.replace("{tool_names_placeholder}", tool_list)
        wrapped = preamble + system_prompt + PLAN_MODE_SUFFIX

    return PreparedToolset(tool_schemas=tool_schemas, tool_names=tool_names, system_prompt=wrapped)


async def _load_adapters(registry: Any, agent_config: Any, agent_id: str) -> None:
    """Best-effort. A dead MCP server costs its own tools and nothing else."""
    try:
        from robothor.engine.adapters import get_adapters_for_agent
        from robothor.engine.mcp_client import configure_mcp_servers, register_adapter

        if agent_config.mcp_servers:
            configure_mcp_servers(agent_config.mcp_servers)

        adapters = get_adapters_for_agent(agent_id)
        for adapter in adapters:
            register_adapter(adapter)
        if adapters:
            await registry.register_adapter_tools(adapters)
    except Exception as e:  # noqa: BLE001
        logger.warning("Adapter loading failed (non-fatal): %s", _sanitize(e))

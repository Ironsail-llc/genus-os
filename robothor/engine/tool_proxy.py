"""The one way code inside the sandbox reaches a tool.

``execute_code`` exists because a research loop over fifty documents should
cost one model turn rather than fifty (measured 2026-09-16: the competing
harness imported its tool library inside a Python snippet on exactly the three
tasks this engine scored 0.000 on). The whole security question is what that
snippet is allowed to reach, and the answer here is: **nothing it could not
have asked for directly, through the same gates, in the same order.**

A proxied call is not a shortcut around admission. It runs
``_admit_tool_call`` — plan mode, ``tools_allowed``, the PRE_TOOL_USE hook, the
guardrail engine, the system-run RBAC check — and then ``registry.execute``,
which is where the repeat guard, the benchmark sandbox, the audit row and
post-condition verification already live. The snippet holds no credential, no
database handle and no registry reference; it holds a socket that speaks one
verb.

Three properties are structural rather than checked:

* **The allow-set is the agent's own.** It is the union of the schemas this run
  advertised and the deferred reach-set ``tool_call`` consults, which is
  exactly what the model could have invoked from a turn. A tool outside it is
  refused by the same gate that refuses it from a turn.
* **Ids are minted here.** The wire carries a tool name and arguments and
  nothing else, so a snippet cannot present a ``tool_call_id`` at all — there
  is no field to forge.
* **One proxied call at a time.** Every call takes an exclusive lock, so two
  writes cannot race through this path and the session's step counter cannot
  interleave. The win this tool exists for is turns, not tool latency, so the
  bound costs the design nothing.

Proxied calls earn a step row and an audit entry and do NOT earn a ``tool``
message: fifty rows in the ledger, one turn in the context. That asymmetry is
the feature.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, Protocol

from robothor.engine.tools.constants import SPAWN_TOOLS

if TYPE_CHECKING:
    from robothor.engine.tool_turn import ToolTurnRequest

logger = logging.getLogger(__name__)

__all__ = [
    "PROXY_DENIED_TOOLS",
    "RunToolProxy",
    "ToolProxy",
    "clear_tool_proxy",
    "get_tool_proxy",
    "proxy_allow_set",
    "set_tool_proxy",
]

#: Refused before admission is even asked, whatever the agent is granted.
#:
#: ``execute_code`` because a snippet that can start a snippet is an
#: unbounded recursion the call cap does not bound (each level gets its own
#: fresh cap). ``spawn_agent``/``spawn_agents`` because a spawn runs a whole
#: child runner INLINE in the parent's task, and doing that from inside a
#: subprocess's RPC handler nests two execution models with two deadlines.
#: ``ask_user`` because it blocks on a person, and a loop that can page the
#: operator two hundred times is a denial of service aimed at a human.
PROXY_DENIED_TOOLS: frozenset[str] = frozenset({"execute_code", "ask_user"}) | SPAWN_TOOLS


class ToolProxy(Protocol):
    """What the RPC server is handed. One verb, and a cap it reports."""

    max_calls: int
    calls_made: int
    #: The agent's own reach, materialised. Never empty-means-everything: the
    #: admission gate reads an empty set as "unrestricted", and a socket that
    #: inherited that reading would turn one convention into an allow-all.
    allowed: frozenset[str]

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


def proxy_allow_set(req: Any, registry: Any) -> frozenset[str]:
    """Exactly what the model could have called from a turn, as a real set.

    Three sources, in the order they bound each other:

    * the schemas this run advertised — ``tools_allowed`` after filtering;
    * the deferred reach-set, when the toolset is deferred, because that is
      what ``tool_call`` can invoke and a snippet must not have LESS reach than
      the meta-tool beside it, nor more;
    * and, only when the agent is unrestricted, every registered name. The
      admission gate spells "unrestricted" as an empty set; materialising it
      here means the proxy never has to carry that convention, and
      ``"exec" in proxy.allowed`` is a question with one answer.
    """
    from robothor.engine.tools.dispatch import get_deferred_allowed

    advertised = frozenset(getattr(req, "allowed_tool_set", frozenset()) or ())
    if advertised:
        return advertised | frozenset(get_deferred_allowed() or ())
    try:
        return frozenset(registry.registered_tool_names())
    except Exception:  # noqa: BLE001 - a registry that cannot answer grants nothing
        logger.debug("tool proxy could not materialise the unrestricted tool set")
        return frozenset()


_proxy_var: ContextVar[ToolProxy | None] = ContextVar("_genus_tool_proxy", default=None)


def set_tool_proxy(proxy: ToolProxy) -> Token[ToolProxy | None]:
    """Publish the proxy for the duration of one tool turn."""
    return _proxy_var.set(proxy)


def clear_tool_proxy(token: Token[ToolProxy | None]) -> None:
    """Withdraw it. Pair every set with one clear."""
    _proxy_var.reset(token)


def get_tool_proxy() -> ToolProxy | None:
    """The live proxy, or None when there is no run behind this call.

    None is how ``execute_code`` knows it was invoked from outside a run — a
    direct registry call, a test, a plugin — and must refuse rather than
    improvise an allow-set.
    """
    return _proxy_var.get()


class _SyntheticCall:
    """What admission expects where a provider's tool call would be.

    The id is minted here and nowhere else, which is what makes "a snippet
    cannot forge a ``tool_call_id``" a property of the wire format rather than
    a validation rule somebody has to keep.
    """

    __slots__ = ("function", "id")

    def __init__(self, tool_call_id: str) -> None:
        self.id = tool_call_id
        self.function = None


class RunToolProxy:
    """The live proxy: admission, dispatch and the ledger, for one turn's snippet."""

    def __init__(
        self,
        *,
        runner: Any,
        req: ToolTurnRequest,
        allowed: frozenset[str],
        max_calls: int,
        max_approvals: int = 1,
        batch_id: str = "",
    ) -> None:
        self._runner = runner
        self._req = req
        #: The agent's own reach — what the model could have called directly.
        #: A plain attribute rather than a property: the protocol declares it
        #: settable, and a read-only override is a narrower contract than the
        #: thing it claims to satisfy.
        self.allowed = allowed
        self.max_calls = max_calls
        self.calls_made = 0
        #: How many HUMAN-APPROVAL escalations this snippet may raise, and how
        #: many it has. A proxied call goes through the same guardrail gate a
        #: turn's call does, which is right — a snippet must not be able to
        #: bypass an approval — but it also means a loop could queue
        #: `max_calls` prompts at the operator, each blocking for
        #: `human_approval_timeout`. Escalation is a person's attention; the
        #: cap is on requests, not grants, because a refused prompt cost the
        #: same attention as an accepted one.
        self.max_approvals = max_approvals
        self.approvals_requested = 0
        self._lock = asyncio.Lock()
        #: Every step this snippet records shares one id, so the ledger can
        #: answer "which rows belong to that one `execute_code` call" without a
        #: column of its own. `batch_position` counts within it.
        self._batch_id = batch_id or str(uuid.uuid4())
        #: ``(tool name, evidence strings)`` per proxied call, so the handler
        #: can say how many responses the snippet never showed itself. Evidence
        #: strings, not payloads: the payload is already on the step row, and a
        #: second copy of two hundred responses in RAM is not a diagnostic.
        self.responses: list[tuple[str, tuple[str, ...]]] = []

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """One proxied tool call, through every gate a turn's call passes."""
        async with self._lock:
            if self.calls_made >= self.max_calls:
                return {
                    "error": (
                        f"execute_code tool-call limit reached ({self.max_calls}). "
                        "Narrow the loop, or do the remaining work in a second snippet."
                    ),
                    "guard": "execute_code_call_cap",
                }
            approval_refusal = self._approval_budget_refusal(name)
            if approval_refusal:
                return approval_refusal
            self.calls_made += 1
            return await self._dispatch(name, args)

    def _approval_budget_refusal(self, name: str) -> dict[str, Any] | None:
        """None unless this call would page a person once too often.

        Read from the agent's own ``human_approval_tools`` patterns — the same
        list the guardrail engine is configured from — so this answers the same
        question the gate will, one step earlier and without spending the
        prompt. A tool nobody asked for approval on is unaffected.
        """
        patterns = tuple(getattr(self._req.agent_config, "human_approval_tools", ()) or ())
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            return None
        if self.approvals_requested >= self.max_approvals:
            return {
                "error": (
                    f"'{name}' needs a person's approval, and this snippet has already "
                    f"asked for {self.approvals_requested}. Call it from a turn instead, "
                    "where the operator answers one question rather than a queue of them."
                ),
                "guard": "execute_code_approval_cap",
            }
        self.approvals_requested += 1
        return None

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        from robothor.engine.post_execution import apply_post_execution_guardrails
        from robothor.engine.runner import _resolve_tool_timeout

        req = self._req
        session = req.session
        position = self.calls_made - 1
        tc = _SyntheticCall(f"code_{uuid.uuid4().hex[:16]}")

        verdict = await self._runner._admit_tool_call(
            tc=tc,
            tool_name=name,
            tool_args=dict(args or {}),
            session=session,
            agent_config=req.agent_config,
            guardrail_engine=req.guardrail_engine,
            hook_registry=req.hook_registry,
            readonly_mode=req.readonly_mode,
            readonly_tool_set=req.readonly_tool_set,
            allowed_tool_set=self.allowed,
        )
        if not verdict.allowed:
            refusal = {"error": verdict.message, **verdict.output}
            session.record_tool_call(
                tool_name=name,
                tool_input=verdict.tool_args,
                tool_output=refusal,
                tool_call_id=tc.id,
                error_message=verdict.message,
                batch_id=self._batch_id,
                batch_position=position,
                append_message=False,
            )
            return refusal

        timeout = _resolve_tool_timeout(
            name, getattr(req.agent_config, "tool_timeout_seconds", 120)
        )
        started = asyncio.get_running_loop().time()
        result = await self._runner.registry.execute(
            name,
            verdict.tool_args,
            agent_id=req.agent_config.id,
            run_id=session.run.id,
            tenant_id=session.run.tenant_id,
            workspace=str(self._runner.config.workspace),
            user_id=session.run.user_id,
            user_role=session.run.user_role,
            timeout=timeout,
            accessible_tenant_ids=session.run.accessible_tenant_ids,
            task_author_override=req.agent_config.task_author_override,
            is_benchmark=session.run.is_benchmark,
            identity=getattr(session, "identity", None),
        )
        elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)

        error_msg = result.get("error") if isinstance(result, dict) else None
        # Redaction applies to a proxied result exactly as it does to a turn's:
        # a credential the snippet prints lands in stdout, which lands in the
        # model's context, which is the leak this guardrail exists to stop.
        result = apply_post_execution_guardrails(
            session,
            req.guardrail_engine,
            tool_name=name,
            result=result,
            error_msg=error_msg,
            state=req.guard_state,
        )

        session.record_tool_call(
            tool_name=name,
            tool_input=verdict.tool_args,
            tool_output=result,
            tool_call_id=tc.id,
            duration_ms=elapsed_ms,
            error_message=error_msg,
            batch_id=self._batch_id,
            batch_position=position,
            append_message=False,
        )
        from robothor.engine.act_observe import response_evidence

        self.responses.append((name, response_evidence(result)))
        # The snippet working is progress, even though no turn happened: a run
        # whose watchdog only counts turns would kill a five-minute loop.
        if self._runner._active_watchdog:
            self._runner._active_watchdog.touch(f"execute_code:{name}")
        return result if isinstance(result, dict) else {"result": result}

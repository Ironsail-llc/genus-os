"""Bounded candidate tools through a host-prepared native admission service.

Experimental: the host must bootstrap the manifest, identity, policies and
session. This does not implement that bootstrap or checkpoint continuation.
"""

from copy import deepcopy

from robothor.engine.runtime.contracts import RuntimeStoppedError
from robothor.engine.runtime.deadlines import require_time
from robothor.engine.tool_proxy import PROXY_DENIED_TOOLS, RunToolProxy, proxy_allow_set


class NativeGateway:
    def __init__(self, *, runner, turn, schemas, verify, max_calls=4):
        if turn.guardrail_engine is None:
            raise ValueError("native candidate gateway requires prepared guardrails")
        if not turn.session.run.user_role:
            raise ValueError("native candidate gateway requires a resolved principal role")
        if turn.session.run.agent_id != turn.agent_config.id:
            raise ValueError("native session and manifest identities differ")
        if max_calls < 1:
            raise ValueError("positive tool-call bound required")
        # This first bridge supports direct tools only. Deferred dispatch,
        # delegation and interactive approvals need their own lifecycle checks.
        excluded = PROXY_DENIED_TOOLS | {"tool_call", "tool_search"}
        allowed = proxy_allow_set(turn, runner.registry) - excluded
        self._schemas = deepcopy(schemas)
        names = [schema["function"]["name"] for schema in self._schemas]
        if not names or len(set(names)) != len(names) or not set(names) <= allowed:
            raise ValueError("candidate schemas exceed prepared direct-tool authority")
        self.turn, self._verify = turn, verify
        self._bound = False
        self._proxy = RunToolProxy(
            runner=runner,
            req=turn,
            allowed=frozenset(names),
            max_calls=max_calls,
            max_approvals=0,
        )

    def bind_run(self, run):
        """Bind the store's run before any dispatch, preserving trusted identity."""
        previous = self.turn.session.run
        if self._bound or previous.steps:
            raise ValueError("native candidate session must be fresh and bound once")
        fields = ("tenant_id", "user_id", "agent_id")
        if any(getattr(previous, key) != getattr(run, key) for key in fields):
            raise ValueError("candidate run differs from prepared native identity")
        run.user_role = previous.user_role
        run.accessible_tenant_ids = previous.accessible_tenant_ids
        run.is_benchmark = previous.is_benchmark
        self.turn.session.run = run
        self._bound = True

    @property
    def schemas(self):
        return deepcopy(self._schemas)

    @property
    def verified(self):
        # A trusted independent state check, never a model's success string.
        return self._bound and self._verify() is True

    def admit(self, tenant):
        require_time()
        session = self.turn.session
        if tenant != session.run.tenant_id:
            raise ValueError("native gateway tenant mismatch")
        if session._interrupt_requested or session.was_interrupted:
            raise RuntimeStoppedError("native session stopped")

    async def invoke(self, tenant, name, arguments):
        self.admit(tenant)
        if not self._bound:
            raise ValueError("native gateway has no persisted run binding")
        return await self._proxy.call(name, arguments)

"""Durable goal admission around the host's existing business tool gateway."""

import asyncio

from bench.runtime.candidates import admit_gateway


class GoalGateway:
    def __init__(self, host, ledger):
        self.host, self.ledger = host, ledger

    @property
    def schemas(self):
        return self.host.schemas

    @property
    def verified(self):
        return self.host.verified

    async def admit(self, tenant):
        if tenant != self.ledger.tenant:
            raise ValueError("goal gateway tenant mismatch")
        await asyncio.to_thread(self.ledger.assert_tool_authorized)
        await admit_gateway(self.host, tenant)

    async def invoke(self, tenant, name, arguments):
        await self.admit(tenant)
        return await self.host.invoke(tenant, name, arguments)

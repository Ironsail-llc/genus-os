"""Bind recoverable native child output to one work item and exact research inputs."""

import asyncio
import json

from robothor.operations.fragments import Fragments
from robothor.operations.store import Conflict, digest
from robothor.sales.models import Dossier
from robothor.sales.research_fanout import TOPICS


class ResearchRecovery:
    def __init__(self, operations, job, release_id, agent_id, context):
        self.store, self.job = Fragments(operations), job
        self.input_hash = self.inputs(release_id, agent_id, context)
        self.saved = {}

    @staticmethod
    def inputs(release_id, agent_id, context):
        return digest(
            json.loads(
                json.dumps(
                    {
                        "version": 1,
                        "release_id": release_id,
                        "agent_id": agent_id,
                        "context": context,
                        "output_schema": Dossier.model_json_schema(),
                    },
                    default=str,
                )
            )
        )

    async def load(self):
        self.saved = await asyncio.to_thread(self.store.read, self.job, self.input_hash)
        if set(self.saved) - {"plan", *TOPICS} or (self.saved and "plan" not in self.saved):
            raise Conflict("Unexpected partial research checkpoint")

    async def bind(self, fanout, *, release_id):
        if (
            fanout.tenant_id != self.store.ops.tenant
            or self.inputs(release_id, fanout.agent_id, fanout.context) != self.input_hash
        ):
            raise Conflict("Recovery belongs to a different native research stage")
        self.child_id = fanout.child_id
        plan = self.saved.get("plan")
        if plan:
            if set(plan) != {"buying_case", "child_id"} or plan["child_id"] != fanout.child_id:
                raise Conflict("Research worker changed since partial completion")
            fanout.buying_case = plan["buying_case"]
            for topic in TOPICS:
                if topic in self.saved:
                    await fanout.record(topic, self.saved[topic])
        fanout.recovery = self

    async def begin(self, buying_case):
        await asyncio.to_thread(
            self.store.put,
            self.job,
            "plan",
            self.input_hash,
            {
                "buying_case": buying_case,
                "child_id": self.child_id,
            },
        )

    async def save(self, topic, result):
        await asyncio.to_thread(self.store.put, self.job, topic, self.input_hash, result)

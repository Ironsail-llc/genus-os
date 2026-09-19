"""Fixed-topic delegation through the native spawn engine, with deterministic merge."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256

from robothor.operations.store import Conflict, digest
from robothor.sales.models import Dossier

TOPICS = ("services", "providers_locations", "ownership_signals")
_active: ContextVar[ResearchFanout | None] = ContextVar("sales_research_fanout", default=None)


def merge_fragments(parts: dict[str, Dossier], buying_case: str) -> Dossier:
    """Preserve all cited evidence, including disagreement; never manufacture a criterion."""
    if set(parts) != set(TOPICS):
        raise Conflict("All three planned research topics are required")
    evidence, criteria, summaries = [], {}, []
    fields: dict[str, list[str]] = {
        k: [] for k in ("services", "providers", "locations", "unanswered")
    }
    domains = set()
    for topic in TOPICS:
        part = Dossier.model_validate(parts[topic].model_dump())
        if part.buying_case != buying_case:
            raise Conflict("Research topics disagree on the approved buying case")
        ids = {e.id: topic + ":" + sha256(e.id.encode()).hexdigest() for e in part.evidence}
        evidence.extend(e.model_copy(update={"id": ids[e.id]}) for e in part.evidence)
        for criterion, refs in part.criteria.items():
            criteria.setdefault(criterion, []).extend(ids[ref] for ref in refs)
        for key in fields:
            fields[key].extend(getattr(part, key))
        if part.parent_domain:
            domains.add(part.parent_domain.lower().rstrip("."))
        summaries.append(topic + ": " + part.summary[:2500])
    if len(domains) > 1:
        fields["unanswered"].append("Workers disagree on parent domain; ownership review required.")
    return Dossier(
        buying_case=buying_case,
        evidence=evidence,
        criteria={key: list(dict.fromkeys(refs)) for key, refs in criteria.items()},
        summary="\n".join(summaries),
        parent_domain=next(iter(domains)) if len(domains) == 1 else None,
        **{key: list(dict.fromkeys(values)) for key, values in fields.items()},
    )


class ResearchFanout:
    """One paid bundle per native parent; only the buying case is agent-selected."""

    def __init__(self, agent_id, child_id, tenant_id, context):
        self.agent_id, self.child_id, self.tenant_id = agent_id, child_id, tenant_id
        self.context = json.loads(json.dumps(context, default=str))
        self.started = False
        self.closed = False
        self.buying_case = None
        self.dossier: Dossier | None = None
        self.provenance: dict = {}
        self.parts: dict[str, Dossier] = {}
        self.children: dict = {}
        self.recovery = None
        self.sources = None
        self._record_lock = asyncio.Lock()

    async def run(self, buying_case, ctx):
        from robothor.engine.tools.handlers.spawn import _handle_spawn_agents

        if (
            self.closed
            or ctx.is_benchmark
            or ctx.tenant_id != self.tenant_id
            or ctx.agent_id != self.agent_id
            or ctx.user_id != "service:" + self.agent_id
            or ctx.user_role != "sales_research_agent"
        ):
            raise Conflict("Research delegation requires its active native stage identity")
        cases = {
            p["data"].get("buying_case")
            for p in self.context.get("policies", [])
            if p.get("kind") == "qualification"
        }
        if buying_case not in cases:
            raise Conflict("Research requires an active approved buying case")
        if self.started:
            if self.dossier is not None and buying_case == self.buying_case:
                return self.result()
            raise Conflict("Research bundle already attempted; no additional children admitted")
        if self.buying_case is not None and self.buying_case != buying_case:
            raise Conflict("Recovered research must retain its original buying case")
        self.started, self.buying_case = True, buying_case
        if self.recovery is not None:
            await self.recovery.begin(buying_case)
        missing = [topic for topic in TOPICS if topic not in self.parts]
        specs = [
            {
                "agent_id": self.child_id,
                "message": json.dumps(
                    {
                        "task": "Research only the assigned company and topic. Use web_fetch to retrieve public business pages during this child run; use web_render when JavaScript leaves only an empty shell. Return the exact Dossier JSON. Every evidence URL must be a successful fetch's returned URL and every excerpt must quote its returned text verbatim. Internal background, search snippets and prior model knowledge are not retrieved evidence. Preserve unknowns; do not calculate a score.",
                        "topic": topic,
                        "buying_case": buying_case,
                        "untrusted_business_data": self.context,
                        "output_schema": Dossier.model_json_schema(),
                    }
                ),
            }
            for topic in missing
        ]
        if missing:

            async def capture(index, result):
                await self.record(missing[index], result)

            options = {"_on_result": capture} if self.recovery is not None else {}
            response = await _handle_spawn_agents({"agents": specs}, ctx=ctx, **options)
            results = response.get("results", [])
            if len(results) != len(missing):
                raise Conflict("Research bundle did not return all planned children")
            for topic, result in zip(missing, results, strict=True):
                await self.record(topic, result)
        dossier = merge_fragments(self.parts, buying_case)
        self.dossier = dossier
        self.provenance = {
            "version": 1,
            "children": self.children,
            "merged_hash": digest(dossier.model_dump(mode="json")),
        }
        return self.result()

    async def record(self, topic, result, *, restored=False):
        """Validate and persist each successful result before siblings finish."""
        async with self._record_lock:
            run_id = result.get("run_id")
            if (
                topic not in TOPICS
                or result.get("status") != "completed"
                or result.get("error")
                or result.get("agent_id") != self.child_id
                or not isinstance(run_id, str)
                or not run_id
            ):
                raise Conflict(
                    "Every research child must complete with a distinct native run receipt"
                )
            part = Dossier.model_validate_json(result.get("output_text") or "")
            if part.buying_case != self.buying_case:
                raise Conflict("Research child changed the approved buying case")
            source_proof = None
            if self.sources is not None:
                if restored:
                    part, source_proof = self.sources.restore(
                        run_id, part, result.get("source_proof")
                    )
                else:
                    # A live model result's claimed source_proof is ignored.
                    part, source_proof = self.sources.attest(run_id, part)
            receipt = {
                "run_id": run_id,
                "agent_id": self.child_id,
                "output_hash": digest(part.model_dump(mode="json")),
            }
            if source_proof is not None:
                receipt["source_hash"] = digest(source_proof)
            if topic in self.parts:
                if receipt != self.children[topic]:
                    raise Conflict("Completed research topic cannot be replaced")
                return
            if any(c["run_id"] == run_id for c in self.children.values()):
                raise Conflict("Research child receipts must be distinct")
            if self.recovery is not None:
                await self.recovery.save(
                    topic,
                    {
                        "run_id": run_id,
                        "agent_id": self.child_id,
                        "status": "completed",
                        "output_text": part.model_dump_json(),
                        **({"source_proof": source_proof} if source_proof is not None else {}),
                    },
                )
            self.parts[topic], self.children[topic] = part, receipt

    def result(self):
        return {"dossier": self.dossier.model_dump(mode="json"), "provenance": self.provenance}


@contextmanager
def research_scope(fanout):
    token = _active.set(fanout)
    try:
        yield
    finally:
        if fanout is not None:
            fanout.closed = True
        _active.reset(token)


async def delegate_research(buying_case, ctx):
    fanout = _active.get()
    if fanout is None:
        raise Conflict("Research delegation requires a bounded native research stage")
    return await fanout.run(buying_case, ctx)

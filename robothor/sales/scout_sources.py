"""Bind native discovery candidates to actual search and page-read results."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from robothor.operations.store import Conflict, digest
from robothor.sales.models import CandidateBatch
from robothor.sales.research_sources import valid_url


def url_key(url):
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def domain(url):
    return urlsplit(url).hostname.lower().rstrip(".").removeprefix("www.")


class ScoutSources:
    def __init__(self, tenant_id, agent_id, limit):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise Conflict("Native scout requires a bounded candidate allowance")
        self.tenant_id, self.agent_id, self.limit = tenant_id, agent_id, limit
        self.runs = {}

    def observe(self, name, args, result, ctx):
        if (
            ctx.tenant_id != self.tenant_id
            or ctx.agent_id != self.agent_id
            or not ctx.run_id
            or not isinstance(result, dict)
            or "error" in result
        ):
            return
        if name == "web_search" and isinstance(result.get("results"), list):
            urls = [
                r["url"]
                for r in result["results"][:100]
                if isinstance(r, dict) and valid_url(r.get("url"))
            ]
        elif (
            name == "web_fetch"
            and type(result.get("status")) is int
            and 200 <= result["status"] < 300
            and valid_url(result.get("url"))
        ):
            urls = [result["url"]]
        else:
            return
        observations = self.runs.setdefault(str(ctx.run_id), [])
        if len(observations) >= 32:
            raise Conflict("Native scout observation limit reached")
        observations.append(
            {
                "tool": name,
                "urls": urls,
                "query": str(args.get("query", ""))[:2000] if name == "web_search" else None,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "degraded": bool(result.get("degraded")),
            }
        )

    @staticmethod
    def ready(rows):
        searches = [o for o in rows if o["tool"] == "web_search"]
        queries = {" ".join(o["query"].lower().split()) for o in searches}
        return any(not o["degraded"] for o in searches) or len(queries) >= 3

    def searched(self):
        return any(self.ready(rows) for rows in self.runs.values())

    def attest(self, run_id, text):
        rows = self.runs.get(str(run_id), [])
        if not any(o["tool"] == "web_search" for o in rows):
            raise Conflict("Scout must complete an actual search before returning candidates")
        if not self.ready(rows):
            raise Conflict(
                "Refine degraded results with shorter service-and-geography queries; up to three distinct searches are required when results remain degraded"
            )
        batch = CandidateBatch.model_validate_json(text or "")
        if len(batch.companies) > self.limit:
            raise Conflict("Scout exceeded the supplied candidate allowance")
        urls = {url_key(url) for o in rows for url in o["urls"]}
        domains = {domain(url) for url in urls}
        selected = set()
        for candidate in batch.companies:
            host = domain(candidate.website)
            if url_key(candidate.source_url) not in urls or host not in domains:
                raise Conflict(
                    "Candidate source URL and website domain must appear in this run's actual search or page-read results"
                )
            if host in selected:
                raise Conflict("Scout candidates must have distinct business domains")
            selected.add(host)
        return batch, {
            "version": 1,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "run_id": str(run_id),
            "observations": rows,
            "output_hash": digest(batch.model_dump(mode="json")),
        }


@contextmanager
def scout_scope(stage, tenant_id, agent_id, message):
    if stage != "scout":
        yield None
        return
    from robothor.engine.output_validation import output_validation_scope
    from robothor.engine.required_tool import required_tool_scope
    from robothor.engine.response_schema import response_schema_scope
    from robothor.engine.tool_observation import tool_observation_scope

    context = json.loads(message)["untrusted_business_data"]
    sources = ScoutSources(tenant_id, agent_id, context["max_companies"])

    def validate(run, text):
        if run.tenant_id != tenant_id or run.agent_id != agent_id:
            return "Scout output identity does not match the owning run"
        try:
            sources.attest(str(run.id), text)
        except Conflict as exc:
            return str(exc)
        except ValueError:
            return "Return CandidateBatch JSON with companies, not search-tool arguments"
        return None

    with (
        required_tool_scope("web_search", lambda: not sources.searched()),
        tool_observation_scope(sources.observe, names={"web_search", "web_fetch"}),
        response_schema_scope(
            "candidate_batch", CandidateBatch.model_json_schema(), ready=sources.searched
        ),
        output_validation_scope(validate),
    ):
        yield sources

"""Match citations to successful native retrievals belonging to the emitting child."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from unicodedata import normalize
from urllib.parse import urlsplit

from robothor.operations.store import Conflict, digest
from robothor.sales.models import Dossier
from robothor.sales.research_contract import ResearchDossier, passages

MAX_SOURCES = 32
MAX_CONTENT = 8000  # Native web_fetch's returned text limit.


def text_key(text):
    return " ".join(normalize("NFC", text).split())


def content_hash(text):
    return sha256(text.encode()).hexdigest()


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except (ValueError, TypeError):
        return False


class ResearchSources:
    def __init__(self, tenant_id, agent_id, first_read_tool="web_fetch"):
        if first_read_tool not in {"web_fetch", "web_render"}:
            raise ValueError("Research requires an available page retrieval tool")
        self.tenant_id, self.agent_id = tenant_id, agent_id
        self.first_read_tool = first_read_tool
        self.runs: dict[str, list[dict]] = {}

    @contextmanager
    def child_scope(self, index):
        """A fresh native child must attempt retrieval before answering from context."""
        from robothor.engine.output_validation import output_validation_scope
        from robothor.engine.required_tool import required_tool_scope
        from robothor.engine.response_schema import response_schema_scope
        from robothor.engine.tool_observation import tool_observation_scope

        attempted = False

        def observe(name, args, result, ctx):
            nonlocal attempted
            if (
                name in {"web_fetch", "web_render"}
                and ctx.tenant_id == self.tenant_id
                and ctx.agent_id == self.agent_id
                and ctx.run_id
            ):
                # A blocked page must allow the model to choose another page or
                # the renderer; source attestation still requires success.
                attempted = True
            return self.observe(name, args, result, ctx)

        def validate(run, text):
            from pydantic import ValidationError

            if run.tenant_id != self.tenant_id or run.agent_id != self.agent_id:
                return "Research output identity does not match its owning child"
            try:
                self.attest_output(str(run.id), text or "")
            except ValidationError:
                return "Return the exact ResearchDossier schema with captured source_ref and passage_ref for each evidence item; do not write URLs, quotations or retrieval dates"
            except Conflict as exc:
                return str(exc)
            return None

        with (
            output_validation_scope(validate),
            required_tool_scope(self.first_read_tool, lambda: not attempted),
            tool_observation_scope(observe, names={"web_fetch", "web_render"}, annotations=True),
            response_schema_scope(
                "research_dossier", ResearchDossier.model_json_schema(), ready=lambda: attempted
            ),
        ):
            yield

    def observe(self, name, args, result, ctx):
        """Only the dispatcher supplies this callback; model output cannot supply sources."""
        if (
            name not in {"web_fetch", "web_render"}
            or ctx.tenant_id != self.tenant_id
            or ctx.agent_id != self.agent_id
            or not ctx.run_id
            or not isinstance(result, dict)
            or "error" in result
            or type(result.get("status")) is not int
            or not 200 <= result["status"] < 300
            or not valid_url(result.get("url"))
            or not isinstance(result.get("content"), str)
            or not text_key(result["content"])
            or len(result["content"]) > MAX_CONTENT
        ):
            return None
        rows = self.runs.setdefault(str(ctx.run_id), [])
        if len(rows) >= MAX_SOURCES:
            return None
        rows.append(
            {
                "tool": name,
                "url": result["url"],
                "content": result["content"],
                "content_hash": content_hash(result["content"]),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "passage_version": 2,
            }
        )
        return self.packet(str(ctx.run_id), rows[-1])

    def packet(self, run_id, source):
        version = source.get("passage_version", 1)
        identity = [
            self.tenant_id,
            self.agent_id,
            run_id,
            source["url"],
            source["content_hash"],
            source["retrieved_at"],
        ]
        if version != 1:
            identity.append(version)
        return {
            "kind": f"captured_passages_v{version}",
            "trust": "untrusted_website_content",
            "source_ref": digest(identity)[:24],
            "passages": passages(source["content"], version=version),
        }

    def _resolve(self, run_id, selection, sources):
        available = {}
        for source in sources:
            packet = self.packet(run_id, source)
            available[packet["source_ref"]] = (
                source,
                {p["ref"]: p["text"] for p in packet["passages"]},
            )
        evidence = []
        for index, item in enumerate(selection.evidence):
            match = available.get(item.source_ref)
            if match is None:
                raise Conflict(f"evidence[{index}].source_ref is not a capture from this child")
            source, excerpts = match
            if item.passage_ref not in excerpts:
                raise Conflict(f"evidence[{index}].passage_ref is not in the selected capture")
            evidence.append(
                {
                    **item.model_dump(exclude={"source_ref", "passage_ref"}),
                    "url": source["url"],
                    "excerpt": excerpts[item.passage_ref],
                    "retrieved_at": source["retrieved_at"],
                }
            )
        return Dossier.model_validate(
            {
                **selection.model_dump(exclude={"evidence"}),
                "evidence": evidence,
            }
        )

    def attest_output(self, run_id, output):
        selection = (
            ResearchDossier.model_validate_json(output)
            if isinstance(output, str)
            else ResearchDossier.model_validate(output)
        )
        sources = deepcopy(self.runs.get(run_id, []))
        resolved = self._resolve(run_id, selection, sources)
        dossier, proof = self.attest(run_id, resolved)
        proof.update(version=2, selection=selection.model_dump(mode="json", exclude_unset=True))
        return dossier, proof

    def _match(self, dossier, sources):
        if not sources:
            raise Conflict("Research requires a successful retrieval by the same native child")
        part = Dossier.model_validate(dossier.model_dump(mode="json"))
        if not part.evidence and not any(item.strip() for item in part.unanswered):
            raise Conflict("Research without evidence must explain explicit unknowns in unanswered")
        facts = {e.id: e for e in part.evidence}
        for criterion, references in part.criteria.items():
            if any(
                facts[ref].field != criterion or type(facts[ref].value) is not bool
                for ref in references
            ):
                raise Conflict(
                    "Scored criteria require matching boolean evidence; put contextual text outside criteria"
                )
        problems = []
        for index, evidence in enumerate(part.evidence):
            quote = text_key(evidence.excerpt)
            pages = [s for s in sources if s["url"] == str(evidence.url)]
            matches = [s for s in pages if quote and quote in text_key(s["content"])]
            if not matches:
                field = "excerpt" if pages else "url"
                problems.append(f"evidence[{index}].{field}")
                continue
            matched = next(
                (s for s in matches if s["retrieved_at"] == evidence.retrieved_at.isoformat()),
                matches[0],
            )
            evidence.retrieved_at = datetime.fromisoformat(matched["retrieved_at"])
        if problems:
            # Only engine-generated field positions enter correction feedback;
            # never replay a model's IDs, quotations or URLs as instructions.
            raise Conflict(
                "Research citation validation failed at "
                + ", ".join(problems[:8])
                + (" (additional mismatches omitted)" if len(problems) > 8 else "")
                + ". Use a URL returned by this child's successful retrieval and copy a short, "
                "continuous verbatim passage from its content. Do not join separated sentences, "
                "headings or list items. Correct or remove unsupported evidence and its criterion references."
            )
        return part

    def attest(self, run_id, dossier):
        sources = deepcopy(self.runs.get(run_id, []))
        part = self._match(dossier, sources)
        return part, {
            "version": 1,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "run_id": run_id,
            "sources": sources,
            "output_hash": digest(part.model_dump(mode="json")),
        }

    def restore(self, run_id, dossier, proof):
        """Recheck trusted immutable fragments; never accept proof from a live model result."""
        try:
            if (
                not isinstance(proof, dict)
                or proof.get("version") not in {1, 2}
                or proof.get("tenant_id") != self.tenant_id
                or proof.get("agent_id") != self.agent_id
                or proof.get("run_id") != run_id
                or not isinstance(proof.get("sources"), list)
                or not 1 <= len(proof["sources"]) <= MAX_SOURCES
                or proof.get("output_hash") != digest(dossier.model_dump(mode="json"))
            ):
                raise ValueError
            for source in proof["sources"]:
                if (
                    not valid_url(source["url"])
                    or not isinstance(source["content"], str)
                    or not 1 <= len(source["content"]) <= MAX_CONTENT
                    or source["content_hash"] != content_hash(source["content"])
                    or type(source.get("passage_version", 1)) is not int
                    or source.get("passage_version", 1) not in {1, 2}
                ):
                    raise ValueError
                when = datetime.fromisoformat(source["retrieved_at"])
                if when.tzinfo is None or when.utcoffset() is None:
                    raise ValueError
            resolved = (
                self._resolve(
                    run_id, ResearchDossier.model_validate(proof["selection"]), proof["sources"]
                )
                if proof["version"] == 2
                else dossier
            )
            part = self._match(resolved, proof["sources"])
            if part != dossier:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Conflict("Saved research lacks valid native retrieval evidence") from None
        return part, deepcopy(proof)

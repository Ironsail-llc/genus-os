"""Attest literal public email addresses before native contact verification work."""

import re
from contextlib import contextmanager
from datetime import UTC, datetime

from robothor.operations.store import Conflict, digest
from robothor.sales.models import ContactBatch
from robothor.sales.research_contract import passages
from robothor.sales.research_sources import MAX_CONTENT, content_hash, valid_url
from robothor.sales.scout_sources import url_key


def address_pattern(email):
    return re.compile(
        r"(?<![\w.!#$%&'*+/=?^`{|}~@-])" + re.escape(email) + r"(?![\w@-]|\.[\w-])", re.IGNORECASE
    )


def validate_checkpoint(checkpoint, batch, tenant_id, agent_id, release_id):
    """Reject old/foreign cached contact output before any enrichment work."""
    proof = (checkpoint.get("provenance") or {}).get("contact_sources", {})
    if (
        proof.get("version") != 1
        or proof.get("tenant_id") != tenant_id
        or proof.get("agent_id") != agent_id
        or proof.get("run_id") != checkpoint.get("run_id")
        or not proof.get("run_id")
        or checkpoint.get("fleet_release_id") != release_id
        or proof.get("output_hash") != digest(batch.model_dump(mode="json"))
        or type(proof.get("pages_read")) is not int
        or not 1 <= proof["pages_read"] <= 32
        or not isinstance(proof.get("contacts"), list)
        or len(proof["contacts"]) != len(batch.contacts)
    ):
        raise Conflict("Current native contact source proof required; research this contact again")
    for contact, source in zip(batch.contacts, proof["contacts"], strict=True):
        if (
            not isinstance(source, dict)
            or source.get("email") != contact.email
            or not valid_url(source.get("source_url"))
            or not valid_url(contact.source_url)
            or url_key(source["source_url"]) != url_key(contact.source_url)
            or not isinstance(source.get("excerpt"), str)
            or not 1 <= len(source["excerpt"]) <= 800
            or not address_pattern(contact.email).search(source["excerpt"])
        ):
            raise Conflict("Contact checkpoint does not match its captured address")


class ContactSources:
    def __init__(self, tenant_id, agent_id):
        self.tenant_id, self.agent_id = tenant_id, agent_id
        self.runs = {}

    def observe(self, name, args, result, ctx):
        if (
            name != "web_fetch"
            or ctx.tenant_id != self.tenant_id
            or ctx.agent_id != self.agent_id
            or not ctx.run_id
            or not isinstance(result, dict)
            or "error" in result
            or type(result.get("status")) is not int
            or not 200 <= result["status"] < 300
            or not valid_url(result.get("url"))
            or not isinstance(result.get("content"), str)
            or not result["content"].strip()
        ):
            return
        rows = self.runs.setdefault(str(ctx.run_id), [])
        if len(rows) >= 32:
            raise Conflict("Native contact source limit reached")
        content = result["content"][:MAX_CONTENT]
        rows.append(
            {
                "url": result["url"],
                "retrieved_at": datetime.now(UTC).isoformat(),
                "content_hash": content_hash(content),
                "passages": passages(content, version=2),
            }
        )

    def ready(self):
        return any(self.runs.values())

    def attest(self, run_id, text):
        rows = self.runs.get(str(run_id), [])
        if not rows:
            raise Conflict("Contact research must read an actual public business page")
        batch = ContactBatch.model_validate_json(text or "")
        selected, emails = [], set()
        for contact in batch.contacts:
            if not valid_url(contact.source_url) or contact.email in emails:
                raise Conflict("Contact sources must be public URLs and emails must be distinct")
            # Match a whole address, not a substring of another mailbox/domain.
            pattern = address_pattern(contact.email)
            matches = [
                (row, passage)
                for row in rows
                if url_key(row["url"]) == url_key(contact.source_url)
                for passage in row["passages"]
                if pattern.search(passage["text"])
            ]
            if not matches:
                raise Conflict(
                    "Every contact email must appear literally on its source page fetched in this run; do not guess addresses or substitute search snippets"
                )
            row, passage = matches[-1]
            selected.append(
                {
                    "email": contact.email,
                    "source_url": row["url"],
                    "retrieved_at": row["retrieved_at"],
                    "content_hash": row["content_hash"],
                    "passage_ref": passage["ref"],
                    "excerpt": passage["text"],
                }
            )
            emails.add(contact.email)
        return batch, {
            "version": 1,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "run_id": str(run_id),
            "pages_read": len(rows),
            "contacts": selected,
            "output_hash": digest(batch.model_dump(mode="json")),
        }


@contextmanager
def contact_scope(stage, tenant_id, agent_id):
    if stage != "contacts":
        yield None
        return
    from robothor.engine.output_validation import output_validation_scope
    from robothor.engine.required_tool import required_tool_scope
    from robothor.engine.response_schema import response_schema_scope, set_tool_format_deferred
    from robothor.engine.tool_observation import tool_observation_scope

    sources = ContactSources(tenant_id, agent_id)

    def validate(run, text):
        if run.tenant_id != tenant_id or run.agent_id != agent_id:
            return "Contact output identity does not match the owning run"
        try:
            sources.attest(str(run.id), text)
        except Conflict as exc:
            set_tool_format_deferred(True)
            return str(exc)
        except ValueError:
            set_tool_format_deferred(False)
            return "Return one ContactBatch JSON object with unknown verification, without markdown or commentary"
        return None

    with (
        required_tool_scope("web_fetch", lambda: not sources.ready()),
        tool_observation_scope(sources.observe, names={"web_fetch"}),
        response_schema_scope(
            "contact_batch",
            ContactBatch.model_json_schema(),
            ready=sources.ready,
            defer_for_tools=True,
        ),
        output_validation_scope(validate),
    ):
        yield sources

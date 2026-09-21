"""Reviewed public business contact identity, separate from email verification."""

from uuid import uuid4

from psycopg2.extras import Json
from pydantic import Field, field_validator

from robothor.operations.store import Conflict, digest
from robothor.sales.models import Contact, Contract, Evidence
from robothor.sales.service import operator


def identity_hash(data):
    return digest({key: data[key] for key in ("name", "role", "email", "source_url")})


class ManualContact(Contract):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    email: str
    source_url: str

    @field_validator("email")
    @classmethod
    def address(cls, value):
        return Contact.email_address(value)

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        return Evidence.public_url(value)


class ContactReview(Contract):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    source_url: str | None = None
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=10, max_length=2000)

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        return Evidence.public_url(value) if value is not None else None


class ContactEntry(Contract):
    contact: ManualContact
    reason: str = Field(min_length=10, max_length=2000)


class Contacts:
    def __init__(self, sales):
        self.sales, self.tenant = sales, sales.tenant

    def list(self, prospect_id):
        with self.sales.ops.transaction() as cur:
            self.sales.require(prospect_id, cur)
            cur.execute(
                "SELECT c.*,a.actor AS reviewed_by,a.detail AS review_detail,a.created_at AS reviewed_at FROM sales_contacts c LEFT JOIN LATERAL(SELECT actor,detail,created_at FROM operation_audit WHERE tenant_id=c.tenant_id AND entity_id=c.id::text AND event='contact.identity_reviewed' ORDER BY id DESC LIMIT 1) a ON true WHERE c.tenant_id=%s AND c.prospect_id=%s ORDER BY c.email",
                (self.tenant, prospect_id),
            )
            result = []
            for row in cur.fetchall():
                row = dict(row)
                row["identity_hash"] = identity_hash(row["data"])
                row["review"] = {
                    "actor": row.pop("reviewed_by"),
                    "detail": row.pop("review_detail"),
                    "at": row.pop("reviewed_at"),
                }
                row["review"]["current"] = (row["review"]["detail"] or {}).get(
                    "identity_hash"
                ) == row["identity_hash"]
                result.append(row)
            return result

    def add(self, prospect_id, data, actor, reason):
        operator(actor)
        entry = ContactEntry(contact=data, reason=reason)
        contact = entry.contact.model_dump(mode="json")
        with self.sales.ops.transaction() as cur:
            self.sales.require(prospect_id, cur)
            cur.execute(
                "SELECT id,data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s AND email=%s",
                (self.tenant, prospect_id, contact["email"]),
            )
            existing = cur.fetchone()
            if existing:
                if identity_hash(existing["data"]) != identity_hash(contact):
                    raise Conflict("Contact already exists; review its current identity instead")
                contact_id = str(existing["id"])
            else:
                contact_id = self.sales.add_contact(prospect_id, contact, cur=cur)
                self.sales.ops.enqueue(
                    "sales.verify",
                    "manual:" + contact_id,
                    {
                        "prospect_id": str(prospect_id),
                        "contact_id": contact_id,
                        "email": contact["email"],
                    },
                    cur=cur,
                )
                self.sales.ops.audit(
                    cur,
                    contact_id,
                    "contact.identity_reviewed",
                    actor,
                    {
                        "reason": reason,
                        "identity_hash": identity_hash(contact),
                        "source_url": contact["source_url"],
                    },
                )
        return next(c for c in self.list(prospect_id) if str(c["id"]) == contact_id)

    def review(
        self, prospect_id, contact_id, *, expected_hash, name, role, actor, reason, source_url=None
    ):
        operator(actor)
        ContactReview(
            expected_hash=expected_hash, name=name, role=role, reason=reason, source_url=source_url
        )
        with self.sales.ops.transaction() as cur:
            self.sales.require(prospect_id, cur)
            cur.execute(
                "SELECT * FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s AND id=%s FOR UPDATE",
                (self.tenant, prospect_id, contact_id),
            )
            row = cur.fetchone()
            if not row or identity_hash(row["data"]) != expected_hash:
                raise Conflict("Contact identity changed; reload before review")
            data = Contact.model_validate(
                row["data"]
                | {
                    "name": name,
                    "role": role,
                    "source_url": source_url or row["data"]["source_url"],
                }
            ).model_dump(mode="json")
            cur.execute(
                "UPDATE sales_contacts SET data=%s WHERE tenant_id=%s AND id=%s",
                (Json(data), self.tenant, contact_id),
            )
            first, _, last = name.partition(" ")
            cur.execute(
                "UPDATE crm_people SET first_name=%s,last_name=%s,job_title=%s WHERE tenant_id=%s AND id=%s",
                (first, last, role, self.tenant, row["person_id"]),
            )
            cur.execute(
                "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND payload->>'prospect_id'=%s AND payload->>'recipient'=%s AND status IN ('review','approved')",
                (self.tenant, str(prospect_id), row["email"]),
            )
            self.sales.ops.enqueue(
                "sales.stop", str(uuid4()), {"prospect_id": str(prospect_id)}, cur=cur
            )
            self.sales.ops.audit(
                cur,
                contact_id,
                "contact.identity_reviewed",
                actor,
                {
                    "reason": reason,
                    "identity_hash": identity_hash(data),
                    "previous_hash": expected_hash,
                    "source_url": data["source_url"],
                },
            )
        return next(c for c in self.list(prospect_id) if str(c["id"]) == str(contact_id))

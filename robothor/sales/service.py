"""The sales domain, shared by agents and authenticated operator interfaces."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg2.extras import Json

from robothor.operations.store import Conflict, Operations, digest
from robothor.sales.models import (
    Contact,
    Dossier,
    Draft,
    Message,
    Outcome,
    QualificationPolicy,
    SalesSettings,
)


def operator(actor):
    """Require a caller authenticated as a human by the interface boundary."""
    if not actor or not actor.startswith("operator:"):
        raise Conflict("Human operator required")


def domain_of(url):
    """Normalize a business URL, preserving subdomains rather than merging brands."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Business website URL required")
    host = parsed.hostname.lower().rstrip(".")
    return host.removeprefix("www.")


class Sales:
    """Domain writes and their follow-on work are committed together."""

    def __init__(self, tenant_id):
        self.ops = Operations(tenant_id)
        self.tenant = tenant_id

    def configure(self, config, actor, *, expected_revision=None, reason=None):
        """Set explicit switches. Unconfigured instances have no active capabilities."""
        operator(actor)
        if "library_revision" in config:
            raise Conflict("Library revision is managed by the sales service")
        if expected_revision is not None:
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError("Nonnegative settings revision required")
            if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
                raise ValueError("Settings review reason required")
        with self.ops.transaction() as cur:
            # Row locks cannot serialize the first configuration when no row
            # exists yet. Preserve concurrent partial operator changes as well.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (self.tenant + ":sales-settings",),
            )
            cur.execute(
                "SELECT config,revision FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
                (self.tenant,),
            )
            previous = cur.fetchone()
            revision = previous["revision"] if previous else 0
            if expected_revision is not None and expected_revision != revision:
                raise Conflict("Settings changed; refresh and review the current values")
            from robothor.sales.deployment import assert_structure_editable

            assert_structure_editable(cur, self, previous["config"] if previous else {}, config)
            merged = SalesSettings.model_validate(
                {**(previous["config"] if previous else {}), **config}
            ).model_dump(mode="json")
            self._validate_library_selection(config, merged, cur)
            old = previous["config"] if previous else {}
            from robothor.sales.setup import sender_context_hash

            senders = set(old.get("senders", []) + merged.get("senders", []))
            for sender in sorted(senders):
                if sender_context_hash(old, sender) == sender_context_hash(merged, sender):
                    continue
                cur.execute(
                    "SELECT 1 FROM operation_actions WHERE tenant_id=%s AND kind='sales.email' AND payload->>'sender'=%s LIMIT 1",
                    (self.tenant, sender),
                )
                if cur.fetchone():
                    cur.execute(
                        "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND payload->>'sender'=%s AND status IN ('review','approved')",
                        (self.tenant, sender),
                    )
                    self.ops.enqueue("sales.stop", str(uuid4()), {"sender": sender}, cur=cur)
            library_changed = any(
                key in config and old.get(key, default) != merged[key]
                for key, default in (
                    ("active_knowledge_version", ""),
                    ("active_policy_versions", {}),
                )
            )
            if library_changed:
                merged["library_revision"] = old.get("library_revision", 0) + 1
                cur.execute(
                    "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' AND status IN ('review','approved')",
                    (self.tenant,),
                )
                cur.execute(
                    "SELECT 1 FROM operation_actions WHERE tenant_id=%s AND kind='sales.email' LIMIT 1",
                    (self.tenant,),
                )
                if cur.fetchone():
                    self.ops.enqueue(
                        "sales.stop",
                        str(uuid4()),
                        {"library_revision_before": merged["library_revision"] - 1},
                        cur=cur,
                    )
            cur.execute(
                "INSERT INTO sales_settings(tenant_id,config,revision) VALUES(%s,%s,1) "
                "ON CONFLICT(tenant_id) DO UPDATE SET config=EXCLUDED.config,revision=sales_settings.revision+1,updated_at=now()",
                (self.tenant, Json(merged)),
            )
            detail = {"fields": sorted(config), "revision": revision + 1}
            if expected_revision is not None:
                detail.update(reason=reason.strip(), expected_revision=expected_revision)
                detail["changes"] = {
                    key: {
                        "before": (previous["config"] if previous else {}).get(key),
                        "after": merged[key],
                    }
                    for key in config
                }
            self.ops.audit(cur, self.tenant, "sales.configured", actor, detail)
            if (
                config.get("sending_enabled") is False
                and previous
                and previous["config"].get("sending_enabled") is True
            ):
                self.ops.enqueue("sales.stop", str(uuid4()), {"scope": "all"}, cur=cur)

    def settings(self):
        return self.settings_snapshot()["config"]

    def require_request_open(self, prospect_id, *, cur=None):
        from robothor.sales.requests import Requests

        if cur is None:
            with self.ops.transaction() as cursor:
                return self.require_request_open(prospect_id, cur=cursor)
        return Requests(self).guard({"prospect_id": str(prospect_id)}, cur=cur)

    def settings_snapshot(self):
        """Read values and their review revision from the same database row."""
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT config,revision FROM sales_settings WHERE tenant_id=%s", (self.tenant,)
            )
            row = cur.fetchone()
            return dict(row) if row else {"config": {}, "revision": 0}

    def discover(self, name, website, source_url, *, cur=None, request_id=None):
        """Resolve a CRM company and enqueue research once for this domain."""
        if not name.strip() or len(name) > 300:
            raise ValueError("Company name required")
        domain = domain_of(website)
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.discover(name, website, source_url, cur=cursor, request_id=request_id)
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.tenant + ":discovery",)
        )
        # Serialize identities before touching the legacy company table, which
        # deliberately has no unique domain constraint.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (self.tenant + ":" + domain,),
        )
        cur.execute(
            "SELECT * FROM sales_prospects WHERE tenant_id=%s AND domain=%s",
            (self.tenant, domain),
        )
        existing = cur.fetchone()
        if existing:
            return dict(existing)
        cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s", (self.tenant,))
        config = cur.fetchone()
        settings = SalesSettings.model_validate(config["config"] if config else {})
        local = datetime.now(ZoneInfo(settings.timezone))
        cur.execute(
            "SELECT count(*) AS n FROM sales_prospects WHERE tenant_id=%s AND created_at >= %s",
            (self.tenant, local.replace(hour=0, minute=0, second=0, microsecond=0)),
        )
        if cur.fetchone()["n"] >= settings.discovery_daily_limit:
            raise Conflict("Discovery daily admission limit reached")
        cur.execute(
            "SELECT count(*) AS n FROM sales_prospects WHERE tenant_id=%s "
            "AND status IN ('discovered','researched','needs_research','qualified')",
            (self.tenant,),
        )
        if cur.fetchone()["n"] >= settings.review_backlog_limit:
            raise Conflict("Prospect review backlog limit reached")
        cur.execute(
            "SELECT id FROM crm_companies WHERE tenant_id=%s AND deleted_at IS NULL "
            "AND lower(domain_name)=%s ORDER BY created_at LIMIT 2",
            (self.tenant, domain),
        )
        companies = cur.fetchall()
        if len(companies) > 1:
            raise Conflict("Existing company identities require a merge review")
        company = str(companies[0]["id"]) if companies else str(uuid4())
        if not companies:
            cur.execute(
                "INSERT INTO crm_companies(id,tenant_id,name,domain_name) VALUES(%s,%s,%s,%s)",
                (company, self.tenant, name, domain),
            )
        prospect = str(uuid4())
        cur.execute(
            "INSERT INTO sales_prospects(id,tenant_id,company_id,domain,name,source_url) "
            "VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
            (prospect, self.tenant, company, domain, name, source_url),
        )
        result = dict(cur.fetchone())
        if request_id:
            from robothor.sales.requests import Requests

            Requests(self).attach(request_id, prospect, cur=cur)
        self.ops.enqueue(
            "sales.research", prospect + ":0", {"prospect_id": prospect, "version": 0}, cur=cur
        )
        self.ops.audit(cur, prospect, "prospect.discovered")
        return result

    def get(self, prospect_id, *, cur=None, lock=False):
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.get(prospect_id, cur=cursor, lock=lock)
        cur.execute(
            "SELECT * FROM sales_prospects WHERE tenant_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (self.tenant, prospect_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def require(self, prospect_id, cur):
        row = self.get(prospect_id, cur=cur, lock=True)
        if not row:
            raise Conflict("Prospect not found")
        return row

    def research(self, prospect_id, dossier: Dossier, *, expected_version: int, cur=None):
        """Optimistic revision prevents two researchers from overwriting evidence."""
        dossier = Dossier.model_validate(dossier)
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.research(
                    prospect_id, dossier, expected_version=expected_version, cur=cursor
                )
        prospect = self.require(prospect_id, cur)
        request = self.require_request_open(prospect_id, cur=cur)
        if request and dossier.buying_case != request["config"]["buying_case"]:
            raise Conflict("Research dossier must use its requested buying case")
        if prospect["version"] != expected_version:
            raise Conflict("Dossier changed during research")
        cur.execute(
            "INSERT INTO sales_dossier_history(tenant_id,prospect_id,version,dossier) VALUES(%s,%s,%s,%s)",
            (self.tenant, prospect_id, expected_version + 1, Json(dossier.model_dump(mode="json"))),
        )
        cur.execute(
            "UPDATE sales_prospects SET dossier=%s,version=version+1,status='researched',"
            "qualification=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s",
            (Json(dossier.model_dump(mode="json")), self.tenant, prospect_id),
        )
        self.ops.enqueue(
            "sales.qualify",
            f"{prospect_id}:{expected_version + 1}",
            {"prospect_id": str(prospect_id), "version": expected_version + 1},
            cur=cur,
        )
        self.ops.audit(
            cur, prospect_id, "dossier.updated", detail={"version": expected_version + 1}
        )
        return None

    def _publish(self, kind, version, data, actor, *, review=None):
        operator(actor)
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 80
            or version != version.strip()
        ):
            raise ValueError("Published version must be a nonempty label of at most 80 characters")
        with self.ops.transaction() as cur:
            cur.execute(
                "INSERT INTO sales_policies(tenant_id,kind,version,data,approved_by) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (self.tenant, kind, version, Json(data), actor),
            )
            if not cur.rowcount:
                cur.execute(
                    "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind=%s AND version=%s",
                    (self.tenant, kind, version),
                )
                if cur.fetchone()["data"] != data:
                    raise Conflict("Published versions are immutable")
            self.ops.audit(cur, version, kind + ".published", actor, review)

    def publish_library(self, packet, *, expected_hash, reason, actor):
        from robothor.sales.library import preview

        operator(actor)
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
            raise ValueError("Publication review reason required")
        reviewed = preview(packet)
        if reviewed["content_hash"] != expected_hash:
            raise Conflict("Library content changed since review")
        canonical = reviewed["packet"]
        self._publish(
            canonical["kind"],
            canonical["version"],
            canonical["data"],
            actor,
            review={"content_hash": expected_hash, "reason": reason.strip()},
        )

    def publish_policy(self, policy: QualificationPolicy, actor):
        self._publish("qualification", policy.version, policy.model_dump(mode="json"), actor)

    def publish_knowledge(self, version, data, actor):
        if not isinstance(data.get("claims"), dict) or not data["claims"]:
            raise ValueError("Approved claim library required")
        self._publish("knowledge", version, data, actor)

    def _validate_library_selection(self, changes, settings, cur):
        references = []
        if "active_knowledge_version" in changes and settings["active_knowledge_version"]:
            references.append(("knowledge", settings["active_knowledge_version"], None))
        if "active_policy_versions" in changes:
            references.extend(
                ("qualification", version, case)
                for case, version in settings["active_policy_versions"].items()
            )
        for kind, version, case in references:
            cur.execute(
                "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind=%s AND version=%s",
                (self.tenant, kind, version),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Published library version required")
            if kind == "qualification" and row["data"].get("buying_case") != case:
                raise Conflict("Buying case does not match the published qualification policy")

    def library(self, *, kind, after=None, limit=100):
        """Page immutable published records; version order gives a stable cursor."""
        if (
            kind not in {"knowledge", "qualification"}
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Valid library kind and page size required")
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT kind,version,data,approved_by,approved_at FROM sales_policies "
                "WHERE tenant_id=%s AND kind=%s AND (%s IS NULL OR version>%s) ORDER BY version LIMIT %s",
                (self.tenant, kind, after, after, limit + 1),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["version"] if len(rows) > limit else None,
        }

    def library_record(self, *, kind, version):
        if kind not in {"knowledge", "qualification"}:
            raise ValueError("Valid library kind required")
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT kind,version,data,approved_by,approved_at FROM sales_policies WHERE tenant_id=%s AND kind=%s AND version=%s",
                (self.tenant, kind, version),
            )
            row = cur.fetchone()
        if row is None:
            return None
        record = dict(row)
        record["content_hash"] = digest({key: record[key] for key in ("kind", "version", "data")})
        return record

    def select_library(
        self, *, policy_versions, knowledge_version, expected_revision, reason, actor
    ):
        self.configure(
            {
                "active_policy_versions": policy_versions,
                "active_knowledge_version": knowledge_version,
            },
            actor,
            expected_revision=expected_revision,
            reason=reason,
        )

    def qualification_context(self, prospect_id, settings, *, cur=None):
        """Bind an assessment to current published policy and immutable dossier content."""
        from robothor.sales.qualification import assessment_context

        if cur is None:
            with self.ops.transaction() as cursor:
                return self.qualification_context(prospect_id, settings, cur=cursor)
        cur.execute(
            "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE", (self.tenant,)
        )
        row = cur.fetchone()
        current = SalesSettings.model_validate(row["config"] if row else {})
        if current != settings or not current.research_enabled:
            raise Conflict("Sales settings changed during qualification")
        prospect = self.require(prospect_id, cur)
        dossier = Dossier.model_validate(prospect["dossier"])
        version = current.active_policy_versions.get(dossier.buying_case)
        cur.execute(
            "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind='qualification' AND version=%s",
            (self.tenant, version),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("No active approved policy for this buying case")
        policy = QualificationPolicy.model_validate(row["data"])
        return {
            **assessment_context(dossier, policy),
            "binding": {
                "prospect_id": str(prospect_id),
                "dossier_version": prospect["version"],
                "dossier_hash": digest(prospect["dossier"]),
                "agent_id": current.agents.get("qualify"),
                "fleet_release_id": current.fleet_release_id,
                "library_revision": current.library_revision,
            },
        }

    def qualify(self, prospect_id, policy_version, *, assessment_job=None, cur=None):
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.qualify(
                    prospect_id, policy_version, assessment_job=assessment_job, cur=cursor
                )
        cur.execute(
            "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE", (self.tenant,)
        )
        settings_row = cur.fetchone()
        settings = SalesSettings.model_validate(settings_row["config"] if settings_row else {})
        self.require_request_open(prospect_id, cur=cur)
        prospect = self.require(prospect_id, cur)
        cur.execute(
            "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind='qualification' AND version=%s",
            (self.tenant, policy_version),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("An approved qualification policy is required")
        policy = QualificationPolicy.model_validate(row["data"])
        dossier = Dossier.model_validate(prospect["dossier"])
        if settings.agents.get("qualify") or assessment_job is not None:
            from robothor.sales.qualification import QualificationAssessment, evaluate_assessment

            if assessment_job is None:
                raise Conflict("Native qualification assessment required")
            context = self.qualification_context(prospect_id, settings, cur=cur)
            cur.execute(
                "SELECT result,payload FROM operation_jobs WHERE tenant_id=%s AND id=%s "
                "AND kind='sales.qualify' AND status='running' AND lease_token=%s "
                "AND lease_until>clock_timestamp() AND deadline>clock_timestamp() FOR UPDATE",
                (self.tenant, assessment_job["id"], assessment_job["lease_token"]),
            )
            saved = cur.fetchone()
            checkpoint = saved["result"] if saved else None
            if (
                not checkpoint
                or checkpoint.get("checkpoint_version") != 1
                or checkpoint.get("stage") != "qualify"
                or not checkpoint.get("run_id")
                or checkpoint.get("context") != context
                or checkpoint.get("fleet_release_id") != settings.fleet_release_id
                or policy_version != context["policy"]["version"]
                or saved["payload"].get("prospect_id") != str(prospect_id)
                or saved["payload"].get("version") != prospect["version"]
            ):
                raise Conflict("Qualification assessment is stale or belongs to different work")
            assessment = QualificationAssessment.model_validate(checkpoint["output"])
            result = evaluate_assessment(policy, dossier, assessment)
            result["assessment"] = assessment.model_dump(mode="json")
            result["assessment_receipt"] = {
                **context["binding"],
                "run_id": checkpoint["run_id"],
                "job_id": str(assessment_job["id"]),
                "context_hash": digest(context),
            }
        else:
            result = policy.evaluate(dossier)
        cur.execute(
            "UPDATE sales_prospects SET qualification=%s,status=%s,updated_at=now() WHERE tenant_id=%s AND id=%s",
            (Json(result), result["decision"], self.tenant, prospect_id),
        )
        if result["decision"] == "qualified":
            self.ops.enqueue(
                "sales.contacts",
                f"{prospect_id}:{prospect['version']}",
                {"prospect_id": str(prospect_id)},
                cur=cur,
            )
        self.ops.audit(cur, prospect_id, "qualification.completed", detail=result)
        return result

    def require_assessment(self, prospect_id, *, cur=None):
        """A fleet upgrade must not grandfather old researcher-only scores."""
        from robothor.sales.qualification import QualificationAssessment, evaluate_assessment

        if cur is None:
            with self.ops.transaction() as cursor:
                return self.require_assessment(prospect_id, cur=cursor)
        cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s", (self.tenant,))
        row = cur.fetchone()
        settings = SalesSettings.model_validate(row["config"] if row else {})
        if not settings.agents.get("qualify"):
            return None
        p = self.require(prospect_id, cur)
        qualification = p["qualification"] or {}
        receipt = qualification.get("assessment_receipt") or {}
        if (
            not qualification.get("assessment")
            or not receipt.get("run_id")
            or receipt.get("dossier_version") != p["version"]
            or receipt.get("dossier_hash") != digest(p["dossier"])
            or receipt.get("agent_id") != settings.agents["qualify"]
            or receipt.get("fleet_release_id") != settings.fleet_release_id
            or settings.active_policy_versions.get(qualification.get("buying_case"))
            != qualification.get("policy_version")
        ):
            raise Conflict("Current independent qualification assessment required")
        cur.execute(
            "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind='qualification' AND version=%s",
            (self.tenant, qualification["policy_version"]),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Qualification assessment policy unavailable")
        try:
            current = evaluate_assessment(
                QualificationPolicy.model_validate(row["data"]),
                Dossier.model_validate(p["dossier"]),
                QualificationAssessment.model_validate(qualification["assessment"]),
            )
        except ValueError:
            raise Conflict("Invalid qualification assessment") from None
        if current["decision"] != "qualified" or any(
            current[k] != qualification.get(k) for k in current
        ):
            raise Conflict("Qualification assessment needs current supporting evidence")
        return None

    def accept(
        self, prospect_id, accepted, actor, note="", *, expected_version, expected_policy_version
    ):
        """Human dossier review and promotion intent are atomic."""
        operator(actor)
        with self.ops.transaction() as cur:
            p = self.require(prospect_id, cur)
            if (
                p["version"] != expected_version
                or (p["qualification"] or {}).get("policy_version") != expected_policy_version
            ):
                raise Conflict(
                    "The reviewed dossier or qualification changed; reload before deciding"
                )
            if accepted and (p["qualification"] or {}).get("decision") != "qualified":
                raise Conflict("Qualification required before acceptance")
            if accepted:
                self.require_assessment(prospect_id, cur=cur)
            cur.execute(
                "UPDATE sales_prospects SET status=%s,review_note=%s,updated_at=now() WHERE tenant_id=%s AND id=%s",
                (
                    p["status"]
                    if accepted and p["status"] == "promoted"
                    else "accepted"
                    if accepted
                    else "rejected",
                    note,
                    self.tenant,
                    prospect_id,
                ),
            )
            if accepted:
                self.ops.enqueue(
                    "sales.promote",
                    f"{prospect_id}:{expected_version}:{expected_policy_version}",
                    {
                        "prospect_id": str(prospect_id),
                        "version": expected_version,
                        "policy_version": expected_policy_version,
                    },
                    cur=cur,
                )
            self.ops.audit(
                cur, prospect_id, "prospect.accepted" if accepted else "prospect.rejected", actor
            )

    def add_contact(self, prospect_id, data, *, cur=None):
        contact = Contact.model_validate(data)
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.add_contact(prospect_id, data, cur=cursor)
        p = self.require(prospect_id, cur)
        if (p["qualification"] or {}).get("decision") != "qualified":
            raise Conflict("Contact enrichment requires qualification")
        self.require_request_open(prospect_id, cur=cur)
        self.require_assessment(prospect_id, cur=cur)
        from robothor.sales.contacts import identity_hash

        cur.execute(
            "SELECT c.id,c.data,a.detail FROM sales_contacts c JOIN LATERAL(SELECT detail FROM operation_audit WHERE tenant_id=c.tenant_id AND entity_id=c.id::text AND event='contact.identity_reviewed' ORDER BY id DESC LIMIT 1) a ON true WHERE c.tenant_id=%s AND c.prospect_id=%s AND c.email=%s",
            (self.tenant, prospect_id, contact.email),
        )
        reviewed = cur.fetchone()
        if reviewed and reviewed["detail"].get("identity_hash") == identity_hash(reviewed["data"]):
            if identity_hash(contact.model_dump(mode="json")) != identity_hash(reviewed["data"]):
                raise Conflict("Contact has a human-reviewed identity; review conflicting research")
            return str(reviewed["id"])
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (self.tenant + ":" + contact.email,),
        )
        cur.execute(
            "SELECT id,company_id FROM crm_people WHERE tenant_id=%s AND lower(email)=%s AND deleted_at IS NULL FOR UPDATE",
            (self.tenant, contact.email),
        )
        people = cur.fetchall()
        if len(people) > 1 or (people and people[0]["company_id"] not in (None, p["company_id"])):
            raise Conflict("Contact belongs to another company or needs identity review")
        person = str(people[0]["id"]) if people else str(uuid4())
        if people and people[0]["company_id"] is None:
            cur.execute(
                "UPDATE crm_people SET company_id=%s WHERE tenant_id=%s AND id=%s AND company_id IS NULL",
                (p["company_id"], self.tenant, person),
            )
            self.ops.audit(
                cur, person, "contact.company_linked", detail={"company_id": str(p["company_id"])}
            )
        if not people:
            first, _, last = contact.name.partition(" ")
            cur.execute(
                "INSERT INTO crm_people(id,tenant_id,first_name,last_name,email,job_title,company_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    person,
                    self.tenant,
                    first,
                    last,
                    contact.email,
                    contact.role,
                    p["company_id"],
                ),
            )
        cur.execute(
            "INSERT INTO sales_contacts(id,tenant_id,prospect_id,person_id,email,data) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(tenant_id,prospect_id,email) DO UPDATE SET data=EXCLUDED.data RETURNING id",
            (
                str(uuid4()),
                self.tenant,
                prospect_id,
                person,
                contact.email,
                Json(contact.model_dump(mode="json")),
            ),
        )
        return str(cur.fetchone()["id"])

    def contacts(self, prospect_id):
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT * FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s",
                (self.tenant, prospect_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def context(self, prospect_id):
        with self.ops.transaction() as cur:
            prospect = self.require(prospect_id, cur)
            cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s", (self.tenant,))
            row = cur.fetchone()
            settings = row["config"] if row else {}
            cur.execute(
                "SELECT kind,version,data FROM sales_policies WHERE tenant_id=%s", (self.tenant,)
            )
            policies = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT r.config FROM sales_request_members m JOIN sales_requests r ON r.tenant_id=m.tenant_id AND r.id=m.request_id WHERE m.tenant_id=%s AND m.prospect_id=%s",
                (self.tenant, prospect_id),
            )
            request = cur.fetchone()
            brief = request["config"] if request else None
        active = settings.get("active_policy_versions", {})
        return {
            "prospect": prospect,
            "contacts": self.contacts(prospect_id),
            "messages": self.messages(prospect_id),
            "research_request": brief,
            "policies": [
                p
                for p in policies
                if (
                    p["kind"] == "knowledge"
                    and settings.get("active_knowledge_version")
                    and p["version"] == settings.get("active_knowledge_version")
                )
                or (
                    p["kind"] == "qualification"
                    and active.get(p["data"].get("buying_case")) == p["version"]
                    and (brief is None or p["data"].get("buying_case") == brief["buying_case"])
                )
            ],
            "outreach": {
                k: settings.get(k) for k in ("senders", "postal_address", "unsubscribe_url")
            },
        }

    def _suppressed(self, email, cur):
        cur.execute(
            "SELECT 1 FROM sales_suppression WHERE tenant_id=%s AND email IN (%s,%s) "
            "UNION ALL SELECT 1 FROM crm_people WHERE tenant_id=%s AND lower(email)=%s AND do_not_contact=true LIMIT 1",
            (self.tenant, email, "@" + email.split("@")[-1], self.tenant, email),
        )
        return bool(cur.fetchone())

    def draft(self, prospect_id, data, *, cur=None):
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.draft(prospect_id, data, cur=cursor)
        draft = Draft.model_validate(data)
        self.require_request_open(prospect_id, cur=cur)
        followup_basis = None
        if draft.purpose == "followup":
            from robothor.sales.followups import Followups

            followup_basis = Followups(self).basis(prospect_id, cur=cur)
            if any(
                getattr(draft, k) != followup_basis[k]
                for k in ("sender", "recipient", "reply_to_uuid")
            ):
                raise Conflict("Follow-up participants or preceding message changed")
        p = self.require(prospect_id, cur)
        self.require_assessment(prospect_id, cur=cur)
        if (
            p["status"] not in {"accepted", "promoted", "engaged", "onboarding", "active"}
            or p["owner"] != "agent"
        ):
            raise Conflict("Reviewed prospect under agent ownership required")
        if self._suppressed(draft.recipient, cur):
            raise Conflict("Recipient suppressed")
        cur.execute(
            "SELECT data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s AND email=%s",
            (self.tenant, prospect_id, draft.recipient),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Known company contact required")
        contact = Contact.model_validate(row["data"])
        if (
            contact.verification != "valid"
            or not contact.verified_at
            or contact.verified_at.tzinfo is None
            or not datetime.now(UTC) - timedelta(days=30)
            <= contact.verified_at
            <= datetime.now(UTC)
        ):
            raise Conflict("Current email verification required")
        cur.execute(
            "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind='knowledge' AND version=%s",
            (self.tenant, draft.knowledge_version),
        )
        knowledge = cur.fetchone()
        if not knowledge or not set(draft.claim_ids) <= knowledge["data"].get("claims", {}).keys():
            raise Conflict("Referenced sales claims are not approved")
        dossier = Dossier.model_validate(p["dossier"])
        evidence = {e.id: e for e in dossier.evidence}
        if not set(draft.evidence_ids) <= evidence.keys():
            raise Conflict("Missing supporting evidence")
        from robothor.sales.contacts import identity_hash

        payload = draft.model_dump(mode="json") | {
            "contact_identity_hash": identity_hash(contact.model_dump(mode="json")),
            "prospect_id": str(prospect_id),
            "dossier_version": p["version"],
            "conversation_version": p["conversation_version"],
            "outcome_version": p["outcome_version"],
        }
        cur.execute("SELECT config FROM sales_settings WHERE tenant_id=%s", (self.tenant,))
        row = cur.fetchone()
        payload["library_revision"] = (row["config"] if row else {}).get("library_revision", 0)
        from robothor.sales.setup import sender_context_hash

        payload["sender_context_hash"] = sender_context_hash(
            row["config"] if row else {}, draft.sender
        )
        if followup_basis:
            payload["followup_basis"] = followup_basis
        return self.ops.propose("sales.email", str(uuid4()), payload, cur=cur)

    def validate_send(self, action, *, cur=None):
        """Recheck the persisted authorization immediately before the external effect.

        A claimed action alone is insufficient: evidence, policy, ownership,
        suppression and conversation state can all change after human review.
        """
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.validate_send(action, cur=cursor)
        cur.execute(
            "SELECT config FROM sales_settings WHERE tenant_id=%s FOR UPDATE", (self.tenant,)
        )
        row = cur.fetchone()
        settings = SalesSettings.model_validate(row["config"] if row else {})
        if not settings.sending_enabled:
            raise Conflict("Sending paused")
        cur.execute(
            "SELECT * FROM operation_actions WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (self.tenant, action["id"]),
        )
        live = cur.fetchone()
        now = datetime.now(UTC)
        if (
            not live
            or live["kind"] != "sales.email"
            or live["status"] != "executing"
            or live["lease_token"] != action["lease_token"]
            or live["expires_at"] <= now
            or live["lease_until"] <= now
        ):
            raise Conflict("Current approved send lease required")
        payload = live["payload"]
        if live["approved_hash"] != digest(payload) or digest(payload) != digest(action["payload"]):
            raise Conflict("Approved content changed")
        from robothor.sales.setup import sender_context_hash

        if payload.get("sender_context_hash") != sender_context_hash(
            settings.model_dump(mode="json"), payload["sender"]
        ):
            raise Conflict("Sender configuration or mailbox readiness changed since approval")
        if payload.get("library_revision", 0) != settings.library_revision:
            raise Conflict("Sales knowledge or qualification selection changed")
        p = self.require(payload["prospect_id"], cur)
        self.require_request_open(p["id"], cur=cur)
        if p["owner"] != "agent":
            raise Conflict("Agent ownership required")
        if p["version"] != payload["dossier_version"]:
            raise Conflict("Dossier changed since approval")
        if p["conversation_version"] != payload.get("conversation_version"):
            raise Conflict("New conversation activity since approval")
        if p["outcome_version"] != payload.get("outcome_version"):
            raise Conflict("New customer milestone since approval")
        if p["status"] not in {"accepted", "promoted", "engaged", "onboarding", "active"}:
            raise Conflict("Accepted prospect required")
        if payload["knowledge_version"] != settings.active_knowledge_version:
            raise Conflict("Active knowledge version changed")
        q = p["qualification"] or {}
        self.require_assessment(p["id"], cur=cur)
        if q.get("decision") != "qualified" or settings.active_policy_versions.get(
            q.get("buying_case")
        ) != q.get("policy_version"):
            raise Conflict("Active qualification policy changed")
        if payload["sender"] not in settings.senders:
            raise Conflict("Sender not enabled")
        if self._suppressed(payload["recipient"], cur):
            raise Conflict("Recipient suppressed")
        if (
            not settings.postal_address
            or settings.postal_address not in payload["body"]
            or not settings.unsubscribe_url
            or settings.unsubscribe_url not in payload["body"]
        ):
            raise Conflict("Approved body must include configured address and opt-out link")
        cur.execute(
            "SELECT data FROM sales_contacts WHERE tenant_id=%s AND prospect_id=%s AND email=%s",
            (self.tenant, p["id"], payload["recipient"]),
        )
        contact_row = cur.fetchone()
        if not contact_row:
            raise Conflict("Contact no longer exists")
        contact = Contact.model_validate(contact_row["data"])
        from robothor.sales.contacts import identity_hash

        if payload.get("contact_identity_hash") != identity_hash(contact.model_dump(mode="json")):
            raise Conflict("Contact identity changed since approval")
        if (
            contact.verification != "valid"
            or not contact.verified_at
            or not contact.verified_at.tzinfo
            or not now - timedelta(days=30) <= contact.verified_at <= now
        ):
            raise Conflict("Email verification expired")
        evidence = {e.id: e for e in Dossier.model_validate(p["dossier"]).evidence}
        for ref in payload["evidence_ids"]:
            e = evidence.get(ref)
            if (
                not e
                or e.confidence != "supported"
                or not now - timedelta(days=90) <= e.retrieved_at <= now
                or (e.expires_at is not None and e.expires_at <= now)
            ):
                raise Conflict("Supporting evidence expired or unsupported")
        if payload.get("reply_to_uuid"):
            cur.execute(
                "SELECT data FROM sales_messages WHERE tenant_id=%s AND prospect_id=%s AND provider_id=%s",
                (self.tenant, p["id"], payload["reply_to_uuid"]),
            )
            message = cur.fetchone()
            if not message or {message["data"]["sender"], message["data"]["recipient"]} != {
                payload["sender"],
                payload["recipient"],
            }:
                raise Conflict("Thread participants do not match approved message")
        if payload.get("purpose") == "followup":
            from robothor.sales.followups import Followups

            basis = Followups(self).basis(p["id"], cur=cur, exclude_action=live["id"])
            if basis != payload.get("followup_basis"):
                raise Conflict("Follow-up cadence or conversation basis changed since approval")
        return settings

    def history(self, prospect_id):
        with self.ops.transaction() as cur:
            self.require(prospect_id, cur)
            cur.execute(
                "SELECT * FROM sales_dossier_history WHERE tenant_id=%s AND prospect_id=%s ORDER BY version",
                (self.tenant, prospect_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def record_message(self, data, *, cur=None):
        """Normalized provider event; inbound text remains untrusted agent input."""
        message = Message.model_validate(data)
        payload = message.model_dump(mode="json")
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.record_message(data, cur=cursor)
        self.require(message.prospect_id, cur)
        cur.execute(
            "INSERT INTO sales_messages(tenant_id,provider_id,prospect_id,direction,occurred_at,data) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                self.tenant,
                message.provider_id,
                message.prospect_id,
                message.direction,
                message.occurred_at,
                Json(payload),
            ),
        )
        if not cur.rowcount:
            cur.execute(
                "SELECT data FROM sales_messages WHERE tenant_id=%s AND provider_id=%s",
                (self.tenant, message.provider_id),
            )
            if cur.fetchone()["data"] != payload:
                raise Conflict("Provider message ID reused with different content")
            return False
        cur.execute(
            "UPDATE sales_prospects SET conversation_version=conversation_version+1,updated_at=now() "
            "WHERE tenant_id=%s AND id=%s",
            (self.tenant, message.prospect_id),
        )
        if message.direction == "inbound":
            cur.execute(
                "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' "
                "AND status IN ('review','approved') AND payload->>'prospect_id'=%s",
                (self.tenant, message.prospect_id),
            )
            if not message.auto_reply:
                self.ops.enqueue(
                    "sales.conversation",
                    message.provider_id,
                    {"prospect_id": message.prospect_id, "provider_id": message.provider_id},
                    cur=cur,
                )
        self.ops.audit(
            cur,
            message.prospect_id,
            "message." + message.direction,
            detail={"provider_id": message.provider_id},
        )
        return True

    def messages(self, prospect_id):
        with self.ops.transaction() as cur:
            self.require(prospect_id, cur)
            cur.execute(
                "SELECT * FROM sales_messages WHERE tenant_id=%s AND prospect_id=%s ORDER BY occurred_at",
                (self.tenant, prospect_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def provider_reads(self, *, state="attention", kind=None, after=None):
        kinds = ("sales.inbound", "sales.reconcile", "sales.business", "sales.provider_status")
        if state not in {"attention", "all"} or (kind is not None and kind not in kinds):
            raise ValueError("Provider read inventory filter invalid")
        with self.ops.transaction() as cur:
            boundary = None
            if after:
                cur.execute(
                    "SELECT created_at FROM operation_jobs WHERE tenant_id=%s AND id=%s AND kind=ANY(%s)",
                    (self.tenant, after, list(kinds)),
                )
                cursor = cur.fetchone()
                if not cursor:
                    raise Conflict("Provider read cursor missing in this tenant")
                boundary = cursor["created_at"]
            cur.execute(
                "SELECT id,kind,status,error,attempts,max_attempts,created_at,updated_at,available_at,deadline, "
                "jsonb_strip_nulls(jsonb_build_object('source',payload->>'source','account_id',payload->>'account_id', "
                "'kind',payload->>'kind','practice_id',payload->>'practice_id','campaign_id',payload->>'campaign_id','sender',payload->>'sender','status_scope',payload->>'scope')) AS scope "
                "FROM operation_jobs WHERE tenant_id=%s AND kind=ANY(%s) "
                "AND (%s::text IS NULL OR kind=%s) "
                "AND (%s='all' OR (status IN ('failed','pending') AND (status='failed' OR COALESCE(error,'')<>''))) "
                "AND (%s::timestamptz IS NULL OR (created_at,id)<(%s,%s::uuid)) "
                "ORDER BY created_at DESC,id DESC LIMIT 101",
                (self.tenant, list(kinds), kind, kind, state, boundary, boundary, after),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return {
            "items": rows[:100],
            "next_cursor": str(rows[99]["id"]) if len(rows) > 100 else None,
        }

    def retry_provider_read(self, job_id, actor, reason):
        """Retry repaired event reads without granting any external write authority."""
        operator(actor)
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
            raise ValueError("A repair reason of 10–2000 characters is required")
        with self.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET status='pending',attempts=0,available_at=now(), "
                "deadline=now()+interval '1 day',lease_token=NULL,lease_until=NULL,error='',updated_at=now() "
                "WHERE tenant_id=%s AND id=%s AND kind IN ('sales.inbound','sales.reconcile','sales.business','sales.provider_status') "
                "AND status IN ('failed','pending')",
                (self.tenant, job_id),
            )
            if cur.rowcount != 1:
                raise Conflict("Only inactive provider read jobs can be retried")
            self.ops.audit(cur, job_id, "provider.read_retried", actor, {"reason": reason.strip()})

    def suppress(self, email, reason, actor, *, cur=None):
        """Stop all pending outreach immediately, including across campaigns."""
        email = email.strip().lower()
        if cur is None:
            with self.ops.transaction() as cursor:
                return self.suppress(email, reason, actor, cur=cursor)
        cur.execute(
            "INSERT INTO sales_suppression(tenant_id,email,reason) VALUES(%s,%s,%s) "
            "ON CONFLICT(tenant_id,email) DO UPDATE SET reason=EXCLUDED.reason",
            (self.tenant, email, reason),
        )
        cur.execute(
            "UPDATE crm_people SET do_not_contact=true WHERE tenant_id=%s AND lower(email)=%s",
            (self.tenant, email),
        )
        cur.execute(
            "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND kind='sales.email' "
            "AND status IN ('review','approved') AND (payload->>'recipient'=%s OR "
            "'@'||split_part(payload->>'recipient','@',2)=%s)",
            (self.tenant, email, email),
        )
        self.ops.enqueue("sales.suppress", email, {"email": email}, cur=cur)
        self.ops.audit(cur, email, "contact.suppressed", actor, {"reason": reason})
        return None

    def takeover(self, prospect_id, actor):
        operator(actor)
        with self.ops.transaction() as cur:
            self.require(prospect_id, cur)
            cur.execute(
                "UPDATE sales_prospects SET owner=%s,updated_at=now() WHERE tenant_id=%s AND id=%s",
                (actor, self.tenant, prospect_id),
            )
            cur.execute(
                "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s AND status IN ('review','approved') "
                "AND payload->>'prospect_id'=%s",
                (self.tenant, str(prospect_id)),
            )
            self.ops.audit(cur, prospect_id, "conversation.taken_over", actor)
            self.ops.enqueue("sales.stop", str(uuid4()), {"prospect_id": str(prospect_id)}, cur=cur)

    def escalate(self, prospect_id, reason, *, cur):
        """A worker may stop automation for review, never restore its own authority."""
        p = self.require(prospect_id, cur)
        if p["owner"] == "agent":
            cur.execute(
                "UPDATE sales_prospects SET owner='human_review',review_note=%s,updated_at=now() "
                "WHERE tenant_id=%s AND id=%s",
                (reason[:2000], self.tenant, prospect_id),
            )
        cur.execute(
            "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s "
            "AND status IN ('review','approved') AND payload->>'prospect_id'=%s",
            (self.tenant, str(prospect_id)),
        )
        self.ops.enqueue("sales.stop", str(uuid4()), {"prospect_id": str(prospect_id)}, cur=cur)
        self.ops.audit(cur, prospect_id, "conversation.escalated", detail={"reason": reason[:2000]})

    def business_records(self, **query):
        from robothor.sales.business import BusinessObservations

        return BusinessObservations(self).records(**query)

    def bind_business_customer(self, prospect_id, observation_id, revision, actor, reason):
        from robothor.sales.business import BusinessObservations

        return BusinessObservations(self).bind(prospect_id, observation_id, revision, actor, reason)

    def reassign_business_customer(self, observation_id, **review):
        from robothor.sales.business import BusinessObservations

        return BusinessObservations(self).reassign(observation_id, **review)

    def bind_customer(self, prospect_id, external_id, actor):
        operator(actor)
        with self.ops.transaction() as cur:
            p = self.require(prospect_id, cur)
            cur.execute(
                "SELECT 1 FROM sales_customer_bindings WHERE tenant_id=%s AND prospect_id=%s LIMIT 1",
                (self.tenant, prospect_id),
            )
            if cur.fetchone() or p["business_attribution_detached"]:
                raise Conflict("Current business attribution already exists")
            if p["external_company_id"] not in (None, external_id):
                raise Conflict("Customer association already exists")
            cur.execute(
                "UPDATE sales_prospects SET external_company_id=%s WHERE tenant_id=%s AND id=%s",
                (external_id, self.tenant, prospect_id),
            )
            self.ops.audit(cur, prospect_id, "customer.bound", actor)

    def record_outcome(self, data):
        """Allowlist the business-level event contract; reject patient-level fields."""
        event = Outcome.model_validate(data)
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT id FROM sales_prospects WHERE tenant_id=%s AND external_company_id=%s",
                (self.tenant, event.external_company_id),
            )
            row = cur.fetchone()
            if not row:
                raise Conflict("Customer mapping must be confirmed before importing outcomes")
            cur.execute(
                "INSERT INTO sales_outcomes(tenant_id,event_id,prospect_id,kind,occurred_at,order_ref) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    self.tenant,
                    event.event_id,
                    row["id"],
                    event.kind,
                    event.occurred_at,
                    event.order_ref,
                ),
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE sales_prospects SET outcome_version=outcome_version+1,updated_at=now() WHERE tenant_id=%s AND id=%s",
                    (self.tenant, row["id"]),
                )
                cur.execute(
                    "UPDATE operation_actions SET status='cancelled' WHERE tenant_id=%s "
                    "AND kind='sales.email' AND status IN ('review','approved') AND payload->>'prospect_id'=%s",
                    (self.tenant, str(row["id"])),
                )
                self.ops.audit(cur, str(row["id"]), "outcome." + event.kind)
                self.ops.enqueue(
                    "sales.activation",
                    event.event_id,
                    {"prospect_id": str(row["id"]), "milestone": event.kind},
                    cur=cur,
                )
            else:
                cur.execute(
                    "SELECT * FROM sales_outcomes WHERE tenant_id=%s AND event_id=%s",
                    (self.tenant, event.event_id),
                )
                existing = cur.fetchone()
                if any(
                    existing[k] != v
                    for k, v in {
                        "prospect_id": row["id"],
                        "kind": event.kind,
                        "occurred_at": event.occurred_at,
                        "order_ref": event.order_ref,
                    }.items()
                ):
                    raise Conflict("Outcome event ID reused with different content")

    def retention(self, prospect_id, now=None):
        from robothor.sales.business import BusinessObservations, retention_metrics

        current = BusinessObservations(self).retention(prospect_id, now)
        if current is not None:
            return current
        now = now or datetime.now(UTC)
        with self.ops.transaction() as cur:
            self.require(prospect_id, cur)
            cur.execute(
                "SELECT min(occurred_at) AS placed_at FROM sales_outcomes "
                "WHERE tenant_id=%s AND prospect_id=%s AND kind='first_order_placed'",
                (self.tenant, prospect_id),
            )
            placed_at = cur.fetchone()["placed_at"]
            cur.execute(
                "SELECT min(occurred_at) AS occurred_at FROM sales_outcomes o WHERE tenant_id=%s AND prospect_id=%s "
                "AND kind='order_completed' AND NOT EXISTS (SELECT 1 FROM sales_outcomes r "
                "WHERE r.tenant_id=o.tenant_id AND r.prospect_id=o.prospect_id AND r.kind='order_reversed' "
                "AND r.order_ref=o.order_ref AND r.order_ref IS NOT NULL) "
                "GROUP BY coalesce(o.order_ref,o.event_id) ORDER BY occurred_at",
                (self.tenant, prospect_id),
            )
            dates = [r["occurred_at"] for r in cur.fetchall()]
        return retention_metrics(placed_at, dates, now)

    def overview(self):
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT * FROM sales_prospects WHERE tenant_id=%s ORDER BY updated_at DESC LIMIT 200",
                (self.tenant,),
            )
            prospects = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT * FROM operation_actions WHERE tenant_id=%s AND kind LIKE 'sales.%%' ORDER BY created_at DESC LIMIT 200",
                (self.tenant,),
            )
            actions = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT * FROM operation_jobs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 100",
                (self.tenant,),
            )
            jobs = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM operation_budgets WHERE tenant_id=%s", (self.tenant,))
            budgets = [dict(r) for r in cur.fetchall()]
        return {
            "prospects": prospects,
            "actions": actions,
            "jobs": jobs,
            "budgets": budgets,
            "settings": self.settings(),
        }

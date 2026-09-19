"""Auditable human qualification cohorts; never approval to promote or send."""

from uuid import uuid4

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest
from robothor.sales.service import operator


def _reason(value):
    if not isinstance(value, str) or not 10 <= len(value.strip()) <= 2000:
        raise ValueError("Assessment review reason required")
    return value.strip()


def _metrics(rows, field):
    reviewed = [row for row in rows if row[field] is not None]
    agree = sum(row["model_decision"] == row[field] for row in reviewed)
    decisive = [row for row in reviewed if row[field] != "needs_research"]
    predicted_positive = [row for row in decisive if row["model_decision"] == "qualified"]
    return {
        "enrolled": len(rows),
        "reviewed": len(reviewed),
        "agreements": agree,
        "agreement_percent": 100 * agree / len(reviewed) if reviewed else None,
        "uncertain_reference": sum(row[field] == "needs_research" for row in reviewed),
        "reference_qualified": sum(row[field] == "qualified" for row in reviewed),
        "reference_rejected": sum(row[field] == "rejected" for row in reviewed),
        "model_needs_research": sum(row["model_decision"] == "needs_research" for row in rows),
        "false_positives": sum(
            row["model_decision"] == "qualified" and row[field] == "rejected" for row in reviewed
        ),
        "missed_fits": sum(
            row["model_decision"] != "qualified" and row[field] == "qualified" for row in reviewed
        ),
        "false_negatives": sum(
            row["model_decision"] == "rejected" and row[field] == "qualified" for row in reviewed
        ),
        "abstained_fits": sum(
            row["model_decision"] == "needs_research" and row[field] == "qualified"
            for row in reviewed
        ),
        "qualified_precision_percent": 100
        * sum(row[field] == "qualified" for row in predicted_positive)
        / len(predicted_positive)
        if predicted_positive
        else None,
    }


class Calibration:
    def __init__(self, sales):
        self.sales = sales
        self.tenant = sales.tenant
        self.ops = sales.ops

    def create(
        self,
        *,
        name,
        target_size,
        agreement_target_percent,
        expected_settings_revision,
        actor,
        reason,
    ):
        operator(actor)
        reason = _reason(reason)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError("Cohort name required")
        if type(target_size) is not int or not 1 <= target_size <= 1000:
            raise ValueError("Cohort size must be between 1 and 1000")
        if type(agreement_target_percent) is not int or not 0 <= agreement_target_percent <= 100:
            raise ValueError("Agreement target must be a percentage")
        if type(expected_settings_revision) is not int or expected_settings_revision < 0:
            raise ValueError("Settings revision required")
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT config,revision FROM sales_settings WHERE tenant_id=%s FOR SHARE",
                (self.tenant,),
            )
            state = cur.fetchone()
            if not state or state["revision"] != expected_settings_revision:
                raise Conflict("Settings changed; refresh before creating the review cohort")
            versions = state["config"].get("active_policy_versions", {})
            if not versions:
                raise Conflict("Active published qualification policies are required")
            policies = {}
            for case, version in versions.items():
                cur.execute(
                    "SELECT data FROM sales_policies WHERE tenant_id=%s AND kind='qualification' AND version=%s",
                    (self.tenant, version),
                )
                row = cur.fetchone()
                if not row or row["data"].get("buying_case") != case:
                    raise Conflict("Published qualification policy does not match the buying case")
                policies[case] = row["data"]
            identity = str(uuid4())
            cur.execute(
                "INSERT INTO sales_calibration_cohorts(id,tenant_id,name,target_size,agreement_target_percent,policy_versions,policies,settings_revision,created_by,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    identity,
                    self.tenant,
                    name.strip(),
                    target_size,
                    agreement_target_percent,
                    Json(versions),
                    Json(policies),
                    expected_settings_revision,
                    actor,
                    reason,
                ),
            )
            result = dict(cur.fetchone())
            self.ops.audit(
                cur,
                identity,
                "calibration.created",
                actor,
                {"target_size": target_size, "policy_versions": versions, "reason": reason},
            )
            return result

    def _cohort(self, identity, cur, *, lock=False):
        cur.execute(
            "SELECT * FROM sales_calibration_cohorts WHERE tenant_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (self.tenant, identity),
        )
        row = cur.fetchone()
        if row is None:
            raise Conflict("Review cohort not found")
        return dict(row)

    def list_cohorts(self, *, after=None):
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT id,name,target_size,agreement_target_percent,policy_versions,created_by,created_at FROM sales_calibration_cohorts WHERE tenant_id=%s AND (%s IS NULL OR id>%s::uuid) ORDER BY id LIMIT 51",
                (self.tenant, after, after),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return {"items": rows[:50], "next_cursor": str(rows[49]["id"]) if len(rows) > 50 else None}

    def enroll(self, cohort_id, *, actor):
        operator(actor)
        with self.ops.transaction() as cur:
            cohort = self._cohort(cohort_id, cur, lock=True)
            cur.execute(
                "SELECT count(*) AS n FROM sales_calibration_items WHERE tenant_id=%s AND cohort_id=%s",
                (self.tenant, cohort_id),
            )
            count = cur.fetchone()["n"]
            room = cohort["target_size"] - count
            cur.execute(
                "SELECT p.* FROM sales_prospects p WHERE p.tenant_id=%s AND p.dossier IS NOT NULL AND p.qualification IS NOT NULL "
                "AND p.qualification->>'decision' IN ('qualified','rejected','needs_research') "
                "AND p.qualification->>'policy_version'=(%s::jsonb)->>(p.qualification->>'buying_case') "
                "AND NOT EXISTS(SELECT 1 FROM sales_calibration_items i WHERE i.tenant_id=p.tenant_id AND i.cohort_id=%s AND i.prospect_id=p.id) "
                "ORDER BY p.created_at,p.id LIMIT %s FOR SHARE OF p",
                (self.tenant, Json(cohort["policy_versions"]), cohort_id, room),
            )
            prospects = list(cur.fetchall())
            for offset, p in enumerate(prospects, 1):
                q = p["qualification"]
                snapshot = {
                    key: p[key]
                    for key in (
                        "name",
                        "domain",
                        "source_url",
                        "version",
                        "dossier",
                        "qualification",
                    )
                }
                snapshot["policy"] = cohort["policies"][q["buying_case"]]
                cur.execute(
                    "INSERT INTO sales_calibration_items(id,tenant_id,cohort_id,prospect_id,ordinal,buying_case,policy_version,snapshot,snapshot_hash,enrolled_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        str(uuid4()),
                        self.tenant,
                        cohort_id,
                        p["id"],
                        count + offset,
                        q["buying_case"],
                        q["policy_version"],
                        Json(snapshot),
                        digest(snapshot),
                        actor,
                    ),
                )
            self.ops.audit(
                cur,
                cohort_id,
                "calibration.enrolled",
                actor,
                {
                    "added": len(prospects),
                    "enrolled": count + len(prospects),
                    "method": "oldest_eligible_at_enrollment",
                },
            )
            return {"added": len(prospects), "enrolled": count + len(prospects)}

    def items(self, cohort_id, *, after=0):
        if type(after) is not int or after < 0:
            raise ValueError("Nonnegative item cursor required")
        with self.ops.transaction() as cur:
            self._cohort(cohort_id, cur)
            cur.execute(
                "SELECT i.id,i.prospect_id,i.ordinal,i.buying_case,i.policy_version,i.snapshot_hash,i.snapshot->>'name' AS name,i.snapshot->>'domain' AS domain, "
                "(SELECT a.reference_decision FROM sales_calibration_assessments a WHERE a.tenant_id=i.tenant_id AND a.item_id=i.id ORDER BY revision DESC LIMIT 1) AS reference_decision "
                "FROM sales_calibration_items i WHERE i.tenant_id=%s AND i.cohort_id=%s AND i.ordinal>%s ORDER BY i.ordinal LIMIT 21",
                (self.tenant, cohort_id, after),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return {"items": rows[:20], "next_cursor": rows[19]["ordinal"] if len(rows) > 20 else None}

    def item(self, identity):
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT * FROM sales_calibration_items WHERE tenant_id=%s AND id=%s",
                (self.tenant, identity),
            )
            row = cur.fetchone()
            if row is None:
                raise Conflict("Review item not found")
            result = dict(row)
            cur.execute(
                "SELECT * FROM sales_calibration_assessments WHERE tenant_id=%s AND item_id=%s ORDER BY revision",
                (self.tenant, identity),
            )
            result["assessments"] = [dict(row) for row in cur.fetchall()]
            return result

    def assess(
        self,
        item_id,
        *,
        expected_snapshot_hash,
        expected_assessment_id,
        reference_decision,
        actor,
        reason,
    ):
        operator(actor)
        reason = _reason(reason)
        if reference_decision not in {"qualified", "rejected", "needs_research"}:
            raise ValueError("Reference decision required")
        with self.ops.transaction() as cur:
            cur.execute(
                "SELECT snapshot_hash FROM sales_calibration_items WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (self.tenant, item_id),
            )
            item = cur.fetchone()
            if item is None or item["snapshot_hash"] != expected_snapshot_hash:
                raise Conflict("Review snapshot is missing or changed")
            cur.execute(
                "SELECT id,revision FROM sales_calibration_assessments WHERE tenant_id=%s AND item_id=%s ORDER BY revision DESC LIMIT 1",
                (self.tenant, item_id),
            )
            previous = cur.fetchone()
            previous_id = str(previous["id"]) if previous else None
            if previous_id != expected_assessment_id:
                raise Conflict("Current assessment changed; reload before correcting it")
            cur.execute(
                "INSERT INTO sales_calibration_assessments(id,tenant_id,item_id,revision,supersedes,reference_decision,actor,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    str(uuid4()),
                    self.tenant,
                    item_id,
                    previous["revision"] + 1 if previous else 1,
                    previous_id,
                    reference_decision,
                    actor,
                    reason,
                ),
            )
            result = dict(cur.fetchone())
            self.ops.audit(
                cur,
                item_id,
                "calibration.assessed",
                actor,
                {
                    "assessment_id": str(result["id"]),
                    "revision": result["revision"],
                    "reference_decision": reference_decision,
                    "reason": reason,
                },
            )
            return result

    def report(self, cohort_id):
        with self.ops.transaction() as cur:
            cohort = self._cohort(cohort_id, cur)
            cur.execute(
                "SELECT i.buying_case,i.policy_version,i.snapshot->'qualification'->>'decision' AS model_decision, "
                "first.reference_decision AS initial,last.reference_decision AS latest,last.revision "
                "FROM sales_calibration_items i "
                "LEFT JOIN LATERAL(SELECT reference_decision FROM sales_calibration_assessments a WHERE a.tenant_id=i.tenant_id AND a.item_id=i.id ORDER BY revision LIMIT 1) first ON true "
                "LEFT JOIN LATERAL(SELECT reference_decision,revision FROM sales_calibration_assessments a WHERE a.tenant_id=i.tenant_id AND a.item_id=i.id ORDER BY revision DESC LIMIT 1) last ON true "
                "WHERE i.tenant_id=%s AND i.cohort_id=%s ORDER BY i.ordinal",
                (self.tenant, cohort_id),
            )
            rows = list(cur.fetchall())
        initial = _metrics(rows, "initial")
        complete = (
            initial["reviewed"] == cohort["target_size"] and not initial["uncertain_reference"]
        )
        return {
            "cohort": cohort,
            "initial": initial,
            "latest": _metrics(rows, "latest"),
            "corrections": sum((row["revision"] or 0) > 1 for row in rows),
            "review_complete": complete,
            "agreement_target_met": complete
            and initial["agreements"] * 100
            >= cohort["target_size"] * cohort["agreement_target_percent"],
            "by_buying_case": {
                case: {
                    "policy_version": version,
                    "initial": _metrics(
                        [row for row in rows if row["buying_case"] == case], "initial"
                    ),
                    "latest": _metrics(
                        [row for row in rows if row["buying_case"] == case], "latest"
                    ),
                }
                for case, version in cohort["policy_versions"].items()
            },
            "sampling_method": "oldest_eligible_at_enrollment",
            "limitations": [
                "Only dossiers qualified under the frozen policy versions are eligible; unresearched or differently versioned prospects are excluded.",
                "The sample is not randomized and may reflect discovery and website-visibility bias.",
                "This measures human qualification agreement, not customer conversion, fulfillment or complete customer-history coverage.",
            ],
        }

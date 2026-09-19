"""Durable cold deployment coordination; native runtime verification is mandatory.

The runtime adapter must reconcile native schedules/plugins and verify its live
generation before commit or abort. There is deliberately no default verifier and
no public activation endpoint until that native adapter is implemented.
"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from psycopg2.extras import Json

from robothor.operations.gates import gate
from robothor.operations.store import Conflict
from robothor.sales.models import SalesSettings
from robothor.sales.service import operator
from robothor.templates.fleet_snapshot import load_snapshot
from robothor.templates.fleet_store import staged_release_path

STRUCTURAL = ("fleet_release_id", "agents", "workflow_bindings")
SWITCHES = (
    "research_enabled",
    "enrichment_enabled",
    "promotion_enabled",
    "sending_enabled",
    "outcomes_enabled",
)


def assert_structure_editable(cur, sales, previous, changes):
    if "fleet_release_id" in changes and changes["fleet_release_id"] != previous.get(
        "fleet_release_id"
    ):
        raise Conflict("Use the deployment coordinator to change fleet selection")
    if not any(key in changes and changes[key] != previous.get(key) for key in STRUCTURAL):
        return
    cur.execute(
        "SELECT 1 FROM sales_deployments WHERE tenant_id=%s AND status='preparing'", (sales.tenant,)
    )
    if previous.get("fleet_release_id") or cur.fetchone():
        raise Conflict("Use the deployment coordinator to change managed fleet bindings")


def assert_queue_open(sales):
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT 1 FROM sales_deployments WHERE tenant_id=%s AND status='preparing' LIMIT 1",
            (sales.tenant,),
        )
        if cur.fetchone():
            raise Conflict("Sales deployment is awaiting runtime reconciliation")


class DeploymentCoordinator:
    def __init__(self, sales, workspace):
        self.sales, self.workspace = sales, workspace

    def _settings(self, cur):
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (self.sales.tenant + ":sales-settings",),
        )
        cur.execute(
            "SELECT config,revision FROM sales_settings WHERE tenant_id=%s FOR UPDATE",
            (self.sales.tenant,),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Configure sales settings before deployment")
        return row

    def _record(self, cur, transition_id):
        cur.execute(
            "SELECT * FROM sales_deployments WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (self.sales.tenant, str(transition_id)),
        )
        row = cur.fetchone()
        if not row:
            raise Conflict("Deployment transition not found")
        return dict(row)

    def _idle(self, cur):
        cur.execute(
            "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND kind LIKE 'sales.%%' AND status='running' LIMIT 1",
            (self.sales.tenant,),
        )
        if cur.fetchone():
            raise Conflict("Deployment refused: unfinished sales work")
        cur.execute(
            "SELECT 1 FROM operation_actions WHERE tenant_id=%s AND kind LIKE 'sales.%%' AND status IN ('executing','unknown') LIMIT 1",
            (self.sales.tenant,),
        )
        if cur.fetchone():
            raise Conflict("Deployment refused: unfinished sales action")
        cur.execute(
            "SELECT 1 FROM operation_effects WHERE tenant_id=%s AND (kind LIKE 'sales.%%' OR kind LIKE 'pipedrive.%%' OR kind LIKE 'instantly.%%') AND status IN ('executing','unknown') LIMIT 1",
            (self.sales.tenant,),
        )
        if cur.fetchone():
            raise Conflict("Deployment refused: unfinished provider effect")

    def _artifact(self, release_id):
        if release_id is None:
            return {"release_id": None}, None
        snapshot = load_snapshot(
            staged_release_path(self.workspace, release_id), expected_digest=release_id
        )
        metadata = snapshot.metadata()
        settings = snapshot.document(metadata.get("sales_settings"))
        return {
            "release_id": release_id,
            "platform_revision": snapshot.platform_revision,
            "contracts": metadata["contracts"],
        }, settings

    def status(self):
        with self.sales.ops.transaction() as cur:
            cur.execute(
                "SELECT revision FROM sales_settings WHERE tenant_id=%s", (self.sales.tenant,)
            )
            settings = cur.fetchone()
            cur.execute(
                "SELECT * FROM sales_deployments WHERE tenant_id=%s AND status='preparing'",
                (self.sales.tenant,),
            )
            row = cur.fetchone()
            return {
                "settings_revision": settings["revision"] if settings else 0,
                "pending": dict(row) if row else None,
            }

    def prepare(self, release_id, *, expected_revision, actor, reason):
        operator(actor)
        artifact, settings = self._artifact(release_id)
        if settings is None:
            raise Conflict("Deploying requires a verified sales release")
        target = SalesSettings.model_validate(settings).model_dump(mode="json")
        target["fleet_release_id"] = release_id
        return self._prepare(target, artifact, expected_revision, actor, reason, "deploy")

    def prepare_rollback(self, transition_id, *, expected_revision, actor, reason):
        operator(actor)
        with self.sales.ops.transaction() as cur:
            prior = self._record(cur, transition_id)
        if prior["status"] != "committed":
            raise Conflict("Only a committed deployment can be rolled back")
        artifact, _ = self._artifact(prior["source_release_id"])
        return self._prepare(
            prior["previous_config"], artifact, expected_revision, actor, reason, "rollback", prior
        )

    def _prepare(self, desired, artifact, expected_revision, actor, reason, direction, prior=None):
        operator(actor)
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not isinstance(reason, str)
            or not 10 <= len(reason.strip()) <= 2000
        ):
            raise ValueError("A settings revision and explicit deployment reason are required")
        with gate(self.sales.ops, "sales-fleet") as cur:
            self._idle(cur)
            current = self._settings(cur)
            if current["revision"] != expected_revision:
                raise Conflict("Sales settings changed before deployment preparation")
            cur.execute(
                "SELECT 1 FROM sales_deployments WHERE tenant_id=%s AND status='preparing'",
                (self.sales.tenant,),
            )
            if cur.fetchone():
                raise Conflict("Another deployment is already preparing")
            previous = SalesSettings.model_validate(current["config"]).model_dump(mode="json")
            if prior and previous["fleet_release_id"] != prior["target_release_id"]:
                raise Conflict("Deployment is no longer the selected release")
            if prior:
                cur.execute(
                    "SELECT id FROM sales_deployments WHERE tenant_id=%s AND status='committed' ORDER BY base_revision DESC LIMIT 1",
                    (self.sales.tenant,),
                )
                latest = cur.fetchone()
                if not latest or latest["id"] != prior["id"]:
                    raise Conflict("Only the latest committed deployment can be rolled back")
            target = {
                **previous,
                **{key: desired.get(key) for key in STRUCTURAL},
                **dict.fromkeys(SWITCHES, False),
            }
            target = SalesSettings.model_validate(target).model_dump(mode="json")
            if direction == "deploy" and target["fleet_release_id"] == previous["fleet_release_id"]:
                raise Conflict("Release is already selected")
            source_artifact, _ = self._artifact(previous["fleet_release_id"])
            transition_id = str(uuid4())
            cur.execute(
                "INSERT INTO sales_deployments(id,tenant_id,direction,source_transition_id,source_release_id,target_release_id,base_revision,previous_config,target_config,source_artifact,target_artifact,actor,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    transition_id,
                    self.sales.tenant,
                    direction,
                    prior["id"] if prior else None,
                    previous["fleet_release_id"],
                    target["fleet_release_id"],
                    expected_revision,
                    Json(previous),
                    Json(target),
                    Json(source_artifact),
                    Json(artifact),
                    actor,
                    reason.strip(),
                ),
            )
            self.sales.ops.audit(
                cur,
                transition_id,
                "sales.deployment.prepared",
                actor,
                {
                    "direction": direction,
                    "release_id": target["fleet_release_id"],
                    "settings_revision": expected_revision,
                },
            )
            return self._record(cur, transition_id)

    def _runtime(self, runtime, record, *, restoring=False):
        if runtime is None:
            raise Conflict("Native runtime verification is required")
        key = "source_artifact" if restoring else "target_artifact"
        current, _ = self._artifact(record[key]["release_id"])
        if current != record[key]:
            raise Conflict("Deployment artifact changed")
        try:
            proof = runtime.verify(deepcopy(record), restoring=restoring)
        except Exception:
            raise Conflict("Native runtime reconciliation is not verified") from None
        if (
            not isinstance(proof, dict)
            or proof.get("transition_id") != str(record["id"])
            or proof.get("release_id") != current["release_id"]
            or proof.get("restoring") is not restoring
            or not isinstance(proof.get("runtime_generation"), str)
            or not 1 <= len(proof["runtime_generation"]) <= 200
        ):
            raise Conflict("Native runtime evidence does not match this transition")
        # The runtime adapter owns live platform/plugin/schedule verification.
        # Do not accept proof documents from an HTTP caller or an agent tool.
        return {
            key: proof[key]
            for key in ("transition_id", "release_id", "runtime_generation", "restoring")
        }

    def commit(self, transition_id, runtime, *, actor):
        operator(actor)
        with gate(self.sales.ops, "sales-fleet") as cur:
            self._idle(cur)
            current = self._settings(cur)
            record = self._record(cur, transition_id)
            if record["status"] != "preparing":
                raise Conflict("Deployment is not preparing")
            if current["revision"] != record["base_revision"]:
                raise Conflict("Sales settings changed after deployment preparation")
            proof = self._runtime(runtime, record)
            cur.execute(
                "UPDATE sales_settings SET config=%s,revision=revision+1,updated_at=now() WHERE tenant_id=%s",
                (Json(record["target_config"]), self.sales.tenant),
            )
            cur.execute(
                "UPDATE sales_deployments SET status='committed',runtime_evidence=%s,completed_at=now() WHERE tenant_id=%s AND id=%s",
                (Json(proof), self.sales.tenant, str(transition_id)),
            )
            self.sales.ops.audit(cur, transition_id, "sales.deployment.committed", actor, proof)
            return self._record(cur, transition_id)

    def abort(self, transition_id, runtime, *, actor, reason):
        operator(actor)
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 2000:
            raise ValueError("An explicit abort reason is required")
        with gate(self.sales.ops, "sales-fleet") as cur:
            self._idle(cur)
            self._settings(cur)
            record = self._record(cur, transition_id)
            if record["status"] != "preparing":
                raise Conflict("Deployment is not preparing")
            proof = self._runtime(runtime, record, restoring=True)
            cur.execute(
                "UPDATE sales_deployments SET status='aborted',runtime_evidence=%s,completed_at=now() WHERE tenant_id=%s AND id=%s",
                (Json(proof), self.sales.tenant, str(transition_id)),
            )
            self.sales.ops.audit(
                cur,
                transition_id,
                "sales.deployment.aborted",
                actor,
                {**proof, "reason": reason.strip()},
            )
            return self._record(cur, transition_id)

"""Lease-fenced immutable fragments for recovering partially completed paid work."""

import re
from typing import Any

from psycopg2.extras import Json

from robothor.operations.store import Conflict, digest


class Fragments:
    def __init__(self, operations: Any) -> None:
        self.ops = operations

    def _read(self, cur: Any, job: dict[str, Any], input_hash: str) -> dict[str, Any]:
        if not isinstance(input_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", input_hash):
            raise ValueError("Invalid fragment input hash")
        cur.execute(
            "SELECT id FROM operation_jobs WHERE tenant_id=%s AND id=%s AND lease_token=%s "
            "AND status='running' AND lease_until>clock_timestamp() "
            "AND deadline>clock_timestamp() FOR UPDATE",
            (self.ops.tenant, job["id"], job["lease_token"]),
        )
        if not cur.fetchone():
            raise Conflict("Fragment work lease expired or replaced")
        # PostgreSQL may evaluate the temporal predicate before waiting for
        # FOR UPDATE. Recheck the clock after acquiring that row lock.
        cur.execute(
            "SELECT 1 FROM operation_jobs WHERE tenant_id=%s AND id=%s "
            "AND lease_until>clock_timestamp() AND deadline>clock_timestamp()",
            (self.ops.tenant, job["id"]),
        )
        if not cur.fetchone():
            raise Conflict("Fragment lease expired while acquiring the work lock")
        cur.execute(
            "SELECT fragment_key,input_hash,payload,payload_hash FROM operation_fragments "
            "WHERE tenant_id=%s AND job_id=%s",
            (self.ops.tenant, job["id"]),
        )
        rows = cur.fetchall()
        if any(
            row["input_hash"] != input_hash or digest(row["payload"]) != row["payload_hash"]
            for row in rows
        ):
            raise Conflict("Fragment inputs or stored content changed; review required")
        return {row["fragment_key"]: row["payload"] for row in rows}

    def read(self, job: dict[str, Any], input_hash: str) -> dict[str, Any]:
        with self.ops.transaction() as cur:
            return self._read(cur, job, input_hash)

    def put(self, job: dict[str, Any], key: str, input_hash: str, payload: dict[str, Any]) -> None:
        if not isinstance(key, str) or not 0 < len(key) <= 100 or not isinstance(payload, dict):
            raise ValueError("Invalid fragment key or payload")
        payload_hash = digest(payload)
        with self.ops.transaction() as cur:
            existing = self._read(cur, job, input_hash)
            if key in existing:
                if digest(existing[key]) != payload_hash:
                    raise Conflict("A paid fragment cannot be replaced")
                return
            cur.execute(
                "INSERT INTO operation_fragments(tenant_id,job_id,fragment_key,input_hash,payload,payload_hash) "
                "SELECT %s,%s,%s,%s,%s,%s FROM operation_jobs WHERE tenant_id=%s AND id=%s "
                "AND lease_until>clock_timestamp() AND deadline>clock_timestamp()",
                (
                    self.ops.tenant,
                    job["id"],
                    key,
                    input_hash,
                    Json(payload),
                    payload_hash,
                    self.ops.tenant,
                    job["id"],
                ),
            )
            if cur.rowcount != 1:
                raise Conflict("Fragment lease expired before persistence")
            self.ops.audit(
                cur,
                job["id"],
                "work.fragment_saved",
                detail={"key": key, "input_hash": input_hash, "payload_hash": payload_hash},
            )

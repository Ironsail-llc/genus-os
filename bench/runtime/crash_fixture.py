"""Disposable-database crash fixture. Exits after committing one synthetic receipt."""

import asyncio
import os
import sys
from contextlib import contextmanager
from uuid import uuid4

import psycopg2

from bench.runtime import store_host
from bench.runtime.adapter import CandidateRuntime
from bench.runtime.candidates import FixtureGateway
from bench.runtime.test_candidate_boundaries import candidate
from robothor.engine.runtime import controls
from robothor.engine.runtime.contracts import ExecutionContext, RunRequest


def main(dsn, tenant, framework):
    @contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    class ReceiptGateway(FixtureGateway):
        async def invoke(self, tenant, name, arguments):
            await super().invoke(tenant, name, arguments)
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO synthetic_crash_receipts(tenant_id) VALUES (%s)", (tenant,)
                )
            os._exit(23)  # No finally blocks, result persistence or framework cleanup.

    async def gateway(request):
        return ReceiptGateway(tenant)

    store_host.get_connection = controls.get_connection = connect
    adapter = candidate(framework, [("record", {"key": "report", "value": "delivered"})], [])
    runtime = CandidateRuntime(adapter, store_host.StoreHost(gateway))
    req = RunRequest(
        ExecutionContext(tenant, "operator", str(uuid4())), "synthetic", "record report=delivered"
    )
    asyncio.run(runtime.run(req))
    raise AssertionError("fixture must terminate at the committed synthetic effect")


if __name__ == "__main__":
    main(*sys.argv[1:])

"""Tenant-scoped readback of durable interrupted writes, independent of chat."""

import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from types import SimpleNamespace

from psycopg2.extras import RealDictCursor

from robothor.engine import calendar_operations as operations
from robothor.engine.calendar_reconciliation import reconcile_record

logger = logging.getLogger(__name__)

# Five sequential provider reads may each need credential refresh and readback.
# Bound the whole process too, including database connection/query stalls.
BATCH_TIMEOUT_SECONDS = 300


def sweep(tenant_id: str) -> int:
    """Inspect a bounded oldest-first batch; the shared writer lock decides eligibility."""
    with operations.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id,user_id,agent_id FROM calendar_operations
               WHERE tenant_id=%s AND status='executing'
                 AND updated_at < now() - interval '30 seconds'
               ORDER BY updated_at,id LIMIT 5""",
            (tenant_id,),
        )
        rows = cur.fetchall()
    for row in rows:
        try:
            # Identity comes from the authorized write-ahead record. This worker
            # can only GET the recorded resource; it never admits a new action.
            auth = SimpleNamespace(tenant_id=tenant_id, user_id=row["user_id"])
            reconcile_record(str(row["id"]), auth, row["agent_id"])
        except Exception as exc:
            logger.warning("Calendar recovery operation deferred (%s)", type(exc).__name__)
    return len(rows)


def _credentials_for_child() -> bytes:
    """What a batch needs from the engine and may not inherit.

    A process's environment is readable by its own uid until it hardens itself,
    and a batch is a same-uid child that needs the database. So these travel on
    a pipe, and the batch's environment never holds them.
    """
    from robothor.secrets.classification import is_platform_credential

    return json.dumps({k: v for k, v in os.environ.items() if is_platform_credential(k)}).encode()


def _receive_credentials() -> None:
    """The batch's half: harden first, then take the pipe's contents."""
    from robothor.engine.process_hardening import drop_inherited_credentials, harden_process

    harden_process()
    os.environ.update(json.loads(sys.stdin.buffer.read() or b"{}"))
    drop_inherited_credentials()


async def run(tenant_id: str) -> None:
    """Isolate blocking provider IO so daemon shutdown can stop it promptly."""
    while True:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                __name__,
                "--tenant",
                tenant_id,
                stdin=asyncio.subprocess.PIPE,
            )
            if process.stdin is not None:
                process.stdin.write(_credentials_for_child())
                await process.stdin.drain()
                process.stdin.close()
            code = await asyncio.wait_for(process.wait(), timeout=BATCH_TIMEOUT_SECONDS)
            if code:
                logger.warning("Calendar recovery batch exited (%s)", code)
        except Exception as exc:
            logger.warning("Calendar recovery sweep deferred (%s)", type(exc).__name__)
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        await asyncio.sleep(30)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    tenant = parser.parse_args().tenant
    _receive_credentials()
    sweep(tenant)

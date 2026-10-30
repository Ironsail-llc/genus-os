"""Memory proxy routes — HTTP-accessible memory operations."""

from __future__ import annotations

import logging

from deps import get_tenant_id
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from models import (  # noqa: TC002 — used at runtime by FastAPI
    MemoryBlockAppendRequest,
    MemoryBlockWriteRequest,
    MemorySearchRequest,
    MemoryStoreRequest,
)

from robothor.audit.logger import log_event
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.post("/search")
async def memory_search(body: MemorySearchRequest, tenant_id: str = Depends(get_tenant_id)):
    """Semantic search over memory facts."""
    try:
        from robothor.memory.facts import search_facts

        results = await search_facts(body.query, limit=body.limit, tenant_id=tenant_id)
        return {"results": results, "count": len(results)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/store")
async def memory_store(
    body: MemoryStoreRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Store content and extract facts."""
    from robothor.constants import DEFAULT_TENANT

    # ``ingest_content`` is not tenant-aware yet.  Do not silently route a
    # secondary tenant's content into the primary tenant while that boundary
    # remains unresolved.
    if tenant_id != DEFAULT_TENANT:
        return JSONResponse(
            {"error": "memory ingestion is not available for this tenant"},
            status_code=403,
        )
    try:
        from robothor.memory.ingestion import ingest_content

        result = await ingest_content(
            body.content,
            source_channel="api",
            content_type=body.content_type,
            metadata={"tenant_id": tenant_id},
        )
        return {"status": "ok", "facts_extracted": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/entity/{name}")
async def memory_entity(name: str, tenant_id: str = Depends(get_tenant_id)):
    """Get entity with relationships from the knowledge graph."""
    try:
        from robothor.memory.entities import get_all_about

        result = await get_all_about(name, tenant_id=tenant_id)
        return result or {"entity": name, "relations": []}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/stats")
def memory_stats(tenant_id: str = Depends(get_tenant_id)):
    """Get memory system statistics."""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            stats = {}
            for table in ("memory_facts", "memory_entities", "memory_relations"):
                cur.execute(  # noqa: S608 -- table is selected from a constant allowlist
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id = %s",
                    (tenant_id,),
                )
                stats[table] = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE is_active = true AND tenant_id = %s",
                (tenant_id,),
            )
            stats["active_facts"] = cur.fetchone()[0]
            return stats
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Memory Blocks ───────────────────────────────────────────────────────


@router.get("/blocks")
def list_memory_blocks(tenant_id: str = Depends(get_tenant_id)):
    """List all memory blocks."""
    try:
        from robothor.memory.blocks import list_blocks

        return list_blocks(tenant_id=tenant_id)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/blocks/{block_name}")
def get_memory_block(
    block_name: str,
    tenant_id: str = Depends(get_tenant_id),
):
    """Read a named memory block."""
    try:
        from robothor.memory.blocks import read_block

        result = read_block(block_name, tenant_id=tenant_id)
        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=404)
        return result
    except Exception as e:
        logger.exception("Failed to read memory block: %s", e)
        return JSONResponse({"error": "internal error"}, status_code=500)


@router.put("/blocks/{block_name}")
def put_memory_block(
    block_name: str,
    body: MemoryBlockWriteRequest,
    tenant_id: str = Depends(get_tenant_id),
):
    """Write/update a named memory block."""
    try:
        from robothor.memory.blocks import write_block

        result = write_block(block_name, body.content, tenant_id=tenant_id)
        if result.get("success"):
            log_event(
                "crm.update",
                f"Memory block '{block_name}' updated",
                details={"block_name": block_name, "size": len(body.content)},
            )
        return result
    except Exception as e:
        logger.exception("Failed to write memory block: %s", e)
        return JSONResponse({"error": "internal error"}, status_code=500)


@router.post("/blocks/{block_name}/append")
def append_memory_block(
    block_name: str, body: MemoryBlockAppendRequest, tenant_id: str = Depends(get_tenant_id)
):
    """Append a timestamped entry to a memory block, trimming oldest."""
    try:
        from robothor.crm.dal import append_to_block

        ok = append_to_block(
            block_name, body.entry, max_entries=body.maxEntries, tenant_id=tenant_id
        )
        if ok:
            return {"success": True, "block_name": block_name}
        return JSONResponse({"error": "failed to append"}, status_code=500)
    except Exception as e:
        logger.exception("Failed to append to memory block: %s", e)
        return JSONResponse({"error": "internal error"}, status_code=500)


# ─── Pipeline Status & Trigger ─────────────────────────────────────────


@router.get("/pipeline/status")
def pipeline_status(tenant_id: str = Depends(get_tenant_id)):
    """Get intelligence pipeline status — watermarks and last run times."""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            # Get ingest watermarks
            cur.execute(
                "SELECT source_name, last_ingested_at, items_ingested, "
                "last_error, error_count, updated_at "
                "FROM ingestion_watermarks WHERE tenant_id = %s ORDER BY source_name",
                (tenant_id,),
            )
            watermarks = [
                {
                    "source": r[0],
                    "last_ingested_at": r[1].isoformat() if r[1] else None,
                    "items_ingested": r[2],
                    "last_error": r[3],
                    "error_count": r[4],
                    "updated_at": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]
            # Get recent pipeline runs from audit log
            cur.execute(
                "SELECT event_type, action, timestamp, status, details "
                "FROM audit_log WHERE event_type LIKE 'pipeline.%%' "
                "AND details->>'tenant_id' = %s "
                "ORDER BY timestamp DESC LIMIT 10",
                (tenant_id,),
            )
            runs = [
                {
                    "event_type": r[0],
                    "action": r[1],
                    "timestamp": r[2].isoformat(),
                    "status": r[3],
                    "details": r[4],
                }
                for r in cur.fetchall()
            ]
            return {"watermarks": watermarks, "recent_runs": runs}
    except Exception as e:
        logger.exception("Failed to get pipeline status: %s", e)
        return JSONResponse({"error": "internal error"}, status_code=500)


@router.post("/pipeline/trigger/{tier}")
def pipeline_trigger(
    tier: int,
    tenant_id: str = Depends(get_tenant_id),
):
    """Trigger a pipeline tier on demand (1=ingest, 2=analysis, 3=deep)."""
    import subprocess

    from robothor.config import get_config
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        return JSONResponse(
            {"error": "pipeline trigger is not available for this tenant"},
            status_code=403,
        )

    cfg = get_config()
    scripts = {
        1: cfg.workspace / "memory_system" / "continuous_ingest.py",
        2: cfg.workspace / "memory_system" / "periodic_analysis.py",
        3: cfg.workspace / "memory_system" / "intelligence_pipeline.py",
    }
    script = scripts.get(tier)
    if not script:
        return JSONResponse({"error": f"Invalid tier: {tier}. Use 1, 2, or 3."}, status_code=400)
    if not script.exists():
        return JSONResponse({"error": f"Script not found: {script}"}, status_code=404)

    try:
        proc = subprocess.Popen(  # noqa: S603
            ["python3", str(script)],
            cwd=str(script.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log_event(
            "pipeline.trigger",
            f"Tier {tier} pipeline triggered",
            details={
                "tier": tier,
                "script": str(script),
                "pid": proc.pid,
                "tenant_id": tenant_id,
            },
        )
        return {"status": "triggered", "tier": tier, "pid": proc.pid}
    except Exception as e:
        logger.exception("Failed to trigger pipeline: %s", e)
        return JSONResponse({"error": "internal error"}, status_code=500)

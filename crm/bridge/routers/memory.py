"""Memory proxy routes — HTTP-accessible memory operations."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from models import (
    MemoryBlockAppendRequest,
    MemoryBlockWriteRequest,
    MemorySearchRequest,
    MemoryStoreRequest,
)

from robothor.audit.logger import log_event
from robothor.db.connection import get_connection

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.post("/search")
async def memory_search(body: MemorySearchRequest):
    """Semantic search over memory facts."""
    try:
        from robothor.memory.facts import search_facts

        results = await search_facts(body.query, limit=body.limit)
        return {"results": results, "count": len(results)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/store")
async def memory_store(body: MemoryStoreRequest):
    """Store content and extract facts."""
    try:
        from robothor.memory.ingestion import ingest_content

        result = await ingest_content(body.content, source_channel=body.content_type)
        return {"status": "ok", "facts_extracted": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/entity/{name}")
async def memory_entity(name: str):
    """Get entity with relationships from the knowledge graph."""
    try:
        from robothor.memory.entities import get_all_about

        result = await get_all_about(name)
        return result or {"entity": name, "relations": []}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/stats")
async def memory_stats():
    """Get memory system statistics."""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            stats = {}
            for table in ("memory_facts", "memory_entities", "memory_relations"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                stats[table] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM memory_facts WHERE is_active = true")
            stats["active_facts"] = cur.fetchone()[0]
            return stats
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Memory Blocks ───────────────────────────────────────────────────────


@router.get("/blocks")
async def list_memory_blocks():
    """List all memory blocks."""
    try:
        from robothor.memory.blocks import list_blocks

        return list_blocks()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/blocks/{block_name}")
async def get_memory_block(block_name: str):
    """Read a named memory block."""
    try:
        from robothor.memory.blocks import read_block

        result = read_block(block_name)
        if "error" in result:
            return JSONResponse({"error": result["error"]}, status_code=404)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/blocks/{block_name}")
async def put_memory_block(block_name: str, body: MemoryBlockWriteRequest):
    """Write/update a named memory block."""
    try:
        from robothor.memory.blocks import write_block

        result = write_block(block_name, body.content)
        if result.get("success"):
            log_event(
                "crm.update",
                f"Memory block '{block_name}' updated",
                details={"block_name": block_name, "size": len(body.content)},
            )
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/blocks/{block_name}/append")
async def append_memory_block(block_name: str, body: MemoryBlockAppendRequest):
    """Append a timestamped entry to a memory block, trimming oldest."""
    try:
        from robothor.crm.dal import append_to_block

        ok = append_to_block(block_name, body.entry, max_entries=body.maxEntries)
        if ok:
            return {"success": True, "block_name": block_name}
        return JSONResponse({"error": "failed to append"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Pipeline Status & Trigger ─────────────────────────────────────────


@router.get("/pipeline/status")
async def pipeline_status():
    """Get intelligence pipeline status — watermarks and last run times."""
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            # Get ingest watermarks
            cur.execute(
                "SELECT source_name, last_ingested_at, items_ingested, "
                "last_error, error_count, updated_at "
                "FROM ingestion_watermarks ORDER BY source_name",
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
                "ORDER BY timestamp DESC LIMIT 10",
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
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/pipeline/trigger/{tier}")
async def pipeline_trigger(tier: int):
    """Trigger a pipeline tier on demand (1=ingest, 2=analysis, 3=deep)."""
    import subprocess

    from robothor.config import get_config

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
            details={"tier": tier, "script": str(script), "pid": proc.pid},
        )
        return {"status": "triggered", "tier": tier, "pid": proc.pid}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

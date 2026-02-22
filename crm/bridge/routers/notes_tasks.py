"""Notes & Tasks routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from robothor.crm.dal import (
    create_note,
    create_task,
    delete_note,
    delete_task,
    get_note,
    get_task,
    list_notes,
    list_tasks,
)

from models import CreateNoteRequest

router = APIRouter(prefix="/api", tags=["notes", "tasks"])


# ─── Notes ───────────────────────────────────────────────────────────────


@router.get("/notes")
async def api_list_notes(
    personId: str | None = Query(None),
    companyId: str | None = Query(None),
    limit: int = Query(50),
):
    return {"notes": list_notes(personId, companyId, limit)}


@router.post("/notes")
async def api_create_note(body: CreateNoteRequest):
    if not body.title:
        return JSONResponse({"error": "title required"}, status_code=400)
    note_id = create_note(body.title, body.body, body.personId, body.companyId)
    if note_id:
        return {"id": note_id, "title": body.title}
    return JSONResponse({"error": "failed to create note"}, status_code=500)


@router.get("/notes/{note_id}")
async def api_get_note(note_id: str):
    result = get_note(note_id)
    if not result:
        return JSONResponse({"error": "note not found"}, status_code=404)
    return result


@router.delete("/notes/{note_id}")
async def api_delete_note(note_id: str):
    if delete_note(note_id):
        return {"success": True, "id": note_id}
    return JSONResponse({"error": "note not found"}, status_code=404)


# ─── Tasks ───────────────────────────────────────────────────────────────


@router.get("/tasks")
async def api_list_tasks(
    status: str | None = Query(None),
    personId: str | None = Query(None),
    limit: int = Query(50),
):
    return {"tasks": list_tasks(status, personId, limit)}


@router.get("/tasks/{task_id}")
async def api_get_task(task_id: str):
    result = get_task(task_id)
    if not result:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return result


@router.delete("/tasks/{task_id}")
async def api_delete_task(task_id: str):
    if delete_task(task_id):
        return {"success": True, "id": task_id}
    return JSONResponse({"error": "task not found"}, status_code=404)

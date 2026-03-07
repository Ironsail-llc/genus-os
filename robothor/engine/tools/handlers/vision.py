"""Vision tool handlers — look, who_is_here, enroll/unenroll, mode."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from robothor.engine.tools.dispatch import ToolContext, _cfg

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("look")
async def _look(args: dict, ctx: ToolContext) -> dict:
    prompt = args.get("prompt", "Describe what you see in this image in detail.")
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/look", json={"prompt": prompt})
        resp.raise_for_status()
        return dict(resp.json())


@_handler("who_is_here")
async def _who_is_here(args: dict, ctx: ToolContext) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_cfg().vision_url}/health")
        resp.raise_for_status()
        data = resp.json()
        return {
            "people_present": data.get("people_present", []),
            "running": data.get("running", False),
            "mode": data.get("mode"),
            "last_detection": data.get("last_detection"),
        }


@_handler("enroll_face")
async def _enroll_face(args: dict, ctx: ToolContext) -> dict:
    face_name = args.get("name", "")
    if not face_name:
        return {"error": "Name is required for face enrollment"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/enroll", json={"name": face_name})
        resp.raise_for_status()
        return dict(resp.json())


@_handler("enroll_face_from_image")
async def _enroll_face_from_image(args: dict, ctx: ToolContext) -> dict:
    face_name = args.get("name", "")
    image_paths = args.get("image_paths", [])
    if not face_name:
        return {"error": "Name is required"}
    if not image_paths:
        return {"error": "image_paths is required"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_cfg().vision_url}/enroll-from-image",
            json={"name": face_name, "image_paths": image_paths},
        )
        resp.raise_for_status()
        return dict(resp.json())


@_handler("list_enrolled_faces")
async def _list_enrolled_faces(args: dict, ctx: ToolContext) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_cfg().vision_url}/enrolled")
        resp.raise_for_status()
        return dict(resp.json())


@_handler("unenroll_face")
async def _unenroll_face(args: dict, ctx: ToolContext) -> dict:
    face_name = args.get("name", "")
    if not face_name:
        return {"error": "Name is required"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/unenroll", json={"name": face_name})
        resp.raise_for_status()
        return dict(resp.json())


@_handler("set_vision_mode")
async def _set_vision_mode(args: dict, ctx: ToolContext) -> dict:
    mode = args.get("mode", "")
    if mode not in ("disarmed", "basic", "armed"):
        return {"error": f"Invalid mode: {mode}. Valid: disarmed, basic, armed"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_cfg().vision_url}/mode", json={"mode": mode})
        resp.raise_for_status()
        return dict(resp.json())


@_handler("log_interaction")
async def _log_interaction(args: dict, ctx: ToolContext) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_cfg().bridge_url}/log-interaction",
            json={
                k: args.get(k, "")
                for k in [
                    "contact_name",
                    "channel",
                    "direction",
                    "content_summary",
                    "channel_identifier",
                ]
            },
        )
        resp.raise_for_status()
        return dict(resp.json())

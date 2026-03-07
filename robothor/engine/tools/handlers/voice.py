"""Voice tool handlers — make_call."""

from __future__ import annotations

from typing import Any

import httpx

from robothor.engine.tools.dispatch import ToolContext, _cfg

HANDLERS: dict[str, Any] = {}


async def _make_call(args: dict, ctx: ToolContext) -> dict:
    to_number = args.get("to", "")
    recipient = args.get("recipient", "someone")
    purpose = args.get("purpose", "")
    if not to_number:
        return {"error": "Missing 'to' phone number"}
    if not purpose:
        return {"error": "Missing 'purpose' for the call"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_cfg().voice_url}/call",
                json={"to": to_number, "recipient": recipient, "purpose": purpose},
            )
            resp.raise_for_status()
            return dict(resp.json())
    except Exception as e:
        return {"error": f"Call failed: {e}"}


HANDLERS["make_call"] = _make_call

"""Knowledge-base tool handlers — kb_search, kb_upsert_document,
kb_delete_document, kb_list_collections.

Curated reference documents, grouped in collections named ``<plugin>/<topic>``
(robothor/knowledge/, docs/knowledge-base.md). The tenant always comes from
the run's context, never from the arguments.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from robothor.constants import SANDBOX_DENIAL_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

#: Largest document text kb_upsert_document accepts (characters), about 500
#: chunks of 400 words. Checked before anything is embedded: every chunk is
#: one embedding call, so an unbounded paste is an unbounded tool call.
MAX_DOCUMENT_CHARS = 200_000

# Writes are refused in a benchmark run: fixture text stored as reference
# documents would be read back by later real runs as curated fact.
_MUTATING: frozenset[str] = frozenset({"kb_upsert_document", "kb_delete_document"})


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(
            args: dict[str, Any], ctx: ToolContext, _fn: Callable[..., Any] = fn
        ) -> dict[str, Any]:
            if name in _MUTATING and ctx.is_benchmark:
                return {
                    "error": f"{SANDBOX_DENIAL_PREFIX} {name} writes are disabled",
                    "guard": "is_benchmark",
                }
            try:
                return cast("dict[str, Any]", await _fn(args, ctx))
            except ValueError as exc:
                return {"error": str(exc)}

        HANDLERS[name] = wrapped
        return fn

    return decorator


def _required(args: dict[str, Any], *names: str) -> None:
    missing = [n for n in names if not str(args.get(n) or "").strip()]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")


@_handler("kb_search")
async def _kb_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.knowledge import search as kb_search

    _required(args, "collection", "query")
    results = await kb_search.search(
        ctx.tenant_id, args["collection"], args["query"], limit=int(args.get("limit") or 5)
    )
    return {"results": results, "count": len(results)}


@_handler("kb_upsert_document")
async def _kb_upsert_document(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.knowledge import store

    _required(args, "collection", "doc_id", "text")
    length = len(str(args["text"]))
    if length > MAX_DOCUMENT_CHARS:
        return {
            "error": "document_too_large",
            "limit": MAX_DOCUMENT_CHARS,
            "length": length,
            "hint": "split the document into several doc_ids",
        }
    result = await store.upsert_document(
        ctx.tenant_id,
        args["collection"],
        args["doc_id"],
        str(args.get("title") or ""),
        args["text"],
        source_uri=str(args.get("source_uri") or ""),
    )
    return {"collection": args["collection"], "doc_id": args["doc_id"], **result}


@_handler("kb_delete_document")
async def _kb_delete_document(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.knowledge import store

    _required(args, "collection", "doc_id")
    deleted = await store.delete_document(ctx.tenant_id, args["collection"], args["doc_id"])
    return {"collection": args["collection"], "doc_id": args["doc_id"], "deleted": deleted}


@_handler("kb_list_collections")
async def _kb_list_collections(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.knowledge import store

    collections = await store.list_collections(ctx.tenant_id)
    for row in collections:
        updated = row.get("updated_at")
        if updated is not None and hasattr(updated, "isoformat"):
            row["updated_at"] = updated.isoformat()
    return {"collections": collections}

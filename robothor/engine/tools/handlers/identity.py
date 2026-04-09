"""Identity mapping tool handlers — link GitHub/JIRA/etc handles to CRM people."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _get_conn():
    """Get a database connection. Use: with _get_conn() as conn:"""
    from robothor.engine.tools.dispatch import get_db

    return get_db()


@_handler("link_identity")
async def _link_identity(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Link a channel identity (github, jira, etc.) to a CRM person."""
    person_id = args.get("person_id", "")
    channel = args.get("channel", "")
    identifier = args.get("identifier", "")

    if not person_id or not channel or not identifier:
        return {"error": "person_id, channel, and identifier are required"}

    display_name = args.get("display_name", "")

    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contact_identifiers
                        (tenant_id, channel, identifier, display_name, person_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s::uuid, now(), now())
                    ON CONFLICT (tenant_id, channel, identifier)
                    DO UPDATE SET person_id = EXCLUDED.person_id,
                                  display_name = EXCLUDED.display_name,
                                  updated_at = now()
                    RETURNING id
                    """,
                    (ctx.tenant_id, channel, identifier, display_name or None, person_id),
                )
                row = cur.fetchone()
                conn.commit()
    except Exception as e:
        return {"error": f"Failed to link identity: {e}"}

    return {
        "linked": True,
        "id": row[0] if row else None,
        "person_id": person_id,
        "channel": channel,
        "identifier": identifier,
    }


@_handler("resolve_identities")
async def _resolve_identities(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Look up all identities for a person across all channels."""
    person_id = args.get("person_id", "")
    channel = args.get("channel", "")
    identifier = args.get("identifier", "")

    if not person_id and not (channel and identifier):
        return {"error": "Provide person_id OR (channel + identifier)"}

    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # If given channel+identifier, first resolve to person_id
                if not person_id and channel and identifier:
                    cur.execute(
                        """
                        SELECT person_id::text FROM contact_identifiers
                        WHERE tenant_id = %s AND channel = %s AND identifier = %s
                        """,
                        (ctx.tenant_id, channel, identifier),
                    )
                    row = cur.fetchone()
                    if not row or not row[0]:
                        return {"error": f"No identity found for {channel}:{identifier}"}
                    person_id = row[0]

                # Get all identities for this person
                cur.execute(
                    """
                    SELECT channel, identifier, display_name
                    FROM contact_identifiers
                    WHERE tenant_id = %s AND person_id = %s::uuid
                    ORDER BY channel, identifier
                    """,
                    (ctx.tenant_id, person_id),
                )
                rows = cur.fetchall()

                # Also get the person's name and email from crm_people
                cur.execute(
                    """
                    SELECT first_name, last_name, email
                    FROM crm_people
                    WHERE id = %s::uuid AND deleted_at IS NULL
                    """,
                    (person_id,),
                )
                person_row = cur.fetchone()
    except Exception as e:
        return {"error": f"Failed to resolve identities: {e}"}

    identities = [{"channel": r[0], "identifier": r[1], "display_name": r[2]} for r in rows]

    result: dict[str, Any] = {
        "person_id": person_id,
        "identities": identities,
        "count": len(identities),
    }
    if person_row:
        result["name"] = f"{person_row[0] or ''} {person_row[1] or ''}".strip()
        result["email"] = person_row[2] or ""

    return result

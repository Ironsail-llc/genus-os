"""One way for a bridge route to write an audit event.

``require_operator`` answers *may this caller act*; this answers *who acted, on
what, and did it work*.  Routes that change appliance state need both — a gate
with no trail leaves an operator unable to say afterwards which of two admins
installed the agent that started sending mail.

Reuses ``robothor.audit.logger.log_event`` (the single audit store the whole
platform writes to, and the one ``/api/audit`` reads back).  There is no second
store here, and no second table.

The details convention, which every caller must honor:

    details carry IDENTIFIERS ONLY — ids, slugs, names, counts, booleans.

Never a secret value, a token, a password, or a marketplace ``variables`` dict:
the audit log is read by auditors, exported to a SIEM
(``robothor.audit.siem``), and is exactly the wrong place for the credential
this PR just stopped serving over HTTP.  ``audited`` takes ``**details``
explicitly rather than a dict so that a caller has to *name* each field it
records, which is what makes that convention reviewable in a diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.audit.logger import log_event

if TYPE_CHECKING:
    from fastapi import Request


def audited(
    request: Request,
    event_type: str,
    *,
    action: str,
    status: str = "ok",
    **details: Any,
) -> None:
    """Write one audit event attributed to the request's verified caller.

    ``action`` is the human-readable subject of the act — a slug or id, never a
    value.  ``status`` follows ``log_event``: ``"ok"``, ``"denied"``,
    ``"error"``.  Extra keyword arguments land in ``details`` (identifiers
    only — see the module docstring).

    Never raises: ``log_event`` swallows its own failures, and an audit write
    must not be the reason a route 500s.
    """
    auth = getattr(request.state, "auth", None)
    actor = (
        getattr(auth, "actor_id", None) or getattr(request.state, "actor_id", None) or "anonymous"
    )
    tenant_id = getattr(auth, "tenant_id", None) or getattr(request.state, "tenant_id", None)

    payload: dict[str, Any] = {k: v for k, v in details.items() if v is not None}
    payload.setdefault("method", request.method)
    payload.setdefault("path", request.url.path)
    if tenant_id:
        payload.setdefault("tenant_id", tenant_id)

    log_event(
        event_type,
        action=action,
        category="bridge",
        actor=str(actor),
        status=status,
        details=payload,
    )

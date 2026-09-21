"""Does this appliance offer personal automation over HTTP at all?

``ROBOTHOR_AUTONOMY_ENABLED`` already decides whether the dashboard serves
``/account/autonomy``, whether an agent's system prompt carries the
standing-grant paragraph and whether the ``browser`` tool schema carries the
delegated-execution wording. The bridge did not read it, so with the feature
switched off the only thing standing in front of ``/api/autonomy/*`` was
``require_personal_owner`` — a check on role and identity, not on whether the
instance offers the feature. Any authenticated ``owner``, ``admin``,
``member`` or ``user`` could therefore still POST an enrollment or a grant:
the two endpoints that store a payment card and hand an agent spending
authority.

The setting's own description says "off means absent". This is the half that
was missing.
"""

from __future__ import annotations

from fastapi import HTTPException


def require_feature_offered() -> None:
    """404 unless this instance offers personal automation.

    404, not 403, and the message says nothing about the caller. A 403 — or a
    404 that explained the caller's role — would confirm that the feature is
    here and merely switched off, which is precisely what "absent" means not
    to say. It is the same answer ``/setup`` gives once an owner exists, and
    the same one the dashboard route gives: a capability this appliance does
    not offer is not a permission the caller happens to lack.

    Applied at the mount (``bridge_service.include_router(...,
    dependencies=[Depends(require_feature_offered)])``) rather than inside the
    router, for two reasons. It covers every route in the router including any
    added later, and it leaves the routes in the assembled app so
    ``crm/bridge/tests/test_mutations_are_gated.py`` still enumerates them — a
    router that vanished would make that file's per-route assertions vacuously
    green, which is the failure it exists to catch.
    """
    from robothor.autonomy.availability import feature_offered

    if not feature_offered():
        raise HTTPException(404, "Not Found")
    return

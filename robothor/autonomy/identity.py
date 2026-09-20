"""One resource owner across dashboard and linked messaging identities."""

from robothor.autonomy.models import Scope
from robothor.autonomy.store import AutonomyStore


def scope_for_actor(tenant_id: str, actor_id: str) -> Scope:
    if not tenant_id or not actor_id:
        raise PermissionError("verified_owner_required")
    with AutonomyStore().transaction() as cur:
        cur.execute(
            "SELECT person_id::text FROM user_accounts WHERE tenant_id=%s AND id::text=%s "
            "UNION SELECT person_id::text FROM tenant_users WHERE tenant_id=%s AND user_id=%s",
            (tenant_id, actor_id, tenant_id, actor_id),
        )
        people = {row["person_id"] for row in cur.fetchall() if row["person_id"]}
    if len(people) != 1:
        raise PermissionError("link_account_identity_before_enrollment")
    return Scope(tenant_id=tenant_id, owner_id="person:" + people.pop())

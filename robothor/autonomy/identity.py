"""One resource owner across dashboard and linked messaging identities."""

from robothor.autonomy.models import Scope
from robothor.autonomy.store import AutonomyStore


class UnlinkedActor(PermissionError):
    """The actor resolves to no person at all - an unlinked service account.

    Distinct from the ambiguity case below. An account with no ``person_id``
    (a machine/service account, or one whose enrolment never completed) is a
    legitimate "no owner" for a prompt-verbosity hint, not a broken enrolment
    an operator must repair - so a caller that only wants a yes/no may treat
    it as a quiet no. A ``PermissionError`` subclass, so existing callers that
    catch ``PermissionError`` are unaffected.
    """


def scope_for_actor(tenant_id: str, actor_id: str) -> Scope:
    if not tenant_id or not actor_id:
        raise PermissionError("verified_owner_required")
    # The Scope does not exist yet — that is what this resolves — but the
    # tenant half is already known and authenticated, so the lookup is bound
    # to it rather than running unbound against `user_accounts`.
    with AutonomyStore().transaction(Scope(tenant_id=tenant_id, owner_id="unresolved")) as cur:
        cur.execute(
            "SELECT person_id::text FROM user_accounts WHERE tenant_id=%s AND id::text=%s "
            "UNION SELECT person_id::text FROM tenant_users WHERE tenant_id=%s AND user_id=%s",
            (tenant_id, actor_id, tenant_id, actor_id),
        )
        people = {row["person_id"] for row in cur.fetchall() if row["person_id"]}
    if len(people) > 1:
        # Ambiguous: the actor is linked to more than one person. Only an
        # operator can decide which owner this is.
        raise PermissionError("link_account_identity_before_enrollment")
    if not people:
        raise UnlinkedActor("account_not_linked_to_person")
    return Scope(tenant_id=tenant_id, owner_id="person:" + people.pop())

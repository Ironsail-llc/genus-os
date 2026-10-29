# Tenant Hierarchy (one deployment, a parent and its children)

One deployment can serve a **parent** tenant and any number of **child**
tenants. The parent's owner and admins can switch into a child from the Helm;
children never see each other or the parent. This runbook is the operator's
side: what the pieces are, how to add a child, how people land in the right
tenant, how to move one who landed in the wrong one, and how to prove it all
holds.

The examples use a root tenant `acme-parent` with one child `acme-child-a`,
and the address `alice@example.com`. Substitute your own.

## Concepts

| Term | Meaning |
|------|---------|
| Root tenant | A tenant with no `parent_tenant_id`. It has exactly one `owner` account (migration 071 allows at most one per tenant). |
| Child tenant | A tenant whose `parent_tenant_id` names another. A child has **no owner**: its most privileged account is an `admin`. |
| Role | One of `owner`, `admin`, `member`, `viewer` (and `guest` for Telegram). **A role is per tenant**: an account has one role in the one tenant it belongs to. |
| Descendant access | An `owner` or `admin` of a tenant may act in that tenant's descendants with the same role (`resolve_accessible_tenants` in `robothor/engine/permissions.py`). A `member` or `viewer` reaches only their own tenant. |
| Acting tenant | The tenant a request acts in. It is a claim **in the access token**, never a header the browser chooses. `POST /api/auth/switch-tenant` re-issues the token for another tenant the account may reach; `GET /api/tenants/accessible` lists those tenants. |
| `GENUS_TENANT_SWITCH_MODE` | The rollout flag for switching (`off`, `observe`, `enforce`, the ladder in [GUARDRAIL_FLIPS.md](GUARDRAIL_FLIPS.md)). At `off` every token acts in the account's own tenant. |

Two things are deliberately **not** shared between tenants, even parent and
child: data (every tenant-scoped row is isolated by row-level security) and
connectors (each tenant has its own vault rows and its own engine).

## Order of operations

Do these in order. Each step assumes the one before it.

1. **Turn on row-level security first.** Without it, a child's isolation rests
   on every query remembering its `WHERE` clause. Follow
   [TENANT_RLS.md](TENANT_RLS.md) and prove it with the query there before
   creating any child. A superuser database role bypasses RLS entirely.
2. Create the child tenant (below).
3. Add the child's engine cell to the deployment and let it sync.
4. Store the child's connector credentials.
5. Claim the child's SSO domains, if its people sign in from their own domain.
6. Move any account that already exists in the wrong tenant.
7. Run the verification queries.
8. Raise `GENUS_TENANT_SWITCH_MODE` from `off` to `observe`, then `enforce`.

## Create a child tenant

```bash
genus tenant create acme-child-a --name "Acme Child A" --parent acme-parent
genus tenant status acme-child-a
```

`status` should show `Parent: acme-parent`. The command also seeds the child's
memory blocks.

## Add the child's engine

One engine runs per tenant. Each is a cell in the Helm chart's `tenants:` list
in the environment's values layer:

```yaml
tenants:
  - id: acme-parent
  - id: acme-child-a
```

The chart renders one engine per entry and sets both `ROBOTHOR_TENANT_ID` and
`ROBOTHOR_DEFAULT_TENANT` to the entry's `id`, so that engine's connections
are bound to that tenant for RLS and everything it writes lands there. The
bridge and the Helm dashboard stay single: they serve every tenant and pick
the tenant from the access token.

## Connectors are per tenant

- **Vault rows** are keyed by tenant. Store a child's credentials with the
  tenant selected through the environment, from a shell on the deployment:

  ```bash
  export ROBOTHOR_DEFAULT_TENANT=acme-child-a
  genus vault set slack/bot_token
  genus vault list
  ```

  Nothing the parent stored is visible to the child, and the reverse.
- **Google Workspace** is not a vault row: it is a file secret mounted into one
  engine (`engine.googleWorkspace` in the chart). Give each tenant's engine its
  own secret. Two engines sharing one would read and send one mailbox for both.

## SSO domains: where a new user lands

A brand-new SSO user is provisioned into the tenant that **claims their email
domain**, and into the default tenant (`ROBOTHOR_DEFAULT_TENANT` of the
bridge) when no tenant does.

```bash
genus tenant set-domains acme-child-a example.com other.example
genus tenant status acme-child-a      # SSO domains: example.com, other.example
genus tenant set-domains acme-child-a # no domains: clear the claim
```

The same list is `settings.sso_domains` on `PATCH /api/tenants/{id}`.

- **Domain claims are made by the root operator.** A claim decides where every
  new SSO user from that domain lands, across the whole deployment, so adding,
  changing or dropping one needs an owner or admin whose home and acting
  tenant are both the platform tenant. That is the same rule as re-parenting.
  A child's admin, or a root admin who has switched into a child, gets a 403.
  They may still rename a tenant they can reach and edit its other settings,
  as long as the save leaves `sso_domains` as it was. The `settings` object is
  replaced whole, so a save that leaves the key out drops the claim and counts
  as a change.

- **One tenant per domain.** Claiming a domain another tenant holds is refused
  (409 from the API, an error from the CLI). A malformed entry (a scheme, an
  `@`, a wildcard, a single label) is refused with a 422.
- **Exact match.** `example.com` does not claim `mail.example.com`; list both.
- **Only new accounts are routed.** An account already bound to the person's
  identity provider subject signs in to whatever tenant it is in, with RLS on
  or off: the sign-in looks the binding up across every tenant. Claiming a
  domain does not move anyone: use `move-account` for that.
- **Move invited accounts first.** An account that exists but is not yet bound
  (an invitation, or one waiting on a binding grant) is looked up by email in
  the *routed* tenant only. If it sits in another tenant when its domain is
  claimed, the person's first sign-in creates a second, new account in the
  claiming tenant instead. Move such accounts before you claim the domain.
- **Fail safe.** If two tenants ever claim one domain (a hand-edited row), the
  sign-in lands in the default tenant and the bridge logs an error naming the
  domain. Fix the claim; nobody is locked out by it.
- A binding grant (`genus auth grant-binding --email alice@example.com --tenant
  acme-child-a`) for an invited account in the claiming tenant is spent by that
  person's first sign-in, exactly as in the default tenant.

## Move an account to another tenant

```bash
genus tenant move-account --email alice@example.com --to acme-child-a --dry-run
genus tenant move-account --email alice@example.com --to acme-child-a
```

The dry run prints how many rows each table would move and changes nothing.
The live move does everything in **one transaction**: it moves the account,
its CRM contact (see below), its per-user permission overrides, its channel identities,
its face enrolments, its Telegram rows (both the contact's routing row and
the pairing row keyed by the account, which is what Telegram reads to decide
who may drive an agent) and any pending SSO binding grant; revokes every refresh session the account holds, so its next token is
minted for the new tenant; and writes one `auth.account_moved` row to the
audit log. If any step fails, none of it happened.

| Flag | Use |
|------|-----|
| `--from acme-parent` | The address has an account in more than one tenant. The command refuses and lists them until you pick one. |
| `--role admin` | Change the role on the way. `admin`, `member` or `viewer`; a move never makes an owner. `--role viewer` also caps the account's channel roles at `viewer`, both its channel identities and its Telegram pairing; no role change ever raises them. |
| `--detach-person` | Leave the account's CRM contact, and everything that references it, in the source tenant, and give the account a fresh contact in the target (same name and address). Required when that contact has CRM history; see below. |
| `--json` | Print the report as JSON. |

**The CRM contact.** An account may be linked to a CRM contact (`person_id`).
If nothing else in the source tenant references that contact, it moves with
the account. If notes, tasks, conversations, contact identifiers or any other
tenant table do, the move is refused and the error lists each table and its
count. That includes a face enrolment of the person that is not this
account's own, and another account linked to the same contact; the account's
own enrolment and Telegram routing row move with it and do not count. That history belongs to the source tenant, and moving the contact would
leave it pointing at someone the source can no longer see. Re-run with
`--detach-person` to keep the history where it is. An account with no linked
contact is unaffected either way.

**The contact's company.** A moved contact whose company stays in another
tenant arrives with no company: the link is cleared in the same transaction
rather than left pointing across tenants. The report, the dry run included,
names the unlinked company (`Company unlinked: …`, or `company_unlinked` in
`--json`). Re-link the contact to a company in the target tenant afterwards
if it needs one.

A move is refused, with nothing written, when:

- the target tenant does not exist or is not active;
- the account is already in the target tenant;
- the account is its tenant's **last owner** (make someone else the owner
  first; on this schema every owner is their tenant's only owner, so an owner
  cannot be moved at all until the role is handed over);
- the account is an `owner` and the target is a child (children have no
  owner; pass `--role admin`);
- the target already has an account with that address;
- the account's CRM contact has history in the source tenant and
  `--detach-person` was not given.

After a move the person signs in again. An access token issued before the move
stays valid for its few minutes of lifetime and still names the old tenant;
the refresh that would extend it is gone.

## Verification queries

Run these as the non-superuser application role (see
[TENANT_RLS.md](TENANT_RLS.md)), substituting your tenant ids.

```sql
-- The hierarchy, and who claims which domain.
SELECT id, parent_tenant_id, active, settings->'sso_domains' AS sso_domains
FROM crm_tenants ORDER BY parent_tenant_id NULLS FIRST, id;

-- No domain is claimed twice (must return no rows).
SELECT d AS domain, array_agg(id) AS tenants
FROM crm_tenants, jsonb_array_elements_text(COALESCE(settings->'sso_domains', '[]')) AS d
GROUP BY d HAVING count(*) > 1;

-- Each root has one owner, each child none (must return no rows).
SELECT t.id, t.parent_tenant_id, count(a.id) AS owners
FROM crm_tenants t
LEFT JOIN user_accounts a ON a.tenant_id = t.id AND a.role = 'owner'
GROUP BY t.id, t.parent_tenant_id
HAVING (t.parent_tenant_id IS NULL AND count(a.id) <> 1)
    OR (t.parent_tenant_id IS NOT NULL AND count(a.id) <> 0);

-- After a move: every linked row is in the target tenant.
SELECT 'user_accounts' AS tbl, tenant_id FROM user_accounts WHERE email = 'alice@example.com'
UNION ALL
SELECT 'user_permissions', tenant_id FROM user_permissions
 WHERE user_id IN (SELECT id::text FROM user_accounts WHERE email = 'alice@example.com');

-- The move is audited.
SELECT timestamp, actor, details->>'from_tenant', details->>'to_tenant'
FROM audit_log WHERE event_type = 'auth.account_moved' ORDER BY timestamp DESC LIMIT 5;
```

And the isolation check from [TENANT_RLS.md](TENANT_RLS.md), bound to the
child: it must return exactly one tenant.

```sql
SELECT set_config('app.tenant_id', 'acme-child-a', false);
SELECT DISTINCT tenant_id FROM crm_tasks;
```

## Rolling back

- A claimed domain: `genus tenant set-domains <id>` with no domains.
- A move: move the account back (`--to` the old tenant, `--role` the old role
  unless it was `owner`; restore an owner with `genus auth bootstrap` or the
  Users page of the root tenant).
- Switching: set `GENUS_TENANT_SWITCH_MODE=off`. Tokens then act only in the
  account's own tenant again; nothing else changes.

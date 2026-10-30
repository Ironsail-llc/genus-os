# Tenant Hierarchy (one deployment, a parent and its children)

One deployment can serve a **parent** tenant and any number of **child**
tenants. The parent's owner and admins can switch into a child from the Helm;
children never see each other or the parent. This runbook is the operator's
side: what the pieces are, how to add a child, how people land in the right
tenant, how to move one who landed in the wrong one, how to move a workload's
history to a child's engine, and how to prove it all holds.

The examples use a root tenant `acme-parent` with one child `acme-child-a`,
and the address `alice@example.com`. Substitute your own. The root is the
platform tenant: `ROBOTHOR_PLATFORM_TENANT` if set, else the bridge's
`ROBOTHOR_DEFAULT_TENANT` (`default` unless you set it). It is the tenant root
operators sign in to, and the one a "root operator" must have as both home
and acting tenant.

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

Do these in order. Each step assumes the one before it. Every command below
runs from a root shell (the deployment's own tenant). A `genus` command given
`--tenant <child>` (`genus user …`, `genus auth …`), and `genus tenant create`
for the tenant it creates, runs its database work in that child's scope
automatically, so it works with RLS enforcing.

0. **Create the vault-key material in Vault** (see [Vault key](#vault-key)),
   before adding any tenant cell.
1. **Turn on row-level security first.** Without it, a child's isolation rests
   on every query remembering its `WHERE` clause. Follow
   [TENANT_RLS.md](TENANT_RLS.md) and prove it with the query there before
   creating any child. A superuser database role bypasses RLS entirely.
2. Create the child tenant (below).
3. Add the child's engine cell to the deployment and let it sync.
4. Store the child's connector credentials.
4a. Add the child's people (see [Add people to a child](#add-people-to-a-child)).
    A binding grant armed by the root operator signs the person in to the
    tenant that holds the account; grants armed by a child admin apply only
    to addresses routed to that child. A routing-override grant for a CHILD
    account is armed from the CLI (`genus auth grant-binding`); dashboard
    invites arm grants that apply only to addresses routed to the inviting
    tenant.
5. Move any account that already exists in the wrong tenant, BEFORE claiming
   its domain (see [Move an account](#move-an-account-to-another-tenant)).
6. Optional: claim the child's SSO domains, so that NEW people nobody has
   added yet are provisioned into the child on their first SSO sign-in.
   People you add in step 4a do not need a claim.
7. Run the verification queries.
8. Raise `GENUS_TENANT_SWITCH_MODE` from `off` to `observe`, then `enforce`.

## Create a child tenant

```bash
genus tenant create acme-child-a --name "Acme Child A" --parent acme-parent
genus tenant status acme-child-a
```

`status` should show `Parent: acme-parent`. The command also seeds the child's
memory blocks, in the child's scope. If an earlier run was interrupted after
the tenant row was written, running the same command again seeds whatever is
missing instead of refusing. `genus tenant status acme-child-a` lists the
seeded blocks, and the child's runs, reading them in the child's scope.

## Add the child's engine

One engine runs per tenant. The root tenant's engine is the chart's `engine:`
block, as before. Each CHILD is a cell in the `tenants:` list in the
environment's values layer. The root does not appear in that list:

```yaml
tenants:
  - id: acme-child-a
```

The chart renders one engine per entry and sets both `ROBOTHOR_TENANT_ID` and
`ROBOTHOR_DEFAULT_TENANT` to the entry's `id`, so that engine's connections
are bound to that tenant for RLS and everything it writes lands there. The
bridge and the Helm dashboard stay single: they serve every tenant and pick
the tenant from the access token. The chart README has the full set of keys
per cell.

**On a single host** (no chart), run one more engine process per child, and
set on each:

| Where | Variable | Value |
|---|---|---|
| The child's engine | `ROBOTHOR_TENANT_ID` and `ROBOTHOR_DEFAULT_TENANT` | the child's id (both the same) |
| The child's engine | `GENUS_EVENT_CONSUMER_GROUP` | `engine-<child id>`, so engines do not share one event-consumer group |
| The child's engine | `ROBOTHOR_WORKSPACE` | a directory of its own, never the root's. The engine keeps its plugin lockfile there, and a child admin enables and disables plugins in it; on a shared workspace that would reach the root and every sibling. (`ROBOTHOR_PLUGIN_LOCKFILE` alone also separates the lockfile.) |
| The bridge | `GENUS_TENANT_ENGINE_URLS` | a JSON object mapping each child id to its engine's URL, e.g. `{"acme-child-a": "http://127.0.0.1:18811"}` |
| The bridge AND every engine | `GENUS_TENANT_SWITCH_MODE` | the same value everywhere (`off`, `observe` or `enforce`) |

Every process also needs the shared vault key below.

### Vault key

Every tenant shares one vault master key. A credential the bridge stores while acting in a child tenant is encrypted with that key and decrypted later by the child's own engine, so the bridge, the root engine and every child engine must hold the same key. A key per workspace makes the child's credentials unreadable by its engine. On Kubernetes the chart provides the key: set `vaultKey.enabled: true` and point `vault.secrets.vault-key.path` at a Vault path holding two base64 fields, `key` and `fingerprint-salt`, each made with `openssl rand -base64 32`. The chart mounts both read-only on the bridge and every engine, at `/etc/genus/vault/.vault-key` and `/etc/genus/vault/.fingerprint-salt`, and sets `GENUS_VAULT_KEY_FILE` and `GENUS_FINGERPRINT_SALT_FILE` to them. The files keep the `.vault-key` / `.fingerprint-salt` names because the engine refuses agent reads of those names; the override paths are refused as well. It refuses to render tenant cells without this. On a single host, point both variables at the same files for every process. Treat `key` as write-once: secrets already stored cannot be read under a new key. The fingerprint salt only affects how credentials are displayed (`b2:…`), not decryption, but sharing it keeps one credential's fingerprint identical everywhere.

## Connectors are per tenant

- **Vault rows** are keyed by tenant. `genus vault` has no `--tenant`: it
  works in the shell's own tenant. Store a child's credentials from a shell
  whose `ROBOTHOR_TENANT_ID` is unset or equal to the child, with the child
  selected as its default tenant. A shell left at the root's
  `ROBOTHOR_TENANT_ID` is bound to the root, and the child's rows are written
  or read nowhere it can see:

  ```bash
  unset ROBOTHOR_TENANT_ID
  export ROBOTHOR_DEFAULT_TENANT=acme-child-a
  genus vault set slack/bot_token
  genus vault list
  ```

  Nothing the parent stored is visible to the child, and the reverse. From a
  root shell, `genus vault set` may also print a notice that the engine
  refused an unauthenticated reload. It is harmless: the secret is stored, and
  the child's engine reads it on its next reload.
- **Google Workspace** is not a vault row: it is a file secret mounted into one
  engine (`engine.googleWorkspace` in the chart). Give each tenant's engine its
  own secret. Two engines sharing one would read and send one mailbox for both.

## Plugins are per tenant

Each tenant's engine loads plugins from its own lockfile, so the child decides
which installed plugins it runs. The chart gives every cell its own workspace,
and with it its own lockfile; on a single host that is the `ROBOTHOR_WORKSPACE`
row above. Each engine claims its lockfile for its tenant at boot (a
`plugins.lock.tenant` marker beside it; the root's engine takes the claim
back from a child), and an engine whose lockfile another tenant's engine
claimed logs an error and refuses enable, disable and record with a 409 until
it has a workspace of its own. A child's workspace starts empty: point it at a
new directory, and the child's agent manifests and plugin lockfile live
there. Its admins manage that on the Helm's
Settings › Plugins page, which lists and toggles the **acting** tenant's
engine. A root admin switched into the child manages the child's plugins the
same way.

What is installed stays root: install, remove and record (sync) are root-only
and act on the root's engine unless the request names a tenant in the root's
tree.

The root can also force a plugin for one child. As a root owner or admin
**at home** (not switched in), the Plugins page shows a "For a child tenant"
control on each plugin card: pick the child, then **Force on**, **Force off**
or **Let it decide**. Over HTTP it is
`PUT /api/plugins/{name}/tenants/{tenant_id}` with `{"state": "on" | "off" | null}`.
The bridge writes the grant, audits it under the child, and reloads the
child's engine. If that reload fails the answer says so, and the grant
applies on that engine's next reload or restart.

What the child sees: a forced plugin's switch is locked with "Set by the root
tenant", and enable or disable on it is refused (409). A plugin forced off is
not imported at all, and a read error on the grants keeps it refused (fail
closed), across a restart too, from the copy each engine keeps in its
workspace (`plugins.lock.grants.json`). The child can read its grants but not
change them: row-level security lets only the root write them. The full rules are in [PLUGINS.md, Per tenant](../PLUGINS.md#per-tenant).

To check a child's grants directly:

```sql
SELECT plugin, state, set_by, set_at FROM tenant_plugin_grants
WHERE tenant_id = 'acme-child-a' ORDER BY plugin;
```

## Add people to a child

From a root shell (`--tenant` runs each command in the child's scope). Pick
one path per person.

**SSO sign-in.** Add the account with `--sso`, then arm its one-shot binding
grant:

```bash
genus user add --tenant acme-child-a --name "Alice Example" --email alice@example.com --role admin --sso
genus auth grant-binding --email alice@example.com --tenant acme-child-a --ttl 1d
```

`--sso` makes the same account the Helm's SSO invite makes: active, with no
password and no identity-provider binding, so nothing can sign in as it yet.
The person's first verified SSO sign-in with that address spends the grant,
binds the account to their IdP identity, and signs them in to the child.

A binding grant armed by the root operator signs the person in to the tenant
that holds the account; grants armed by a child admin apply only to addresses
routed to that child. A routing-override grant for a CHILD account is armed
from the CLI (`genus auth grant-binding`); dashboard invites arm grants that
apply only to addresses routed to the inviting tenant. The dashboard's invite
and re-arm routes create a grant in the tenant the caller is acting in, so a
root owner who switches into a child and invites there gets a grant with the
child's rules, and that sign-in is refused unless the address routes to the
child. A root grant overrides domain
routing only when the tenant the address routes to has no account for it at
all, so it can never take a sign-in that routing already names an account
for. When it does override, the bridge writes an `auth.sso.grant_bound` audit
row. Re-arm the grant if it expires first. Without `--sso` the
account is `invited`, and
`grant-binding` refuses it, because an invited account cannot consume a grant.

**Password sign-in.** Add the account, then set its password:

```bash
genus user add --tenant acme-child-a --name "Alice Example" --email alice@example.com --role admin
genus user set-password --tenant acme-child-a alice@example.com
```

A password sign-in needs the account `active`, and `genus user add` without
`--sso` leaves it `invited`. Nothing in the CLI activates it yet (a follow-up),
so activate it in SQL, as the application role:

```sql
UPDATE user_accounts SET status = 'active', updated_at = NOW()
WHERE tenant_id = 'acme-child-a' AND email = 'alice@example.com';
```

The person can then sign in with their password, and is signed in to the
child.

## SSO domains: where a new user lands

A brand-new SSO user (someone nobody has added) is provisioned into the tenant
that **claims their email domain**, and into the default tenant
(`ROBOTHOR_DEFAULT_TENANT` of the bridge) when no tenant does. Claims are
optional, and only matter for people like that: anyone the root operator
added with `genus user add --sso` and armed a binding grant for signs in to
the tenant that holds their account, provided the tenant the address routes
to holds no account for it.

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
- **Move existing accounts first.** A claim never creates a second account
  for an address that already has one. Take an account that is not yet bound
  to the person's IdP subject and has no live binding grant (an invitation, or
  a `genus user add` + `set-password` account). If it sits in another tenant
  when its domain is claimed, the person's SSO sign-in is refused with
  "account not eligible for SSO". The bridge logs which tenant holds the
  account, and password sign-in keeps working. So move such accounts before
  you claim the domain (step 5 before step 6 above). An account with a live
  binding grant from the root operator is not affected: the grant signs the
  person in to the tenant that holds it, as long as the claiming tenant has
  no account for the address.
- **Fail safe.** If two tenants ever claim one domain (a hand-edited row), the
  sign-in lands in the default tenant and the bridge logs an error naming the
  domain. Fix the claim; nobody is locked out by it.
- A binding grant (`genus auth grant-binding --email alice@example.com --tenant
  acme-child-a`) from a root shell, for an account added with `--sso`, is
  spent by that person's first SSO sign-in, which signs them in to
  `acme-child-a` whatever any claim says, unless the tenant the address routes
  to already holds an account for it. A grant armed from the dashboard (by
  anyone, including a root owner acting in a child) is spent only when the
  address routes to the tenant it was armed in.

## Move an account to another tenant

```bash
genus tenant move-account --email alice@example.com --to acme-child-a --dry-run
genus tenant move-account --email alice@example.com --to acme-child-a
```

The dry run prints how many rows each table would move and changes nothing.
The live move does everything in **one transaction**: it moves the account,
its CRM contact and that contact's own addresses and handles
(`contact_identifiers`; see below), its per-user permission overrides, its channel identities,
its face enrolments, its Telegram rows (both the contact's routing row and
the pairing row keyed by the account, which is what Telegram reads to decide
who may drive an agent) and any pending SSO binding grant; revokes every refresh session the account holds, so its next token is
minted for the new tenant; and writes one `auth.account_moved` row to the
audit log. If any step fails, none of it happened.

Run it from a root shell: `ROBOTHOR_TENANT_ID` unset or the root's id, and
`ROBOTHOR_DEFAULT_TENANT` the root. Unlike `move-workload`, `move-account`
does not check this itself yet, so check it before you run the command.

| Flag | Use |
|------|-----|
| `--from acme-parent` | The address has an account in more than one tenant. The command refuses and lists them until you pick one. |
| `--role admin` | Change the role on the way. `admin`, `member` or `viewer`; a move never makes an owner. `--role viewer` also caps the account's channel roles at `viewer`, both its channel identities and its Telegram pairing; no role change ever raises them. |
| `--detach-person` | Leave the account's CRM contact, and everything that references it, in the source tenant, and give the account a fresh contact in the target (same name and address). Required when that contact has CRM history; see below. |
| `--json` | Print the report as JSON. |

**The CRM contact.** An account may be linked to a CRM contact (`person_id`).
If nothing else in the source tenant references that contact, it moves with
the account. If notes, tasks, conversations or any other tenant table do, the
move is refused and the error lists each table and its count. That includes a
face enrolment of the person that is not this account's own, and another
account linked to the same contact. What does not count: the account's own
enrolment and Telegram rows, and the contact's own addresses and handles
(`contact_identifiers`, such as the email row `genus user add` writes), which
are part of the contact and move with it. Their link to a memory entity is
cleared, because that entity stays in the source. So a plain `genus user add`
account moves, and its dry run prints counts. That history belongs to the source tenant, and moving the contact would
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

After a move the person signs in again, by SSO or with their password: the
password sign-in looks the address up in every tenant and signs the person in
to the one tenant where it has an active account with a password. If several
tenants have one, the platform tenant's wins and the bridge logs an error;
with several and none in the platform tenant, the sign-in is refused (and
logged) until the duplicate is removed. An access token
issued before the move still names the old tenant, and the refresh that would
extend it is gone. The old access token remains valid until it expires (15
min) unless the session-standing check from the tenant-admin-gates change is
deployed; with it, the bridge and every engine refuse the token within 30 s
(immediately if the move runs inside the bridge).

## Move a workload to another tenant

A workload is a set of agents and workflows. When it moves from the tenant
it ran in (usually the root) to another tenant's engine (usually a child),
move its history with it, after the child's engine is in place and BEFORE
the child's engine runs it for the first time. Stop the workload in the
source first: remove the agents and workflows from the source engine's
manifests (or disable their schedules) and let anything running finish. The
move runs at `REPEATABLE READ`, so a write the source engine commits during
the move is never split between the tenants: it stays in the source, or the
move fails and changes nothing.

```bash
genus tenant move-workload --from acme-parent --to acme-child-a \
  --agent triage --agent intake-helper --workflow intake --dry-run
genus tenant move-workload --from acme-parent --to acme-child-a \
  --agent triage --agent intake-helper --workflow intake
```

`--agent` and `--workflow` repeat; name at least one. `--json` prints the
report as JSON. The dry run runs every check below, prints the per-table
counts the live move would write, and changes nothing. The live move does
everything in **one transaction** and writes one `tenant.workload_moved` row
to the audit log, filed under the root of the source tenant (the details name
both tenants, the agents, the workflows and the counts). If any step fails,
none of it happened. A run that finds nothing to move (a re-run of a move
that already happened) reports zeros and writes no audit row.

Run it from the root's shell: the command refuses unless the shell's engine
tenant (`ROBOTHOR_TENANT_ID`, else `ROBOTHOR_DEFAULT_TENANT`) is the root of
both tenants. A shell left bound to a child, or to another root, is refused
with the tenant it is bound to and the root it needs.

**What moves.** Each named agent's runs, every run descended from them
(sub-agent runs), and the runs the named workflows' steps started; with
those runs, their steps, checkpoints, evidence and tool events (which carry
no tenant column and follow their run), runtime controls and effects,
breadcrumbs, questions, reviews, interventions, coding jobs, memory-write
jobs, fact-access records and message reactions. The agents' schedules,
user cron jobs, benchmark results and managed-agent runs. The workflows'
runs, their steps and their approvals. The agents' chat sessions (keys of
the form `agent:<id>:…`), with their messages, approval receipts and channel
message map. A delivery the channel bus surfaced into another agent's session
(main's, usually) stays with that session, so a reply in the source still
finds the message it quotes. The report lists every one of these tables,
zeros included.

**What stays.** Everything that belongs to the tenant rather than to one
workload: the CRM, tenant memory and the knowledge base, identities and
accounts (`move-account` moves those), vault rows and connector mirrors, the
autonomy ledger, goal pursuits, the operations and sales ledgers, and the
audit trail, which keeps the tenant each row was written under. The full
list, with the reason for each table, is `UNTOUCHED_TABLES` in
`robothor/tenancy/move_workload.py`.

**Links that would cross tenants.** A moved row that links to a row that
stays in another tenant (a run's CRM contact or task, a session's contact, a
run whose parent run belongs to an agent that stays) has the link cleared,
in the same transaction. The report lists each cleared column and its
count (`Links cleared`).

A move is refused, with nothing written, when:

- either tenant does not exist, or the target is not active;
- `--from` and `--to` name the same tenant;
- no `--agent` or `--workflow` is given;
- the target is not under the same root as the source, or is one of its
  ancestors (a workload moves to a descendant or a sibling);
- the shell's engine tenant is not that root;
- a tenant table in the database has no move classification (a migration
  newer than the command: upgrade first);
- any of the workload's runs, workflow runs or coding jobs is still pending,
  running or awaiting approval, or a run has an unresolved runtime effect
  (`prepared`, `dispatching`, `uncertain`) or an unfinished memory write
  (`pending`, `running`);
- a step of a workflow that is not being moved started one of the
  workload's runs (name that workflow with `--workflow` too);
- the workload has an attempt in a goal pursuit, or an open autonomy
  operation, workflow or handoff;
- the target already holds a row with the same unique key as a moved row
  (for example a chat session with the same key); the error names the table
  and the key;
- a row that stays in the source references a row that moves.

The command never moves a workload up to an ancestor, so it cannot return a
workload to the root once it has moved: do the dry run first and read it.
Between children it moves both ways (`--from acme-child-a --to acme-child-b`
and back). Moving back does not restore the links the first move cleared:
they stay empty, and the audit row records only how many were cleared per
column.

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
-- robothor-primary and benchmark-sandbox are system roots with no owner
-- (migrations 032a and 104 create them), so they are left out.
SELECT t.id, t.parent_tenant_id, count(a.id) AS owners
FROM crm_tenants t
LEFT JOIN user_accounts a ON a.tenant_id = t.id AND a.role = 'owner'
WHERE t.id NOT IN ('robothor-primary', 'benchmark-sandbox')
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

-- After a workload move: the agent's runs are all in the target, and audited.
SELECT tenant_id, count(*) FROM agent_runs WHERE agent_id = 'triage' GROUP BY tenant_id;
SELECT timestamp, actor, details->>'to_tenant', details->'counts'
FROM audit_log WHERE event_type = 'tenant.workload_moved' ORDER BY timestamp DESC LIMIT 5;
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
- A workload move between children: move it back with `move-workload`. A
  move to a child cannot be undone with the command (it refuses an ancestor
  as the target). Links a move cleared stay cleared either way.
- Switching: set `GENUS_TENANT_SWITCH_MODE=off` on the bridge and every
  engine, and restart them. No new switch is issued from then on. An acting
  token issued before the flip is rejected by the bridge once it has restarted
  with the flag off. The engines reject it too once the tenant-admin-gates
  change, which adds that check to them, is deployed. Until then the token
  stays valid for the rest of its lifetime (at most 15 minutes). Nothing else
  changes.

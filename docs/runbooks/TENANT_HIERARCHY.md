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
connectors (each tenant has its own vault rows and its own engine). A parent's
agent can still read and act in a child, one audited call at a time, inside
that child's scope (see [Agents in a parent tenant](#agents-in-a-parent-tenant)),
and can use a child's own connectors through the child's engine (see
[Using a child's connectors](#using-a-childs-connectors)).

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

`genus doctor` in a child's engine reports `identity.owner_account` as passing
and not applicable: a child has no owner by design, and an engine bound to the
child cannot see the root's owner. A root tenant with no owner still fails.

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

### Using a child's connectors

A connector is never copied to the parent. Instead the parent's agent calls
the child's tool where it lives: home engine → bridge (`POST
/api/org/remote/call`) → the child's engine, which runs the tool with its own
credentials, permissions and audit and returns the result. The child offers
only the tools its plugins declare remote, each as a read or a write
([Plugins](../PLUGINS.md)). The agent's tools are `org_tools`, `org_tool_read`
and `org_tool_write` ([Tools](../TOOLS.md#a-child-companys-own-tools-org_tools-org_tool_read-org_tool_write));
the Command centre's connections view (`GET /api/org/connections`) shows the
same catalogue and each plugin's health to the parent's owner or admin.

| Caller | Read a child's tool | Write through a child's tool |
|-----|---------|---------|
| The parent's verified `owner`, in chat | Yes | Yes |
| The parent's verified `admin`, in chat | Yes | No |
| An unattended run (cron and heartbeat, hook, event, workflow) | Only with both grants under [The unattended grant](#agents-in-a-parent-tenant) **and** the child granting its `parent_agent` role on that tool | Never |
| A sub-agent, a benchmark run, a member, a viewer, a person signed in to a child | No | No |

What has to be in place:

1. The child's plugin declares the tool remote, and is enabled in the child.
2. If the tool requires an attestation, the bridge's
   `GENUS_ORG_REMOTE_ATTESTATIONS` lists it. The bridge presents its own
   value; a request cannot claim one.
3. Nothing, on a fresh install, at the bridge's capabilities manifest. The
   engine's token for these routes carries the `org:remote` scope, which only
   the remote tools mint, and the bridge authorises the routes by that scope.
   An instance whose `agent_capabilities.json` **lists** the agent must also
   list the two remote patterns for it (below).
4. For unattended reads only: the child grants `parent_agent` on that tool.
   The role has no permission rows by default, so it is denied everything
   until granted.

There is no mode or flag for remote tools: they are the only code path, and
what a child offers is decided by its plugin's `remote` declaration (a tool
not declared is never callable from a parent) and its own roles. To stop or
narrow them without a release:

- **Unattended reads:** delete the child's `parent_agent` allow rows
  (`DELETE FROM role_permissions WHERE tenant_id = '<child>' AND role =
  'parent_agent' AND access = 'allow'`, or one `tool_pattern`). The role falls
  back to its deny-all.
- **One tool, for everyone:** add a tenant-scoped deny row in the child for
  the roles a parent's caller runs as (`owner`, `admin`, `parent_agent`) on
  that tool ([Service roles](SERVICE_ROLES.md#roles-available)). A tenant row
  is evaluated before the `__default__` rows, so the call is refused
  (`remote_not_permitted`); it also binds the child's own people in those
  roles.
- **One connector:** disable the plugin in the child
  (`genus plugin disable <name>`, or Settings › Plugins while acting in the
  child), or use the plugin's own switch where it has one. Its tools leave
  the child's catalogue.
- **Every unattended read from a parent:** clear the parent's
  `crm_tenants.child_data_access`
  ([the unattended grant](#agents-in-a-parent-tenant)). People in chat are
  unaffected; they are bounded by their own role in the parent.
- **The feature itself:** `git revert` the change and roll out the release.

A plugin author narrows what is exposed by changing the `remote` declaration
([Plugins](../PLUGINS.md)); a write declared there can always be previewed
with `org_tool_write`'s `dry_run: true`, which passes every check, is audited
on both sides and executes nothing.

**What this does not protect against.**

- **Presence.** The bridge and the child check that the person a call names
  is an owner (for a write) or owner or admin of the parent and may act in
  the child. They cannot check that the person is there: "attended" is the
  parent engine's word, carried in `requested_by`. A compromised parent engine
  can name its owner on an unattended run.
- **One signing key.** Every engine and the bridge share one signing key, so
  an engine that is compromised can mint tokens the others accept. Remote
  tools add no key separation between tenants.
- **Steering through a result.** Text a child's read returns can try to talk
  the same run into a write. The control is the untrusted-content boundary
  (results of all three tools are wrapped as external content), the owner-only
  rule for writes and the child's own roles, not a check on what the text
  says.

**An instance with its own capabilities manifest.** The bridge reads
`agent_capabilities.json` from `ROBOTHOR_CAPABILITIES_MANIFEST`, else
`<bridge workspace>/agent_capabilities.json`, else
`<bridge workspace>/brain/agent_capabilities.json`. Nothing generates it from
the agent manifests: the `bridge_endpoints` in the instance's `docs/agents/main.yaml` (and in
the shipped template) record what the agent calls, but the bridge does not read
them. It is one file for the whole deployment, keyed by agent id only, so an
entry for `main` covers every tenant's `main`. If the file has an entry for the
agent that makes remote calls (usually `main`), add the two patterns to that
entry's `bridge_endpoints`:

```json
"main": {
  "bridge_endpoints": [
    "GET /api/org/remote/*",
    "POST /api/org/remote/call"
  ]
}
```

keeping the entry's other patterns, then restart the bridge: it loads the file
only when it starts. Until then every remote call from that agent is refused
with a 403 that names the missing patterns, and `genus doctor --category
capabilities` reports it (`capabilities.remote_tools`). For an installed
instance `docs/agents/main.yaml`, add the same two lines under `bridge_endpoints:` so the
manifest matches the template; it does not change what the bridge allows.

Every call leaves a row on each side: `agent.cross_tenant_call` under the
parent (with `details.remote_tool`), `org.remote_call` under the child from the
bridge, and `agent.remote_tool_call` under the child from its engine, which
carries a digest of the arguments, never the arguments.

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
minted for the new tenant; removes the account's memberships of the source
tenant's shared threads (rooms belong to the tenant and stay; the report
counts the memberships removed); and writes one `auth.account_moved` row to the
audit log. If any step fails, none of it happened.

Run it from a root shell: `ROBOTHOR_TENANT_ID` unset or the root's id, and
`ROBOTHOR_DEFAULT_TENANT` the root. The command refuses, dry run included,
unless the shell's engine tenant is the root of both the account's tenant and
the target. That also refuses a move between tenants under two different
roots.

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
- the shell's engine tenant is not the root of both tenants;
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
the agent's manifest reaches the child's engine. Stop the workload in the
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
that already happened) prints `Nothing to move` and writes no audit row.

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
user cron jobs, benchmark results and managed-agent runs (a schedule moves
with all of the agent's jobs: `<agent>`, and its `:heartbeat`, `:worker` and
`:ambient` rows). The workflows'
runs, their steps and their approvals. The agents' chat sessions (keys of
the form `agent:<id>:…`), with their messages, approval receipts and channel
message map. The agents' shared threads (rooms), with their members,
revisions and editions; a thread's conversation is one of the agent's
sessions (`agent:<id>:room:<room id>`), and a thread linked to CRM tasks refuses
the move (see the refusal list). The dashboard calls these threads
workspaces ([Workspaces](../WORKSPACES.md)); an account move removes the
account's memberships instead of carrying them. A delivery the channel bus surfaced into another agent's session
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
- one of the workload's shared threads is linked to CRM tasks (`room_tasks`),
  which stay in the source tenant's CRM;
- the target already holds a row with the same unique key as a moved row
  (for example a chat session with the same key); the error names the table
  and the key. `agent_schedules` is keyed by tenant and job, and an engine
  writes the row when it loads the manifest, before the job first fires, which
  is why the move belongs before the manifest reaches the target. If it has
  already loaded it, delete the target's `agent_schedules` row for that job
  and move: the moved row takes its place,
  and the engine rewrites its schedule columns at its next start;
- a row that stays in the source references a row that moves.

The command never moves a workload up to an ancestor, so it cannot return a
workload to the root once it has moved: do the dry run first and read it.
Between children it moves both ways (`--from acme-child-a --to acme-child-b`
and back). Moving back does not restore the links the first move cleared:
they stay empty, and the audit row records only how many were cleared per
column.

**Schedule rows after a workload leaves.** Each tenant holds its own
`agent_schedules` row per job, and two tenants may run the same agent id, so a
row left in the source no longer gets in the way of the target. It fires
nothing either: the scheduler runs from manifests. The source engine's next
reconcile prunes it once the manifest is gone. The exception is a source with no
scheduled job left at all, because the prune refuses to run from an empty set;
there the row stays, and shows on that tenant's fleet view, until you delete
it. `genus tenant move-workload` is the way to carry a row over.

## Agents in a parent tenant

The parent's agents can read and act in the children. Each child's data is
still isolated by row-level security: an agent call that names a child runs with
its database scope bound to that child, one call at a time, and every such call
leaves an `agent.cross_tenant_call` row in `audit_log` naming both tenants, the
user, the tool and the outcome. The tools, and which writes are allowed, are in
[Tools](../TOOLS.md#child-tenants-org_overview-the-tenant-argument-and-dry_run):
`org_overview` reports every reachable company at once, and an allowlisted tool
given `tenant: acme-child-a` runs in that child.

**Who reaches a child.** Decided once, when a run starts:

| Run | Reaches |
|-----|---------|
| A person signed in to the parent as `owner` or `admin` (a verified identity, on chat or another channel a person uses) | The parent and its descendants. |
| An `owner` or `admin` of the parent who has switched into a child | That child and the child's own descendants: never the parent or a sibling from there. |
| A `member` or `viewer` | Their own tenant only. |
| A person signed in to a child, even its `admin` | That child only: never a sibling, never the parent. |
| A person nobody verified: an unpaired or unverified sender, or a chat with no resolved identity, on any channel a person uses | Their own tenant only, whatever the grants below say. |
| A sub-agent, even one spawned by the owner's chat | Its own tenant only. |
| A benchmark run, or the engine's own continuation of a task after a verified repair | Its own tenant only. |
| A request from a federated peer instance | Its own tenant only. |
| An unattended run (cron and heartbeat, hook, event, workflow) | Its own tenant only, unless both grants below are set. |

A verified person's reach is judged by the weaker of their own role and the
role the run carries, in the tenant they were verified in: a `member` on a run
that says `owner` reaches their own tenant only.

**Writes.** Any run that reaches a child may create a task or send a
notification there, which leaves work for that child's team. Updating a task
and creating or updating a note in a child take the verified owner of the
parent, on a top-level run. Each write keeps the approval tier and role check
it has in the parent. Every write accepts `dry_run: true`, which checks it,
audits it, and reports what would change without changing anything; it reads
the record it would change only as far as the caller may read it. `dry_run`
must be the boolean `true` or `false`: any other value (`"true"`, `1`, `null`)
is refused and nothing runs.

**The unattended grant.** A scheduled agent in the parent reaches the children
only when the parent tenant allows it AND the agent's manifest asks for it:

```sql
UPDATE crm_tenants SET child_data_access = TRUE WHERE id = 'acme-parent';
```

```yaml
# docs/agents/<agent>.yaml
v2:
  child_tenant_access: true
```

Either one alone grants nothing. With both, the agent's unattended runs can use
the allowlisted reads in every child, and of the writes only create a task or
send a notification. They may also read through a child's connectors where
the child grants `parent_agent` on that tool, and never write through them
([Using a child's connectors](#using-a-childs-connectors)). The grant covers unattended runs only: the same agent
answering a person on chat reaches the children only when that person is a
verified `owner` or `admin` of the parent. Revoke by setting either back to false; the next run picks
it up.

### The organisation watch

The parent's `main` agent can brief the owner when something changes in one of
the companies, instead of waiting to be asked. Its heartbeat calls
[`org_watch`](../TOOLS.md#child-tenants-org_overview-the-tenant-argument-and-dry_run),
which compares every reachable company with what it saw last time and returns a
plain-language briefing of what changed: an agent that started failing or keeps
failing, a schedule that is late, a newly overdue task, a company that went
quiet or could not be read, and each of those clearing. A problem is told once,
not on every beat. Once a day, `daily: true` adds every company's state.

The template's `main` ships without a heartbeat, so nothing runs until you add
one. In the parent instance's own `main` manifest (instance data, not in this
repository):

```yaml
heartbeat:
  cron: "0 * * * *"           # hourly; the daily summary rides the 08:00 beat
  delivery:
    mode: announce
    channel: telegram          # the owner's channel
v2:
  child_tenant_access: true   # with crm_tenants.child_data_access, as above
```

The heartbeat follows the "Organisation watch" section of `main`'s starter
instructions (`templates/agents/core/main/instructions.template.md`): call
`org_watch`, add `daily: true` on the beat that runs in the 08:00 hour, and send the
briefing only when it is not empty. On a quiet beat the agent answers "nothing
to report", which delivery suppresses.

Without both grants the watch covers the parent only and says so (`limited`);
a limited scheduled watch still remembers what it reported about the parent,
and keeps what the full watch last saw of the children. Only a scheduled run
moves what the watch remembers: a person who calls it in chat, the owner
included, gets the report and the next heartbeat still reports the same
changes. The first watch reports every problem present
at that moment, so problems that existed before it was switched on are not
hidden; after that only changes are reported. What it saw is kept in the
parent tenant's `org_watch_snapshot` memory block. That block is engine state:
no agent can read, write, append to or list it, and no manifest can load it
into an agent's context. Deleting the row (an operator, in the database) makes
the next watch a first watch again.

## The command centre

The Helm's **Command centre** (`?v=org`, first in the Workspace group) shows one
card per company: the parent and each of its descendants, with the same
figures `org_overview` gives the agent, from the same rollup. Companies that
need attention come first. A card for a company that could not be read says so
instead of showing zeros.

| Who | Sees |
|-----|------|
| The parent's `owner` or `admin`, signed in to the parent | Every company: the parent and its descendants. |
| The same person while acting in a child | Nothing: the page is refused until they switch back. While acting, the child's own pages are theirs. |
| A child's `admin` | No nav entry. The endpoint answers with the child alone and `has_children: false`; never a sibling or the parent. |
| A `member`, `viewer` or `auditor`, and any service token | Refused. |

Clicking a card switches into that company through the usual tenant switch, so
it works only while `GENUS_TENANT_SWITCH_MODE=enforce`; otherwise the cards
are not links. The page reads the overview when it is opened and when
**Refresh** in its header is clicked, never on its own (not on returning to
the tab, nor on a timer); the as-of time says how old the figures are. Each
read writes one `org.overview` row to `audit_log` for every child read, filed
under that child, so the child's admins can see who looked.
The endpoint is `GET /api/org/overview`
([configuration](../configuration.md#the-command-centre-over-the-api)).

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

- The release itself: reset the deployment to the previous release. Its
  migrations stay applied, and the older release starts beside them only if it
  carries the rule for newer migrations, and only if every migration the newer
  release added is numbered above the older release's highest. Otherwise the
  older release refuses them and its migration Job fails. See
  [Rolling back to an older release](../deployment.md#rolling-back-to-an-older-release)
  for what the migrator accepts and what it still refuses.
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

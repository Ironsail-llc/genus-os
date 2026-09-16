# 2. Identity, roles and tenants

Goal: your people signed in, holding the narrowest role that lets them work,
with the owner account protected.

Three sign-in methods, and a deployment needs at least one. Local email and
password exists so a half-hour install does not have to stand up an identity
provider first; OIDC and Cloudflare Access are additions, not replacements, and
can run alongside it.

## Local login, and owner MFA

The setup wizard turns local email and password on when it creates your
account. You do not set `GENUS_LOCAL_LOGIN` yourself, and the dashboard asks
the Bridge which methods are live rather than reading its own environment.

What the settings reference cannot tell you, and an auditor will ask:

- Passwords are argon2id, minimum twelve characters.
- Every failure — unknown address, wrong password, disabled account, locked
  account — answers the same `invalid credentials`. The only distinguishable
  state is `mfa_required`, and only after a correct password.
- Ten consecutive failures freeze an account for fifteen minutes. Five attempts
  per address and IP per minute; above that, a ceiling nothing in the request
  can change — thirty credential attempts a minute per connecting peer, three
  hundred for the whole Bridge process, checked *before* any account is loaded.
  A credential body over 8 KiB is refused with 413 before it is parsed.
- TOTP codes are single use: the accepted time step is recorded, so a code
  cannot be replayed inside the verifier's window.
- Changing a password revokes every other refresh session, keeping only the one
  that made the change.

**Owner MFA is mandatory whenever local login is on.** The owner still signs
in, but an undismissable banner stands until a factor is enrolled at
`/account/security`. It does not matter what else is configured — the OIDC
issuer list is an allowlist, not a sign-in method, and the Cloudflare Access
variables are dashboard-only, so neither can answer "is there another way in".
`GENUS_OWNER_MFA_REQUIRED=false` is the explicit opt-out, and it is a decision
somebody should have to make on purpose.

Recovering a lost authenticator, and setting a password, are still shell
operations:

```bash
genus user set-password alice@example.com
genus user set-password alice@example.com --password-stdin < secret
genus user mfa-reset alice@example.com
```

Rotating `GENUS_AUTH_SIGNING_KEY` invalidates every enrolled second factor,
because it derives the key that encrypts stored TOTP secrets. Plan the resets.

## OIDC and Cloudflare Access

Both are additive. Configure the issuer allowlist and the dashboard's Access
variables from the [`auth` group of the settings
reference](../reference/configuration.md); `genus doctor` reports when one is
configured.

Binding an existing account to an identity provider is a **one-shot grant**: it
is armed for an account, pinned to the configured issuer when the appliance has
exactly one, and the first verified sign-in for that address spends it. The
grant carries no secret to re-read, which is why the Helm shows its expiry and
issuer once and never again.

If you put the Helm on the internet, put it behind your edge. A reverse proxy
changes what the Bridge sees as the client address, and two allowlists have to
agree before a forwarded address is believed:

- the dashboard walks `X-Forwarded-For` from the **right** and forwards the
  first hop not named in `GENUS_DASHBOARD_TRUSTED_PROXIES` as `X-Client-IP`;
- the Bridge honours that header only from a peer named in
  `GENUS_TRUSTED_PROXIES`.

Either one empty means no forwarded address at all, and the sign-in limiter
sees one address for every user. Loopback is **not** implicitly trusted: a
tunnel on the same host makes every remote client a loopback peer, so
`GENUS_TRUSTED_PROXIES=127.0.0.1/32` is right only when nothing else can reach
that port. Prefer a `/32` to a pod CIDR, which covers every workload in the
namespace. The reasoning is in
[Configuration](../configuration.md#the-forwarded-client-address-needs-two-allowlists-to-agree).

## Roles

`GET /api/auth/roles` is the live list, with one sentence each, served from the
same table the CLI and the token layer use. In practice:

| Role | What it is for |
|---|---|
| `owner` | The instance belongs to this account. One per tenant |
| `admin` | Configures the instance: agents, users, channels, plugins, flags |
| `member` | Ordinary staff use: chat, tasks, the agents they are allowed |
| `user` | **A legacy alias for `member`** — identical scopes, kept for accounts created before the two were distinguished. It narrows nothing; do not reach for it |
| `viewer` | Read-only, plus chat. **The default for a newly paired channel sender**, and the narrower role when `member` is wider than a person needs |
| `auditor` | Read-only, plus the audit log. For review, not operation |

So the ladder that actually narrows is `admin` → `member` → `viewer`.

`auditor` is the role to hand your compliance function: it sees Observe › Audit
and the CSV export, and not Observe › Memory or Observe › Logs, and the
navigation reflects that. It is not a blindfold — an auditor session also
carries read scope on the Bridge and the engine, so treat it as "may read, may
not act" rather than "may read only the audit log". The gating in the browser is
a convenience; the Bridge checks the caller's role on every request regardless
of what the page shows.

**The owner role is not an ordinary role.** Only an owner may grant or remove
it; only an owner may demote, disable or arm a binding grant on the owner
account; the tenant's only owner cannot be demoted or disabled at all, whatever
its status. No caller may demote or disable *themselves* either — the session
keeps its old claims until the token expires, so that would be a lockout
disguised as a change. Changing the owner is a shell operation.

## Inviting people

**Settings › Users & roles** in the Helm is built on six operator-only Bridge
routes, all scoped to the caller's own tenant — an account in another tenant
answers 404, never 403, because 403 is itself a disclosure. The sixth is
`GET /api/auth/roles` above, which serves the role list the picker is built
from; the other five:

| Route | What it does |
|---|---|
| `GET /api/users` | Every account in the tenant: address, name, role, status, whether it is bound to an identity provider, whether MFA is enrolled, last sign-in. Never a hash, a secret or an IdP subject |
| `POST /api/users` | Creates the account row. `{"sso": true}` also arms a one-shot binding grant |
| `PATCH /api/users/{id}` | Role, display name, `status: active` or `disabled` |
| `POST /api/users/{id}/binding-grant` | Arms a fresh one-hour grant for an existing account |
| `GET /api/users/{id}/binding-grants` | Every grant armed for that account, live or spent |

**Nothing here sends mail.** An invited person has to be told out of band —
that is deliberate, and it means your onboarding process, not the platform,
decides how an invitation travels.

`POST /api/users` is narrower than the CLI. `genus user add` also writes the
CRM person, the tenant membership and the channel identifiers, which is what
you want when the person will also talk to an agent over a channel:

```bash
genus user list
genus user add --email alice@example.com --name "Alice" --role member --create-person
genus user link --email alice@example.com --telegram-id 123456789
```

Disabling an account revokes its live sessions. Without that a refresh token
keeps working for up to thirty days, so "disabled" would have meant "disabled
next month".

## Tenants

Tenants separate **data**, not administrators. Row-level security keys every
tenant table on the connection's tenant, and the `tenant_isolation` policy is a
`required` doctor check (`db.rls_coverage`, repairable with
`genus doctor --fix`).

```bash
genus tenant list
genus tenant create acme --name "Acme Holdings"
genus tenant status acme
```

Use tenants for business units inside one administrative domain. Use a **second
instance** when two groups must not share an operator, an audit log or a
credential store — and federate the two if they need to exchange anything.

Next: [Channels](03-channels.md) — reaching people where they already are.

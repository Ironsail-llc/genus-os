# Microsoft 365 workspace

> **Status: in development.** The Microsoft Graph transport, its sign-in, the
> connect command and the doctor checks exist. The mail and calendar tools
> are still being moved onto the transport, so until the release notes say
> otherwise, connect and verify a tenant with this runbook but do not pass
> `--enable` on a production instance. [Connect a tenant](#connect-a-tenant)
> is the step-by-step runbook.

Genus OS reads and sends mail and manages calendars through the `gws_*` tools.
By default they run against Google Workspace. With
`ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` the same tools are meant to run
against Exchange Online (Outlook mail and calendar) through Microsoft Graph.

## Architecture

The tool names stay the same on purpose. The platform's mail and calendar
safety rules (do-not-contact, no automatic scheduling, event dedup,
duplicate-reply and inbound-only checks, verification read-backs, CRM
write-through and benchmark refusal) are keyed on the `gws_*` names, so keeping
the names keeps every rule. Only the transport underneath changes:

| Layer | Google | Microsoft 365 |
|-------|--------|---------------|
| Tools and guards | `gws_*` tools, shared rules | same tools, same rules |
| Transport | `gws` CLI | `robothor/workspace/microsoft/graph.py` (`GraphClient`) |
| Sign-in | Google OAuth | Entra app-only (`robothor/workspace/microsoft/auth.py`) |

`GraphClient` applies Graph's rules in one place:

- **Stable ids and UTC.** Every request sends
  `Prefer: IdType="ImmutableId", outlook.timezone="UTC"`. A message id stays
  valid when the message moves folders, so the id recorded at send time still
  works for the read-back. A caller's own `Prefer` values are merged into that
  one header, and a value with the same name replaces the default.
- **Reads retry, writes don't.** A read that gets 429, 503 or 504 waits for
  `Retry-After`, up to 3 attempts and 60 seconds of waiting in total. A write
  is sent once. If the connection drops after sending, or Graph answers 5xx,
  the result is *unknown effect*: the caller has to read back before trying
  again, so a contact never gets the same email twice.
- **Paging stays on Graph.** `@odata.nextLink` is only followed when it points
  at the same origin as the Graph base URL.
- **Four requests per mailbox at once**, which matches Exchange's per-mailbox
  throttling limit.
- **Errors carry Graph's error code and message, nothing more.** They never
  include a token or a message body. Each request has its own
  `client-request-id` for Microsoft support.

## Auth model

The assistant signs in as an **application**, not as a user:

1. An Entra admin in the client's tenant registers an app and consents to the
   Graph application permissions for mail and calendar.
2. The app authenticates with a **certificate**. Its private key signs a
   10-minute client assertion for each token request, so no shared secret is
   sent. A client secret works as a fallback, but the certificate is
   preferred.
3. **Exchange RBAC for Applications** limits the app to exactly two mailboxes:
   the assistant's and the owner's. An app with tenant-wide access would let
   the assistant read every mailbox in the company. This scope is required.
4. A **canary mailbox** in the same tenant must be unreadable to the app. The
   doctor check `workspace.m365_scope` tries to read it and reports an error
   if it can. That catches a grant that was never scoped.

The credential lives in the instance vault, one row per field, and is re-read
on every token refresh, so a rotated certificate applies without a restart:

| Vault key | Holds |
|-----------|-------|
| `workspace/microsoft365/tenant_id` | Directory id (GUID) or verified domain |
| `workspace/microsoft365/client_id` | Application (client) id |
| `workspace/microsoft365/client_certificate_pem` | The app's certificate (PEM) |
| `workspace/microsoft365/client_private_key_pem` | Its RSA private key (PEM) |
| `workspace/microsoft365/client_secret` | Fallback only, when there is no certificate |

Tokens are cached in memory and refreshed 5 minutes before they expire.
Errors from Entra are reported by their `AADSTS` code only. Entra's own
description can quote a rejected secret back, so it is never logged.

## Settings

| Variable | Meaning |
|----------|---------|
| `ROBOTHOR_WORKSPACE_PROVIDER` | `google` (default) or `microsoft365` |
| `ROBOTHOR_M365_ASSISTANT_MAILBOX` | The assistant's mailbox |
| `ROBOTHOR_M365_OWNER_MAILBOX` | The operator's mailbox, whose calendar the assistant manages |
| `ROBOTHOR_M365_SCOPE_CANARY_MAILBOX` | A mailbox the app must *not* be able to read |

See [Settings](../reference/configuration.md) for the generated reference.

`genus workspace connect microsoft365` writes the three mailbox settings to
`config.yaml` (the same writer as `genus config set`) and sets
`ROBOTHOR_WORKSPACE_PROVIDER` only when you pass `--enable` and the probe
passes. It never writes an environment file.

## Connect a tenant

This runbook connects one Microsoft 365 tenant to one Genus OS instance. It
takes about 30 minutes, plus however long Exchange takes to apply the scope.
An admin in the client's tenant does steps 2, 4 and 5; the instance operator
does the rest.

### Prerequisites

- **An admin in the client's tenant.** Registering the app needs an
  Application Administrator (or Global Administrator) in Entra ID. Scoping it
  needs an Exchange Administrator (or Global Administrator) with the Exchange
  Online PowerShell module (`Install-Module ExchangeOnlineManagement`).
- **The assistant's mailbox.** A licensed user mailbox (any Microsoft 365
  plan that includes Exchange Online). Mail is sent from it and its inbox is
  read.
- **The owner's mailbox.** The operator's own mailbox. The assistant reads and
  edits its calendar.
- **A canary mailbox.** Any third mailbox in the same tenant, for example an
  admin or test user. The app must *not* be able to read it. The doctor tries,
  and an app that can is reported as an error.
- **Shell access to the Genus OS instance** as the user the engine runs as,
  with the vault reachable (`genus vault list` works).

### 1. Preview the plan

A dry run reads the vault and settings and writes nothing. It needs the
directory and application ids from step 2, so run it after step 2 if you
don't have them yet:

```bash
genus workspace connect microsoft365 --tenant-id <directory-id> --client-id <application-id> --assistant-mailbox assistant@example.com --owner-mailbox owner@example.com --canary-mailbox canary@example.com --dry-run
```

### 2. Register the app in Entra ID

In the [Microsoft Entra admin center](https://entra.microsoft.com):

1. **Identity > Applications > App registrations > New registration.**
   Name it, for example, `Genus OS assistant`. Supported account types:
   **Accounts in this organizational directory only (single tenant)**. No
   redirect URI.
2. From the app's **Overview**, copy the **Application (client) ID** and the
   **Directory (tenant) ID**.
3. Don't create a client secret. The connect command makes a certificate.

### 3. Create the certificate and store the credential

On the instance:

```bash
genus workspace connect microsoft365 --tenant-id <directory-id> --client-id <application-id> --assistant-mailbox assistant@example.com --owner-mailbox owner@example.com --canary-mailbox canary@example.com
```

The command:

- generates an RSA 3072 key and a self-signed certificate valid for one year,
  unless one is already stored (then it reuses it);
- stores the directory id, client id, certificate and private key in the vault
  under `workspace/microsoft365/`, for this instance's platform tenant;
- writes the three mailbox settings to `config.yaml`;
- prints the certificate (PEM), its SHA-1 and SHA-256 thumbprints, the Graph
  permissions and the PowerShell for step 5, with your values filled in.

The private key is never printed, not even with `--json`. Re-running the
command is safe: it reuses the stored certificate unless you pass `--rotate`.

### 4. Upload the certificate

Copy the printed block from `-----BEGIN CERTIFICATE-----` to
`-----END CERTIFICATE-----` into a file named `genus-os.cer`. In the app
registration, go to **Certificates & secrets > Certificates > Upload
certificate** and choose the file. Check that the **Thumbprint** Entra shows
matches the printed SHA-1 thumbprint.

### 5. Grant the permissions and scope them to two mailboxes

The app needs three Microsoft Graph **application** permissions:
`Mail.ReadWrite`, `Mail.Send` and `Calendars.ReadWrite`. `MailboxSettings.Read`
is optional; the doctor uses it only to compare timezones.

On their own, application permissions cover **every mailbox in the tenant**.
Use one of the two options below to limit them to the assistant's and the
owner's mailboxes. Don't use both.

#### Option A (recommended): RBAC for Applications

With RBAC for Applications the permissions are granted **in Exchange**, by
role assignments limited to a management scope. **Don't also add and
admin-consent the same permissions under API permissions in Entra.** An Entra
consent is tenant-wide, and Exchange adds it to the scoped grant, so the app
could read every mailbox again. The canary check catches this.

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@example.com

# ObjectId is the ENTERPRISE APPLICATION (service principal) object id:
# Entra > Enterprise applications > Genus OS assistant > Object ID.
# It is not the object id on the app registration's Overview page.
New-ServicePrincipal -AppId <application-id> -ObjectId <enterprise-app-object-id> -DisplayName "Genus OS assistant"

New-ManagementScope -Name "Genus OS assistant mailboxes" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'assistant@example.com' -or PrimarySmtpAddress -eq 'owner@example.com'"

New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.ReadWrite" -CustomResourceScope "Genus OS assistant mailboxes"
New-ManagementRoleAssignment -App <application-id> -Role "Application Mail.Send" -CustomResourceScope "Genus OS assistant mailboxes"
New-ManagementRoleAssignment -App <application-id> -Role "Application Calendars.ReadWrite" -CustomResourceScope "Genus OS assistant mailboxes"
New-ManagementRoleAssignment -App <application-id> -Role "Application MailboxSettings.Read" -CustomResourceScope "Genus OS assistant mailboxes"

Test-ServicePrincipalAuthorization -Identity <application-id> -Resource assistant@example.com   # InScope True
Test-ServicePrincipalAuthorization -Identity <application-id> -Resource canary@example.com      # InScope False
```

Exchange can take from 30 minutes to two hours to apply new role assignments
to Graph (verify against Microsoft docs). If the doctor still reports a 403
for the assistant or the owner, wait and run it again.

#### Option B (legacy): ApplicationAccessPolicy

Microsoft is replacing application access policies with RBAC for
Applications. Use this only if a tenant already relies on them.

1. In Entra, go to **API permissions > Add a permission > Microsoft Graph >
   Application permissions**. Add `Mail.ReadWrite`, `Mail.Send` and
   `Calendars.ReadWrite` (and optionally `MailboxSettings.Read`), then select
   **Grant admin consent**.
2. Restrict the grant to a mail-enabled security group holding the two
   mailboxes:

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@example.com
New-DistributionGroup -Name "Genus OS assistant mailboxes" -Alias genus-os-assistant-mailboxes -Type Security -Members assistant@example.com,owner@example.com
New-ApplicationAccessPolicy -AppId <application-id> -PolicyScopeGroupId genus-os-assistant-mailboxes -AccessRight RestrictAccess -Description "Genus OS: assistant and owner only"
Test-ApplicationAccessPolicy -Identity assistant@example.com -AppId <application-id>   # Granted
Test-ApplicationAccessPolicy -Identity canary@example.com -AppId <application-id>      # Denied
```

### 6. Run the doctor

```bash
genus doctor --category workspace
```

Every check must pass:

| Check | Severity | Passes when |
|-------|----------|-------------|
| `workspace.m365_connection` | required | The mailboxes are set, the vault holds the credential, Entra issues a token, and the assistant's inbox and the owner's calendar can be read |
| `workspace.m365_scope` | required | Reading the canary mailbox is **denied**. If the app can read it, its scope is not restricted: it can read mailboxes beyond the assistant and the owner. That is an error |
| `workspace.m365_canary_configured` | recommended | A canary mailbox is set. Without one the scope is unproven |
| `workspace.m365_timezone` | recommended | The owner's Exchange timezone matches `ROBOTHOR_TIMEZONE`. Windows zone names such as `Eastern Standard Time` are mapped to IANA names. A zone that can't be mapped, or settings the app may not read, are reported but don't fail |

The checks run only on an instance with `ROBOTHOR_WORKSPACE_PROVIDER=microsoft365`
or with a Microsoft 365 credential in the vault. Everywhere else they skip.
With `--offline`, only the local configuration step runs. Results name the
configured mailboxes and Graph status codes, never a token or mail content.

On a Microsoft 365 instance, two Google-only checks pass with a note instead
of asking the `gws` CLI: `calendar.operator_calendar_writable` and the `gws`
half of `email.transport`. SMTP settings, if any, are still checked.

### 7. Enable

```bash
genus workspace connect microsoft365 --enable
```

The ids and mailboxes from step 3 are reused, so you only need flags to
change them. `--enable` runs the connection and scope probes and sets
`ROBOTHOR_WORKSPACE_PROVIDER=microsoft365` in `config.yaml` only if both pass.
It needs a canary: an unproven scope is never enabled. Restart the engine
afterwards so it reads the new setting.

### Rotating the certificate

The certificate is valid for one year. Before it expires:

1. Run `genus workspace connect microsoft365 --rotate` to store a new one.
2. Upload the printed certificate to the app registration (step 4) straight
   away. Delete the old one there once `genus doctor --category workspace`
   passes.

The transport re-reads the vault on every token refresh, so no restart is
needed. Token requests fail between steps 1 and 2, so do both together.

### Troubleshooting

| The doctor says | Cause | Fix |
|-----------------|-------|-----|
| `microsoft365 not connected` | No credential in the vault for this platform tenant | Run step 3 as the engine's user |
| `invalid_client: AADSTS700027` | The stored certificate isn't uploaded to the app, or a different one is | Step 4; compare thumbprints |
| `AADSTS700016` | Wrong client id, or the app is in another directory | Check both ids from step 2 |
| `cannot read the assistant inbox ... graph HTTP 403` | Role assignment missing, mailbox not in the scope, or not applied yet | Step 5; wait for Exchange to apply it |
| `app scope is not restricted` | Tenant-wide Entra consent next to RBAC, or no scope at all | Remove the permissions under API permissions in Entra (option A), or finish option B |
| `canary mailbox ... was not found` | Typo, or the canary is not a mailbox | Set `ROBOTHOR_M365_SCOPE_CANARY_MAILBOX` to a real mailbox |

### Verify against Microsoft docs

These details come from Microsoft's documentation and change from time to
time. Check them against the current pages before a client rollout:

- The RBAC for Applications role names (`Application Mail.ReadWrite`,
  `Application Mail.Send`, `Application Calendars.ReadWrite`,
  `Application MailboxSettings.Read`), and that an Entra consent adds to the
  scoped grant rather than being replaced by it.
- That `New-ServicePrincipal -ObjectId` takes the enterprise application's
  object id, and how long role assignments take to reach Graph.
- That an out-of-scope mailbox answers HTTP 403 (`ErrorAccessDenied`) rather
  than 404.
- That `GET /users/{id}/mailboxSettings/timeZone` needs `MailboxSettings.Read`
  and is not covered by `Mail.ReadWrite`.

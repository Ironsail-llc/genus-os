# Email

Email is a built-in delivery channel. An agent whose manifest says
`delivery.channel: email` mails its output to an address or to a person in the
CRM, and `genus channel verify email --to you@example.com` proves the whole path
before you wait for a scheduled run to tell you it does not work.

Two things make this channel different from the others, and both are worth
reading before you configure it.

**It checks the opt-out list first.** `crm_people.do_not_contact` is a legal
obligation rather than a preference. Before a transport is chosen — before the
`gws` binary is probed, before an SMTP socket is opened — the recipient is
checked against that flag, and a flagged recipient is refused with
`failed:email_dnc` and a row in `agent_guardrail_events`. Nothing left the box.

**There are two transports, and the fallback is for absence only.** The `gws`
CLI when it is installed, SMTP when it is not. A `gws` *error* — an expired
OAuth grant, a quota — is a failed send and **not** a reason to retry over SMTP:
the from-address and the audit trail differ between the two, so a silent swap
after an auth failure would deliver your briefing as somebody else.

## Configure this instance

### If you already use the `gws` CLI

Nothing to do. The channel finds the binary and sends through the Google
Workspace account it is authenticated to, exactly as the `gws_gmail_send` agent
tool does. Check with:

```bash
genus doctor --only email.transport
```

### Otherwise: SMTP

```bash
genus channel add email --smtp-host smtp.example.com --from-address genus@example.com --smtp-user genus@example.com
```

It prompts for the password without echoing it, and writes it to the instance
vault when this install has a master key, or to the 0600 `genus.env` file in
your workspace otherwise. It prints **where** it wrote and a SHA-256
fingerprint — never the value. The host, port, user and from-address are not
credentials and go to `config.yaml` instead.

`--smtp-password` exists only to be **refused**. A credential on a command line
is readable by every account on the box through `ps` and `/proc/<pid>/cmdline`,
your shell has already written it to a history file, and nothing the command
does afterwards takes that back. For a script, export it instead:

```bash
export ROBOTHOR_EMAIL_SMTP_PASSWORD=...   # read from your secret store, not typed
genus channel add email --smtp-host smtp.example.com --from-address genus@example.com
```

| Setting | Default | What it does |
|---------|---------|--------------|
| `ROBOTHOR_EMAIL_FROM` | — | The address SMTP sends from. Required for SMTP; unused by `gws` |
| `ROBOTHOR_EMAIL_SMTP_HOST` | — | The server. Empty means there is no SMTP transport |
| `ROBOTHOR_EMAIL_SMTP_PORT` | `587` | 587 is submission with STARTTLS; 465 opens an implicit-TLS session instead |
| `ROBOTHOR_EMAIL_SMTP_STARTTLS` | `true` | Upgrade before authenticating. Ignored on port 465 |
| `ROBOTHOR_EMAIL_SMTP_USER` | — | Empty means the channel does not authenticate |
| `ROBOTHOR_EMAIL_SMTP_PASSWORD` | — | Read from the environment, then this instance's vault |

A host with no from-address is **not** a transport: SMTP has no equivalent of
Gmail's "send as the authenticated account", so the channel reports itself
unconfigured and `genus doctor` names the missing half.

Restart the engine so it re-reads the settings:

```bash
sudo systemctl restart robothor-engine
```

## Prove it works

```bash
genus channel verify email --to you@example.com
```

There is no configured verify target and there will not be one. A verification
mail carries nothing that distinguishes it from a real briefing, so a fallback
address would be a message to whoever the box happens to point at — `--to` is
named every time.

| Step | What a failure means |
|------|----------------------|
| `transport` | No `gws` binary and no SMTP host, or the SMTP server refused the session or the password |
| `send` | No `--to`; an address that is not one; an address on the opt-out list; or the transport accepted the call and returned no message id |

Exit codes, because this is a command scripts wrap: **0** every step passed,
**1** a step failed, **2** there was nothing to verify — an unknown channel
name, or no transport on this instance. Two is not a failure: an instance that
never wanted email has not failed a check it did not ask for.

**Verify refuses a `--to` that is on the opt-out list, and only that.** It does
not refuse every address the CRM knows: the operator's own address is usually a
`crm_people` row, and a verify that could not be aimed at it would be a verify
nobody runs.

`genus doctor` runs the configuration half (`email.transport`) and stops short
of sending: a diagnostic that mails somebody every time a health panel refreshes
is not a diagnostic. Its SMTP session is skipped under `--offline`.

## Targets

`delivery.to` in an agent manifest takes either form:

| Form | Meaning |
|------|---------|
| `alice@example.com` | An address. Checked against the opt-out list as typed, case-insensitively |
| `Alice <alice@example.com>` | The same; the bare address is what is used |
| `11111111-2222-3333-4444-555555555555` | A `crm_people` id. Resolves to that person's primary email |

The id form is the one worth having. It follows the person rather than the
address, so the opt-out flag on their row is honoured even when the address on
it has changed — and a person whose primary email is empty is
`failed:email_unresolved_target` rather than a message sent nowhere.

One message per send: email has no chunk limit worth splitting on, so there is
no `partial:` outcome here. The subject is the agent's display name unless the
caller passes one.

## What gets recorded

Delivery status is derived from what a transport acknowledged — Gmail's own
message id, or an SMTP `send_message` that refused no recipient — never from the
send call returning:

| `agent_runs.delivery_status` | What happened |
|------------------------------|---------------|
| `delivered` | The transport returned a message id |
| `failed:email_send` | The transport refused, raised, or accepted the call without returning an id |
| `failed:email_dnc` | The recipient is flagged `do_not_contact`. Nothing was sent, and the refusal is in `agent_guardrail_events` |
| `failed:email_dnc_unreadable` | The opt-out list could not be read. "We could not check" is not "nobody opted out", so it is its own status and not a claim about a person |
| `failed:email_no_transport` | Neither the `gws` CLI nor `ROBOTHOR_EMAIL_SMTP_HOST` + `ROBOTHOR_EMAIL_FROM` |
| `failed:email_no_target` | The manifest names no `delivery.to` |
| `failed:email_unexpanded_target` | A literal `${VAR}` reached the target |
| `failed:email_unresolved_target` | Neither an address nor a `crm_people` id, or a person with no primary email |
| `failed:email_no_run` | A send with no run, so no tenant — and the opt-out list is per-tenant. A guard may not guess whose list it is reading |

`ROBOTHOR_DNC_MODE=observe` turns every one of those refusals into a logged,
recorded note (`agent_guardrail_events.action = 'observed'`) and lets the mail
go. It is the same lever the `gws_gmail_send` tool answers to, and it exists so
nobody has to reach for the one below it, which is commenting out the check.

## A known asymmetry

The `gws_gmail_send` **tool** mirrors what it sent into the CRM timeline
(`_record_sent_email`). This **channel** does not: `channel_bus.on_post_delivery`
already mirrors channel sends, and two writers for one message is how a contact
history ends up with duplicates that nothing can tell apart. If you are looking
for a channel-sent briefing in a contact's timeline, it arrives by the channel
bus, not by the mail mirror.

## Not here yet

* **An inbound half.** There is no IMAP reader, so `EmailChannel.ask` raises
  rather than returning a plausible default — a question mailed out could never
  be answered, and an approval nobody gave is worse than a refusal. A run
  triggered from elsewhere still records its question as an `agent_questions`
  row, answerable from the Helm.
* **Threading.** The provider message id is recorded on every send, which is
  what a phase-2 reply reader will thread against; nothing reads it yet.
* **Attachments and HTML.** Plain text only. The agent tool path
  (`gws_gmail_send`) takes `content_type: html`; the channel does not.

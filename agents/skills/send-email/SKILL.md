---
name: send-email
description: Compose and send an email via the gog CLI.
---

# Send Email

Compose and send an email from the configured account.

## Inputs

- **to**: Recipient email address (required)
- **subject**: Email subject line (required)
- **body**: Email body (required, supports HTML)
- **reply-to**: Thread ID if replying (optional)
- **cc**: CC recipients (optional)

## Execution

1. If replying to an existing thread:
```sh
gog gmail send --reply-all --thread-id <THREAD_ID> --cc $ACCOUNT_EMAIL --body-html "<BODY>" --account $ACCOUNT_EMAIL --no-input
```

2. If composing a new email:
```sh
gog gmail send --to "<TO>" --subject "<SUBJECT>" --body-html "<BODY>" --cc $ACCOUNT_EMAIL --account $ACCOUNT_EMAIL --no-input
```

3. After sending, log the interaction:
```
log_interaction(contact_name="<NAME>", channel="email", direction="outgoing", content_summary="<SUMMARY>")
```

## Configuration

Replace `$ACCOUNT_EMAIL` with your sending email address (e.g., `assistant@example.com`).

## Rules

- Always CC the owner on outgoing emails (configure the CC address)
- Use the configured sender account for all outgoing mail
- Keep professional tone consistent with your persona
- HTML body -- use `<br>` for line breaks, `<p>` for paragraphs
- Never send without explicit user approval for new threads

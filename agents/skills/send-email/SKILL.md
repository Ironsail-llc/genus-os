---
name: send-email
description: "Compose and send an email \u2014 using native GWS tools (never gog CLI\
  \ for replies)"
---

# Send Email

Compose and send an email from the configured account using native GWS tools.

## Decision Tree

**Replying to an existing thread?** → `gws_gmail_reply(thread_id, body)`  
**Sending a brand-new email?** → `gws_gmail_send(to, subject, body)`  
**`exec` + `gog gmail send`?** → **NEVER for replies. BANNED.**

## Reply to Existing Thread

1. Identify the `thread_id` from the task body, email search, or prior context.
2. Call `gws_gmail_reply(thread_id, body)` — it handles reply-all recipient resolution, threading headers (In-Reply-To, References), and duplicate prevention automatically.
3. Optionally pass `cc` for additional recipients beyond those already in the thread.

## Send New Email

1. Call `gws_gmail_send(to, subject, body)`.
2. Use `content_type: "html"` for HTML formatting (use `<br>` for line breaks, `<p>` for paragraphs).
3. Optionally pass `cc` for CC recipients.

## After EVERY Send — Verify Delivery

1. Call `gws_gmail_search(query="in:sent", max_results=1)`.
2. Call `gws_gmail_get(message_id=...)` on the result.
3. Confirm the `To` header contains actual recipient addresses.
4. If `To` is empty — the send failed silently. Resend using `gws_gmail_reply` or `gws_gmail_send` with explicit recipients.

## After Sending — Log the Interaction

```
log_interaction(contact_name="<NAME>", channel="email", direction="outgoing", content_summary="<SUMMARY>")
```

## Rules

- Always CC the owner on outgoing emails when appropriate.
- Use the configured sender account for all outgoing mail.
- Keep professional tone consistent with the persona.
- Never send without explicit user approval for new threads.
- For replies driven by a CRM task, pass `parent_task_id` when spawning the email-responder so the child receives the task objective and constraints.

## Pitfalls

- **`gog gmail send --reply-all` is BROKEN.** The CLI's `--reply-all` flag silently resolves zero recipients and sends with an empty `To` field. This has caused real incidents. NEVER use `exec` + `gog gmail send` for replies. Use `gws_gmail_reply` instead.
- **Empty `To` field.** If verification shows an empty `To`, the email went to nobody (except possibly CC). Resend immediately.
- **Thread ID required for replies.** `gws_gmail_reply` needs the `thread_id`, not the `message_id`. Get it from `gws_gmail_search` or `gws_gmail_get(thread_id=...)`.

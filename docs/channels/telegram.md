# Telegram: sending and receiving files

Telegram is a built-in channel and needs no `ROBOTHOR_CHANNELS` entry. Set
`ROBOTHOR_TELEGRAM_BOT_TOKEN` and it starts; see
[Channel access](access.md) for who is allowed to reach it.

This page is about the attachments half — pictures and files, both directions.

## Receiving: the inbox

Every document, photo, video, audio file, voice note and sticker that arrives is
**saved before the agent is told about it**, under the instance workspace:

```
<workspace>/inbox/telegram/<chat_id>/<YYYY-MM-DD>/<file_unique_id>-<safe-name>
```

* The date is the day it arrived, which is what makes retention a directory
  walk rather than a query.
* `file_unique_id` is Telegram's own identity for the bytes, so the same file
  sent twice occupies one path.
* The name is sanitised to a single path segment. A document called
  `../../.ssh/id_rsa` lands as `…/<uid>-id_rsa` and nothing outside the inbox
  is reachable.
* Files are `0600`. An inbound file is the operator's, not the box's.

The agent's turn carries the caption **verbatim and first** — it is the
instruction — then one entry per attachment naming its path, kind and size,
with the extracted text (for text and PDF, capped at 50 000 characters and
labelled with the total so the agent knows to `read_file` the rest) or a local
vision-model description indented under it. An image's entry always says
`call view_image on <path> to look at it`.

An album (several photos sent together) is collected for about 1.5 seconds and
delivered as **one turn with N paths**, because Telegram sends each member as a
separate update with the caption on exactly one of them.

### Limits

| | |
|---|---|
| Largest file the bot can download | **20 MB** — Telegram's own `getFile` limit |
| Text/PDF extracted into the first turn | 50 000 characters (the path ships with it) |
| Kept for | `ROBOTHOR_INBOX_RETENTION_DAYS`, default 30 |

A file above 20 MB gets a reply naming the limit and asking for a link. There
is no way around it from this side: the Bot API will not hand the file over.

### Retention

The daily retention sweep prunes `<workspace>/inbox/` and **nothing else**. A
file an agent moved somewhere useful has left that tree and is never touched.
Set `ROBOTHOR_INBOX_RETENTION_DAYS=0` to disable the prune entirely.

### The attachment row

Each saved file is recorded on the stored user turn's JSONB under
`attachments`, beside the text rather than inside it:

```json
{
  "path": "/…/inbox/telegram/100200300/2026-09-15/AgACAg-report.pdf",
  "name": "report.pdf",
  "kind": "document",
  "mime": "application/pdf",
  "size": 184320,
  "width": 1280,
  "height": 720,
  "telegram_file_id": "BQACAgEAAx…",
  "telegram_file_unique_id": "AgACAg",
  "caption": "summarise this"
}
```

`kind` is one of `image`, `document`, `video`, `audio`, `voice`. `width` and
`height` are present only when known, and absent rather than null — a reader
never has to tell "unknown" from "zero".

A file named like a credentials file (`.env`, `credentials.json`, `id_rsa`, …)
carries `"secret": true` and `"original_name"`, and is kept one level deeper,
in `<date>/secret/`. It is still **saved** — you sent it on purpose and may
want it moved or renamed — but its contents are never quoted into a prompt,
`read_file` refuses it with the secrets-file sentence, and `send_file` refuses
to send it back out.

The verdict is taken from the name Telegram supplied and recorded as the
DIRECTORY, because sanitising a name for the filesystem is exactly what
destroys the evidence (`.env` becomes `env`) and a name that has to be parsed
to be understood eventually gets parsed wrongly.

## Sending: `send_file`

```
send_file {path, caption?, as?: photo|document|auto, target?}
```

The agent writes the file, then sends it by path. It never pastes binary or
base64 into a message.

* **`path`** must be inside the workspace — the inbox counts. The *resolved*
  path is judged, so a symlink pointing out of the workspace is refused, and a
  file with more than one name on the filesystem (a hard link, which the
  resolver cannot follow) is refused with a note to copy it and send the copy.
* **Refused** for anything `robothor.engine.secret_paths` calls a secrets file,
  for anything in the inbox's `secret/` directory, and for any file whose first
  256 KB decode as text and carry a credential-shaped value — whatever it is
  called, because renaming would otherwise be the whole attack. A credential
  buried past that first 256 KB is not seen. The refusal names the file and the
  kind of credential, never the value.
* **Checked twice.** The same ladder runs when the agent asks *and* again
  immediately before the bytes are uploaded, and a file queued for a scheduled
  run is pinned to the digest it was approved with — rewrite it in between and
  it is refused as `changed_since_queued` rather than sent.
* **`as: auto`** (the default) sends an image as a photo when it is under
  10 MB and its width plus height is under 10 000, and as a document
  otherwise — a document the operator can open beats an error.
* **`target`** is optional and only an operator-tier agent may use it. Everyone
  else replies to the chat the run came from.
* Ceiling: 50 MB, which is Telegram's for an outbound document.

Every send is written to the audit log by basename, size, kind and target.
Never by content.

### Scheduled runs

An agent with `delivery.mode: announce` has nobody watching while it works, so
`send_file` **queues** the file onto the run instead of sending it immediately.
It goes out after the announcement, so a nightly report can arrive as a PDF
with its covering note. An agent with `delivery.mode: none` reaches nobody, and
a file it queues is not sent.

### Other channels

`Channel.send_attachment` is on the channel protocol, but only Telegram
implements it today. Slack, email, the Helm web chat and the event bus raise
`NotImplementedError` naming themselves, and `send_file` passes that refusal
back to the agent as text it can relay. Link to the file instead.

## Vision: what the agent can actually see

`view_image` returns `seen_by`:

| `seen_by` | what happened |
|---|---|
| `primary` | the picture is in front of the agent's own model |
| `vision-model` | the model cannot accept images, so the local VLM described it |
| `nobody` | neither worked — said plainly, with an error |

Which one you get depends on `accepts_images` in the model registry, which is
**declared, not probed**: a model is marked able to see only where that is a
documented property of its family. A model nobody has described keeps being
shown pictures and is told the capability is unconfirmed. When a provider
actually refuses an image, that refusal is recorded for the process and
`view_image` switches to the local description from then on — the capability
can only ever be revoked at runtime, never granted.

The local vision model is `ROBOTHOR_VISION_MODEL` on `ROBOTHOR_OLLAMA_URL`.
With no Ollama reachable, an image the primary model cannot see yields
`seen_by: nobody` and an honest error rather than an invented description.

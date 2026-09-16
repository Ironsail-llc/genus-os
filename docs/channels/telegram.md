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
<workspace>/inbox/telegram/<chat_id>/<YYYY-MM-DD>/secret/<file_unique_id>-<safe-name>
```

* The date is the day it arrived, which is what makes retention a directory
  walk rather than a query.
* `file_unique_id` is Telegram's own identity for the bytes, so the same file
  sent twice occupies one path.
* The name is sanitised to a single path segment. A document called
  `../../.ssh/id_rsa` lands as `…/<uid>-id_rsa` and nothing outside the inbox
  is reachable.
* Files are `0600`. An inbound file is the operator's, not the box's.
* The second path is for a file whose **original** name said "credentials" —
  see below. The directory is the flag, because a sanitised filename cannot
  carry it.

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

The daily retention sweep prunes `<workspace>/inbox/` — every channel under it,
including the `secret/` subdirectories — and **nothing else**. A file an agent
moved somewhere useful has left that tree and is never touched. Set
`ROBOTHOR_INBOX_RETENTION_DAYS=0` to disable the prune entirely.

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
want it moved or renamed — but its contents are never quoted into a prompt, and
three things refuse it: `read_file`, `send_file`, and the printing commands
`exec` knows about — `cat`, `head`, `grep`, `python3 -c` and the rest of that
list — which all come back with the same secrets-file sentence. One rule, one
wording, whichever tool the agent reached for.

**Two of those are boundaries and one is a speed bump, and it is worth knowing
which.** `read_file` and `send_file` refuse the file itself, however it is
spelled; so does the content scan that runs on whatever `send_file` is about to
hand the channel, which is what catches a copy. The `exec` refusal is a
denylist over command words, and a denylist over a shell is a cost, not a wall:
an agent that `cd`s into the directory and reads a relative name, or copies the
file somewhere else first, or reads it through a redirect rather than an
argument, is not stopped. That is deliberate — the value of the `exec` rung is
that an agent reaching for `cat` is told the rule and stops, not that a
determined one cannot get the bytes. **The bytes are on your disk because you
sent them; if you did not mean to, move or delete the file.**

The verdict is taken from the name Telegram supplied and recorded as the
DIRECTORY, because sanitising a name for the filesystem is exactly what
destroys the evidence (`.env` becomes `env`) and a name that has to be parsed
to be understood eventually gets parsed wrongly. The `secret/` directory under
a dated inbox folder is a recognised secrets location in its own right, so a
tool that learns about secrets files at all learns about this one too.

`cp` is not refused, here or for a `.env` on disk — the `exec` rung covers the
commands that **print**, and copying is not printing. That is the gap the
paragraph above describes, and the boundary that closes it is at the other end:
`send_file` scans what it is about to hand the channel and refuses a file whose
contents are credential-shaped, wherever that file was copied from — within the
first 256 KB it reads, which is spelled out under [Sending](#sending-send_file)
below.

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
  256 KB decode as text and carry either a credential-shaped **value** or a
  credential-named **key** set to a literal (`client_secret`, `private_key`,
  `api_key`, `access_token`, `password`, …) — whatever the file is called,
  because renaming would otherwise be the whole attack. There is no size limit
  on the scan: only the first 256 KB are read, so a 20 MB video costs the same
  as a 2 KB note. A credential buried past that first 256 KB is not seen. The
  refusal names the file and the kind of credential, never the value.
* **Not refused**, so that scaffolds, examples and documentation stay sendable:
  a comment line; and a credential-named field whose value is a `${VAR}`
  reference, empty, `null`/`~`/a boolean, a bare number, a setting word such as
  `disabled`, a redaction such as `REDACTED`, `changeme` or
  `YOUR_API_KEY_HERE`, or a YAML tag such as `!vault`. Prose that merely
  contains "token:" is a sentence, not an assignment. A file that mixes
  placeholders with one real secret is still refused.
* **Checked twice.** The same ladder runs when the agent asks *and* again
  immediately before the bytes are uploaded, and a file queued for a scheduled
  run is pinned to the digest it was approved with — rewrite it in between and
  it is refused as `changed_since_queued` rather than sent.
* **`as: auto`** (the default) sends an image as a photo when it is under
  10 MB and its width plus height is under 10 000, and as a document
  otherwise — a document the operator can open beats an error.
* **`target`** is optional and only an operator-tier agent may use it. Everyone
  else replies to the chat the run came from.
* Ceiling: 50 MB, which is Telegram's for an outbound document. It is the only
  size rule on the way out — a 12 MB PNG or a 20 MB video sends.

Every send is written to the audit log by basename, size, kind and target, and
so is every refusal. Never the content.
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

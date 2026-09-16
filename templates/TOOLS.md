# TOOLS.md — Local Notes

The platform's tool catalogue lives in `docs/TOOLS.md`: what each tool returns,
which mail tool is which, and how an agent reaches a tool that is not in front
of it. **This file is for what is true about YOUR setup and nowhere else.**

Keeping them apart is the point. The catalogue ships with the platform and is
rewritten by upgrades; this file is yours and survives them. It is also loaded
into an agent's context as a bootstrap file, so everything in it costs tokens
on every run — keep it short, and keep it to facts the platform cannot know.

## What goes here

- Camera names and locations
- SSH hosts and aliases
- Speaker and room names, preferred TTS voice
- Device nicknames
- Google Chat space names you use often (the resource name is unreadable)
- Which calendar is which, if you have more than one
- Anything else environment-specific

## Example

```markdown
### Cameras
- living-room → main area, 180° wide angle
- front-door → entrance, motion-triggered

### SSH
- build-box → 10.0.0.12, user: deploy

### Chat spaces
- "Ops" → spaces/AAAA1111 (use with gws_chat_send)

### TTS
- Preferred voice: "Nova"
- Default speaker: kitchen
```

## What does NOT go here

**Do not re-describe the tools.** A hand-written list of tool names and
arguments in a bootstrap file is a second catalogue that drifts from the first,
and the drift is invisible: the agent believes this file. If a tool's
description is wrong, fix the schema, not this.

**Do not write "you have these N tools".** A broad agent's toolset is loaded on
demand — the engine shows it a small core each turn and tells it how many more
are reachable. A fixed list here contradicts what the run actually hands it.

**Do not name a CLI where a tool exists.** `gws_gmail_reply` threads the reply,
replies to everyone on the thread, checks the do-not-contact list and refuses a
duplicate. `gog gmail send` does none of that. An instruction that prefers the
CLI is an instruction to go around four guards.

**Do not name a tool this agent's manifest does not grant.** The agent cannot
see its manifest; a tool it was told to use and does not have just fails, and
it will find another way. `genus doctor --category agents` reports these.

## Which mail tool

The one thing worth repeating here, because three different things answer to
"inbox":

| You want | Use |
|---|---|
| Live email, right now | `gws_gmail_search` → `gws_gmail_get` |
| The CRM's stored correspondence with a contact | `list_messages`, `get_conversation` |
| This agent's own notifications | `get_inbox`, `ack_notification` |

`look` is a camera.

---

Add whatever helps. This is your cheat sheet, not the manual.

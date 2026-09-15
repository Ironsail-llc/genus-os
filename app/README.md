# The Helm — app.robothor.ai

Genus OS's command center. Live dashboard and chat interface in a two-panel Dockview layout: canvas (65%) + chat (35%).

## Stack

- **Next.js 16** + Dockview + shadcn/ui + Recharts + TanStack Table
- **Port**: 3004, service: `robothor-app.service`
- **Chat**: Custom SSE bridge to Agent Engine (same agent as Telegram)
- **Canvas**: HTML-first rendering via iframe srcdoc (Tailwind CSS), native components as fallback
- **Dashboard generation**: Gemini 2.5 Flash-Lite (Sep) via OpenRouter (~1-3s)

### Home is real data, generation is explicit

The Dashboard tab renders `business/default-dashboard.tsx` — health, tasks,
agents and quick actions read live from the BFF. Opening the app makes **no**
model call. The generated canvas is one click away ("Generate a view with AI"),
and the Canvas tab always says what it is and offers the action rather than
presenting a blank iframe.

**Behaviour change:** because `useDashboardAgent` lives with the AI canvas, a
chat reply no longer regenerates a canvas unless that view is open. The
"updating" flag is raised only while a mounted agent has a regeneration in
flight, so the canvas never opens onto a spinner for work nobody is doing.

Every section degrades honestly: a data call that fails names the endpoint and
the status ("Conversations unavailable (401)") instead of rendering an empty
card. Server-side data fetches for the generation prompt
(`lib/dashboard/conversation-context.ts`, `welcome-context.ts`) carry the
caller's bridge bearer token via `lib/bridge-auth.ts`; their TTL cache is
partitioned per caller so one operator's rows never reach another's dashboard.

### Talking to another agent, and answering escalations in place

The chat header carries an agent switcher when the appliance has more than one
**chattable** agent — `GET /api/agent-manifests` marks an agent chattable when
it holds its session between runs (`schedule.session_target: persistent`) or
when it is the main agent, because an `isolated` worker would answer from a
session it forgets the moment the run ends. The listing also reports
`default_agent`, derived from the agent segment of the engine's
`main_session_key` (not `default_chat_agent` — they are separate variables and
only the session key answers "which id needs no key"). Picking somebody else
reloads that agent's history and adds `agent: "<id>"` to every chat request,
which the BFF turns into `session_key: "agent:<id>:primary"`; the engine's
`_effective_session_key` does the rest for each role. The **main agent sends no
key at all** — that omission is what keeps the operator's shared main session
(webchat and Telegram, on purpose) exactly as it was, so the switcher's first
option always carries the empty string as its value rather than an id. The
canvas prompt injection stays main-only for the same reason. The choice is
remembered per browser in `localStorage`, never in the URL, and is not routed
to until the listing has confirmed it is still chattable.

`chattable` is presentation, not authorization. `chat.py` runs whichever agent
the session key names for any authenticated caller, so every BFF chat route
resolves a named `agent` through `lib/chat/agent-guard.ts` first: it re-fetches
the operator-gated listing **with the caller's own bridge credentials** and
answers 403 if that caller's listing does not offer the agent as chattable —
which refuses every agent for a member, whose listing is refused outright. An
`agent` that cannot form a well-shaped key is a 400. Neither ever falls back to
"no key", because "no key" is the operator's own conversation. The guard is
skipped entirely when no agent is named, so the default chat costs no extra
round-trip.

The same chat answers the two things an agent can block on. An `ask_user`
question is a durable row settled at `POST /api/approvals/question/{id}`; a
**tool-permission escalation** is an in-RAM request the engine is waiting on,
settled at `POST /api/approvals/escalation/{id}` with **Allow once**
(`{approved: true}`), **Allow for this session**
(`{approved: true, remember_session: true}`) or **Deny** (`{approved: false}`).
Both arrive as the same `approval_required` SSE event and the card branches on
its `kind`. An escalation shows the tool and a live countdown from
`timeout_seconds`: when it runs out the engine has denied the tool itself, and
the card says so rather than collecting a decision nobody is waiting for. A run
that raises several approvals shows a card for each, keyed by id — an
escalation that never reached the screen is not one the operator can scroll
back to, it is a coroutine blocked in the engine that will time out and deny.

## Navigation

The Helm is a single client shell: every view is mounted in
`components/layout/app-shell.tsx` and shown by a `visible` prop — there is no
App Router route per view.

- **The URL is the state.** `hooks/use-view-route.ts` keeps the active view in
  the query string (`?v=<view>`, plus `&s=<page>` inside Settings), so a reload
  or a pasted link lands on the same screen and back/forward move between
  views. An unknown value falls back to the chat view rather than a blank shell.
- **Chat is home.** Desktop and mobile both open on chat. Beside other views the
  chat panel stays docked on the right (toggle at the foot of the sidebar); on
  the chat view it takes the column and the AI canvas becomes an optional right
  rail (the "Canvas" button in the header).
- **One nav config.** `components/layout/nav-config.ts` is the only description
  of the navigation — Chat, Workspace, Observe, Settings. The sidebar, the
  mobile "More" sheet and the ⌘K palette all read it, so they cannot drift.
  Entries whose screen is not built yet (Inbox, Memory, Audit, Logs) render as
  disabled buttons with a "soon" pill instead of a dead link.
- **Settings is a container view** with its own grouped sub-navigation. Its
  pages are placeholders that say what will live there; Flags is the existing
  controls screen and Appearance carries the theme toggle.
- **Mobile** gets Chat · Inbox · Agents · More; More opens a sheet with
  everything else, built from the same config.
- **Role gating is UX only.** The sidebar, the sheet and the palette hide
  Settings from anyone who is not `owner`/`admin`, read from the session the
  auth layer already exposes. Authorization itself stays server-side: the
  bridge checks the caller's role on every request regardless of what the nav
  shows.

## Authentication

Auth.js (next-auth v5) with two env-gated sign-in paths; each provider
registers only when fully configured, and the bridge remains the token/RBAC
authority via the `/api/auth/sso` exchange:

- **Cloudflare Access header trust** — set `CF_ACCESS_TEAM_DOMAIN` +
  `CF_ACCESS_AUD` when the app is deployed behind a Cloudflare Access policy.
  `/signin` verifies the edge-injected `Cf-Access-Jwt-Assertion` (JWKS
  signature, issuer, audience — never header presence) and signs the user in
  silently via `/signin/cloudflare`. One authentication, at the edge.
  **Required:** the team domain must also be appended to the bridge's
  `GENUS_OIDC_ISSUERS` allowlist — in production the bridge rejects any issuer
  not listed there, and every sign-in would 403 with
  `error=CloudflareAccessFailed`.
- **Generic OIDC** — set `AUTH_OIDC_ISSUER` + `AUTH_OIDC_CLIENT_ID` (+ secret,
  name) for a standard IdP redirect flow (Okta / Entra / Google / Keycloak…).

**`AUTH_URL` must be the public origin** — the scheme+host a browser actually
uses (`https://helm.example.com`), alongside `CF_ACCESS_TEAM_DOMAIN` /
`CF_ACCESS_AUD` and `AUTH_SECRET`. Next's standalone server binds
`process.env.HOSTNAME || '0.0.0.0'` and attaches it to every request, so inside
a route handler **both** `request.url` and `request.nextUrl` report the *bind*
address rather than the address the browser used (unless
`experimental.trustHostHeader` is set, which this app does not set).
`/signin/cloudflare` therefore resolves its origin as **`AUTH_URL` → the
request's `x-forwarded-host` (else `Host`) with `x-forwarded-proto` (else the
request's own scheme) → `request.nextUrl.origin`**.

That third step is a last resort that in practice never runs: every HTTP/1.1
request carries a `Host`, so the second step answers first. It is **not** a safe
default either — it is the bind address, i.e. the incident. So with `AUTH_URL`
unset the redirect is only ever as trustworthy as the `Host` header reaching the
app, which is whatever a caller that bypasses the proxy chooses to send. **Set
`AUTH_URL`** — it is the only step that is configuration rather than inference.

Where it comes from, per install shape:

| Install | Source |
|---|---|
| systemd | `AUTH_URL=` in **either** `/etc/robothor/robothor.env` (see `infra/robothor.env.example`) or the SOPS-encrypted secrets decrypted to `/run/robothor/secrets.env`. If you put it in the latter, the app only sees it because `robothor-app.service` is ordered after `robothor-secrets.service` — see `infra/systemd/README.md`. |
| Helm | `dashboard.env.AUTH_URL` in values — set it to `https://<dashboard.ingress.host>` |

Existing accounts (including the bootstrapped owner) bind to an IdP identity
only through an operator-armed one-shot grant: `robothor auth grant-binding
--email <email> [--issuer <idp-url>]`, then sign in once. Grants require an
active, unbound account; re-arming replaces any pending grant; an `--issuer`
pin confines the bind to that IdP. Local dev without either provider uses
`GENUS_INSECURE_DEV_MODE=true` (non-production only).

## Architecture

```
Chat Input → Engine (Kimi K2.5) → SSE stream
                                    ├─ delta events → chat text
                                    ├─ dashboard events → agent data passthrough
                                    └─ render events → native components

Dashboard Pipeline:
  Chat messages + agent data → Triage (Gemini Flash, ~1s)
                             → Fetch unsatisfied data needs (SearXNG, Bridge, Orchestrator)
                             → Merge with agent-provided data
                             → Generate HTML dashboard (Gemini Flash)
                             → Validate → Render in iframe
```

### Agent Data Passthrough

When the engine agent (Kimi K2.5) has data from tool calls (web search, memory lookup, etc.), it includes it in dashboard markers:

```
[DASHBOARD:{"intent":"weather","data":{"web":{"results":[...]}}}]
```

The dashboard pipeline skips re-fetching data the agent already provided. This avoids redundant SearXNG/API calls and uses the richer agent tool results directly.

## Key Directories

```
src/
├── app/api/
│   ├── chat/send/         # POST → SSE (engine bridge + marker interception)
│   ├── chat/history/      # GET → message history from engine
│   ├── dashboard/generate/ # Triage → fetch → generate HTML
│   └── dashboard/welcome/ # Welcome dashboard on page load
├── components/
│   ├── canvas/            # LiveCanvas, SrcdocRenderer
│   └── chat-panel.tsx     # Chat UI with SSE streaming
├── hooks/
│   ├── use-visual-state.ts  # Canvas state management
│   └── use-dashboard-agent.ts # Background dashboard update agent
└── lib/
    ├── engine/            # Engine client, types, marker interceptor
    └── dashboard/         # System prompt, triage, code validator, data fetching
```

## Development

```bash
pnpm install
pnpm dev          # http://localhost:3004
pnpm build        # production build
npx vitest run    # unit tests (187 tests)
npx playwright test  # E2E tests
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+Shift+D` | Toggle deep reasoning mode (RLM) |
| `Ctrl+Shift+P` | Toggle plan mode |
| `Enter` | Send message |

## Tests

- **187 unit tests** across 19 files (vitest + happy-dom + @testing-library/react)
- **2 E2E test files** (Playwright, chromium, 1440x900)

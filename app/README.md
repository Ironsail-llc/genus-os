# The Helm — app.robothor.ai

Robothor's command center. Live dashboard and chat interface in a two-panel Dockview layout: canvas (65%) + chat (35%).

## Stack

- **Next.js 16** + Dockview + shadcn/ui + Recharts + TanStack Table
- **Port**: 3004, service: `robothor-app.service`
- **Chat**: Custom SSE bridge to Agent Engine (same agent as Telegram)
- **Canvas**: HTML-first rendering via iframe srcdoc (Tailwind CSS), native components as fallback
- **Dashboard generation**: Gemini 2.5 Flash via OpenRouter (~2-6s)

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

## Tests

- **187 unit tests** across 19 files (vitest + happy-dom + @testing-library/react)
- **2 E2E test files** (Playwright, chromium, 1440x900)

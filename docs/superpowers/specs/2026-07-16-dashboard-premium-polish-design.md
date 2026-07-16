# Plan: Dashboard premium polish — "minimal base + glass accents"

## Context

The dashboard's features are strong but the look doesn't feel premium. A full design review found the root cause: **two disjoint design languages**. The `business/` components (home dashboard, metric cards, charts) use the shadcn/oklch token system — that's the polished bar. Every Helm operator view (fleet, runs, workflows, health, canvas) is hand-rolled from hardcoded `zinc-700/900` literals, never touches `Card`, and has no loading/empty states. Accent colors are ad-hoc (chat panel alone: 21 raw color literals; no success/warning/info tokens). Native `<select>`/`<input>` leak OS chrome. The 40px header + unlabeled icon rail read as dev tooling. The LLM-generated canvas iframe hardcodes its own stylesheet (`#18181b`, `system-ui`) so the home screen doesn't match its own chrome, and re-fetches + fully rebuilds on every visit.

**Design direction (user delegated the call):** Linear/Vercel-style minimal foundation with the existing glass/ambient treatment reserved for hero moments. Competitor research (Linear, Vercel/Geist, Datadog, Grafana, Railway, Resend, Clerk, Supabase, LangSmith/Langfuse, OpenAI/Anthropic consoles) confirms this direction: premium dark UIs win through restraint — one gray ramp, one accent, hairline borders instead of shadows, mono type for machine values, threshold-driven color. Notably, our agent-ops competitors (LangSmith/Langfuse) are functionally strong but visually mediocre — the aesthetic gap is beatable.

**Skills during implementation:** `dataviz` (load before chart/stat-tile/status-color work), `superpowers:test-driven-development` for logic changes, `verify` before commits.

Stack: Next.js 16 App Router, React 19, Tailwind v4, shadcn/ui (new-york), oklch tokens in `app/src/app/globals.css`, lucide, recharts. Dark-only (keep). All views stay mounted in `layout/app-shell.tsx` (toggle via display) — restyling is cosmetic, no data-flow changes. Reference components already embodying the target: `business/default-dashboard.tsx`, `business/metric-card.tsx`, `business/service-health.tsx`, recharts wrappers.

## Phase B0 — Visual mockups (sign-off gate)

Before restyling anything: static HTML mockup page (published as a private Artifact) showing one Helm view (Fleet) and the app chrome (header + grouped sidebar) in the proposed language, side-by-side with current screenshots. User approves the look on screen; iterate until it lands. Cheap insurance against restyling ~15 files toward the wrong target.

## Phase B1 — Token layer (research rec #1: "build it first, ship it everywhere")

`src/app/globals.css` — one day of work that delivers ~70% of the feel; everything else inherits it:
- **Gray ramp**: 4 surface tokens as even oklch lightness steps (~+4–5% L each): canvas (keep 0.12) → panel/card → raised → overlay. 4 text steps: primary ≈0.93 (off-white, not #fff), secondary, tertiary/muted ≥0.65 on panel (verify 4.5:1 — gray-on-gray is the #1 dark-dashboard failure), disabled.
- **Semantic status tokens**: `--success`, `--warning`, `--info` (+ foregrounds) beside `--destructive` — 5 fixed status colors, desaturated ~20–30% for dark (saturated colors bloom on dark). Map in `@theme inline`. Validate values with the `dataviz` color method.
- **One accent** (keep the indigo `oklch(0.65 0.2 265)`) reserved for brand/focus/primary CTA; all other chroma is status.
- **Borders over shadows**: hairline token `white/8–12%` (exists as `--border`); remove box-shadows except overlays/popovers. Elevation = lightness step + hairline.
- **Two radii** (6px / 10px); **two motion tokens** (150ms ease-out micro, 250ms ease-out panels; respect `prefers-reduced-motion`).
- Delete dead `.copilotKitChat` block (lines 95–101).
- New shared components: `business/page-header.tsx` (kills the per-file `<h2>` zoo), `business/empty-state.tsx` (one line + one CTA, Clerk-style), `business/status-badge.tsx` (dot + label, never color alone; blue pulse = running). Standardize loading on `ui/skeleton.tsx` — skeletons for containers shaped like real content, spinners only for actions. Model on the one good example: `views/controls-view.tsx:171-179`.

## Phase B2 — Helm views refactor (biggest visual win)

Pattern per file — `views/fleet-view.tsx`, `runs-view.tsx`, `workflows-view.tsx`, `health-view.tsx`, `canvas-view.tsx`, `controls-view.tsx`:
- Replace all `zinc-*`/`emerald-*`/`amber-*` literals with tokens; wrap panels in `ui/card.tsx`; titles via PageHeader; statuses via StatusBadge.
- Add Skeleton + EmptyState to fleet/runs/workflows (currently blank while fetching, bare when empty).
- **Mono font for machine values** (Geist Mono is already loaded): run IDs, model names, costs, durations, timestamps — the Vercel/Resend "developer-grade" signature.
- Replace native `<select>`/`<input>` (`controls-view.tsx:218,230`, `marketplace-view.tsx`, `business/tenant-selector.tsx`, `business/task-board.tsx`) with shadcn Input/Select (`pnpm dlx shadcn add select` if missing).
- Replace hand-built canvas confirm bar (`canvas-view.tsx:107-124`) with `ui/dialog.tsx`; `&larr;` entity (`canvas/live-canvas.tsx:133`) → lucide `ArrowLeft`.
- Runs/fleet tables: sticky headers now; virtualize (TanStack Virtual) only if row counts exceed ~100.

## Phase B3 — Chrome polish + premium UX features

- `layout/app-shell.tsx`: rework header — product mark left, view title with real weight, status cluster right (StatusBadge with label + clock); modest height increase per mockup.
- `layout/sidebar.tsx`: group ten icons into labeled sections (Workspace / Operator / AI) — uppercase 11px tracking-wide muted labels, separators; expand-on-hover labels if mockup validates.
- **Cmd-K command palette** (shadcn `Command`/cmdk): navigation + operator verbs ("open latest failed run", "pause agent…", tab switching). Research's highest-leverage UX add for a single-operator power tool.
- `src/app/signin/page.tsx`: brand it — logo, glass-panel Card (hero treatment).
- `business/default-dashboard.tsx`: evolve toward a bento grid — big health tile top-left (eye lands there first), cost/run-rate sparklines, small counters; color only when a threshold is crossed (a calm all-gray board IS the "everything's fine" signal).
- `business/welcome-skeleton.tsx`: rebuild on `ui/skeleton.tsx` + tokens mirroring the real layout (today: 16 hardcoded `bg-zinc-800` refs previewing a layout that never renders).
- Accent-literal sweep of `chat-panel.tsx` (21), `business/task-board.tsx` (16), `business/agent-status.tsx` (15) onto semantic tokens — mechanical, do last.

## Phase B4 — Canvas/HTML integration + perf quick wins

- `canvas/srcdoc-renderer.tsx:107-118`: generate the iframe stylesheet from app tokens (surfaces, text steps, border, radius) instead of hardcoded `#18181b/system-ui`, and inject the tokens as CSS variables so LLM-generated HTML can reference them instead of fighting the shell (research rec #3: treat the canvas as a framed, one-step-recessed stage with a hairline frame). Font parity needs a CSP tweak (`font-src data:` at line 112) + Geist as data-URI woff2; fall back to color-only parity if size is prohibitive.
- Update the dashboard-generation prompt (server side of `/api/dashboard/generate`) to reference the injected CSS variables — keeps generated content on-system.
- `views/canvas-view.tsx:15-34`: cache `/api/dashboard/welcome` (module-level or SWR) — currently re-fetched every tab visit.
- Keep the LLM-regeneration heuristic (`chat-panel.tsx:634-638`); 5–11s generation is model latency, out of scope. Full-srcDoc rebuild stays — incremental patching is a stretch goal, not this effort.

## Deferred (flagged, not in this effort)

- **Trace-tree runs inspector** (Langfuse-style collapsible step tree + right detail pane with per-step cost/latency badges) — research says it's the beatable competitive gap, but it's a feature build (L), not a restyle. Own spec/PR later.
- Density toggle for tables (M) — revisit after B2 lands.

## Testing / verification

- TDD for logic branches (loading/empty states, Select, cmd-K actions): vitest — `cd app && pnpm test`. Pure class-swap styling exempt (CLAUDE.md rule 8).
- Per phase: `pnpm test`, run dev server, visual pass over every view (dashboard, tasks, agents, marketplace, controls, fleet, runs, workflows, health, canvas, sign-in) at desktop + mobile widths; before/after screenshots.
- Cross-tab consistency: identical panel shade, radius, heading scale everywhere; focus ring visible on every interactive element.

## Sequencing & delivery

B0 mockup sign-off → B1 → B2 → B3 → B4. Each phase a branch + PR, squash-merged with Conventional Commit titles (e.g. `feat(dashboard): unify Helm views on design tokens`). Design doc to `docs/superpowers/specs/2026-07-16-dashboard-premium-polish-design.md` after plan approval (brainstorming skill), full research report preserved alongside it.

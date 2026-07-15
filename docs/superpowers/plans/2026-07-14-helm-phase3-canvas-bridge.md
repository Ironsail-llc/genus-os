# The Helm — Phase 3 (Canvas Bridge) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the LLM-composed canvas render *live* system state and *propose* operator actions — reading only through a parent-mediated whitelist that maps to the Phase 1/2 GET endpoints, and acting only via a confirm button drawn in **parent chrome** that the sandboxed iframe can never draw, style, or fake.

**Architecture:** A parent-side mediator (`app/src/lib/canvas-bridge.ts`) owns a declared read-op whitelist and a declared propose-action whitelist. A React hook (`use-canvas-bridge.ts`) listens for `postMessage` from the sandboxed iframe (validated exactly as the existing renderer does: `event.origin === "null"` **and** `event.source === iframe.contentWindow`), resolves a read op against the whitelist, calls the bridge through the existing server-side `/api/bridge` proxy (which attaches the operator's session token — the iframe never sees it), and posts the data back to that specific iframe window. A `propose` message never executes; it surfaces a pending proposal that the parent renders as a confirm dialog **built from the parent's own data**. A trusted, parent-authored bootstrap shim injected into the `srcdoc` exposes `window.robothor.read()` / `window.robothor.propose()` to the LLM HTML; the LLM code itself still cannot call `fetch`, raw `postMessage`, storage, or navigation.

**Tech Stack:** Next.js 16 / React 19 + TypeScript (client), vitest + jsdom + Testing Library, the existing `SrcdocRenderer` (#213) and `/api/bridge` proxy.

## Global Constraints

- **The iframe never gains a credential, `fetch`, or same-origin.** `sandbox="allow-scripts"` only (never add `allow-same-origin`); the inner document CSP keeps `connect-src 'none'`; the operator bearer token stays server-side in the `/api/bridge` proxy and is NEVER placed in a `postMessage` payload sent into the iframe. Pinned by test.
- **Reads go only through the declared whitelist.** Every inbound read is resolved by `resolveReadOp(op, args)`; an op not in the whitelist, or args that fail validation, is dropped, logged, and surfaced in parent chrome — never fetched. No raw URL ever crosses the channel; the iframe names an *op*, not a path. Pinned by test.
- **The single write path stays singular.** The ONLY proposable action is `set_flag` → `PATCH /api/controls/{name}` (the Phase 1 operator-audited write). No new mutation endpoint. The agent-trigger mentioned in the spec is explicitly OUT OF SCOPE here (no such operator-scoped endpoint exists yet) — noted, not silently dropped.
- **A propose never acts.** A `{kind:"propose"}` message only creates a pending proposal in parent state; the write executes solely when the operator clicks Confirm in parent chrome. Pinned by test.
- **The confirm renders parent data, not the iframe's claim.** The confirm dialog text is built by the parent from the resolved action (`name`, `value` validated against `store.valid_values_for`) — never from any label/string the iframe supplied. A hostile `propose` labelled "Refresh" that actually sets `injection_scan=off` must show the real `set_flag injection_scan → off` action. Pinned by test.
- **Message validation is exactly the existing pattern.** Reuse `event.origin === "null"` **and** `event.source === iframeRef.current?.contentWindow`. Ignore the reserved message types `srcdoc-height` and `robothor:error` (owned by the renderer). Phase 3 messages are tagged `{ __robothor: true, kind: "read" | "propose", ... }`.
- **The LLM-HTML validator is only narrowly relaxed.** `code-validator.ts` continues to block `fetch(`, raw `postMessage(`, `XMLHttpRequest`, storage, `window.location`, timers, `eval`, `<script>`, `<iframe>`, event-handler attributes. It is relaxed ONLY to permit references to the trusted shim API `window.robothor.read(` and `window.robothor.propose(` (and bare `robothor.read(`/`robothor.propose(`). Every other `robothor.*` (e.g. `robothor.action`) stays blocked. Pinned by test.
- **jsdom, `npx pnpm@10`.** vitest env is jsdom (#213). Always use `npx pnpm@10`, never bare `pnpm`.

---

## File Structure

**New:**
- `app/src/lib/canvas-bridge.ts` — pure logic: `READ_OPS`, `resolveReadOp`, `PROPOSE_ACTIONS`, `resolveProposeAction`, message type guards. No React, no network. The security core.
- `app/src/lib/__tests__/canvas-bridge.test.ts` — unit tests for the whitelists/resolvers.
- `app/src/lib/canvas-shim.ts` — exports `CANVAS_SHIM_SOURCE` (a string): the trusted bootstrap script injected into the `srcdoc`, implementing `window.robothor.read/propose` via postMessage round-trips.
- `app/src/components/canvas/use-canvas-bridge.ts` — the `useCanvasBridge(iframeRef)` hook: message listener, validation, read fulfilment via `/api/bridge`, pending-proposal state.
- `app/src/components/canvas/__tests__/use-canvas-bridge.test.tsx` — hook behavior + the security invariant tests.
- `app/src/components/views/canvas-view.tsx` — the Canvas tab: renders the model-composed HTML through `SrcdocRenderer` (shim injected), wires `useCanvasBridge`, renders the parent-chrome confirm dialog + dropped-op notices.
- `app/src/components/views/__tests__/canvas-view.test.tsx` — tab render + confirm-dialog-shows-parent-data test.

**Modified:**
- `app/src/components/canvas/srcdoc-renderer.tsx` — accept an optional `bootstrap?: string` prop (the trusted shim), injected as a `<script>` BEFORE the sanitized LLM HTML; accept an optional `onMessage?` passthrough so the parent hook can observe messages. No change to the `sandbox` attribute or inner CSP.
- `app/src/lib/dashboard/code-validator.ts` — narrow the `robothor.` block to allow `robothor.read(`/`robothor.propose(` only; keep all other blocks.
- `app/src/components/layout/sidebar.tsx` — `ViewId += "canvas"` + navItem.
- `app/src/components/layout/app-shell.tsx` — import `CanvasView`, `viewTitles.canvas`, render with `visible=`.
- `app/src/components/layout/mobile-tab-bar.tsx` — comment (desktop-only, matching Phase 2 tabs).

**Design decisions locked here:**
1. **Trusted shim, not raw postMessage from LLM code.** The LLM calls `window.robothor.read("get_fleet")`; the shim (parent-authored, injected) does the postMessage. The validator keeps blocking raw postMessage/fetch in LLM code. This keeps the iframe→parent protocol under our control, not the model's.
2. **Propose = `set_flag` only.** Keeps the single-write-path invariant exact; agent-trigger is a future addition.
3. **`targetOrigin` for parent→iframe is `"*"`** (a sandboxed opaque-origin iframe has no nameable origin), but the message is posted to the specific `iframe.contentWindow` reference only, and never contains a credential — so `"*"` leaks nothing beyond the operator's own data to the operator's own iframe.

---

### Task 1: The read + propose whitelists (pure core)

**Files:**
- Create: `app/src/lib/canvas-bridge.ts`, `app/src/lib/__tests__/canvas-bridge.test.ts`

**Interfaces:**
- Produces:
  - `type ReadOp` (string union of op names).
  - `resolveReadOp(op: string, args?: Record<string, unknown>): { path: string } | null` — returns the bridge path (relative to `/api/bridge`) or `null` if op unknown or args invalid.
  - `resolveProposeAction(action: string, args: Record<string, unknown>): { method: "PATCH"; path: string; body: unknown; describe: string } | null` — `describe` is the parent-built human text.
  - `isCanvasMessage(data: unknown): data is CanvasMessage` type guard.

- [ ] **Step 1: Write the failing test**

```ts
// app/src/lib/__tests__/canvas-bridge.test.ts
import { describe, it, expect } from "vitest";
import { resolveReadOp, resolveProposeAction, isCanvasMessage } from "../canvas-bridge";

describe("resolveReadOp", () => {
  it("maps known ops to bridge paths", () => {
    expect(resolveReadOp("get_flags")).toEqual({ path: "/api/controls" });
    expect(resolveReadOp("get_fleet")).toEqual({ path: "/api/fleet" });
    expect(resolveReadOp("get_health")).toEqual({ path: "/api/health/system" });
  });
  it("validates and injects id args as path segments", () => {
    expect(resolveReadOp("get_agent", { id: "main" })).toEqual({ path: "/api/fleet/main" });
    expect(resolveReadOp("get_run", { id: "abc-123" })).toEqual({ path: "/api/runs/abc-123" });
    expect(resolveReadOp("get_workflow_runs", { id: "intel" })).toEqual({ path: "/api/workflows/intel/runs" });
  });
  it("drops unknown ops", () => {
    expect(resolveReadOp("delete_everything")).toBeNull();
    expect(resolveReadOp("get_secret")).toBeNull();
  });
  it("rejects id args that could escape the path (injection)", () => {
    expect(resolveReadOp("get_agent", { id: "../controls" })).toBeNull();
    expect(resolveReadOp("get_agent", { id: "a/b" })).toBeNull();
    expect(resolveReadOp("get_agent", { id: "" })).toBeNull();
    expect(resolveReadOp("get_agent", {})).toBeNull();           // id required
    expect(resolveReadOp("get_agent", { id: 123 as unknown as string })).toBeNull();
  });
});

describe("resolveProposeAction", () => {
  it("resolves the only proposable action, set_flag, from PARENT-derived data", () => {
    const r = resolveProposeAction("set_flag", { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" });
    expect(r).not.toBeNull();
    expect(r!.method).toBe("PATCH");
    expect(r!.path).toBe("/api/controls/ROBOTHOR_INJECTION_SCAN_MODE");
    expect(r!.body).toMatchObject({ value: "off" });
    // describe is built here, from the action — never from an iframe label
    expect(r!.describe).toContain("ROBOTHOR_INJECTION_SCAN_MODE");
    expect(r!.describe).toContain("off");
  });
  it("drops any non-whitelisted action", () => {
    expect(resolveProposeAction("delete_flag", { name: "x" })).toBeNull();
    expect(resolveProposeAction("exec", { cmd: "rm -rf /" })).toBeNull();
    expect(resolveProposeAction("trigger_agent", { id: "main" })).toBeNull();  // out of scope in P3
  });
  it("rejects a set_flag with a malformed flag name (path injection / unknown flag shape)", () => {
    expect(resolveProposeAction("set_flag", { name: "../../etc", value: "off" })).toBeNull();
    expect(resolveProposeAction("set_flag", { name: "not a flag", value: "off" })).toBeNull();
    expect(resolveProposeAction("set_flag", { name: "ROBOTHOR_X", value: 5 as unknown as string })).toBeNull();
  });
});

describe("isCanvasMessage", () => {
  it("accepts tagged read/propose messages and rejects everything else", () => {
    expect(isCanvasMessage({ __robothor: true, kind: "read", reqId: "1", op: "get_fleet" })).toBe(true);
    expect(isCanvasMessage({ __robothor: true, kind: "propose", action: "set_flag", args: {}, reqId: "2" })).toBe(true);
    expect(isCanvasMessage({ type: "srcdoc-height", height: 400 })).toBe(false);  // reserved renderer msg
    expect(isCanvasMessage({ type: "robothor:error" })).toBe(false);
    expect(isCanvasMessage({ kind: "read" })).toBe(false);                        // missing __robothor tag
    expect(isCanvasMessage("nope")).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/lib/__tests__/canvas-bridge.test.ts`
Expected: FAIL (cannot resolve `../canvas-bridge`)

- [ ] **Step 3: Implement**

```ts
// app/src/lib/canvas-bridge.ts
// The security core of the canvas bridge. Pure logic: no React, no network.
// Every read the sandboxed canvas can make and every action it can propose is
// declared here; anything not declared is dropped. The iframe names an OP, never
// a URL — this module is the only place op→path resolution happens.

// A flag id is UPPER_SNAKE with a ROBOTHOR_ prefix (matches the governed flags);
// an entity id (agent/run/workflow) is a conservative slug — no slashes, no dots,
// no traversal. These guards keep an iframe-supplied value from escaping its path.
const FLAG_NAME = /^ROBOTHOR_[A-Z0-9_]+$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

function safeId(v: unknown): string | null {
  return typeof v === "string" && SAFE_ID.test(v) ? v : null;
}

type ReadSpec = { path: (args: Record<string, unknown>) => string | null };

const READ_OPS: Record<string, ReadSpec> = {
  get_flags: { path: () => "/api/controls" },
  get_fleet: { path: () => "/api/fleet" },
  get_agent: { path: (a) => { const id = safeId(a.id); return id && `/api/fleet/${id}`; } },
  get_runs: { path: () => "/api/runs" },
  get_run: { path: (a) => { const id = safeId(a.id); return id && `/api/runs/${id}`; } },
  get_workflows: { path: () => "/api/workflows" },
  get_workflow_runs: { path: (a) => { const id = safeId(a.id); return id && `/api/workflows/${id}/runs`; } },
  get_health: { path: () => "/api/health/system" },
};

export type ReadOp = keyof typeof READ_OPS;

export function resolveReadOp(op: string, args: Record<string, unknown> = {}): { path: string } | null {
  const spec = READ_OPS[op];
  if (!spec) return null;
  const path = spec.path(args);
  return path ? { path } : null;
}

// The ONLY proposable action in Phase 3: set_flag → the Phase-1 operator flag PATCH.
// `describe` is built HERE from the resolved action, so the confirm dialog can never
// be worded by the iframe. Value legality (against the flag's value set) is enforced
// server-side by the controls PATCH (422); here we only shape/guard the request.
export function resolveProposeAction(
  action: string,
  args: Record<string, unknown>,
): { method: "PATCH"; path: string; body: { value: string; reason: string }; describe: string } | null {
  if (action !== "set_flag") return null;
  const name = typeof args.name === "string" && FLAG_NAME.test(args.name) ? args.name : null;
  const value = typeof args.value === "string" && args.value.length > 0 && args.value.length < 32 ? args.value : null;
  if (!name || !value) return null;
  const reason = typeof args.reason === "string" ? args.reason.slice(0, 500) : "proposed from canvas";
  return {
    method: "PATCH",
    path: `/api/controls/${name}`,
    body: { value, reason },
    describe: `Set ${name} → ${value}`,
  };
}

export type CanvasMessage =
  | { __robothor: true; kind: "read"; reqId: string; op: string; args?: Record<string, unknown> }
  | { __robothor: true; kind: "propose"; reqId: string; action: string; args: Record<string, unknown>; label?: string };

export function isCanvasMessage(data: unknown): data is CanvasMessage {
  if (typeof data !== "object" || data === null) return false;
  const d = data as Record<string, unknown>;
  if (d.__robothor !== true || typeof d.reqId !== "string") return false;
  if (d.kind === "read") return typeof d.op === "string";
  if (d.kind === "propose") return typeof d.action === "string" && typeof d.args === "object" && d.args !== null;
  return false;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/lib/__tests__/canvas-bridge.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/lib/canvas-bridge.ts app/src/lib/__tests__/canvas-bridge.test.ts
git commit -m "feat(canvas): read-op + propose-action whitelists (the canvas bridge core)"
```

---

### Task 2: The trusted in-iframe shim

**Files:**
- Create: `app/src/lib/canvas-shim.ts`, `app/src/lib/__tests__/canvas-shim.test.ts`

**Interfaces:**
- Produces: `export const CANVAS_SHIM_SOURCE: string` — a self-contained IIFE (no imports) that, when run inside the sandboxed iframe, defines `window.robothor = { read(op, args?) => Promise, propose(action, args, label?) => void }`. `read` posts `{__robothor:true, kind:"read", reqId, op, args}` to `parent` and resolves when a matching `{__robothor:true, kind:"read-result", reqId}` arrives. `propose` posts `{__robothor:true, kind:"propose", reqId, action, args, label}` (fire-and-forget).

**Interfaces (consumes):** nothing — it is a source string. It must NOT reference the operator token or any network API; only `parent.postMessage` and a `message` listener.

- [ ] **Step 1: Write the failing test** (execute the shim source in jsdom, assert the API shape + that a read posts the right message and resolves on the matching reply)

```ts
// app/src/lib/__tests__/canvas-shim.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { CANVAS_SHIM_SOURCE } from "../canvas-shim";

describe("CANVAS_SHIM_SOURCE", () => {
  beforeEach(() => {
    // fresh window.robothor per test
    // @ts-expect-error test cleanup
    delete (window as unknown as { robothor?: unknown }).robothor;
  });

  it("defines window.robothor.read/propose when evaluated", () => {
    const parentPost = vi.fn();
    // simulate the iframe: parent is the mock, self is window
    const fn = new Function("parent", "self", CANVAS_SHIM_SOURCE);
    fn({ postMessage: parentPost }, window);
    const robothor = (window as unknown as { robothor: { read: unknown; propose: unknown } }).robothor;
    expect(typeof robothor.read).toBe("function");
    expect(typeof robothor.propose).toBe("function");
  });

  it("read posts a tagged read message and resolves when the matching result arrives", async () => {
    const posted: unknown[] = [];
    const parent = { postMessage: (m: unknown) => posted.push(m) };
    const fn = new Function("parent", "self", CANVAS_SHIM_SOURCE);
    fn(parent, window);
    const robothor = (window as unknown as { robothor: { read: (op: string, a?: unknown) => Promise<unknown> } }).robothor;

    const p = robothor.read("get_fleet");
    const sent = posted[0] as { __robothor: boolean; kind: string; reqId: string; op: string };
    expect(sent).toMatchObject({ __robothor: true, kind: "read", op: "get_fleet" });
    expect(typeof sent.reqId).toBe("string");

    // deliver the matching result
    window.dispatchEvent(new MessageEvent("message", {
      data: { __robothor: true, kind: "read-result", reqId: sent.reqId, ok: true, data: [{ agent_id: "main" }] },
    }));
    await expect(p).resolves.toEqual([{ agent_id: "main" }]);
  });

  it("propose posts a tagged propose message (fire-and-forget)", () => {
    const posted: unknown[] = [];
    const parent = { postMessage: (m: unknown) => posted.push(m) };
    const fn = new Function("parent", "self", CANVAS_SHIM_SOURCE);
    fn(parent, window);
    const robothor = (window as unknown as { robothor: { propose: (a: string, args: unknown, l?: string) => void } }).robothor;
    robothor.propose("set_flag", { name: "ROBOTHOR_RBAC_MODE", value: "enforce" }, "Enforce RBAC");
    expect(posted[0]).toMatchObject({ __robothor: true, kind: "propose", action: "set_flag" });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/lib/__tests__/canvas-shim.test.ts`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement** — the shim references `parent`/`self` which, inside a real iframe, are the frame globals; the test injects them via `new Function`.

```ts
// app/src/lib/canvas-shim.ts
// Trusted bootstrap injected into the canvas srcdoc BEFORE the model's HTML. This
// is OUR code, never the model's. It gives the model a narrow, mediated API —
// window.robothor.read()/propose() — implemented purely over postMessage to the
// parent. It holds no credentials and does no network I/O; the parent mediator is
// the only thing that can reach the bridge, and only for whitelisted ops.
export const CANVAS_SHIM_SOURCE = `
(function () {
  var pending = {};
  var seq = 0;
  function rid() { seq += 1; return "c" + seq + "_" + String(Math.random()).slice(2); }
  self.addEventListener("message", function (e) {
    var d = e && e.data;
    if (!d || d.__robothor !== true || d.kind !== "read-result") return;
    var cb = pending[d.reqId];
    if (!cb) return;
    delete pending[d.reqId];
    if (d.ok) cb.resolve(d.data); else cb.reject(new Error(d.error || "read failed"));
  });
  self.robothor = {
    read: function (op, args) {
      var reqId = rid();
      return new Promise(function (resolve, reject) {
        pending[reqId] = { resolve: resolve, reject: reject };
        parent.postMessage({ __robothor: true, kind: "read", reqId: reqId, op: op, args: args || {} }, "*");
        setTimeout(function () {
          if (pending[reqId]) { delete pending[reqId]; reject(new Error("read timed out")); }
        }, 10000);
      });
    },
    propose: function (action, args, label) {
      parent.postMessage({ __robothor: true, kind: "propose", reqId: rid(), action: action, args: args || {}, label: label || "" }, "*");
    }
  };
})();
`;
```

Note: the shim uses `Math.random`/`setTimeout` — these run INSIDE the iframe (allowed there); they are NOT in the validated LLM code. The validator's timer/random blocks apply only to model-authored HTML, not to this injected trusted script.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/lib/__tests__/canvas-shim.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/src/lib/canvas-shim.ts app/src/lib/__tests__/canvas-shim.test.ts
git commit -m "feat(canvas): trusted in-iframe shim exposing window.robothor.read/propose over postMessage"
```

---

### Task 3: The parent-side mediator hook

**Files:**
- Create: `app/src/components/canvas/use-canvas-bridge.ts`, `app/src/components/canvas/__tests__/use-canvas-bridge.test.tsx`

**Interfaces:**
- Consumes: `resolveReadOp`, `resolveProposeAction`, `isCanvasMessage` (Task 1). The `/api/bridge/[...path]` proxy for reads.
- Produces: `useCanvasBridge(iframeRef: RefObject<HTMLIFrameElement | null>): { pendingProposal: PendingProposal | null; confirmProposal: () => Promise<void>; cancelProposal: () => void; dropped: DroppedOp[] }`.
  - `PendingProposal = { describe: string; method: "PATCH"; path: string; body: unknown }` — built from `resolveProposeAction`, NEVER from the iframe's `label`.
  - It installs a `window` message listener that validates `event.origin === "null" && event.source === iframeRef.current?.contentWindow`, ignores non-`__robothor` messages and the reserved renderer types, fulfils reads by fetching `\`/api/bridge${path}\`` and posting `{__robothor:true, kind:"read-result", reqId, ok, data|error}` back to `iframeRef.current.contentWindow` with `targetOrigin "*"`, and stores a `propose` as `pendingProposal` (does not execute).

- [ ] **Step 1: Write the failing test** (drive the hook with synthetic MessageEvents; mock `fetch`)

```tsx
// app/src/components/canvas/__tests__/use-canvas-bridge.test.tsx
import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { useCanvasBridge } from "../use-canvas-bridge";

afterEach(() => vi.restoreAllMocks());

// A stand-in for the iframe's contentWindow: it just records messages posted to it.
function makeIframeRef() {
  const posted: unknown[] = [];
  const contentWindow = { postMessage: (m: unknown) => posted.push(m) } as unknown as Window;
  const iframe = { contentWindow } as unknown as HTMLIFrameElement;
  return { ref: { current: iframe }, posted, contentWindow };
}

function fireMessage(source: unknown, data: unknown, origin = "null") {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source: source as Window }));
}

describe("useCanvasBridge", () => {
  it("fulfils a whitelisted read from the correct iframe and posts the data back — no token in the reply", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true, json: async () => [{ agent_id: "main" }],
    } as Response);
    const { ref, posted, contentWindow } = makeIframeRef();
    renderHook(() => useCanvasBridge(ref));

    act(() => fireMessage(contentWindow, { __robothor: true, kind: "read", reqId: "r1", op: "get_fleet" }));

    await waitFor(() => expect(posted.length).toBe(1));
    expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/fleet", expect.anything());
    const reply = posted[0] as { kind: string; reqId: string; ok: boolean; data: unknown };
    expect(reply).toMatchObject({ __robothor: true, kind: "read-result", reqId: "r1", ok: true });
    expect(JSON.stringify(reply)).not.toMatch(/bearer|authorization|token/i);  // no credential ever
  });

  it("drops an unknown op — no fetch, surfaced in `dropped`", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, { __robothor: true, kind: "read", reqId: "r2", op: "get_secret" }));
    await waitFor(() => expect(result.current.dropped.length).toBe(1));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("ignores a message whose origin is not 'null' or whose source is not the iframe", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, { __robothor: true, kind: "read", reqId: "r3", op: "get_fleet" }, "https://evil.example"));
    act(() => fireMessage({ postMessage() {} }, { __robothor: true, kind: "read", reqId: "r4", op: "get_fleet" }, "null"));
    await new Promise((r) => setTimeout(r, 10));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a propose becomes a pending proposal built from PARENT data, and does NOT execute until confirmed", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));

    // hostile: label says "Refresh" but the action turns injection scanning off
    act(() => fireMessage(contentWindow, {
      __robothor: true, kind: "propose", reqId: "p1",
      action: "set_flag", args: { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" }, label: "Refresh",
    }));
    await waitFor(() => expect(result.current.pendingProposal).not.toBeNull());
    // the confirm text is the REAL action, not the "Refresh" label
    expect(result.current.pendingProposal!.describe).toBe("Set ROBOTHOR_INJECTION_SCAN_MODE → off");
    expect(result.current.pendingProposal!.describe).not.toMatch(/refresh/i);
    expect(fetchMock).not.toHaveBeenCalled();  // nothing executed yet

    // confirm → the write fires exactly once, PATCH to the resolved path
    await act(async () => { await result.current.confirmProposal(); });
    expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/controls/ROBOTHOR_INJECTION_SCAN_MODE",
      expect.objectContaining({ method: "PATCH" }));
  });

  it("cancel clears the pending proposal without executing", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    const { ref, contentWindow } = makeIframeRef();
    const { result } = renderHook(() => useCanvasBridge(ref));
    act(() => fireMessage(contentWindow, {
      __robothor: true, kind: "propose", reqId: "p2", action: "set_flag",
      args: { name: "ROBOTHOR_RBAC_MODE", value: "off" }, label: "x",
    }));
    await waitFor(() => expect(result.current.pendingProposal).not.toBeNull());
    act(() => result.current.cancelProposal());
    expect(result.current.pendingProposal).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/canvas/__tests__/use-canvas-bridge.test.tsx`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```ts
// app/src/components/canvas/use-canvas-bridge.ts
"use client";

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { resolveReadOp, resolveProposeAction, isCanvasMessage } from "@/lib/canvas-bridge";

const BRIDGE_URL = "/api/bridge";

export type PendingProposal = { describe: string; method: "PATCH"; path: string; body: unknown };
export type DroppedOp = { reqId: string; op: string; at: number };

export function useCanvasBridge(iframeRef: RefObject<HTMLIFrameElement | null>) {
  const [pendingProposal, setPendingProposal] = useState<PendingProposal | null>(null);
  const [dropped, setDropped] = useState<DroppedOp[]>([]);
  // keep the latest iframe window in a ref so the listener (installed once) always
  // validates against the current frame.
  const frameRef = useRef(iframeRef);
  frameRef.current = iframeRef;

  const postResult = useCallback((reqId: string, ok: boolean, payload: unknown) => {
    const win = frameRef.current.current?.contentWindow;
    if (!win) return;
    win.postMessage(
      ok ? { __robothor: true, kind: "read-result", reqId, ok: true, data: payload }
         : { __robothor: true, kind: "read-result", reqId, ok: false, error: String(payload) },
      "*",  // sandboxed opaque-origin iframe; posted only to this specific window; carries no credential
    );
  }, []);

  useEffect(() => {
    const onMessage = async (event: MessageEvent) => {
      // Exactly the renderer's validation: opaque origin AND our iframe as the source.
      const win = frameRef.current.current?.contentWindow;
      if (event.origin !== "null" || !win || event.source !== win) return;
      if (!isCanvasMessage(event.data)) return;  // ignores srcdoc-height / robothor:error / anything untagged
      const msg = event.data;

      if (msg.kind === "read") {
        const resolved = resolveReadOp(msg.op, msg.args ?? {});
        if (!resolved) {
          setDropped((d) => [...d, { reqId: msg.reqId, op: msg.op, at: Date.now() }]);
          postResult(msg.reqId, false, `unknown op: ${msg.op}`);
          return;
        }
        try {
          const res = await fetch(`${BRIDGE_URL}${resolved.path}`, { headers: { accept: "application/json" } });
          if (!res.ok) { postResult(msg.reqId, false, `error ${res.status}`); return; }
          postResult(msg.reqId, true, await res.json());
        } catch {
          postResult(msg.reqId, false, "bridge unreachable");
        }
        return;
      }

      // propose: never executes here — build the confirm from PARENT data only.
      const action = resolveProposeAction(msg.action, msg.args);
      if (!action) {
        setDropped((d) => [...d, { reqId: msg.reqId, op: `propose:${msg.action}`, at: Date.now() }]);
        return;
      }
      setPendingProposal({ describe: action.describe, method: action.method, path: action.path, body: action.body });
    };

    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [postResult]);

  const confirmProposal = useCallback(async () => {
    const p = pendingProposal;
    if (!p) return;
    setPendingProposal(null);
    await fetch(`${BRIDGE_URL}${p.path}`, {
      method: p.method,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(p.body),
    });
  }, [pendingProposal]);

  const cancelProposal = useCallback(() => setPendingProposal(null), []);

  return { pendingProposal, confirmProposal, cancelProposal, dropped };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/canvas/__tests__/use-canvas-bridge.test.tsx`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add app/src/components/canvas/use-canvas-bridge.ts app/src/components/canvas/__tests__/use-canvas-bridge.test.tsx
git commit -m "feat(canvas): parent mediator hook — whitelisted reads, propose-not-act, parent-built confirm"
```

---

### Task 4: Narrow the LLM-HTML validator carve-out

**Files:**
- Modify: `app/src/lib/dashboard/code-validator.ts`
- Test: extend `app/src/lib/dashboard/__tests__/code-validator.test.ts` (or create if absent)

**Interfaces:** unchanged public API of `validateDashboardCode` / `BLOCKED_PATTERNS`.

**The change:** today `BLOCKED_PATTERNS` blocks any `robothor.` and any `postMessage(`. We must (a) still block raw `postMessage(`, `fetch(`, `XMLHttpRequest`, storage, `window.location`, timers, `eval`, `robothor.action` and every other `robothor.*`, but (b) ALLOW `robothor.read(` and `robothor.propose(` (and `window.robothor.read(` / `.propose(`), so model HTML can call the trusted shim.

- [ ] **Step 1: Write the failing test**

```ts
// in app/src/lib/dashboard/__tests__/code-validator.test.ts
import { describe, it, expect } from "vitest";
import { validateDashboardCode } from "../code-validator";

describe("code-validator canvas-bridge carve-out", () => {
  it("ALLOWS the trusted shim API in model HTML", () => {
    expect(validateDashboardCode(`<div id="x"></div><script>robothor.read("get_fleet").then(d=>{})</script>`).valid).toBe(true);
    expect(validateDashboardCode(`<div></div><script>window.robothor.propose("set_flag",{name:"ROBOTHOR_RBAC_MODE",value:"enforce"})</script>`).valid).toBe(true);
  });
  it("still BLOCKS raw network / other robothor.* / mutation primitives", () => {
    expect(validateDashboardCode(`<script>fetch("/api/controls")</script>`).valid).toBe(false);
    expect(validateDashboardCode(`<script>parent.postMessage({},"*")</script>`).valid).toBe(false);
    expect(validateDashboardCode(`<script>robothor.action("x")</script>`).valid).toBe(false);
    expect(validateDashboardCode(`<script>new XMLHttpRequest()</script>`).valid).toBe(false);
    expect(validateDashboardCode(`<script>localStorage.getItem("t")</script>`).valid).toBe(false);
    expect(validateDashboardCode(`<script>window.location="x"</script>`).valid).toBe(false);
  });
});
```

(If `validateDashboardCode`'s return shape differs — e.g. `{ valid, code, error }` — match the actual shape; read the module first. If the test file doesn't exist, create it; if it exists, append this `describe`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/lib/dashboard/__tests__/code-validator.test.ts`
Expected: FAIL on the "ALLOWS" cases (current validator blocks all `robothor.` and `postMessage`).

- [ ] **Step 3: Implement** — read `code-validator.ts` first. Replace the blanket `robothor.` blocker with a negative-lookahead that blocks `robothor.` EXCEPT `read`/`propose`. Keep the `postMessage(` and `fetch(` blocks as-is (the model still may not call them; only the injected shim does, and the shim is not run through this validator).

Concretely, change the `robothor` blocked pattern from something like `/robothor\./i` to:
```ts
// Block robothor.<anything> EXCEPT the two mediated shim calls read()/propose().
/robothor\.(?!read\s*\(|propose\s*\()/i,
```
Leave `postMessage(`, `fetch(`, `XMLHttpRequest`, storage, location, timer, eval patterns unchanged. Add a code comment explaining the shim carve-out and that raw postMessage/fetch stay blocked because only the trusted injected shim (not model code) performs them.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/lib/dashboard/__tests__/code-validator.test.ts`
Expected: PASS. Also run the FULL existing validator test file to ensure no regression of the other blocks.

- [ ] **Step 5: Commit**

```bash
git add app/src/lib/dashboard/code-validator.ts app/src/lib/dashboard/__tests__/code-validator.test.ts
git commit -m "feat(canvas): narrowly allow robothor.read/propose in model HTML, keep all raw network blocked"
```

---

### Task 5: Inject the shim via SrcdocRenderer + build the Canvas tab

**Files:**
- Modify: `app/src/components/canvas/srcdoc-renderer.tsx` (accept `bootstrap?: string`)
- Create: `app/src/components/views/canvas-view.tsx`, `app/src/components/views/__tests__/canvas-view.test.tsx`
- Modify: `sidebar.tsx`, `app-shell.tsx`, `mobile-tab-bar.tsx`

**Interfaces:**
- `SrcdocRenderer` gains an optional `bootstrap?: string` prop: when present, its content is injected as `<script>${bootstrap}</script>` INSIDE the srcdoc, BEFORE the sanitized model HTML, so `window.robothor` exists before the model script runs. The `sandbox` attr and inner CSP are unchanged (script-src 'unsafe-inline' already permits it). The bootstrap is NOT passed through DOMPurify (it is trusted, parent-authored) — it is concatenated into the srcdoc template string directly, exactly like the existing height/error script the renderer already injects.
- `CanvasView({ visible })` — root `data-testid="canvas-view"`, `style={{display: visible ? "flex":"none"}}`. It holds the iframe ref, calls `useCanvasBridge(iframeRef)`, renders `<SrcdocRenderer html={code} bootstrap={CANVAS_SHIM_SOURCE} ref={iframeRef}/>` (SrcdocRenderer must forward a ref to its iframe — add `forwardRef` if not present), and renders, in PARENT chrome (outside the iframe), the pending-proposal confirm dialog and any dropped-op notices.

- [ ] **Step 1: Write the failing test** (Canvas tab renders; the confirm dialog, when a proposal is pending, shows the parent-derived action text and Confirm/Cancel in parent chrome)

```tsx
// app/src/components/views/__tests__/canvas-view.test.tsx
import { render, screen, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { CanvasView } from "../canvas-view";

afterEach(() => vi.restoreAllMocks());

describe("CanvasView", () => {
  it("renders the canvas tab with the sandboxed renderer", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div>hi</div>" }) } as Response);
    render(<CanvasView visible />);
    expect(await screen.findByTestId("canvas-view")).toBeTruthy();
    // the sandboxed iframe is present and never same-origin
    const iframe = document.querySelector('[data-testid="srcdoc-renderer"]') as HTMLIFrameElement;
    expect(iframe).toBeTruthy();
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("sandbox") ?? "").not.toMatch(/allow-same-origin/);
  });

  it("shows a confirm dialog in parent chrome built from the real action, not the iframe label", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div></div>" }) } as Response);
    render(<CanvasView visible />);
    const iframe = (await screen.findByTestId("srcdoc-renderer")) as HTMLIFrameElement;
    // simulate a hostile propose arriving from the iframe
    act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        origin: "null", source: iframe.contentWindow,
        data: { __robothor: true, kind: "propose", reqId: "p1", action: "set_flag",
                args: { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" }, label: "Refresh" },
      }));
    });
    const dialog = await screen.findByTestId("canvas-confirm");
    expect(dialog.textContent).toContain("ROBOTHOR_INJECTION_SCAN_MODE");
    expect(dialog.textContent).toContain("off");
    expect(dialog.textContent).not.toMatch(/refresh/i);
    expect(screen.getByTestId("canvas-confirm-accept")).toBeTruthy();
    expect(screen.getByTestId("canvas-confirm-cancel")).toBeTruthy();
  });
});
```

(If `iframe.contentWindow` is null under jsdom for a `srcDoc` iframe, the test may need the renderer mounted; if `contentWindow` is unavailable, assert via the hook path instead — but jsdom does provide a `contentWindow` for a rendered iframe. Verify during Step 2.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/views/__tests__/canvas-view.test.tsx`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement** — read `srcdoc-renderer.tsx` first. Add `forwardRef` to expose the iframe element, add the `bootstrap` prop (concatenated into the srcdoc before the model HTML), then write `CanvasView`:

```tsx
// app/src/components/views/canvas-view.tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";
import { useCanvasBridge } from "@/components/canvas/use-canvas-bridge";
import { CANVAS_SHIM_SOURCE } from "@/lib/canvas-shim";

export function CanvasView({ visible = true }: { visible?: boolean }) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [code, setCode] = useState<string>("");
  const { pendingProposal, confirmProposal, cancelProposal, dropped } = useCanvasBridge(iframeRef);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/dashboard/welcome");
      if (res.ok) {
        const body = await res.json();
        setCode(typeof body?.html === "string" ? body.html : "");
      }
    } catch {
      /* leave code empty; the tab still renders */
    }
  }, []);

  useEffect(() => { if (visible) load(); }, [visible, load]);

  return (
    <div data-testid="canvas-view" className="flex-col gap-3 p-4" style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Canvas</h2>
      <SrcdocRenderer ref={iframeRef} html={code} bootstrap={CANVAS_SHIM_SOURCE} />

      {dropped.length > 0 && (
        <div data-testid="canvas-dropped" className="rounded border border-amber-500/50 bg-amber-500/5 p-2 text-xs text-amber-300">
          The canvas reached for {dropped.length} thing(s) it was not given:{" "}
          {dropped.map((d) => d.op).join(", ")}
        </div>
      )}

      {pendingProposal && (
        <div data-testid="canvas-confirm" className="fixed inset-x-0 bottom-4 mx-auto w-max rounded-lg border border-zinc-600 bg-zinc-900 p-4 shadow-xl">
          <p className="text-sm text-zinc-100">The canvas proposes: <strong>{pendingProposal.describe}</strong></p>
          <p className="mt-1 text-xs text-zinc-400">This is an operator write. Confirm to apply it.</p>
          <div className="mt-3 flex gap-2">
            <button data-testid="canvas-confirm-accept" onClick={() => confirmProposal()}
              className="rounded bg-emerald-600 px-3 py-1 text-sm text-white hover:bg-emerald-500">Confirm</button>
            <button data-testid="canvas-confirm-cancel" onClick={cancelProposal}
              className="rounded bg-zinc-700 px-3 py-1 text-sm text-zinc-100 hover:bg-zinc-600">Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default CanvasView;
```

Then register: `sidebar.tsx` (`ViewId += "canvas"`, navItem with a lucide icon e.g. `LayoutDashboard` or `Sparkles`), `app-shell.tsx` (import + `viewTitles.canvas = "Canvas"` + `<CanvasView visible={sidebarView === "canvas"} />`), `mobile-tab-bar.tsx` (comment — desktop-only).

- [ ] **Step 4: Run to verify it passes + tsc**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/views/__tests__/canvas-view.test.tsx && npx pnpm@10 exec tsc --noEmit`
Expected: PASS; tsc clean.

- [ ] **Step 5: Commit**

```bash
git add app/src/components/canvas/srcdoc-renderer.tsx app/src/components/views/canvas-view.tsx app/src/components/views/__tests__/canvas-view.test.tsx app/src/components/layout/sidebar.tsx app/src/components/layout/app-shell.tsx app/src/components/layout/mobile-tab-bar.tsx
git commit -m "feat(canvas): Canvas tab — shim-injected sandboxed renderer + parent-chrome confirm"
```

---

### Task 6: Pin the sandbox-isolation invariant by test

**Files:**
- Create: `app/src/components/canvas/__tests__/isolation.test.tsx`

**Interfaces:** none — this is a guard test, the Phase 3 analogue of Phase 1's "no control tool in schemas.py".

- [ ] **Step 1: Write the test** (it encodes the boundary; it should PASS against the code from Tasks 1-5 — if any part fails, that's a real defect to fix, not a test to weaken)

```tsx
// app/src/components/canvas/__tests__/isolation.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { SrcdocRenderer } from "../srcdoc-renderer";
import { CANVAS_SHIM_SOURCE } from "@/lib/canvas-shim";

describe("canvas isolation invariants", () => {
  it("the iframe is allow-scripts only — never allow-same-origin", () => {
    render(<SrcdocRenderer html="<div>x</div>" bootstrap={CANVAS_SHIM_SOURCE} />);
    const iframe = screen.getByTestId("srcdoc-renderer");
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("sandbox") ?? "").not.toContain("allow-same-origin");
  });

  it("the trusted shim carries no credential and does no direct network I/O", () => {
    expect(CANVAS_SHIM_SOURCE).not.toMatch(/bearer|authorization|token|cookie/i);
    expect(CANVAS_SHIM_SOURCE).not.toMatch(/\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource/);
    // the shim talks ONLY to parent via postMessage
    expect(CANVAS_SHIM_SOURCE).toMatch(/parent\.postMessage/);
  });

  it("the srcdoc keeps connect-src 'none' (no network from inside the frame)", () => {
    const { container } = render(<SrcdocRenderer html="<div>x</div>" bootstrap={CANVAS_SHIM_SOURCE} />);
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toMatch(/connect-src 'none'/);
  });
});
```

- [ ] **Step 2: Run**

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run src/components/canvas/__tests__/isolation.test.tsx`
Expected: PASS. If the `connect-src 'none'` assertion fails because the renderer builds the CSP differently, adjust the assertion to match the renderer's real CSP string (do NOT weaken the intent — it must assert the frame cannot open a network connection).

- [ ] **Step 3: Run the FULL app suite + tsc** (last task — the registration + renderer change must not break AppShell)

Run: `cd /home/philip/robothor/app && npx pnpm@10 exec vitest run && npx pnpm@10 exec tsc --noEmit`
Expected: whole app suite green; tsc clean.

- [ ] **Step 4: Commit**

```bash
git add app/src/components/canvas/__tests__/isolation.test.tsx
git commit -m "test(canvas): pin sandbox isolation — allow-scripts only, no credential, no direct network"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

1. **Whole-branch SECURITY review** (most capable model) via superpowers:requesting-code-review — this phase modifies a security control (the validator) and adds an inbound channel to operator APIs, so the review must confirm:
   - The iframe still has `allow-scripts` only, no `allow-same-origin`, and inner CSP `connect-src 'none'`.
   - No credential is ever posted into the iframe; reads flow only through the server-side `/api/bridge` proxy.
   - Every read is whitelist-resolved; no raw URL crosses the channel; unknown ops are dropped + surfaced.
   - The ONLY write is the confirmed `set_flag` PATCH; a propose never executes without the parent-chrome Confirm; the confirm text is parent-derived, immune to a hostile label.
   - The validator carve-out admits ONLY `robothor.read`/`robothor.propose`; raw `fetch`/`postMessage`/other `robothor.*`/storage/navigation stay blocked; no regression to the other blocked patterns.
   - Message validation is `origin==="null"` AND `source===contentWindow`; reserved renderer message types are untouched.
2. **Full suites:** `cd app && npx pnpm@10 exec vitest run && npx pnpm@10 exec tsc --noEmit && npx pnpm@10 lint` (0 errors — remember the `set-state-in-effect` rule; use the cancellation pattern in `CanvasView`'s effect if it fires).
3. **finishing-a-development-branch** — open the PR (`feat(helm): canvas bridge — the LLM's live, propose-only rendering surface`). Because Phase 3 changes a security control on the untrusted-LLM path, present the whole-branch security review in the PR and **hold for the operator's explicit merge decision** rather than admin-merging automatically. Deploy after merge: `npx pnpm@10 build` + restart `robothor-app`; live-smoke the Canvas tab and confirm a hostile-label propose still shows the real action.
</content>

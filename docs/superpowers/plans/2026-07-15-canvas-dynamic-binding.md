# Canvas Dynamic Binding (Option A — declarative data-attributes) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Make the Canvas tab dynamic without letting the model run JavaScript: the model composes HTML with a narrow whitelist of `data-*` attributes; a trusted, injected binder reads them, calls the Phase-3 read/propose channel, and fills content via `textContent` only.

**Architecture:** Extend the Phase-3 canvas bridge. The model's HTML gains a tiny declarative vocabulary — `data-read` (fetch an op), `data-bind` (place a value), `data-propose`/`data-name`/`data-value` (a proposable action). DOMPurify is relaxed to permit ONLY those five attribute names (via `ADD_ATTR`, keeping `ALLOW_DATA_ATTR:false`), so every other `data-*`, all scripts, and all handlers stay stripped. A trusted binder script (`canvas-binder.ts`, injected in the srcdoc beside the shim from Phase 3) scans those attributes on load and drives the existing `window.robothor.read/propose` shim, rendering results with `textContent` — never `innerHTML`. Model JS never runs.

**Tech Stack:** TypeScript, DOMPurify (isomorphic), vitest + jsdom, the Phase-3 shim/mediator/whitelists (already live).

## Global Constraints

- **Only five attribute names are permitted through the sanitizer:** `data-read`, `data-bind`, `data-propose`, `data-name`, `data-value`. Keep `ALLOW_DATA_ATTR:false`; add exactly these to `ADD_ATTR`. Any other `data-*` (e.g. `data-evil`, `data-x`) must still be stripped. Pinned by test.
- **The binder renders via `textContent` only — never `innerHTML`/`dangerouslySetInnerHTML`/`insertAdjacentHTML`.** Values from a read are strings placed as text; they cannot become markup. Pinned by test.
- **No model JavaScript runs.** `<script>` stays in `FORBID_TAGS`; `on*=` handlers stay blocked by the validator. The binder is trusted, parent-authored, injected outside DOMPurify (exactly like the shim). The model only writes declarative attributes.
- **Reads/proposes still go only through the Phase-3 whitelist + mediator.** The binder calls `window.robothor.read(op)` / `.propose(action, args, label)` — the same shim; the parent mediator still whitelist-resolves every op and still renders the propose confirm in parent chrome. No new data path, no new write path. A propose still never executes without the operator's confirm.
- **Sandbox unchanged:** `sandbox="allow-scripts"` only, inner CSP `connect-src 'none'`. The isolation guard test must still pass.
- **jsdom, `npx pnpm@10`.**

---

## File Structure

**New:** `app/src/lib/canvas-binder.ts` (`CANVAS_BINDER_SOURCE`), `app/src/lib/__tests__/canvas-binder.test.ts`.
**Modified:** `app/src/components/canvas/srcdoc-renderer.tsx` (ADD_ATTR the 5 keys), `app/src/components/views/canvas-view.tsx` (inject binder; caption now "live"), `app/src/lib/dashboard/system-prompt.ts` (teach the model the declarative vocabulary), and the renderer/isolation tests.

---

### Task 1: The trusted binder

**Files:** Create `app/src/lib/canvas-binder.ts`, `app/src/lib/__tests__/canvas-binder.test.ts`.

**Interfaces:** Produces `export const CANVAS_BINDER_SOURCE: string` — a trusted IIFE injected into the srcdoc AFTER the Phase-3 shim. On `DOMContentLoaded` (or immediately if already parsed) it:
- For each `[data-read]` element: `window.robothor.read(el.getAttribute("data-read"))` → on resolve, for the element and its descendants carrying `[data-bind]`, set `textContent` to the value resolved from the read result by the dotted path in `data-bind` (arrays resolve to their `.length`; primitives to `String(value)`; objects/undefined to `""`).
- For each `[data-propose]` element: on `click`, `window.robothor.propose(el.getAttribute("data-propose"), { name: el.getAttribute("data-name"), value: el.getAttribute("data-value") }, el.textContent)`.
- All writes to the DOM use `textContent`. Never `innerHTML`.

- [ ] **Step 1: Write the failing test**

```ts
// app/src/lib/__tests__/canvas-binder.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { CANVAS_BINDER_SOURCE } from "../canvas-binder";

function runBinder(robothor: unknown) {
  (window as unknown as { robothor: unknown }).robothor = robothor;
  // execute the trusted binder source in this jsdom window
  new Function(CANVAS_BINDER_SOURCE)();
  // the binder defers to DOMContentLoaded; fire it
  window.document.dispatchEvent(new Event("DOMContentLoaded"));
}

beforeEach(() => { document.body.innerHTML = ""; });

describe("CANVAS_BINDER_SOURCE", () => {
  it("fills data-bind targets from a data-read result via textContent", async () => {
    document.body.innerHTML =
      `<section data-read="get_fleet"><span data-bind="length" id="n"></span><span data-bind="0.agent_id" id="a"></span></section>`;
    const read = vi.fn().mockResolvedValue([{ agent_id: "main" }, { agent_id: "auto" }]);
    runBinder({ read, propose: vi.fn() });
    await new Promise((r) => setTimeout(r, 0));
    expect(read).toHaveBeenCalledWith("get_fleet");
    expect(document.getElementById("n")!.textContent).toBe("2");        // array → length
    expect(document.getElementById("a")!.textContent).toBe("main");     // dotted path
  });

  it("renders read values as TEXT, never as markup (no XSS)", async () => {
    document.body.innerHTML = `<div data-read="get_flags"><b data-bind="0.value" id="v"></b></div>`;
    const read = vi.fn().mockResolvedValue([{ value: "<img src=x onerror=alert(1)>" }]);
    runBinder({ read, propose: vi.fn() });
    await new Promise((r) => setTimeout(r, 0));
    const v = document.getElementById("v")!;
    expect(v.textContent).toBe("<img src=x onerror=alert(1)>");  // literal text
    expect(v.querySelector("img")).toBeNull();                    // NOT parsed as HTML
  });

  it("a data-propose element routes a click to robothor.propose with name/value + its label", async () => {
    document.body.innerHTML =
      `<button data-propose="set_flag" data-name="ROBOTHOR_RBAC_MODE" data-value="enforce">Enforce RBAC</button>`;
    const propose = vi.fn();
    runBinder({ read: vi.fn(), propose });
    await new Promise((r) => setTimeout(r, 0));
    (document.querySelector("[data-propose]") as HTMLElement).click();
    expect(propose).toHaveBeenCalledWith(
      "set_flag", { name: "ROBOTHOR_RBAC_MODE", value: "enforce" }, "Enforce RBAC");
  });

  it("does nothing dangerous when robothor is absent (no throw)", () => {
    document.body.innerHTML = `<div data-read="get_fleet"></div>`;
    (window as unknown as { robothor?: unknown }).robothor = undefined;
    expect(() => { new Function(CANVAS_BINDER_SOURCE)(); window.document.dispatchEvent(new Event("DOMContentLoaded")); }).not.toThrow();
  });
});
```

- [ ] **Step 2: Run — expect FAIL** (`cd app && npx pnpm@10 exec vitest run src/lib/__tests__/canvas-binder.test.ts`)

- [ ] **Step 3: Implement**

```ts
// app/src/lib/canvas-binder.ts
// Trusted, parent-authored binder injected into the canvas srcdoc AFTER the shim.
// It translates the model's DECLARATIVE attributes into calls on the trusted
// window.robothor shim, and renders every result with textContent — never HTML.
// The model writes no JavaScript; this is our code, not the model's.
export const CANVAS_BINDER_SOURCE = `
(function () {
  function resolvePath(obj, path) {
    if (!path) return obj;
    var cur = obj;
    var parts = String(path).split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur == null) return undefined;
      var k = parts[i];
      if (k === "length" && Array.isArray(cur)) return cur.length;
      cur = cur[k];
    }
    return cur;
  }
  function asText(v) {
    if (Array.isArray(v)) return String(v.length);
    if (v == null || typeof v === "object") return "";
    return String(v);
  }
  function scan() {
    var R = self.robothor;
    if (!R || typeof R.read !== "function") return;
    var readEls = document.querySelectorAll("[data-read]");
    for (var i = 0; i < readEls.length; i++) {
      (function (el) {
        var op = el.getAttribute("data-read");
        R.read(op).then(function (data) {
          var targets = [];
          if (el.hasAttribute("data-bind")) targets.push(el);
          var kids = el.querySelectorAll("[data-bind]");
          for (var j = 0; j < kids.length; j++) targets.push(kids[j]);
          for (var t = 0; t < targets.length; t++) {
            targets[t].textContent = asText(resolvePath(data, targets[t].getAttribute("data-bind")));
          }
        }).catch(function () {});
      })(readEls[i]);
    }
    var propEls = document.querySelectorAll("[data-propose]");
    for (var p = 0; p < propEls.length; p++) {
      (function (el) {
        el.addEventListener("click", function () {
          if (!R || typeof R.propose !== "function") return;
          R.propose(el.getAttribute("data-propose"),
            { name: el.getAttribute("data-name"), value: el.getAttribute("data-value") },
            el.textContent);
        });
      })(propEls[p]);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan);
  else scan();
})();
`;
```

- [ ] **Step 4: Run — expect PASS.** **Step 5: Commit** `feat(canvas): trusted declarative binder — data-read/bind/propose via textContent`.

---

### Task 2: Relax DOMPurify to the five keys + inject the binder

**Files:** Modify `app/src/components/canvas/srcdoc-renderer.tsx`; extend `app/src/components/canvas/__tests__/sanitization.test.ts` (or the isolation test) with a data-attr allowlist test.

**Interfaces:** unchanged props. The renderer's DOMPurify `ADD_ATTR` gains the five keys. The `bootstrap` it injects is unchanged in mechanism (canvas-view now passes shim+binder in Task 3).

- [ ] **Step 1: Write the failing test** — assert DOMPurify (as configured in the renderer) keeps the five whitelisted `data-*` attrs and strips any other `data-*`, and still strips `<script>` and `on*=`.

```ts
// app/src/components/canvas/__tests__/data-attr-allowlist.test.ts
import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { SrcdocRenderer } from "../srcdoc-renderer";

function srcdocOf(html: string): string {
  const { container } = render(<SrcdocRenderer html={html} />);
  return (container.querySelector("iframe") as HTMLIFrameElement).getAttribute("srcdoc") ?? "";
}

describe("canvas data-attr allowlist", () => {
  it("keeps the five whitelisted data-* attrs", () => {
    const s = srcdocOf(
      `<div data-read="get_fleet"><span data-bind="length"></span></div>` +
      `<button data-propose="set_flag" data-name="ROBOTHOR_RBAC_MODE" data-value="enforce">x</button>`);
    for (const a of ["data-read", "data-bind", "data-propose", "data-name", "data-value"]) {
      expect(s).toContain(a);
    }
  });
  it("strips every OTHER data-* attribute", () => {
    const s = srcdocOf(`<div data-evil="1" data-x="2" data-onload="hack"></div>`);
    expect(s).not.toContain("data-evil");
    expect(s).not.toContain("data-x");
    expect(s).not.toContain("data-onload");
  });
  it("still strips scripts and event handlers", () => {
    const s = srcdocOf(`<div onclick="alert(1)" data-read="get_fleet"></div><script>alert(2)</script>`);
    expect(s).not.toContain("onclick");
    expect(s).not.toMatch(/<script>alert\(2\)/);
  });
});
```

- [ ] **Step 2: Run — expect FAIL** (renderer doesn't yet allow the five keys).

- [ ] **Step 3: Implement** — in `srcdoc-renderer.tsx`, add the five keys to the existing `ADD_ATTR` array (leave `ALLOW_DATA_ATTR:false`), with a comment that ONLY these declarative binding attrs pass and everything else is stripped. Do not touch `FORBID_TAGS`, the sandbox attr, or the CSP.

- [ ] **Step 4: Run — expect PASS.** Also run the existing `sanitization.test.ts` + `isolation.test.tsx` — must still pass. **Step 5: Commit** `feat(canvas): allow only the 5 declarative data-* attrs through DOMPurify`.

---

### Task 3: Wire the binder into the Canvas tab + teach the model

**Files:** Modify `app/src/components/views/canvas-view.tsx`, `app/src/lib/dashboard/system-prompt.ts`; update `app/src/components/views/__tests__/canvas-view.test.tsx`.

- [ ] **Step 1: Write the failing test** — the Canvas view injects BOTH the shim and the binder (assert the bootstrap passed to SrcdocRenderer contains both `robothor` and the binder marker), and the caption no longer says "gated".

```tsx
// add to canvas-view.test.tsx
it("injects the shim + binder and presents the canvas as live", async () => {
  vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div data-read='get_fleet'></div>" }) } as Response);
  const { container } = render(<CanvasView visible />);
  const iframe = await screen.findByTestId("canvas-srcdoc-renderer");
  const srcdoc = iframe.getAttribute("srcdoc") ?? "";
  expect(srcdoc).toMatch(/self\.robothor/);        // shim present
  expect(srcdoc).toMatch(/data-read/);             // binder present (scans data-read)
  expect(container.textContent).not.toMatch(/gated/i);   // caption updated
});
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** —
  - In `canvas-view.tsx`: `import { CANVAS_BINDER_SOURCE } from "@/lib/canvas-binder";` and pass `bootstrap={CANVAS_SHIM_SOURCE + "\n" + CANVAS_BINDER_SOURCE}`. Update the caption to something truthful and live, e.g. *"Live canvas — the operator's system, rendered by the model. Reads are whitelisted; any change is confirmed by you."* (no "gated").
  - In `system-prompt.ts`: add a short section teaching the model the declarative vocabulary — it MAY use `data-read="<op>"` on a container with `data-bind="<path>"` descendants to show live values (ops: `get_fleet`, `get_runs`, `get_workflows`, `get_health`, `get_flags`), and `data-propose="set_flag" data-name="ROBOTHOR_*" data-value="..."` on a button to propose an operator-confirmed change. It must STILL NOT use `<script>`, `on*=` handlers, `fetch`, or raw `postMessage` (those remain stripped). Keep the existing read-only/no-mutation guardrails for everything else.

- [ ] **Step 4: Run — expect PASS + tsc + lint + FULL app suite** (`cd app && npx pnpm@10 exec vitest run && npx pnpm@10 exec tsc --noEmit && npx pnpm@10 lint`). **Step 5: Commit** `feat(canvas): go live — inject binder, teach the model the declarative vocabulary`.

---

## Post-implementation

1. **Whole-branch SECURITY review** (most capable model): confirm the sanitizer relaxation is EXACTLY the five keys (no other `data-*`, no scripts/handlers), the binder renders only via `textContent` (no `innerHTML`), no model JS path exists, reads/proposes still go through the Phase-3 whitelist + parent-chrome confirm, and the sandbox/CSP/isolation invariants are unchanged.
2. **Full suites green + e2e** (run playwright on a FREE port — the live `robothor-app` holds 3004; a fresh `pnpm build` is required so e2e tests the change): `CI=1 PLAYWRIGHT_PORT=3026 PORT=3026 npx pnpm@10 exec playwright test` → 23/23.
3. **finishing-a-development-branch** — PR (`feat(canvas): live declarative binding — the model renders your system`), CI green, merge, deploy (rebuild app + restart robothor-app), verify a data-read canvas renders live and a hostile data-value still shows the real action in the confirm.
</content>

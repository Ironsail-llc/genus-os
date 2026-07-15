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

import { describe, it, expect } from "vitest";
import { validateDashboardCode } from "../code-validator";

// NOTE: these fixtures deliberately avoid wrapping the robothor.read/propose
// calls in a literal `<script>` tag. `<\s*script\b/i` is a separate,
// pre-existing, UNCHANGED blocked pattern in BLOCKED_PATTERNS — it rejects
// any `<script` occurrence unconditionally, regardless of content. Task 4's
// scope is narrowly the `robothor.` pattern; broadening the `<script` block
// is out of scope here (see the pinning test below, and the task report).
describe("code-validator canvas-bridge carve-out", () => {
  it("ALLOWS the trusted shim API in model HTML", () => {
    expect(
      validateDashboardCode(`<div id="x">robothor.read("get_fleet").then(d=>{})</div>`).valid
    ).toBe(true);
    expect(
      validateDashboardCode(
        `<div>window.robothor.propose("set_flag",{name:"ROBOTHOR_RBAC_MODE",value:"enforce"})</div>`
      ).valid
    ).toBe(true);
  });

  it("still BLOCKS raw network / other robothor.* / mutation primitives", () => {
    expect(validateDashboardCode(`<div>fetch("/api/controls")</div>`).valid).toBe(false);
    expect(validateDashboardCode(`<div>parent.postMessage({},"*")</div>`).valid).toBe(false);
    expect(validateDashboardCode(`<div>robothor.action("x")</div>`).valid).toBe(false);
    expect(validateDashboardCode(`<div>new XMLHttpRequest()</div>`).valid).toBe(false);
    expect(validateDashboardCode(`<div>localStorage.getItem("t")</div>`).valid).toBe(false);
    expect(validateDashboardCode(`<div>window.location="x"</div>`).valid).toBe(false);
  });

  it("PINS the pre-existing, unchanged <script> tag block — model HTML still cannot use an inline <script>, even one that only calls robothor.read/propose (out of scope for this task; see report)", () => {
    expect(
      validateDashboardCode(`<div></div><script>robothor.read("get_fleet")</script>`).valid
    ).toBe(false);
  });
});

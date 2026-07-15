import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { SrcdocRenderer } from "../srcdoc-renderer";

function srcdocOf(html: string): string {
  const { container } = render(<SrcdocRenderer html={html} />);
  return (container.querySelector("iframe") as HTMLIFrameElement).getAttribute("srcdoc") ?? "";
}

describe("canvas data-attr allowlist", () => {
  it("keeps the five whitelisted data-* attrs", () => {
    // NOTE: uses <div>, not <button>, for the data-propose/-name/-value case.
    // srcdoc-renderer's FORBID_TAGS (unchanged by this task) already strips
    // <button> wholesale, attrs included — that's a pre-existing, deliberate
    // isolation choice (no native interactive elements), not something this
    // task touches. The trusted binder (canvas-binder.ts) selects by
    // attribute (`[data-propose]`), not by tag name, so a styled <div> is the
    // correct/expected authoring pattern for a proposal control. See the
    // task report for the empirical finding that <button> + data-propose
    // silently loses the whole element under the current sanitizer config.
    const s = srcdocOf(
      `<div data-read="get_fleet"><span data-bind="length"></span></div>` +
      `<div data-propose="set_flag" data-name="ROBOTHOR_RBAC_MODE" data-value="enforce">x</div>`);
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

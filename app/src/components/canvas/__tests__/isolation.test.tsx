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

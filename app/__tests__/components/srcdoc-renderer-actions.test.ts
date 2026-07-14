import { act, render, screen } from "@testing-library/react";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import { SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";

async function getSrcdoc(html: string): Promise<string> {
  render(React.createElement(SrcdocRenderer, { html }));
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
  const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
  return iframe.getAttribute("srcdoc") || "";
}

describe("SrcdocRenderer read-only capability boundary", () => {
  it("does not expose a generated-dashboard action API", async () => {
    const srcdoc = await getSrcdoc("<section><p>Safe dashboard</p></section>");
    expect(srcdoc).not.toContain("window.robothor");
    expect(srcdoc).not.toContain("robothor:action");
    expect(srcdoc).not.toContain("/api/actions/execute");
  });

  it("removes inline handlers, scripts, and action payloads", async () => {
    const srcdoc = await getSrcdoc(
      `<button onclick="robothor.action('delete_routine', {})">Delete</button>
       <script>window.parent.postMessage({type:'robothor:action'}, '*')</script>`,
    );
    expect(srcdoc).not.toContain("onclick=");
    expect(srcdoc).not.toContain("delete_routine");
    expect(srcdoc).not.toContain("robothor:action");
  });

  it("does not load third-party runtime scripts", async () => {
    const srcdoc = await getSrcdoc("<section>Safe</section>");
    expect(srcdoc).not.toContain("cdn.tailwindcss.com");
    expect(srcdoc).not.toContain("cdn.jsdelivr.net");
    expect(srcdoc).not.toMatch(/<script\s+src=/i);
  });

  it("ignores a same-origin message not sent by the exact iframe", async () => {
    render(React.createElement(SrcdocRenderer, { html: "<section>Safe</section>" }));
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    expect(iframe.style.height).toBe("400px");

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 1000 },
          origin: "null",
          source: window,
        }),
      );
    });
    expect(iframe.style.height).toBe("400px");
  });

  it("never invokes an action callback for a forged iframe message", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(React.createElement(SrcdocRenderer, { html: "<section>Safe</section>" }));
    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "robothor:action", tool: "delete_routine", params: {} },
          origin: "null",
          source: window,
        }),
      );
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});

import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";

async function renderSanitized(html: string) {
  render(<SrcdocRenderer html={html} />);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
  return screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
}

describe("SrcdocRenderer", () => {
  beforeEach(() => vi.clearAllMocks());

  it("uses a unique-origin script sandbox and no referrer", () => {
    render(<SrcdocRenderer html="<section>Hello</section>" />);
    const iframe = screen.getByTestId("srcdoc-renderer");
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(iframe.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("ships a deny-default CSP with no remote script or network source", () => {
    render(<SrcdocRenderer html="<section>Test</section>" />);
    const srcdoc = screen.getByTestId("srcdoc-renderer").getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("default-src 'none'");
    expect(srcdoc).toContain("connect-src 'none'");
    expect(srcdoc).toContain("form-action 'none'");
    expect(srcdoc).not.toContain("https://cdn.");
  });

  it("sanitizes the model output before first render", () => {
    render(<SrcdocRenderer html="<section>UNTRUSTED_SENTINEL</section>" />);
    const srcdoc = screen.getByTestId("srcdoc-renderer").getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("UNTRUSTED_SENTINEL");
  });

  it("strips scripts, event handlers, remote links, and nested frames", async () => {
    const iframe = await renderSanitized(
      `<section>Safe</section><script>alert(1)</script>
       <button onclick="alert(2)">Bad</button><iframe src="https://evil.test"></iframe>
       <link rel="stylesheet" href="https://evil.test/x.css">`,
    );
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("Safe");
    expect(srcdoc).not.toContain("alert(1)");
    expect(srcdoc).not.toContain("onclick=");
    expect(srcdoc).not.toContain("evil.test");
  });

  it("strips navigation, forms, and every interactive form control", async () => {
    const iframe = await renderSanitized(
      `<a href="/api/actions/execute">Navigate</a>
       <form action="/api/actions/execute"><fieldset>
         <input value="secret"><textarea>text</textarea>
         <select><option>one</option></select><button>Submit</button>
       </fieldset></form><section>Static metric</section>`,
    );
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("Static metric");
    expect(srcdoc).not.toMatch(
      /<\/?(?:a|form|fieldset|input|textarea|select|option|button)\b/i,
    );
    expect(srcdoc).not.toContain("/api/actions/execute");
  });

  it("preserves safe semantic HTML, scoped style, and SVG", async () => {
    const iframe = await renderSanitized(
      `<section><style>.metric{color:#fff}</style><table><tbody><tr><td>42</td></tr></tbody></table>
       <svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"></circle></svg></section>`,
    );
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("metric");
    expect(srcdoc).toContain("<table>");
    expect(srcdoc).toContain("<svg");
    expect(srcdoc).toContain("<circle");
  });

  it("accepts bounded height only from the exact iframe window", async () => {
    render(<SrcdocRenderer html="<section>Test</section>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 600 },
          origin: "null",
          source: iframe.contentWindow,
        }),
      );
    });
    expect(iframe.style.height).toBe("632px");

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 10000 },
          origin: "null",
          source: iframe.contentWindow,
        }),
      );
    });
    expect(iframe.style.height).toBe("5000px");
  });

  it("rejects wrong source and wrong origin messages", async () => {
    render(<SrcdocRenderer html="<section>Test</section>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 900 },
          origin: window.location.origin,
          source: iframe.contentWindow,
        }),
      );
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 900 },
          origin: "null",
          source: window,
        }),
      );
    });
    expect(iframe.style.height).toBe("400px");
  });
});

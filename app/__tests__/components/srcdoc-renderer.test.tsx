import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";

describe("SrcdocRenderer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders iframe with sandbox allow-scripts", () => {
    render(<SrcdocRenderer html="<div>Hello</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer");
    expect(iframe).toBeTruthy();
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("renders iframe with referrerpolicy no-referrer", () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer");
    expect(iframe.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("contains CSP meta tag in srcdoc", () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("Content-Security-Policy");
    expect(srcdoc).toContain("default-src 'none'");
    expect(srcdoc).toContain("https://cdn.tailwindcss.com");
    expect(srcdoc).toContain("https://cdn.jsdelivr.net");
  });

  it("contains Chart.js CDN script tag", () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("chart.js@4");
    expect(srcdoc).toContain("chartjs-plugin-datalabels");
  });

  it("contains chart hydration script for data-chart", () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("hydrateCharts");
    expect(srcdoc).toContain("data-chart");
    expect(srcdoc).toContain("resolveColor");
  });

  it("strips dangerous iframe tags via DOMPurify", async () => {
    render(<SrcdocRenderer html='<div>OK</div><iframe src="evil.com"></iframe>' />);
    // Wait for async DOMPurify import to complete and re-render
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).not.toContain("<iframe src=");
    expect(srcdoc).toContain("OK");
  });

  it("strips event handler attributes via DOMPurify", async () => {
    render(<SrcdocRenderer html='<img onerror="alert(1)" src="x">' />);
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    // The malicious onerror attribute on the <img> should be stripped by DOMPurify.
    // Note: window.onerror in the trusted wrapper script is intentional (error observability).
    expect(srcdoc).not.toContain('onerror="alert');
  });

  it("strips iframe tags via DOMPurify", async () => {
    render(<SrcdocRenderer html='<div>Safe</div><iframe src="evil.com">inside</iframe>' />);
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    // DOMPurify removes the forbidden <iframe> tag but preserves safe content
    expect(srcdoc).not.toContain("evil.com");
    expect(srcdoc).toContain("Safe");
  });

  it("preserves canvas and svg tags", () => {
    render(<SrcdocRenderer html='<canvas id="c1"></canvas><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="40"/></svg>' />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("<canvas");
    expect(srcdoc).toContain("<svg");
    expect(srcdoc).toContain("<circle");
  });

  it("preserves data-chart attributes", () => {
    const chartHtml = `<div data-chart='{"type":"bar","datasets":[{"data":[1,2,3]}]}'></div>`;
    render(<SrcdocRenderer html={chartHtml} />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
    const srcdoc = iframe.getAttribute("srcdoc") || "";
    expect(srcdoc).toContain("data-chart");
  });

  it("responds to postMessage height updates", async () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;

    // Initial height
    expect(iframe.style.height).toBe("400px");

    // Simulate postMessage
    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 600 },
          origin: "null",
        })
      );
    });

    expect(iframe.style.height).toBe("632px"); // 600 + 32
  });

  it("ignores postMessage with wrong type", async () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "other-type", height: 9999 },
          origin: "null",
        })
      );
    });

    expect(iframe.style.height).toBe("400px"); // unchanged
  });

  it("caps max height at 5000px", async () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 10000 },
          origin: "null",
        })
      );
    });

    expect(iframe.style.height).toBe("5000px");
  });

  it("enforces minimum height of 200px", async () => {
    render(<SrcdocRenderer html="<div>Test</div>" />);
    const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "srcdoc-height", height: 10 },
          origin: "null",
        })
      );
    });

    expect(iframe.style.height).toBe("200px");
  });
});

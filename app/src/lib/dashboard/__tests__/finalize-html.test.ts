import { describe, it, expect, vi } from "vitest";
import { finalizeDashboardHtml } from "../finalize-html";

// The canvas is read-only: interactive tags are removed by the sanitizer.
// A generated view that merely CONTAINS one (a news story's link, a stray
// button) must be served with that tag removed — not rejected whole, which
// left the canvas silently unchanged for any reply about articles.
describe("finalizeDashboardHtml", () => {
  const view = (inner: string) =>
    `<section class="genus-dashboard"><style>.x{color:red}</style><h1>Health tech news</h1>${inner}</section>`;

  it("keeps a view whose stories carry links, minus the links", () => {
    const result = finalizeDashboardHtml(
      view('<ul><li><a href="https://example.com/story">Ortet raises $500M</a></li></ul>'),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.html).toContain("Ortet raises $500M");
    expect(result.html).not.toMatch(/<\s*a\b/i);
    expect(result.html).not.toContain("https://example.com/story");
  });

  it("removes form controls the same way", () => {
    const result = finalizeDashboardHtml(view('<form><button>Refresh</button><input value="q"></form><p>Body</p>'));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.html).not.toMatch(/<\s*(?:form|button|input)\b/i);
    expect(result.html).toContain("Body");
  });

  it("never serves script or handlers", () => {
    const result = finalizeDashboardHtml(view('<p onclick="steal()">Hi</p><script>alert(1)</script>'));
    if (result.ok) {
      expect(result.html).not.toMatch(/<\s*script\b|onclick/i);
    }
  });

  it("still rejects what the sanitizer cannot neutralise", () => {
    const result = finalizeDashboardHtml(view("<style>@import url(https://evil.example/x.css);</style>"));
    expect(result.ok).toBe(false);
  });

  it("rejects empty output", () => {
    expect(finalizeDashboardHtml("").ok).toBe(false);
  });
});

describe("renderDashboard — one corrective retry", () => {
  const good = '<section class="genus-dashboard"><h1>Fleet</h1></section>';
  const bad = '<section><style>@import url(https://evil.example/x.css);</style></section>';

  it("returns the first attempt when it is servable, with one model call", async () => {
    const { renderDashboard } = await import("../finalize-html");
    const complete = vi.fn().mockResolvedValue(good);
    const result = await renderDashboard(complete, "draw the fleet");
    expect(result.ok).toBe(true);
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it("retries once, telling the model what was rejected", async () => {
    const { renderDashboard } = await import("../finalize-html");
    const complete = vi.fn().mockResolvedValueOnce(bad).mockResolvedValueOnce(good);
    const result = await renderDashboard(complete, "draw the fleet");
    expect(result.ok).toBe(true);
    expect(complete).toHaveBeenCalledTimes(2);
    const retryPrompt = complete.mock.calls[1][0] as string;
    expect(retryPrompt).toContain("draw the fleet");
    expect(retryPrompt).toMatch(/rejected/i);
  });

  it("gives up after the retry", async () => {
    const { renderDashboard } = await import("../finalize-html");
    const complete = vi.fn().mockResolvedValue(bad);
    const result = await renderDashboard(complete, "draw the fleet");
    expect(result.ok).toBe(false);
    expect(complete).toHaveBeenCalledTimes(2);
  });
});
